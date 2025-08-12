import os, json, time, datetime as dt, csv
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import requests, numpy as np, pandas as pd
import ccxt, yfinance as yf
from ta.momentum import RSIIndicator
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from filelock import FileLock
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# .env (ETHERSCAN_KEY itd.)
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
except Exception:
    pass

# Google Trends
try:
    from pytrends.request import TrendReq
    HAS_TRENDS = True
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning, module=r".*pytrends.*")
except Exception:
    HAS_TRENDS = False

CONFIG: Dict[str, Any] = json.load(open("config.json", "r"))
TZ = pytz.timezone(CONFIG["schedule"]["timezone"])
STATE_FILE = "state.json"
STATE_LOCK = FileLock(STATE_FILE + ".lock", timeout=5)
LOGS_DIR = Path("logs"); LOGS_DIR.mkdir(exist_ok=True)

TG_TOKEN: str = CONFIG["telegram"]["bot_token"]
TG_CHAT: str = CONFIG["telegram"].get("chat_ids")
if not TG_CHAT:
    TG_CHAT = [CONFIG["telegram"]["chat_id"]]
ETHERSCAN_KEY: str = os.getenv("ETHERSCAN_KEY", "")

# Global HTTP session with retry/backoff (soft errors)
_S = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET", "POST"])
)
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=20, pool_maxsize=50)
_S.mount("https://", _adapter)
_S.headers.update({"User-Agent": "hot-exit-bot/8.7 (+github.com/your/repo)"})


def http_json(url: str, method: str = "GET", params: Optional[Dict[str, Any]] = None,
              data: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Optional[Dict[str, Any]]:
    # Does not raise exceptions on 4xx/5xx — returns json/text with status code
    try:
        r = _S.request(method, url, params=params, data=data, timeout=timeout)
    except requests.RequestException as e:
        return {"_exc": str(e), "_http_status": None}
    try:
        j = r.json()
    except ValueError:
        j = None
    if r.status_code >= 400:
        if isinstance(j, dict):
            j["_http_status"] = r.status_code
            return j
        return {"_http_status": r.status_code, "_text": (r.text[:200] if r.text else "no-body")}
    return j


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ)


def tg_send(text, disable_preview=True):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        max_len = 3800
        parts = []
        if len(text) <= max_len:
            parts = [text]
        else:
            buf, size = [], 0
            for line in text.splitlines(True):
                if size + len(line) > max_len and buf:
                    parts.append("".join(buf)); buf=[]; size=0
                buf.append(line); size += len(line)
            if buf: parts.append("".join(buf))
        for chunk in parts:
            for chat in TG_CHAT:
                data = {"chat_id": chat, "text": chunk, "disable_web_page_preview": disable_preview}
                r = _S.post(url, data=data, timeout=15)
                try:
                    j = r.json()
                    if not j.get("ok", False):
                        print("TG send failed:", j)
                except Exception:
                    print("TG non-JSON:", r.status_code, r.text[:200])
    except Exception as e:
        print("TG error:", e)


def load_state() -> Dict[str, Any]:
    with STATE_LOCK:
        if Path(STATE_FILE).exists():
            try: return json.load(open(STATE_FILE,"r"))
            except: pass
        return {
            "alerts": {},
            "btc_d_hist": [],
            "total3_hist": [],
            "stable_hist": [],
            "trends": {"last_date": None, "holo_last": None, "holo_peak": None, "holo_rel": None, "tf": None, "last_above": None},
            "last_memecoin_count": None
        }


def save_state(state: Dict[str, Any]) -> None:
    with STATE_LOCK:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f: json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)


# CCXT cache + markets + OHLCV cache
CCXT_CACHE: Dict[str, Any] = {}
MARKETS_LOADED: set = set()
OHLCV_CACHE: Dict[Any, Any] = {}
OHLCV_TTL_SEC = 15*60  # with intraday might be 1800-3600 for less usage


def get_ccxt(exchange: str):
    if exchange not in CCXT_CACHE:
        CCXT_CACHE[exchange] = getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": 15000})
    exi = CCXT_CACHE[exchange]
    if exchange not in MARKETS_LOADED:
        try:
            exi.load_markets()
            MARKETS_LOADED.add(exchange)
        except Exception:
            pass
    return exi


def ohlcv_first_available(symbol: str, tf: str = "1d", limit: int = 200) -> Optional[pd.DataFrame]:
    key = (symbol, tf, limit)
    hit = OHLCV_CACHE.get(key)
    now = time.time()
    if hit and now - hit["ts"] < OHLCV_TTL_SEC:
        return hit["df"].copy()
    for ex in CONFIG["exchanges"]:
        exi = get_ccxt(ex)
        try:
            if symbol not in exi.markets:
                continue
            raw = exi.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            if raw and len(raw) > 50:
                df = pd.DataFrame(raw, columns=["ts","o","h","l","c","v"])
                df["ts"] = pd.to_datetime(df["ts"], unit="ms")
                df = df.set_index("ts")
                OHLCV_CACHE[key] = {"ts": now, "df": df}
                return df
        except Exception:
            continue
    return None


def synthetic_hot_eth(tf: str = "1d", limit: int = 200) -> Optional[pd.DataFrame]:
    hot = ohlcv_first_available(CONFIG["symbols"]["hot_usdt"], tf=tf, limit=limit)
    eth = ohlcv_first_available(CONFIG["symbols"]["eth_usdt"], tf=tf, limit=limit)
    if hot is None or eth is None: return None
    idx = hot.index.union(eth.index)
    df = pd.DataFrame(index=idx)
    df["hot"] = hot["c"]; df["eth"] = eth["c"]
    df = df.dropna()
    df["hot_eth"] = df["hot"] / df["eth"]
    df["vol_hot"] = hot.reindex(df.index)["v"]
    return df


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    return RSIIndicator(series, window=period).rsi()


