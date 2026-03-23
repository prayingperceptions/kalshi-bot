# Kalshi Weather Arb Bot

> Scan NOAA weather forecasts against Kalshi prediction markets.
> Detect edges ≥15¢, alert via Telegram, and optionally auto-execute trades.
>
> *All glory to Jesus.*

---

## Quick Start

### Prerequisites

- Python 3.10+
- [Kalshi API Key](https://kalshi.com/settings) + RSA private key (`.pem`)
- [Telegram Bot Token](https://t.me/BotFather) + Chat ID ([get yours](https://t.me/userinfobot))

### Install

```bash
git clone https://github.com/prayingperceptions/kalshi-bot.git
cd kalshi-bot
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env with your credentials
```

Place your Kalshi RSA private key at `kalshi_private.pem` in the project root.

### Run

```bash
python bot.py
```

The bot will:
1. Run an initial scan immediately
2. Repeat every `SCAN_INTERVAL_HRS` (default: 2 hours)
3. Schedule FedWatch scan at 9:00 AM ET daily

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `KALSHI_KEY_ID` | — | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | `kalshi_private.pem` | Path to RSA private key |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Your Telegram chat ID |
| `MIN_EDGE` | `0.15` | Minimum edge (15¢) to trigger alert/trade |
| `MAX_POSITION_PCT` | `0.20` | Max position as % of bankroll |
| `SCAN_INTERVAL_HRS` | `2` | Hours between weather scans |
| `ALERT_ONLY` | `true` | `true` = alerts only · `false` = auto-execute |

---

## How It Works

```
Every SCAN_INTERVAL_HRS:
  1. GET /portfolio/balance → bankroll
  2. GET /markets → filter weather markets
  3. For each market:
     a. Parse title → city + temp threshold + direction
     b. Look up city → lat/lon
     c. NOAA hourly forecast → next 48hrs
     d. noaa_prob = fraction of hours matching condition
     e. edge = noaa_prob - kalshi_ask
     f. If edge ≥ MIN_EDGE → quarter-Kelly size → alert/trade

Daily at 9am ET:
  1. Fetch CME FedWatch probabilities
  2. Match to Kalshi Fed rate markets
  3. Same edge detection pipeline
```

### Cities Supported

NYC, Chicago, Miami, LA, Denver, Houston, Atlanta, Dallas, Phoenix, Seattle,
Boston, DC, Philadelphia, San Francisco, Las Vegas, Minneapolis, Detroit,
Nashville, Charlotte, Austin

---

## Trade Log

Every scan writes to `trade_log.jsonl` (append-only, one JSON object per line):

```json
{
  "ts": "2026-03-22T14:30:00Z",
  "event": "edge_found",
  "ticker": "KXHIGHTEMP-NYC-75",
  "noaa_prob": 0.87,
  "kalshi_ask": 0.61,
  "edge": 0.26,
  "position_size": 20.00,
  "ev": 8.52,
  "side": "yes",
  "bankroll_before": 100.00
}
```

---

## Testing Without Real Money

```bash
# Test NOAA endpoint
python -c "
import requests
r = requests.get('https://api.weather.gov/points/40.71,-74.01',
                 headers={'User-Agent':'test'}).json()
print('Forecast URL:', r['properties']['forecastHourly'])
"

# Test with low edge threshold (set MIN_EDGE=0.01 in .env)
# Keep ALERT_ONLY=true until you've validated 10+ alerts
```

---

## Deploy to Railway

1. Push to GitHub
2. Create new Railway project → link repo
3. Set environment variables in Railway dashboard
4. Railway auto-detects `Procfile` → runs `worker: python bot.py`

---

## File Structure

```
kalshi-bot/
├── bot.py              ← Main scanner, auth, edge detection, alerts
├── fed_scanner.py      ← CME FedWatch scanner
├── .env.example        ← Config template
├── .env                ← Live config (gitignored)
├── kalshi_private.pem  ← RSA key (gitignored)
├── requirements.txt    ← Python dependencies
├── Procfile            ← Railway deploy
├── trade_log.jsonl     ← Trade log (gitignored)
└── README.md           ← This file
```

---

*kalshi-bot v1.0 · All glory to Jesus.*
