#!/usr/bin/env python3
"""
Kalshi Weather Arb Bot
Scans NOAA forecasts vs Kalshi prediction market prices, detects edges,
alerts via Telegram, and optionally auto-executes trades.

All glory to Jesus.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone

import requests
import schedule
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

# ─── Config ──────────────────────────────────────────────────────────────────

load_dotenv()

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private.pem")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.15"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.20"))
SCAN_INTERVAL_HRS = int(os.getenv("SCAN_INTERVAL_HRS", "2"))
ALERT_ONLY = os.getenv("ALERT_ONLY", "true").lower() == "true"
REENABLE_PIN = os.getenv("REENABLE_PIN", "")

# Moonshot Anomaly Settings
MOONSHOT_MAX_DAILY = int(os.getenv("MOONSHOT_MAX_DAILY", "2"))
MOONSHOT_MIN_EDGE = float(os.getenv("MOONSHOT_MIN_EDGE", "0.30")) # e.g. 30% edge
MOONSHOT_MAX_ASK = float(os.getenv("MOONSHOT_MAX_ASK", "0.15"))   # max cost 15 cents

# Circuit breaker — auto-disables trading if balance drops too low
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "0"))  # set to your initial deposit
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.70"))     # trip at 70% of high-water mark
HWM_PATH = "high_water_mark.txt"

KALSHI_BASE = "https://api.elections.kalshi.com"
KALSHI_API = f"{KALSHI_BASE}/trade-api/v2"
TRADE_LOG_PATH = "trade_log.jsonl"

# ─── Logging ─────────────────────────────────────────────────────────────────

log = logging.getLogger("kalshi-bot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_sh)
_fh = logging.FileHandler("bot.log")
_fh.setFormatter(_fmt)
log.addHandler(_fh)

# ─── City Lookup ─────────────────────────────────────────────────────────────

CITY_COORDS = {
    "nyc":     (40.7128, -74.0060),
    "new york": (40.7128, -74.0060),
    "chicago": (41.8781, -87.6298),
    "miami":   (25.7617, -80.1918),
    "la":      (34.0522, -118.2437),
    "los angeles": (34.0522, -118.2437),
    "denver":  (39.7392, -104.9903),
    "houston": (29.7604, -95.3698),
    "atlanta": (33.7490, -84.3880),
    "dallas":  (32.7767, -96.7970),
    "phoenix": (33.4484, -112.0740),
    "seattle": (47.6062, -122.3321),
    "boston":   (42.3601, -71.0589),
    "dc":      (38.9072, -77.0369),
    "washington": (38.9072, -77.0369),
    "philadelphia": (39.9526, -75.1652),
    "san francisco": (37.7749, -122.4194),
    "sf":      (37.7749, -122.4194),
    "las vegas": (36.1699, -115.1398),
    "minneapolis": (44.9778, -93.2650),
    "detroit": (42.3314, -83.0458),
    "nashville": (36.1627, -86.7816),
    "charlotte": (35.2271, -80.8431),
    "austin":  (30.2672, -97.7431),
}

# ─── RSA-PSS Request Signing ────────────────────────────────────────────────

_private_key = None


def _load_private_key():
    """Load the RSA private key from PEM file (cached)."""
    global _private_key
    if _private_key is None:
        with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
            _private_key = serialization.load_pem_private_key(f.read(), password=None)
    return _private_key


def _sign_request(method: str, path: str, body: str = "") -> dict:
    """
    Compute Kalshi RSA-PSS authentication headers.

    Signs: timestamp_ms + method + path (no query string)
    Returns dict with the three required headers.
    """
    timestamp_ms = str(int(time.time() * 1000))

    # Strip query params for signing
    sign_path = path.split("?")[0]

    message = f"{timestamp_ms}{method.upper()}{sign_path}{body}"

    key = _load_private_key()
    signature = key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json",
    }


# ─── Kalshi API Client ──────────────────────────────────────────────────────

def kalshi_request(method: str, path: str, body: dict | None = None) -> dict:
    """Make an authenticated request to the Kalshi API."""
    url = f"{KALSHI_API}{path}"
    body_str = json.dumps(body) if body else ""
    headers = _sign_request(method, f"/trade-api/v2{path}", body_str)

    try:
        resp = requests.request(method, url, headers=headers, data=body_str, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.warning("Kalshi API error on %s %s: %s", method, path, e)
        return {}


def get_balance() -> float:
    """Get current portfolio balance in dollars."""
    data = kalshi_request("GET", "/portfolio/balance")
    if not data:
        return 0.0
    # Kalshi returns balance in cents
    return data.get("balance", 0) / 100.0


def get_weather_markets() -> list[dict]:
    """
    Fetch open weather markets from Kalshi.
    Uses cursor pagination and filters for weather/temp-related tickers.
    """
    markets = []
    cursor = None
    weather_keywords = ["KXHIGHTEMP", "KXTEMP", "WEATHER", "HIGHTEMP", "TEMP"]

    for _ in range(20):  # safety cap on pages
        params = "?status=open&limit=200"
        if cursor:
            params += f"&cursor={cursor}"

        data = kalshi_request("GET", f"/markets{params}")
        if not data:
            break

        for m in data.get("markets", []):
            ticker = m.get("ticker", "").upper()
            title = m.get("title", "").upper()
            # Match weather-related markets
            if any(kw in ticker or kw in title for kw in weather_keywords):
                markets.append(m)

        cursor = data.get("cursor")
        if not cursor:
            break

    log.info("Found %d weather markets", len(markets))
    return markets


def place_order(ticker: str, side: str, count: int, price: int) -> dict | None:
    """
    Place a limit order on Kalshi.
    side: 'yes' or 'no'
    count: number of contracts
    price: price in cents (1-99)
    """
    body = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "count": count,
        "type": "limit",
        "yes_price": price if side == "yes" else None,
        "no_price": price if side == "no" else None,
    }
    # Remove None values
    body = {k: v for k, v in body.items() if v is not None}

    data = kalshi_request("POST", "/portfolio/orders", body)
    if data and data.get("order"):
        log.info("Order placed: %s %s x%d @ %d¢", side, ticker, count, price)
        return data["order"]
    else:
        log.warning("Order failed for %s: %s", ticker, data)
        return None


# ─── NOAA Forecast ───────────────────────────────────────────────────────────

NOAA_HEADERS = {"User-Agent": "kalshi-weather-bot/1.0 (bot@example.com)"}


def get_noaa_forecast(lat: float, lon: float) -> list[dict] | None:
    """
    Get hourly forecast from NOAA for the given coordinates.
    Returns list of hourly period dicts with 'temperature' + 'temperatureUnit'.
    """
    try:
        # Step 1: Get the forecast URL from the points endpoint
        points_url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
        r = requests.get(points_url, headers=NOAA_HEADERS, timeout=10)
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecastHourly"]

        # Step 2: Fetch hourly forecast
        r2 = requests.get(forecast_url, headers=NOAA_HEADERS, timeout=10)
        r2.raise_for_status()
        periods = r2.json()["properties"]["periods"]
        return periods

    except requests.RequestException as e:
        log.warning("NOAA API error for (%s, %s): %s", lat, lon, e)
        return None
    except (KeyError, TypeError) as e:
        log.warning("NOAA response parse error: %s", e)
        return None


def parse_market_title(title: str) -> tuple[str, float, str] | None:
    """
    Parse a Kalshi weather market title to extract city, threshold, direction.

    Example titles:
      'Will NYC high exceed 75°F?'         → ('nyc', 75.0, 'above')
      'Will Chicago temp stay below 30°F?'  → ('chicago', 30.0, 'below')
      'Will the high temperature in New York exceed 80°F on March 25?'

    Returns (city_key, threshold_f, direction) or None if unparseable.
    """
    title_lower = title.lower()

    # Find city
    matched_city: str | None = None
    for city in sorted(CITY_COORDS.keys(), key=len, reverse=True):
        if city in title_lower:
            matched_city = city
            break

    if matched_city is None:
        return None

    # Find temperature threshold
    temp_match = re.search(r'(\d+)\s*°?\s*f', title_lower)
    if not temp_match:
        return None
    threshold = float(temp_match.group(1))

    # Determine direction
    if any(w in title_lower for w in ["exceed", "above", "over", "higher", "at least", "or more"]):
        direction = "above"
    elif any(w in title_lower for w in ["below", "under", "lower", "at most", "or less", "stay below"]):
        direction = "below"
    else:
        # Default: "exceed" is most common phrasing
        direction = "above"

    return matched_city, threshold, direction


def compute_noaa_probability(periods: list[dict], threshold: float, direction: str) -> float:
    """
    Compute probability based on NOAA hourly forecast.

    For 'above': fraction of hours where temp >= threshold
    For 'below': fraction of hours where temp < threshold

    Only uses the next 48 hours of data.
    """
    if not periods:
        return 0.0

    # Use up to 48 hours
    hours: list[dict] = periods[:48]
    matching = 0

    for p in hours:
        temp = p.get("temperature")
        unit = p.get("temperatureUnit", "F")
        if temp is None:
            continue

        # Convert Celsius to Fahrenheit if needed
        if unit == "C":
            temp = temp * 9.0 / 5.0 + 32.0

        if direction == "above" and temp >= threshold:
            matching += 1
        elif direction == "below" and temp < threshold:
            matching += 1

    total = len(hours)
    return matching / total if total > 0 else 0.0


# ─── Edge Detection & Sizing ────────────────────────────────────────────────

def compute_edge(noaa_prob: float, kalshi_ask: float, side: str) -> float:
    """
    Compute the edge between NOAA probability and Kalshi price.

    For 'yes' side: edge = noaa_prob - kalshi_yes_ask
    For 'no' side:  edge = (1 - noaa_prob) - kalshi_no_ask
    """
    if side == "yes":
        return noaa_prob - kalshi_ask
    else:
        return (1.0 - noaa_prob) - kalshi_ask


def quarter_kelly_size(edge: float, odds: float, bankroll: float) -> float:
    """
    Compute quarter-Kelly position size.

    Kelly fraction = (edge * odds - (1 - prob)) / odds
    Position = 0.25 * kelly_fraction * bankroll, capped at MAX_POSITION_PCT * bankroll
    """
    if odds <= 0 or edge <= 0:
        return 0.0

    prob = edge + (1.0 - edge)  # simplified — use implied prob
    # For binary markets: kelly = edge / odds
    kelly = edge / odds if odds > 0 else 0.0
    position = 0.25 * kelly * bankroll
    max_pos = MAX_POSITION_PCT * bankroll
    return min(max(position, 0.0), max_pos)


def compute_ev(noaa_prob: float, kalshi_ask: float, position_dollars: float) -> float:
    """Expected value of the trade."""
    if kalshi_ask <= 0:
        return 0.0
    # EV = position * (prob_win * payout - prob_lose * cost)
    # For a yes contract at ask price: payout = 1.00, cost = ask
    payout_per_contract = 1.0 - kalshi_ask
    loss_per_contract = kalshi_ask
    ev: float = position_dollars * (noaa_prob * payout_per_contract - (1 - noaa_prob) * loss_per_contract)
    return round(ev, 2)


# ─── Trade Logger ────────────────────────────────────────────────────────────

def log_trade(event: dict):
    """Append a trade event as a JSON line to trade_log.jsonl."""
    event.setdefault("ts", datetime.now(timezone.utc).isoformat())
    try:
        with open(TRADE_LOG_PATH, "a") as f:
            f.write(json.dumps(event) + "\n")
    except IOError as e:
        log.warning("Failed to write trade log: %s", e)


# ─── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    """Send a message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping alert")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("Telegram send failed: %s", resp.text)
        else:
            log.info("Telegram alert sent")
    except requests.RequestException as e:
        log.warning("Telegram error: %s", e)