def sma(series: pd.Series, window: int) -> pd.Series: return series.rolling(window).mean()
def ema(series: pd.Series, window: int) -> pd.Series: return series.ewm(span=window, adjust=False).mean()


def get_global_caps() -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    j = http_json("https://api.coingecko.com/api/v3/global", timeout=15)
    if not j or "data" not in j: return None, None, None, None
    try:
        btc_d = float(j["data"]["market_cap_percentage"]["btc"])
        eth_d = float(j["data"]["market_cap_percentage"].get("eth", 0))
        total_usd = float(j["data"]["total_market_cap"]["usd"])
        total3_usd = total_usd * (1 - (btc_d + eth_d)/100.0)
        return btc_d, eth_d, total_usd, total3_usd
    except Exception:
        return None, None, None, None


def get_eth_btc() -> Optional[float]:
    df = ohlcv_first_available(CONFIG["symbols"]["eth_btc"], tf="1d", limit=200)
    return None if df is None else float(df["c"].iloc[-1])


def get_gas_gwei() -> Optional[Dict[str, float]]:
    if not ETHERSCAN_KEY: return None
    try:
        j = http_json(
            "https://api.etherscan.io/api",
            params={"module": "gastracker", "action": "gasoracle", "apikey": ETHERSCAN_KEY},
            timeout=10
        )
        if not j or j.get("status")!="1": return None
        r = j["result"]; base = r.get("suggestBaseFee")
        return {
            "safe": float(r.get("SafeGasPrice")),
            "propose": float(r.get("ProposeGasPrice")),
            "fast": float(r.get("FastGasPrice")),
            "base": float(base) if base is not None else None
        }
    except Exception:
        return None


def format_gas_line(gas: Optional[Dict[str, float]]) -> Optional[str]:
    if not gas or not isinstance(gas, dict): return None
    def f(x):
        try: return f"{float(x):.1f}"
        except: return "—"
    parts = [f"safe {f(gas.get('safe'))}", f"propose {f(gas.get('propose'))}", f"fast {f(gas.get('fast'))}"]
    line = "Gas (ETH): " + " | ".join(parts) + " gwei"
    if gas.get("base") is not None: line += f" (base {f(gas.get('base'))})"
    return line


def get_dxy() -> Tuple[Optional[float], Optional[float]]:
    s = yf.Ticker("DX-Y.NYB").history(period="6mo", interval="1d")["Close"]
    if s is None or len(s)<5: s = yf.Ticker("DX=F").history(period="6mo", interval="1d")["Close"]
    if s is None or len(s)<5: return None, None
    dxy = float(s.iloc[-1]); last20 = s.dropna().iloc[-20:]; slope = (last20.iloc[-1]-last20.iloc[0])/max(1,len(last20)-1)
    return dxy, slope


def count_memecoins_top50(watchlist: List[str], return_names: bool=False):
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {"vs_currency":"usd","order":"volume_desc","per_page":250,"page":1,"price_change_percentage":"24h"}
        arr = http_json(url, params=params, timeout=15)
        if not isinstance(arr, list):
            return (None, []) if return_names else None
        top50 = arr[:50]
        wl = set([w.lower() for w in watchlist])
        names, count = [], 0
        for c in top50:
            sym = str(c.get("symbol","")).lower()
            name = str(c.get("name","")).lower()
            if sym in wl or any(w in name for w in wl):
                count += 1
                if return_names:
                    names.append(c.get("symbol","").upper() or c.get("name","")[:10])
        return (count, names) if return_names else count
    except Exception:
        return (None, []) if return_names else None


