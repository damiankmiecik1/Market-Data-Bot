Daily Telegram report about the cryptocurrency market with opinionated signals and risk flags. It computes a HOT/ETH ratio, checks ETH/BTC zone, TOTAL3 vs SMA50, gas (ETH), sentiment (Fear & Greed), VIX, Binance funding, stablecoins cap, Google Trends, Pi Cycle (BTC), and tracks memecoins in TOP50 by volume. Ships with a scheduler (12:00 CET/CEST by default) and a systemd unit for VPS deployment.

Use it to get a compact market pulse once a day.

Note
- The bot sends one “startup” report when it launches, then runs daily at the configured time.

## Features

- Telegram daily report (supports one or many recipients):
  - HOT/ETH synthetic ratio (from HOT/USDT and ETH/USDT)
    - RSI, deviation vs SMA50, 7d/30d volume ratio, daily change
  - ETH/BTC with zone classification (e.g., 0.05–0.08)
  - TOTAL3 (alt market cap excl. BTC/ETH) and its position vs SMA50
  - BTC dominance (BTC.D)
  - Gas (ETH) via Etherscan (optional API key)
  - Fear & Greed index
  - VIX (via Yahoo Finance)
  - Binance funding (BTC/ETH)
  - Stablecoins market cap and 7d delta
  - Google Trends (Holochain/HoloFuel, 5y window)
  - Pi Cycle metric (BTC 111DMA vs 2×350DMA)
  - Memecoins in TOP50 by volume (simple watchlist + sample names)
- “Phases” summary
  - Phase 2 (parabolic HOT): RSI/SMA50/volume combo
  - Phase 3‑A (market euphoria): multi-signal tally (BTC.D low, ETH/BTC zone, high gas, memecoins, F&G, funding)
  - Phase 3‑B (HOT weakness): red day with volume, RSI drop, EMA8 break
  - Phase 4 (distribution): BTC.D reversal up, DXY level/slope, optional VIX
- Robustness and ops
  - HTTP session with retry/backoff (soft‑fail, no unhandled exceptions on 4xx/5xx)
  - ccxt market cache + OHLCV cache (TTL) to reduce API calls
  - File lock around state.json (safe concurrent writes)
  - Telegram message chunking (prevents 4096‑char limit errors)
  - Daily CSV logs in logs/daily_YYYY.csv
  - Timezone from config (e.g., Europe/Warsaw); scheduler via APScheduler
  - Systemd unit (autostart on reboot, CPU/RAM limits, NO_PROXY to avoid interference with other apps)

Disclaimers
- Educational use. No financial advice. APIs may change or throttle; use sensible rate limits.

## Tech stack

- Python 3.12 (works on 3.10+)
- Libraries: ccxt, pandas, numpy, ta, yfinance, requests, APScheduler, pytz, pytrends, python-dotenv, filelock.

## Directory structure (suggested)

```
.
├─ bot.py
├─ requirements.txt
├─ config.sample.json     # copy to config.json and edit
├─ service/
│  └─ hot-exit-bot.service
├─ logs/                  # created at runtime
├─ .gitignore
└─ README.md
```

## Requirements

- Python 3.10+ (tested on 3.12)
- Telegram Bot token and at least one chat ID
- Optional: Etherscan API key (for the Gas line)

## Configuration

1) Copy the sample config and edit it:
```bash
cp config.sample.json config.json
```

2) Edit config.json:
- Put your Telegram bot token
- Either a single “chat_id” or an array “chat_ids” (the code will send to all IDs)
- Keep “timezone” and “daily_report_hour” as you like (12 Europe/Warsaw by default)
- “intraday_minutes”: 0 (default) → only the daily report
- Features can be toggled on/off
- Thresholds are opinionated defaults; adjust to taste

Example (truncated):
```json
{
  "telegram": {
    "bot_token": "PUT_YOUR_TOKEN",
    "chat_ids": ["123456789", "987654321"]
  },
  "schedule": {
    "timezone": "Europe/Warsaw",
    "daily_report_hour": 12,
    "intraday_minutes": 0,
    "intraday_active_hours": [8, 22]
  },
  "features": {
    "google_trends": true,
    "memecoin_counter": true,
    "fear_greed": true,
    "vix": true,
    "funding": true,
    "stablecoins": true
  }
}
```

3) Optional .env for Gas (Etherscan):
Create a .env file next to bot.py:
```
ETHERSCAN_KEY=YOUR_ETHERSCAN_API_KEY
```

## Local run (one-time test)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

# First run sends one immediate report, then schedules daily runs.
python bot.py
# Stop with Ctrl+C
```

Notes
- The immediate “startup” report is expected. Subsequent reports arrive daily at the configured time.
- Intraday alerts are off by default (intraday_minutes = 0).

## Deploy on a VPS (Ubuntu 22.04/24.04, systemd)

1) Create a project directory and venv:
```bash
sudo apt update
sudo apt install -y python3-venv python3-pip

mkdir -p /root/hot-exit-bot
cd /root/hot-exit-bot
# copy bot.py, requirements.txt, config.json, .env here
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

2) Create a systemd unit (adjust paths if you use a different user/dir):
Save as /etc/systemd/system/hot-exit-bot.service
```ini
[Unit]
Description=HOT Exit Bot
After=network-online.target

[Service]
User=root
WorkingDirectory=/root/hot-exit-bot
ExecStart=/root/hot-exit-bot/.venv/bin/python -u bot.py
Environment="PYTHONUNBUFFERED=1"
# Make sure this bot does not inherit proxy settings from other apps
Environment="HTTP_PROXY="
Environment="HTTPS_PROXY="
Environment="ALL_PROXY="
Environment="NO_PROXY=*"
Restart=always
RestartSec=5
# Conservative limits (tweak as you wish)
MemoryMax=300M
CPUQuota=35%
Nice=10

[Install]
WantedBy=multi-user.target
```

3) Enable and check:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hot-exit-bot
systemctl status hot-exit-bot --no-pager -l
journalctl -u hot-exit-bot -f   # logs (Ctrl+C to exit)
```

Behavior
- Sends one startup report upon service launch, then a daily report at the configured hour (timezone from config.json).
- Survives reboots (enabled service).

Update on VPS
```bash
cd /root/hot-exit-bot
# replace files or git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart hot-exit-bot
```

## Troubleshooting

- No Telegram messages:
  - Double‑check telegram.bot_token and chat IDs in config.json.
  - Verify service is running: systemctl status hot-exit-bot
  - Check logs: journalctl -u hot-exit-bot -n 200 --no-pager

- Gas shows “—”:
  - Add ETHERSCAN_KEY in .env (or the environment); Etherscan may throttle anonymously.

- Rate limits / timeouts:
  - The bot uses a requests Session with retry/backoff. External APIs (CoinGecko, Binance, Yahoo, etc.) may still be slow or rate‑limited at times.

- Timezone:
  - The scheduler uses the timezone in config.json. You don’t need to change system time on the server.

- Immediate report on start:
  - This is by design. If you prefer “only the daily report”, don’t restart the service unnecessarily close to report time.


## License

MIT