def format_alert(ticker: str, title: str, noaa_prob: float, kalshi_ask: float,
                 edge: float, ev: float, position: float, side: str) -> str:
    """Format a Markdown alert message for Telegram."""
    return (
        f"🌤️ *EDGE DETECTED*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"*{title}*\n"
        f"Ticker: `{ticker}`\n\n"
        f"📊 NOAA Prob: *{noaa_prob:.0%}*\n"
        f"💰 Kalshi Ask: *{kalshi_ask:.0%}*\n"
        f"📈 Edge: *{edge:.0%}*\n"
        f"🎯 Side: *{side.upper()}*\n"
        f"💵 Position: *${position:.2f}*\n"
        f"📐 EV: *${ev:.2f}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Mode: {'🔔 ALERT ONLY' if ALERT_ONLY else '⚡ AUTO-EXECUTE'}"
    )


# ─── Main Scanner ───────────────────────────────────────────────────────────

def run_scan():
    """Run one full scan cycle: fetch markets, check NOAA, detect edges."""
    log.info("═══ Starting scan cycle ═══")

    # 1. Get bankroll
    bankroll = get_balance()
    if bankroll <= 0:
        log.warning("Bankroll is $%.2f — skipping scan", bankroll)
        log_trade({"event": "no_edges", "bankroll_before": bankroll,
                    "reason": "zero_bankroll"})
        return

    log.info("Bankroll: $%.2f", bankroll)

    # 2. Get weather markets
    markets = get_weather_markets()
    if not markets:
        log.info("No weather markets found")
        log_trade({"event": "no_edges", "bankroll_before": bankroll,
                    "reason": "no_markets"})
        return

    edges_found = 0

    # 3. Scan each market
    for market in markets:
        ticker: str = market.get("ticker", "UNKNOWN")
        title: str = market.get("title", "")
        yes_ask: float = float(market.get("yes_ask", 0)) / 100.0  # cents → dollars
        no_ask: float = float(market.get("no_ask", 0)) / 100.0

        # Parse title
        parsed = parse_market_title(title)
        if not parsed:
            log.debug("Could not parse market title: %s", title)
            continue

        city_key, threshold, direction = parsed
        lat, lon = CITY_COORDS[city_key]

        # Get NOAA forecast
        forecast = get_noaa_forecast(lat, lon)
        if not forecast:
            log.warning("No forecast data for %s — skipping %s", city_key, ticker)
            continue

        # Compute probability
        noaa_prob = compute_noaa_probability(forecast, threshold, direction)

        # Check edge for YES side
        edge_yes = compute_edge(noaa_prob, yes_ask, "yes")
        # Check edge for NO side
        edge_no = compute_edge(noaa_prob, no_ask, "no")

        # Pick the better side
        if edge_yes >= edge_no and edge_yes >= MIN_EDGE:
            side, edge, ask = "yes", edge_yes, yes_ask
        elif edge_no > edge_yes and edge_no >= MIN_EDGE:
            side, edge, ask = "no", edge_no, no_ask
        else:
            log.debug("No edge on %s (yes=%.3f, no=%.3f)", ticker, edge_yes, edge_no)
            continue

        # Compute position size and EV
        odds = (1.0 / ask) - 1.0 if ask > 0 else 0.0
        position = quarter_kelly_size(edge, odds, bankroll)
        ev = compute_ev(noaa_prob, ask, position)

        if position < 1.0:
            log.debug("Position too small ($%.2f) on %s — skipping", position, ticker)
            continue

        edges_found += 1
        count = max(1, int(position / ask))  # number of contracts
        price_cents = int(ask * 100)

        log.info("EDGE: %s | NOAA=%.0f%% Ask=%.0f%% Edge=%.0f%% EV=$%.2f Side=%s",
                 ticker, noaa_prob * 100, ask * 100, edge * 100, ev, side)
        # Build trade log entry
        trade_entry: dict = {
            "event": "edge_found",
            "ticker": ticker,
            "title": title,
            "noaa_prob": round(float(noaa_prob), 4),
            "kalshi_ask": round(float(ask), 4),
            "edge": round(float(edge), 4),
            "position_size": round(float(position), 2),
            "ev": ev,
            "side": side,
            "bankroll_before": round(float(bankroll), 2),
            "order_id": None,
        }

        # Alert
        alert_msg = format_alert(ticker, title, noaa_prob, ask, edge, ev, position, side)

        # Moonshot check
        is_moonshot = (ask <= MOONSHOT_MAX_ASK and edge >= MOONSHOT_MIN_EDGE)
        allowed_moonshot = is_moonshot and check_moonshot_limit()
        
        should_execute = not ALERT_ONLY
        if allowed_moonshot:
            log.info("🚀 MOONSHOT DETECTED: %s", ticker)
            should_execute = True  # Auto-overrides ALERT_ONLY for moonshots
            alert_msg = f"🚀🚀🚀 *MOONSHOT ANOMALY DETECTED* 🚀🚀🚀\n" + alert_msg
            
        if not should_execute:
            send_telegram(alert_msg)
            log_trade(trade_entry)
        else:
            # Auto-execute
            order = place_order(ticker, side, count, price_cents)
            if order:
                if allowed_moonshot:
                    increment_moonshot_count()
                    trade_entry["is_moonshot"] = True
                    
                trade_entry["event"] = "executed"
                trade_entry["order_id"] = order.get("order_id")
                send_telegram(alert_msg + f"\n\n✅ *ORDER PLACED*\nID: `{order.get('order_id')}`")
            else:
                trade_entry["event"] = "failed"
                send_telegram(alert_msg + "\n\n❌ *ORDER FAILED*")
            log_trade(trade_entry)

    if edges_found == 0:
        log.info("No edges found this cycle")
        log_trade({"event": "no_edges", "bankroll_before": round(float(bankroll), 2),
                    "markets_scanned": len(markets)})

    log.info("═══ Scan complete — %d edges found ═══", edges_found)