def google_trends_scores(timeframe: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not HAS_TRENDS or not CONFIG["features"].get("google_trends", True): return None
    tf = timeframe or CONFIG.get("report", {}).get("trends_timeframe", "today 5-y")
    try:
        pytr = TrendReq(hl="en-US", tz=0)
        kw = ["Holochain","HoloFuel"]
        pytr.build_payload(kw, timeframe=tf, geo="", gprop="")
        df = pytr.interest_over_time().reset_index()
        holo = df["Holochain"].dropna()
        if len(holo) < 10: return None
        peak = float(np.max(holo.values)) if len(holo)>0 else 0.0
        last = float(holo.iloc[-1])
        rel = float((last/peak)*100.0) if peak>0 else 0.0
        threshold = CONFIG.get("report", {}).get("trends_high_threshold", 40)
        holo_high = rel >= threshold
        holoFuel_last = float(df["HoloFuel"].dropna().iloc[-1]) if "HoloFuel" in df else None
        return {"tf": tf, "holo_last": last, "holo_peak": peak, "holo_rel": rel, "holo_high": holo_high,
                "holofuel_last": holoFuel_last, "threshold": threshold}
    except Exception: return None


def phase2_signals(df: pd.DataFrame) -> Tuple[bool, bool, bool, Dict[str, Any]]:
    price = df["hot_eth"]; r = rsi(price)
    rsi_cond = (r.iloc[-3:] >= CONFIG["thresholds"]["phase2"]["rsi"]).sum() >= CONFIG["thresholds"]["phase2"]["rsi_days"]
    sma50 = sma(price, 50); dev = (price.iloc[-1] - sma50.iloc[-1]) / sma50.iloc[-1]
    dev_cond = dev >= CONFIG["thresholds"]["phase2"]["dev_sma50"]
    vol7 = df["vol_hot"].rolling(7).mean().iloc[-1]; vol30 = df["vol_hot"].rolling(30).mean().iloc[-1]
    vol_cond = (vol30>0) and (vol7/vol30 >= CONFIG["thresholds"]["phase2"]["vol_7_vs_30"])
    r_prev = float(r.iloc[-2]) if not np.isnan(r.iloc[-2]) else None
    r_curr = float(r.iloc[-1]) if not np.isnan(r.iloc[-1]) else None
    rsi_delta = (r_curr - r_prev) if (r_prev is not None and r_curr is not None) else None
    p_prev = float(price.iloc[-2]) if not np.isnan(price.iloc[-2]) else None
    p_curr = float(price.iloc[-1]) if not np.isnan(price.iloc[-1]) else None
    p_change_pct = ((p_curr/p_prev - 1.0)*100.0) if (p_prev and p_prev!=0) else None
    return rsi_cond, dev_cond, vol_cond, {
        "rsi": r_curr, "rsi_prev": r_prev, "rsi_delta": rsi_delta,
        "dev_pct": float(dev*100.0), "vol7": float(vol7), "vol30": float(vol30),
        "hot_eth": p_curr, "hot_eth_prev": p_prev, "hot_eth_change_pct": p_change_pct
    }


def phase3_hot_weakness(df: pd.DataFrame) -> Tuple[bool, Dict[str, Any]]:
    price = df["hot_eth"]; vol = df["vol_hot"]
    ret_pct = price.pct_change().iloc[-1]*100
    red_cond = ret_pct <= CONFIG["thresholds"]["phase3"]["red_day_pct"]
    vol20 = vol.rolling(20).mean().iloc[-1]
    vol_cond = (vol20>0) and (vol.iloc[-1] >= CONFIG["thresholds"]["phase3"]["hot_weak_vol_mult"]*vol20)
    r = rsi(price)
    rsi_drop = (r.iloc[-4] > CONFIG["thresholds"]["phase3"]["rsi_drop_from"]) and (r.iloc[-1] < CONFIG["thresholds"]["phase3"]["rsi_drop_to"])
    ema8_break = price.iloc[-1] < ema(price, 8).iloc[-1]
    checks = [red_cond and vol_cond, rsi_drop, ema8_break]
    return sum(checks) >= 2, {"ret_pct": float(ret_pct), "vol": float(vol.iloc[-1]),
                              "vol20": float(vol20), "ema8": float(ema(price,8).iloc[-1]), "rsi": float(r.iloc[-1])}


def euphoria_signals(state: Dict[str, Any], btc_d: Optional[float], eth_btc: Optional[float],
                     gas: Optional[Dict[str, float]], meme_count: Optional[int],
                     trends_info: Optional[Dict[str, Any]]) -> Tuple[int, List[str]]:
    hits = 0; details: List[str] = []
    p3 = CONFIG.get("thresholds", {}).get("phase3", {})
    gas_thr = p3.get("gas_gwei", 150)
    low = p3.get("eth_btc_zone_low", 0.05); high = p3.get("eth_btc_zone_high", 0.08)
    btc_d_below = p3.get("btc_d_below", 50)

    if btc_d is not None and btc_d < btc_d_below:
        hits += 1; details.append(f"BTC.D {btc_d:.1f}% (<{btc_d_below}%)")
    if eth_btc is not None and low <= eth_btc <= high:
        hits+=1; details.append(f"ETH/BTC {eth_btc:.4f} (strefa)")
    if gas and isinstance(gas, dict) and gas.get("propose") is not None and gas["propose"] >= gas_thr:
        hits+=1; details.append(f"Gas {gas['propose']:.0f} gwei")
    if meme_count is not None and meme_count >= p3.get("meme_top50_min", 10):
        hits+=1; details.append(f"Memecoiny TOP50: {meme_count}")
    if trends_info and trends_info.get("holo_high"):
        rel = trends_info.get("holo_rel"); hits+=1; details.append(f"Trends(5y): Holochain {rel:.0f}/100")
    return hits, details


def phase4_signals(state: Dict[str, Any], btc_d: Optional[float], dxy: Optional[float], dxy_slope: Optional[float]) -> Tuple[int, List[str]]:
    """
    Phase 4: base signals:
    - BTC.D upwards reversal when compared with previous minimum (+3 pp)
    - DXY high and growing
    VIX besides the function (as an additional signal).
    """
    hits = 0; details: List[str] = []
    p4 = CONFIG.get("thresholds", {}).get("phase4", {})
    btc_vals = [it["val"] for it in state.get("btc_d_hist", [])[-28:]]
    if btc_vals and btc_d is not None:
        recent_min = min(btc_vals)
        if btc_d >= recent_min + p4.get("btc_d_reversal_pp", 3):
            hits+=1; details.append(f"BTC.D odwrót (↑ {btc_d - recent_min:.1f} p.p.)")
    dxy_level = p4.get("dxy_level", 105.0)
    if dxy is not None and dxy >= dxy_level and (dxy_slope is not None and dxy_slope>0):
        hits+=1; details.append(f"DXY {dxy:.2f} ↑")
    return hits, details


def pi_cycle_top_signal() -> Optional[Dict[str, Any]]:
    df = ohlcv_first_available("BTC/USDT", tf="1d", limit=500)
    if df is None or len(df) < 360: return None
    s = df["c"]; sma111 = s.rolling(111).mean(); sma350x2 = s.rolling(350).mean() * 2
    cross_up = sma111.iloc[-2] < sma350x2.iloc[-2] and sma111.iloc[-1] >= sma350x2.iloc[-1]
    gap = float(sma111.iloc[-1] - sma350x2.iloc[-1]); gap_prev = float(sma111.iloc[-2] - sma350x2.iloc[-2])
    gap_delta = float(gap - gap_prev); base = float(sma350x2.iloc[-1]) if sma350x2.iloc[-1] else 0.0
    gap_pct = float((gap/base)*100.0) if base>0 else None
    return {"cross": bool(cross_up), "dma111": float(sma111.iloc[-1]), "dma350x2": float(sma350x2.iloc[-1]),
            "gap": gap, "gap_prev": gap_prev, "gap_delta": gap_delta, "gap_pct": gap_pct}


def format_ethbtc_zone(eth_btc: Optional[float]) -> Optional[str]:
    if eth_btc is None: return None
    p3 = CONFIG.get("thresholds", {}).get("phase3", {})
    low = p3.get("eth_btc_zone_low", 0.05); high = p3.get("eth_btc_zone_high", 0.08)
    if eth_btc < low:
        status = f"poniżej strefy (<{low:.2f})"; dist = low - eth_btc
    elif eth_btc > high:
        status = f"powyżej strefy (>{high:.2f})"; dist = eth_btc - high
    else:
        status = f"w strefie {low:.2f}–{high:.2f}"; dist = min(eth_btc - low, high - eth_btc)
    return f"ETH/BTC: {eth_btc:.5f} — {status}; dystans do granicy {dist:.3f}"


def total3_trend_line(state: Dict[str, Any]) -> str:
    hist = state.get("total3_hist", [])
    if len(hist) < 55: return "TOTAL3 trend: brak wystarczającej historii (min 50 dni)"
    s = pd.Series([h["val"] for h in hist], index=[pd.to_datetime(h["date"]) for h in hist]).sort_index()
    sma50 = s.rolling(50).mean(); cur = float(s.iloc[-1]); prev = float(s.iloc[-2]) if len(s)>1 else None
    sma = float(sma50.iloc[-1]); change_pct = ((cur/prev - 1.0)*100.0) if prev else None
    dir_arrow = "↑" if (change_pct is not None and change_pct>0) else ("↓" if (change_pct is not None and change_pct<0) else "→")
    status = "powyżej SMA50" if cur >= sma else "poniżej SMA50"
    return (f"TOTAL3 trend: {status} (obecnie ${cur/1e9:,.1f}B vs SMA50 ${sma/1e9:,.1f}B) | d/d {change_pct:.2f}% {dir_arrow}").replace(",", " ")


def volume_label(ratio: float) -> str:
    if ratio >= 3.0: return "wybuchowy"
    if ratio >= 1.5: return "silny"
    if ratio >= 0.8: return "normalny"
    if ratio >= 0.6: return "słaby"
    return "bardzo słaby"


# Additional indicators
def get_fear_greed() -> Tuple[Optional[int], Optional[str]]:
    try:
        j = http_json("https://api.alternative.me/fng/?limit=2", timeout=10)
        d = j.get("data", []) if j else []
        if not d: return None, None
        val = int(d[0]["value"]); cls = d[0].get("value_classification")
        return val, cls
    except Exception: return None, None


def get_vix() -> Tuple[Optional[float], Optional[float]]:
    try:
        s = yf.Ticker("^VIX").history(period="6mo", interval="1d")["Close"]
        if s is None or len(s)<5: return None, None
        v = float(s.iloc[-1]); last20 = s.dropna().iloc[-20:]
        slope = (last20.iloc[-1] - last20.iloc[0]) / max(1, len(last20)-1)
        return v, slope
    except Exception: return None, None


def get_funding(symbol: str = "BTCUSDT", limit: int = 8) -> Tuple[Optional[float], Optional[float]]:
    try:
        j = http_json("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol":symbol,"limit":limit}, timeout=15)
        if not isinstance(j, list) or not j: return None, None
        rates = [float(x["fundingRate"]) for x in j]
        last_pct = rates[-1]*100.0
        avg_pct = (sum(rates[-3:])/3.0)*100.0 if len(rates)>=3 else (sum(rates)/len(rates))*100.0
        return last_pct, avg_pct
    except Exception: return None, None


def get_stables_cap() -> Optional[float]:
    try:
        ids = "tether,usd-coin,dai"
        j = http_json("https://api.coingecko.com/api/v3/coins/markets", params={"vs_currency":"usd","ids":ids,"per_page":3,"page":1}, timeout=15)
        if not isinstance(j, list): return None
        cap = sum([c.get("market_cap",0) or 0 for c in j])
        return float(cap)
    except Exception: return None


def stables_line_and_delta(state: Dict[str, Any]) -> Tuple[str, Optional[float]]:
    hist = state.get("stable_hist", [])
    if len(hist) < 8: return "Stablecoins cap: brak wystarczającej historii (min 8 dni)", None
    cur = float(hist[-1]["val"]); prev7 = float(hist[-8]["val"]); delta7 = ((cur/prev7)-1.0)*100.0 if prev7>0 else None
    return (f"Stablecoins cap: ${cur/1e9:,.1f}B (7d Δ {delta7:+.2f}%)".replace(",", " ")), delta7


def log_daily_csv(date_str: str, data: Dict[str, Any]) -> None:
    path = LOGS_DIR / f"daily_{date_str[:4]}.csv"; write_header = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(data.keys()))
        if write_header: w.writeheader()
        w.writerow(data)


def throttle(state: Dict[str, Any], key: str, hours: int = 6) -> bool:
    last = state["alerts"].get(key)
    if not last: return True
    last_dt = dt.datetime.fromisoformat(last)
    return (dt.datetime.utcnow() - last_dt).total_seconds() >= hours*3600