# ─── Circuit Breaker + High-Water Mark ───────────────────────────────────────────

def _load_hwm() -> float:
    """Load the high-water mark from disk."""
    try:
        with open(HWM_PATH, "r") as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return STARTING_CAPITAL


def _save_hwm(hwm: float):
    """Persist the high-water mark to disk."""
    with open(HWM_PATH, "w") as f:
        f.write(f"{hwm:.2f}")


def update_high_water_mark(bankroll: float) -> float:
    """
    Update the high-water mark if current balance exceeds it.
    Returns the current HWM.
    """
    hwm = _load_hwm()
    if bankroll > hwm:
        log.info("📊 New high-water mark: $%.2f (was $%.2f)", bankroll, hwm)
        hwm = bankroll
        _save_hwm(hwm)
    return hwm

# ─── Moonshot Daily Tracker ────────────────────────────────────────────────────

def check_moonshot_limit() -> bool:
    """Check if we've hit the maximum allowed moonshots for today."""
    if MOONSHOT_MAX_DAILY <= 0:
        return False
        
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open("moonshots_today.txt", "r") as f:
            data = f.read().strip().split(",")
            if len(data) == 2 and data[0] == today:
                return int(data[1]) < MOONSHOT_MAX_DAILY
    except (FileNotFoundError, ValueError):
        pass
    
    return True