def mark_alert(state: Dict[str, Any], key: str) -> None:
    state["alerts"][key] = dt.datetime.utcnow().isoformat(); save_state(state)


def daily_report() -> None:
    state = load_state()
    df = synthetic_hot_eth(tf="1d", limit=200)
    if df is None or len(df)<60:
        tg_send("⚠️ Brak danych HOT/ETH. Raport może być niepełny."); return

    rsi_cond, dev_cond, vol_cond, p2info = phase2_signals(df)
    weak_cond, weak_info = phase3_hot_weakness(df)

    btc_d=eth_d=total_usd=total3_usd=None
    eth_btc=None; gas=None; dxy=None; dxy_slope=None
    meme_count=None; meme_names=None; trends_info=None

    try: btc_d, eth_d, total_usd, total3_usd = get_global_caps()
    except: pass
    try: eth_btc = get_eth_btc()
    except: pass
    try: gas = get_gas_gwei()
    except: pass
    try: dxy, dxy_slope = get_dxy()
    except: pass

    if CONFIG["features"].get("memecoin_counter", True):
        try:
            meme_count, meme_names = count_memecoins_top50(CONFIG["watchlists"]["memecoins"], return_names=True)
            state["last_memecoin_count"] = meme_count
        except: pass

    fng_val=fng_cls=None; vix=None; vix_slope=None
    btc_f_last=btc_f_avg=eth_f_last=eth_f_avg=None
    stables_cap=None
    try:
        if CONFIG["features"].get("fear_greed", True): fng_val, fng_cls = get_fear_greed()
    except: pass
    try:
        if CONFIG["features"].get("vix", True): vix, vix_slope = get_vix()
    except: pass
    try:
        if CONFIG["features"].get("funding", True):
            btc_f_last, btc_f_avg = get_funding("BTCUSDT")
            eth_f_last, eth_f_avg = get_funding("ETHUSDT")
    except: pass
    try:
        if CONFIG["features"].get("stablecoins", True): stables_cap = get_stables_cap()
    except: pass

    today = now_local().strftime("%Y-%m-%d")
    if btc_d is not None:
        state["btc_d_hist"] = [x for x in state["btc_d_hist"] if x["date"]!=today]
        state["btc_d_hist"].append({"date": today, "val": float(btc_d)})
        state["btc_d_hist"] = state["btc_d_hist"][-180:]
    if total3_usd is not None:
        state["total3_hist"] = [x for x in state["total3_hist"] if x["date"]!=today]
        state["total3_hist"].append({"date": today, "val": float(total3_usd)})
        state["total3_hist"] = state["total3_hist"][-180:]
    if stables_cap is not None:
        state["stable_hist"] = [x for x in state.get("stable_hist", []) if x["date"]!=today]
        state["stable_hist"].append({"date": today, "val": float(stables_cap)})
        state["stable_hist"] = state["stable_hist"][-365:]

    # Google Trends (5y) — update every N days
    trends_days = int(CONFIG.get("report", {}).get("trends_update_days", 1))
    last_td = state["trends"].get("last_date")
    need_update = (last_td is None) or ((dt.datetime.fromisoformat(last_td).date() + dt.timedelta(days=trends_days)) <= now_local().date())
    if CONFIG["features"].get("google_trends", True) and need_update:
        t = google_trends_scores(timeframe=CONFIG.get("report", {}).get("trends_timeframe", "today 5-y"))
        if t:
            state["trends"] = {"last_date": now_local().isoformat(), "holo_last": t["holo_last"],
                               "holo_peak": t["holo_peak"], "holo_rel": t["holo_rel"], "tf": t["tf"], "last_above": t["holo_high"]}
            trends_info = t
        save_state(state)
    else:
        if state["trends"].get("holo_rel") is not None:
            rel = state["trends"]["holo_rel"]; peak = state["trends"]["holo_peak"]; lastv = state["trends"]["holo_last"]
            threshold = CONFIG.get("report", {}).get("trends_high_threshold", 40)
            trends_info = {"tf": state["trends"].get("tf","today 5-y"), "holo_last": lastv, "holo_peak": peak,
                           "holo_rel": rel, "holo_high": rel >= threshold, "threshold": threshold}

    # Euphoria (basic 5) + others (F&G, Funding)
    eup_hits, eup_details = euphoria_signals(state, btc_d, eth_btc, gas, meme_count, trends_info)
    eup_total = 5 + (1 if CONFIG["features"].get("fear_greed", True) else 0) + (1 if CONFIG["features"].get("funding", True) else 0)
    add_details: List[str] = []
    p3 = CONFIG.get("thresholds", {}).get("phase3", {})
    if CONFIG["features"].get("fear_greed", True) and fng_val is not None:
        if fng_val >= p3.get("fng_greed", 80): eup_hits += 1; add_details.append(f"F&G {fng_val} (Greed)")
    if CONFIG["features"].get("funding", True):
        fl = [x for x in [btc_f_last, eth_f_last] if x is not None]; fa = [x for x in [btc_f_avg, eth_f_avg] if x is not None]
        last_hi = any(x >= p3.get("funding_last_pct", 0.05) for x in fl) if fl else False
        avg_hi  = any(x >= p3.get("funding_avg_pct", 0.03) for x in fa) if fa else False
        if last_hi or avg_hi: eup_hits += 1; add_details.append("Funding gorący (BTC/ETH)")
    if add_details: eup_details += add_details

    # Phase 4 (basic 2: BTC.D reversal, DXY↑) + VIX
    p4_hits, p4_details = phase4_signals(state, btc_d, dxy, dxy_slope)
    p4_total = 2 + (1 if CONFIG["features"].get("vix", True) else 0)
    if CONFIG["features"].get("vix", True) and (vix is not None):
        if vix >= CONFIG.get("thresholds", {}).get("phase4", {}).get("vix_level", 25) and (vix_slope is not None and vix_slope>0):
            p4_hits += 1; p4_details.append(f"VIX {vix:.1f} ↑")

    # Pi Cycle
    pct = pi_cycle_top_signal()

    # Verdict
    p2_ok = (rsi_cond + dev_cond + vol_cond) >= 2
    p3b_ok = weak_cond
    p3a_ok = (eup_hits >= 3)
    p4_ok = (p4_hits >= 2)
    p1_ok = not (p2_ok or p3a_ok or p3b_ok or p4_ok)

    # Filing the report
    def yn(b: bool) -> str: return "Tak ✅" if b else "Nie —"
    line_mkt: List[str] = []
    if p2info.get("hot_eth") is not None: line_mkt.append(f"HOT/ETH {p2info['hot_eth']:.8f}")
    if eth_btc is not None: line_mkt.append(f"ETH/BTC {eth_btc:.5f}")
    if total3_usd is not None: line_mkt.append(f"TOTAL3 ${total3_usd/1e9:,.1f}B".replace(",", " "))
    if btc_d is not None: line_mkt.append(f"BTC.D {btc_d:.1f}%")

    # HOT vs ETH
    rsi_v = p2info['rsi']; dev = p2info['dev_pct']; vol7 = p2info['vol7']; vol30 = p2info['vol30']
    ratio = (vol7/vol30) if vol30>0 else 0
    hot_class = "RSI wysokie" if (rsi_v is not None and rsi_v>=80) else ("RSI umiarkowane" if (rsi_v is not None and rsi_v>=60) else "RSI niskie")
    dev_class = "cena wyraźnie nad średnią" if dev>=30 else ("cena nad średnią" if dev>=0 else "cena poniżej średniej")
    vol_class = "wolumen eksploduje" if ratio>=3 else ("wolumen ≈ norma" if ratio>=1 else "wolumen niższy niż norma")
    hot_line = f"{hot_class} (RSI {rsi_v:.1f}), {dev_class}, {vol_class}"
    vol_today = float(df['vol_hot'].iloc[-1]); vol_ratio = ratio; label = volume_label(vol_ratio)
    hot_vol_line = (f"Wolumen HOT (USDT): dziś {vol_today:,.0f} | śr.7d {vol7:,.0f} | śr.30d {vol30:,.0f} | 7d/30d = {vol_ratio:.2f}× ({vol_ratio*100:.0f}%) — {label}").replace(",", " ")

    ethbtc_zone = format_ethbtc_zone(eth_btc)
    t3_line = total3_trend_line(state)

    # Trends 5y
    trends_line = None
    if trends_info and trends_info.get("holo_rel") is not None:
        rel = trends_info["holo_rel"]; thr = trends_info.get("threshold", 40)
        lastv = trends_info.get("holo_last"); peakv = trends_info.get("holo_peak")
        trends_line = f"Holochain 5y = {rel:.0f}/100 (last {lastv:.0f}, peak {peakv:.0f}, próg {thr})"

    # Pi Cycle
    pi_line = None
    if pct is not None:
        arrow = "↑" if pct["gap_delta"] > 0 else ("↓" if pct["gap_delta"] < 0 else "→")
        pi_line = (f"Pi Cycle: 111DMA {pct['dma111']:,.0f} vs 2×350DMA {pct['dma350x2']:,.0f} | "
                   f"różnica {pct['gap']:,.0f} ({pct['gap_pct']:.2f}%) {arrow} {pct['gap_delta']:,.0f} | "
                   f"sygnał: {'CROSS ⚠️' if pct['cross'] else 'brak'}").replace(",", " ")

    # HOT/ETH vs ATH
    hot_eth_ath = CONFIG.get("overrides", {}).get("hot_eth_ath")
    ath_line = None
    if hot_eth_ath and p2info.get("hot_eth") is not None:
        cur = p2info["hot_eth"]; ath = float(hot_eth_ath)
        diff_pct = (cur/ath - 1.0)*100.0 if ath>0 else 0.0
        ath_line = f"HOT/ETH vs ATH 2021 ({ath:.8f}): {cur:.8f} ({diff_pct:.1f}%)"

    gas_line = format_gas_line(gas)

    # Funding & F&G & Stable & VIX lines
    def fmt_pct(x: Optional[float]) -> str: return "—" if x is None else f"{x:.3f}%"
    fund_line = f"Funding (8h) BTC last/avg {fmt_pct(btc_f_last)}/{fmt_pct(btc_f_avg)} | ETH {fmt_pct(eth_f_last)}/{fmt_pct(eth_f_avg)}"
    fng_line = None if fng_val is None else f"Fear & Greed: {fng_val} ({fng_cls or ''})"
    s_line, _ = stables_line_and_delta(state)
    vix_line = None if vix is None else f"VIX: {vix:.1f} " + ("(rosnący)" if (vix_slope and vix_slope>0) else "(malejący/flat)")

    # Memecoins lines
    meme_line = None
    if meme_count is not None:
        meme_thr = CONFIG["thresholds"]["phase3"]["meme_top50_min"]
        sample = f" (np. {', '.join(meme_names[:5])})" if meme_names else ""
        meme_line = f"Memecoiny w TOP50 wolumenu: {meme_count}/50 (próg {meme_thr}){sample}"

    # Sending the report
    msg: List[str] = []
    msg.append("📊 Raport dzienny HOT (12:00) — strefa CEST/CET")
    msg.append(f"Data: {today}")
    msg.append("")
    msg.append("🚦 Werdykt:")
    msg.append(f"• Faza 1 (trzymaj i obserwuj): {yn(p1_ok)}")
    msg.append(f"• Faza 2 (paraboliczny HOT): {yn(p2_ok)}")
    msg.append(f"• Faza 3‑A (euforia rynku): {yn(p3a_ok)} ({eup_hits}/{eup_total})")
    msg.append(f"• Faza 3‑B (słabość HOT): {yn(p3b_ok)}")
    msg.append(f"• Faza 4 (dystrybucja): {yn(p4_ok)} ({p4_hits}/{p4_total})")
    msg.append("")
    msg.append("Kluczowe sygnały:")
    if line_mkt: msg.append("• " + " | ".join(line_mkt))
    if t3_line: msg.append("• " + t3_line)
    if ethbtc_zone: msg.append("• " + ethbtc_zone)
    if meme_line: msg.append("• " + meme_line)
    msg.append("• HOT vs ETH: " + hot_line)
    if p2info.get("hot_eth_change_pct") is not None and p2info.get("rsi_delta") is not None:
        msg.append(f"• zmiana d/d: HOT/ETH {p2info['hot_eth_change_pct']:.2f}% | RSI {p2info['rsi_delta']:.1f}")
    if ath_line: msg.append("• " + ath_line)
    msg.append("• " + hot_vol_line)
    if trends_line: msg.append("• Google Trends: " + trends_line)
    if dxy is not None:
        msg.append(f"• DXY: {dxy:.2f} " + ("(rosnący)" if (dxy_slope and dxy_slope>0) else "(malejący/flat)"))
    if gas_line: msg.append("• " + gas_line)
    if fng_line: msg.append("• " + fng_line)
    if CONFIG["features"].get("funding", True): msg.append("• " + fund_line)
    if CONFIG["features"].get("stablecoins", True) and s_line: msg.append("• " + s_line)
    if vix_line: msg.append("• " + vix_line)
    if pi_line: msg.append("• " + pi_line)
    if eup_hits>0: msg.append("• Spełnione sygnały euforii: " + "; ".join(eup_details))

    # Explanations
    msg.append("")
    msg.append("Legenda:")
    msg.append("• Pi Cycle: porównanie średnich BTC — 111DMA vs 2×350DMA; gap = 111DMA − 2×350DMA.")
    msg.append("  CROSS = gdy gap przechodzi z wartości ujemnych na dodatnie (ostrzeżenie szczytu).")
    msg.append("  Wskazówka: im bliżej 0 i gdy gap rośnie (↑), tym bliżej sygnału; spadek (↓) = oddalamy się. % = odległość względem 2×350DMA.")
    msg.append("• Gas (ETH): safe/propose/fast = sugerowane ceny gwei; base = opłata bazowa sieci (EIP‑1559). Długo wysokie ⇒ euforia/obciążona sieć.")
    msg.append("• Funding: dodatnie i rosnące (zwłaszcza ≥0.05% last lub ≥0.03% avg) ⇒ ryzyko squeeze/topów lokalnych.")
    msg.append("• Fear & Greed: ≥80 = ekstremalna chciwość (składnik euforii).")

    tg_send("\n".join(msg))

    # CSV
    row = {
        "date": today,
        "hot_eth": round(p2info["hot_eth"],8),
        "hot_eth_change_pct": round(p2info["hot_eth_change_pct"],2) if p2info.get("hot_eth_change_pct") is not None else "",
        "rsi": round(p2info["rsi"],1), "rsi_delta": round(p2info["rsi_delta"],1) if p2info.get("rsi_delta") is not None else "",
        "dev_pct": round(p2info["dev_pct"],1),
        "vol_today": round(float(df['vol_hot'].iloc[-1]),0),
        "vol7": round(p2info["vol7"],0), "vol30": round(p2info["vol30"],0),
        "eth_btc": round(eth_btc,5) if eth_btc is not None else "",
        "total3_usd_b": round(total3_usd/1e9,1) if total3_usd is not None else "",
        "btc_d": round(btc_d,2) if btc_d is not None else "",
        "gas_safe": gas["safe"] if (gas and isinstance(gas,dict) and gas.get("safe") is not None) else "",
        "gas_prop": gas["propose"] if (gas and isinstance(gas,dict) and gas.get("propose") is not None) else "",
        "gas_fast": gas["fast"] if (gas and isinstance(gas,dict) and gas.get("fast") is not None) else "",
        "gas_base": gas["base"] if (gas and isinstance(gas,dict) and gas.get("base") is not None) else "",
        "dxy": round(dxy,2) if dxy is not None else "",
        "pmi": "",
        "pmi_series": "",
        "fng": fng_val if fng_val is not None else "",
        "vix": round(vix,1) if vix is not None else "",
        "btc_f_last": round(btc_f_last,3) if btc_f_last is not None else "",
        "btc_f_avg": round(btc_f_avg,3) if btc_f_avg is not None else "",
        "eth_f_last": round(eth_f_last,3) if eth_f_last is not None else "",
        "eth_f_avg": round(eth_f_avg,3) if eth_f_avg is not None else "",
        "stables_cap_b": round(stables_cap/1e9,1) if stables_cap is not None else "",
        "meme_top50": meme_count if meme_count is not None else "",
        "pi_gap": round(pct["gap"],0) if pct is not None else "",
        "pi_gap_delta": round(pct["gap_delta"],0) if pct is not None else "",
        "pi_gap_pct": round(pct["gap_pct"],2) if (pct and pct["gap_pct"] is not None) else "",
        "euphoria_hits": eup_hits, "euphoria_total": eup_total,
        "phase4_hits": p4_hits, "phase4_total": p4_total,
        "trends_last_5y": round(trends_info["holo_last"],0) if (trends_info and trends_info.get("holo_last") is not None) else "",
        "trends_peak_5y": round(trends_info["holo_peak"],0) if (trends_info and trends_info.get("holo_peak") is not None) else "",
        "trends_rel_5y": round(trends_info["holo_rel"],0) if (trends_info and trends_info.get("holo_rel") is not None) else ""
    }
    log_daily_csv(today, row)

    # Alerts
    state = load_state()
    if p2_ok and throttle(state, "phase2", hours=12):
        tg_send("⚠️ Faza 2: HOT wygląda na paraboliczny (≥2/3). Plan: sprzedać 20% w 3–5 krokach."); mark_alert(state, "phase2")
    if p3a_ok and throttle(state, "phase3_euphoria", hours=12):
        tg_send("⚠️ Faza 3‑A: Euforia rynku (≥3 warunki). Sprzedaż 60% w 3–6 krokach."); mark_alert(state, "phase3_euphoria")
    if p3b_ok and throttle(state, "phase3_weak", hours=8):
        tg_send("⚠️ Faza 3‑B: Słabość HOT (≥2/3). Plan: sprzedać 60% agresywnie."); mark_alert(state, "phase3_weak")
    if p4_ok and throttle(state, "phase4", hours=24):
        tg_send("🔴 Faza 4: Sygn. odwrotu (≥2/3). Domknij ostatnie 20%."); mark_alert(state, "phase4")

    # Additional alerts
    if (CONFIG["features"].get("fear_greed", True)
        and fng_val is not None
        and fng_val >= CONFIG.get("thresholds", {}).get("phase3", {}).get("fng_greed", 80)
        and throttle(state, "fng_greed", hours=12)):
        tg_send(f"⚠️ Fear & Greed {fng_val} — sentyment rozgrzany."); mark_alert(state, "fng_greed")
    if ((btc_f_last and btc_f_last >= CONFIG.get("thresholds", {}).get("phase3", {}).get("funding_last_pct", 0.05)) or
        (eth_f_last and eth_f_last >= CONFIG.get("thresholds", {}).get("phase3", {}).get("funding_last_pct", 0.05)) or
        (btc_f_avg and btc_f_avg >= CONFIG.get("thresholds", {}).get("phase3", {}).get("funding_avg_pct", 0.03)) or
        (eth_f_avg and eth_f_avg >= CONFIG.get("thresholds", {}).get("phase3", {}).get("funding_avg_pct", 0.03))) and throttle(state, "funding_hot", hours=12):
        tg_send("⚠️ Funding (BTC/ETH) wysoki — rynek mocno zlewarowany."); mark_alert(state, "funding_hot")