def increment_moonshot_count():
    """Increment the daily tracker for executed moonshots."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    try:
        with open("moonshots_today.txt", "r") as f:
            data = f.read().strip().split(",")
            if len(data) == 2 and data[0] == today:
                count = int(data[1])
    except (FileNotFoundError, ValueError):
        pass
        
    with open("moonshots_today.txt", "w") as f:
        f.write(f"{today},{count + 1}")


def stop_loss_check() -> bool:
    """
    Circuit breaker: if balance drops below STOP_LOSS_PCT of the high-water mark,
    auto-disable trading and send an urgent Telegram alert.

    Uses high-water mark (not just STARTING_CAPITAL) so gains are protected too.
    Returns True if trading is still safe, False if circuit breaker tripped.
    """
    global ALERT_ONLY

    if STARTING_CAPITAL <= 0:
        # No starting capital configured — can't compute stop loss
        return True

    bankroll = get_balance()
    hwm = update_high_water_mark(bankroll)
    threshold = hwm * STOP_LOSS_PCT

    if bankroll >= threshold:
        return True

    # ═══ CIRCUIT BREAKER TRIPPED ═══
    if not ALERT_ONLY:
        ALERT_ONLY = True  # disable auto-execution in memory
        drawdown_pct = (1.0 - bankroll / hwm) * 100

        log.warning("🚨 CIRCUIT BREAKER TRIPPED — balance $%.2f < threshold $%.2f (%.0f%% drawdown from HWM $%.2f)",
                    bankroll, threshold, drawdown_pct, hwm)

        alert = (
            f"🚨🚨🚨 *CIRCUIT BREAKER TRIPPED* 🚨🚨🚨\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Balance: *${bankroll:.2f}*\n"
            f"High-Water Mark: *${hwm:.2f}*\n"
            f"Drawdown: *{drawdown_pct:.1f}%*\n"
            f"Threshold: *{(1 - STOP_LOSS_PCT) * 100:.0f}% max drawdown*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⚠️ *Auto-execute DISABLED*\n"
            f"Bot switched to ALERT ONLY mode.\n"
            f"Send `/reenable` then `CONFIRM REENABLE` (or with PIN if set)\n"
            f"in this chat to resume trading."
        )

        send_telegram(alert)
        log_trade({
            "event": "circuit_breaker",
            "bankroll": round(bankroll, 2),
            "high_water_mark": round(hwm, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "threshold": round(threshold, 2),
        })

    return False


# ─── P&L Tracking ────────────────────────────────────────────────────────────

SETTLEMENT_TS_PATH = "last_settlement_ts.txt"


def _load_last_settlement_ts() -> str:
    """Load the timestamp of the last reported settlement."""
    try:
        with open(SETTLEMENT_TS_PATH, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _save_last_settlement_ts(ts: str):
    """Save the timestamp of the last reported settlement."""
    with open(SETTLEMENT_TS_PATH, "w") as f:
        f.write(ts)


def get_positions() -> list[dict]:
    """Fetch current open positions from Kalshi."""
    data = kalshi_request("GET", "/portfolio/positions?limit=200")
    if not data:
        return []
    positions = data.get("market_positions", data.get("positions", []))
    return positions if isinstance(positions, list) else []


def get_settlements() -> list[dict]:
    """Fetch settlement history from Kalshi."""
    data = kalshi_request("GET", "/portfolio/settlements?limit=100")
    if not data:
        return []
    settlements = data.get("settlements", [])
    return settlements if isinstance(settlements, list) else []


def check_pnl():
    """
    Check for new settlements and report P&L via Telegram.
    Runs after each scan cycle.
    """
    log.info("Checking P&L settlements...")

    settlements = get_settlements()
    if not settlements:
        return

    last_ts = _load_last_settlement_ts()
    new_settlements: list[dict] = []

    for s in settlements:
        ts = s.get("settled_ts", s.get("ts", s.get("created_time", "")))
        if ts > last_ts:
            new_settlements.append(s)

    if not new_settlements:
        log.debug("No new settlements since last check")
        return

    # Sort by timestamp
    new_settlements.sort(key=lambda x: x.get("settled_ts", x.get("ts", "")))

    total_pnl: float = 0.0
    messages: list[str] = []

    for s in new_settlements:
        ticker = s.get("ticker", s.get("market_ticker", "UNKNOWN"))
        revenue = float(s.get("revenue", s.get("settlement_value", 0))) / 100.0  # cents → dollars
        cost = float(s.get("cost", s.get("total_cost", 0))) / 100.0
        pnl = revenue - cost
        total_pnl += pnl
        result = s.get("market_result", s.get("result", "unknown"))
        side = s.get("side", "unknown")
        count = s.get("count", s.get("quantity", 0))

        emoji = "💰" if pnl >= 0 else "📉"
        messages.append(
            f"{emoji} `{ticker}`\n"
            f"   Side: {side} × {count}\n"
            f"   Result: {result}\n"
            f"   Cost: ${cost:.2f} → Revenue: ${revenue:.2f}\n"
            f"   *P&L: {'+'if pnl >= 0 else ''}${pnl:.2f}*"
        )

        # Log each settlement
        log_trade({
            "event": "settlement",
            "ticker": ticker,
            "side": side,
            "count": count,
            "cost": round(cost, 2),
            "revenue": round(revenue, 2),
            "pnl": round(pnl, 2),
            "result": result,
        })

    # Get current balance for the summary
    bankroll = get_balance()

    # Build the Telegram message
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    alert = (
        f"📊 *P&L SETTLEMENT REPORT*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{len(new_settlements)} market(s) settled:\n\n"
        + "\n\n".join(messages) +
        f"\n\n━━━━━━━━━━━━━━━\n"
        f"{pnl_emoji} *Total P&L: {'+'if total_pnl >= 0 else ''}${total_pnl:.2f}*\n"
        f"💳 *Balance: ${bankroll:.2f}*"
    )

    send_telegram(alert)
    log.info("P&L report sent — %d settlements, total $%.2f", len(new_settlements), total_pnl)

    # Save the latest timestamp
    latest_ts = new_settlements[-1].get("settled_ts", new_settlements[-1].get("ts", ""))
    if latest_ts:
        _save_last_settlement_ts(latest_ts)


def send_portfolio_summary():
    """Send a daily portfolio snapshot via Telegram."""
    bankroll = get_balance()
    positions = get_positions()

    if not positions:
        msg = (
            f"📋 *DAILY PORTFOLIO SNAPSHOT*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🏦 Balance: *${bankroll:.2f}*\n"
            f"📦 Open positions: *0*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"_No active positions_"
        )
        send_telegram(msg)
        return

    lines: list[str] = []
    for p in positions[:20]:  # cap at 20 to avoid message overflow
        ticker = p.get("ticker", p.get("market_ticker", "?"))
        side = p.get("side", "?")
        qty = p.get("quantity", p.get("count", 0))
        avg_price = float(p.get("average_price", p.get("price", 0))) / 100.0
        market_price = float(p.get("market_price", p.get("yes_price", 0))) / 100.0
        unrealized = (market_price - avg_price) * int(qty) if side == "yes" else (avg_price - market_price) * int(qty)
        emoji = "🟢" if unrealized >= 0 else "🔴"
        lines.append(f"{emoji} `{ticker}` {side}×{qty} @ {avg_price:.0%} → {market_price:.0%} (*{'+'if unrealized>=0 else ''}${unrealized:.2f}*)")

    msg = (
        f"📋 *DAILY PORTFOLIO SNAPSHOT*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏦 Balance: *${bankroll:.2f}*\n"
        f"📦 Open positions: *{len(positions)}*\n"
        f"━━━━━━━━━━━━━━━\n"
        + "\n".join(lines)
    )

    send_telegram(msg)
    log.info("Portfolio summary sent — %d positions, $%.2f balance", len(positions), bankroll)


# ─── Telegram Command Handler ────────────────────────────────────────────────

_tg_last_update_id = 0
_reenable_pending = False  # waiting for CONFIRM REENABLE


def poll_telegram_commands():
    """
    Poll Telegram for incoming commands.
    Recognizes:
      /reenable  — initiates the re-enable flow
      CONFIRM REENABLE  — actually flips ALERT_ONLY back to false
      /status  — quick health check
    """
    global _tg_last_update_id, _reenable_pending, ALERT_ONLY

    if not TELEGRAM_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params: dict = {"offset": _tg_last_update_id + 1, "timeout": 0, "limit": 10}

    try:
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code != 200:
            return
        data = resp.json()
        if not data.get("ok"):
            return

        for update in data.get("result", []):
            update_id = update.get("update_id", 0)
            if update_id > _tg_last_update_id:
                _tg_last_update_id = update_id

            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()

            # Only process messages from our configured chat
            if chat_id != TELEGRAM_CHAT_ID:
                continue

            if text == "/reenable":
                if not ALERT_ONLY:
                    send_telegram("✅ Trading is already active. No action needed.")
                else:
                    _reenable_pending = True
                    pin_msg = " [PIN]" if REENABLE_PIN else ""
                    send_telegram(
                        f"⚠️ *Re-enable trading?*\n\n"
                        f"This will switch back to AUTO-EXECUTE mode.\n"
                        f"Type `CONFIRM REENABLE{pin_msg}` to proceed.\n"
                        f"Any other message cancels."
                    )

            elif _reenable_pending and text.startswith("CONFIRM REENABLE"):
                # Check PIN if configured
                parts = text.split()
                provided_pin = parts[2] if len(parts) > 2 else ""

                if REENABLE_PIN and provided_pin != REENABLE_PIN:
                    _reenable_pending = False
                    send_telegram("❌ Incorrect PIN. Re-enable cancelled.")
                    continue

                _reenable_pending = False
                ALERT_ONLY = False
                bankroll = get_balance()
                hwm = _load_hwm()
                send_telegram(
                    f"⚡ *AUTO-EXECUTE RE-ENABLED*\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"Balance: *${bankroll:.2f}*\n"
                    f"High-Water Mark: *${hwm:.2f}*\n"
                    f"Trading is now LIVE."
                )
                log.info("⚡ Trading re-enabled via Telegram command")
                log_trade({"event": "reenable", "bankroll": round(float(bankroll), 2),
                           "high_water_mark": round(float(hwm), 2)})

            elif _reenable_pending:
                _reenable_pending = False
                send_telegram("❌ Re-enable cancelled.")

            elif text == "/status":
                bankroll = get_balance()
                hwm = _load_hwm()
                mode = "🔔 ALERT ONLY" if ALERT_ONLY else "⚡ AUTO-EXECUTE"
                positions = get_positions()
                send_telegram(
                    f"📊 *BOT STATUS*\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"Mode: {mode}\n"
                    f"Balance: *${bankroll:.2f}*\n"
                    f"High-Water Mark: *${hwm:.2f}*\n"
                    f"Open positions: *{len(positions)}*\n"
                    f"Stop-loss floor: *${hwm * STOP_LOSS_PCT:.2f}*"
                )

    except (requests.RequestException, Exception) as e:
        log.debug("Telegram poll error: %s", e)


# ─── Graceful Shutdown ───────────────────────────────────────────────────────

_running = True


def _shutdown(signum, frame):
    global _running
    log.info("Shutdown signal received — stopping")
    _running = False


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ─── Entry Point ─────────────────────────────────────────────────────────────


def main():
    log.info("╔══════════════════════════════════════╗")
    log.info("║   Kalshi Weather Arb Bot v1.0        ║")
    log.info("║   All glory to Jesus.                ║")
    log.info("╚══════════════════════════════════════╝")
    log.info("Mode: %s", "ALERT ONLY" if ALERT_ONLY else "AUTO-EXECUTE")
    log.info("Min edge: %.0f¢", MIN_EDGE * 100)
    log.info("Scan interval: %dh", SCAN_INTERVAL_HRS)

    # Validate config
    if not KALSHI_KEY_ID:
        log.error("KALSHI_KEY_ID not set — exiting")
        sys.exit(1)
    if not os.path.exists(KALSHI_PRIVATE_KEY_PATH):
        log.error("Private key not found at %s — exiting", KALSHI_PRIVATE_KEY_PATH)
        sys.exit(1)

    # Run initial scan immediately
    log.info("Running initial scan...")
    stop_loss_check()
    run_scan()
    check_pnl()

    if STARTING_CAPITAL > 0:
        log.info("Circuit breaker: $%.2f starting capital, trips at $%.2f (%.0f%%)",
                 STARTING_CAPITAL, STARTING_CAPITAL * STOP_LOSS_PCT, STOP_LOSS_PCT * 100)
    else:
        log.info("Circuit breaker: DISABLED (set STARTING_CAPITAL in .env to enable)")

    # Try to import and schedule fed scanner
    try:
        from fed_scanner import run_fed_scan
        schedule.every().day.at("09:00").do(run_fed_scan)
        log.info("FedWatch scanner scheduled for 9:00 AM daily")
    except ImportError:
        log.info("fed_scanner not available — skipping FedWatch")

    # Schedule recurring scans + P&L checks
    def scan_and_check():
        stop_loss_check()  # circuit breaker BEFORE scanning
        run_scan()
        check_pnl()

    schedule.every(SCAN_INTERVAL_HRS).hours.do(scan_and_check)
    log.info("Scheduled weather scans every %d hours", SCAN_INTERVAL_HRS)

    # Daily portfolio summary at 8pm
    schedule.every().day.at("20:00").do(send_portfolio_summary)
    log.info("Daily portfolio summary scheduled for 8:00 PM")

    while _running:
        schedule.run_pending()
        poll_telegram_commands()  # check for /reenable, /status
        time.sleep(30)

    log.info("Bot stopped. All glory to Jesus.")


if __name__ == "__main__":
    main()