def intraday_alerts() -> None:
    hour = now_local().hour
    h0, h1 = CONFIG["schedule"]["intraday_active_hours"]
    if not (h0 <= hour <= h1): return
    state = load_state()
    df = synthetic_hot_eth(tf="1d", limit=200)
    if df is None or len(df)<60: return
    rsi_cond, dev_cond, vol_cond, _ = phase2_signals(df)
    weak_cond, _ = phase3_hot_weakness(df)
    if (rsi_cond + dev_cond + vol_cond) >= 2 and throttle(state, "phase2", hours=6):
        tg_send("⚠️ [Intraday] Faza 2: spełnione ≥2/3. Rozważ sprzedaż 20%."); mark_alert(state, "phase2")
    if weak_cond and throttle(state, "phase3_weak", hours=6):
        tg_send("⚠️ [Intraday] Faza 3‑B: pierwsze oznaki słabości HOT."); mark_alert(state, "phase3_weak")


if __name__ == "__main__":
    try:
        daily_report()
    except Exception as e:
        tg_send(f"❌ Błąd raportu dziennego: {e}")
    sched = BackgroundScheduler(
        timezone=TZ,
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60}
    )
    sched.add_job(daily_report, CronTrigger(hour=CONFIG["schedule"]["daily_report_hour"], minute=0))

    # If you want ONLY daily report, set intraday_minutes to 0 in config.json
    if CONFIG["schedule"].get("intraday_minutes", 0) and CONFIG["schedule"]["intraday_minutes"] > 0:
        sched.add_job(intraday_alerts, "interval", minutes=CONFIG["schedule"]["intraday_minutes"])

    sched.start()
    try:
        while True: time.sleep(60)
    except KeyboardInterrupt:
        pass