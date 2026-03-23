#!/usr/bin/env python3
"""
CME FedWatch Scanner — Futures-Based
Computes Fed rate change probabilities directly from 30-Day Fed Funds
Futures prices (via Yahoo Finance), then compares against Kalshi
Fed rate markets for edge detection.

No scraping. No fragile CME endpoints. Just futures math.

All glory to Jesus.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import requests

import os
import time as _time

# Import shared utilities from bot.py
from bot import (
    ALERT_ONLY,
    MIN_EDGE,
    get_balance,
    kalshi_request,
    send_telegram,
    log_trade,
    quarter_kelly_size,
    place_order,
)

log = logging.getLogger("kalshi-bot.fed")

# ─── Fed Funds Futures via Yahoo Finance ─────────────────────────────────────
#
# 30-Day Fed Funds Futures (ZQ) settle at 100 minus the average effective
# fed funds rate for that month. The implied rate = 100 - futures_price.
#
# To compute rate change probabilities:
#   implied_rate = 100 - futures_price_for_meeting_month
#   If implied_rate < current_rate → market expects a CUT
#   If implied_rate > current_rate → market expects a HIKE
#   prob_of_25bp_cut = (current_rate - implied_rate) / 0.25
#
# Month codes for ZQ tickers:
#   F=Jan G=Feb H=Mar J=Apr K=May M=Jun N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec
#

YAHOO_HEADERS = {"User-Agent": "kalshi-bot/1.0"}
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_DELAY_SEC = 2.5  # sleep before Yahoo calls to avoid rate limiting

# Current Fed Funds target range — configurable via .env
# UPDATE IMMEDIATELY after every FOMC rate decision!
# Remaining 2026 FOMC dates: May 7, Jun 18, Jul 30, Sep 17, Oct 29, Dec 10
CURRENT_RATE_LOW = float(os.getenv("FED_RATE_LOW", "3.50"))
CURRENT_RATE_HIGH = float(os.getenv("FED_RATE_HIGH", "3.75"))
CURRENT_RATE_MID = (CURRENT_RATE_LOW + CURRENT_RATE_HIGH) / 2.0

MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}

# Exact FOMC meeting decision dates for 2026
# The futures contract to use is the one for the MONTH of each meeting
FOMC_DATES_2026 = [
    (1, 29),   # Jan 29
    (3, 18),   # Mar 18
    (5, 7),    # May 7
    (6, 18),   # Jun 18
    (7, 30),   # Jul 30
    (9, 17),   # Sep 17
    (10, 29),  # Oct 29
    (12, 10),  # Dec 10
]


def _next_fomc_meeting(now: datetime) -> tuple[int, int, int]:
    """Find the next FOMC meeting date. Returns (year, month, day)."""
    current_year = now.year
    current_month = now.month
    current_day = now.day

    for month, day in FOMC_DATES_2026:
        if month > current_month or (month == current_month and day >= current_day):
            return current_year, month, day

    # All meetings past — wrap to next year's first meeting
    return current_year + 1, 1, 29


def get_futures_price(ticker: str) -> float | None:
    """Fetch the latest price for a Fed Funds Futures contract from Yahoo Finance."""
    # Rate limit protection — Yahoo throttles aggressive automated calls
    _time.sleep(YAHOO_DELAY_SEC)

    url = f"{YAHOO_CHART_URL}/{ticker}?interval=1d&range=5d"
    try:
        resp = requests.get(url, headers=YAHOO_HEADERS, timeout=10)
        if resp.status_code != 200:
            log.debug("Yahoo Finance returned %d for %s", resp.status_code, ticker)
            return None

        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        price = result[0].get("meta", {}).get("regularMarketPrice")
        if price is None:
            return None

        return float(price)

    except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError) as e:
        log.debug("Yahoo Finance error for %s: %s", ticker, e)
        return None


def compute_fed_probabilities() -> dict | None:
    """
    Compute Fed rate change probabilities from Fed Funds Futures prices.

    Returns dict like:
    {
        "meeting_month": "May 2026",
        "futures_ticker": "ZQK26.CBT",
        "futures_price": 96.32,
        "implied_rate": 3.68,
        "current_rate": "3.50-3.75",
        "probabilities": {
            "hold": 0.28,
            "cut_25": 0.72,
        }
    }
    """
    now = datetime.now(timezone.utc)

    # Auto-select the right contract for the next FOMC meeting
    target_year, target_month, target_day = _next_fomc_meeting(now)
    code = MONTH_CODES.get(target_month, "F")
    yr = f"{target_year % 100:02d}"
    ticker = f"ZQ{code}{yr}.CBT"

    log.info("Next FOMC meeting: %d-%02d-%02d → contract %s",
             target_year, target_month, target_day, ticker)

    price = get_futures_price(ticker)

    if price is None:
        # Try front month as fallback
        log.info("Falling back to front-month ZQ=F")
        price = get_futures_price("ZQ=F")
        if price is None:
            log.warning("Could not fetch any Fed Funds Futures price")
            return None
        ticker = "ZQ=F"

    implied_rate = 100.0 - price
    rate_diff = implied_rate - CURRENT_RATE_MID  # positive = hike, negative = cut

    # Compute probabilities
    probabilities: dict[str, float] = {}

    if abs(rate_diff) < 0.025:
        # Very close to current rate — high probability of hold
        probabilities["hold"] = 1.0
    elif rate_diff < 0:
        # Market expects a cut
        cuts_25bp = abs(rate_diff) / 0.25
        if cuts_25bp <= 1.0:
            probabilities["cut_25"] = cuts_25bp
            probabilities["hold"] = 1.0 - cuts_25bp
        elif cuts_25bp <= 2.0:
            probabilities["cut_50"] = cuts_25bp - 1.0
            probabilities["cut_25"] = 1.0 - (cuts_25bp - 1.0)
        else:
            probabilities["cut_50"] = min(cuts_25bp - 1.0, 1.0)
            probabilities["cut_75"] = max(cuts_25bp - 2.0, 0.0)
    else:
        # Market expects a hike
        hikes_25bp = rate_diff / 0.25
        if hikes_25bp <= 1.0:
            probabilities["hike_25"] = hikes_25bp
            probabilities["hold"] = 1.0 - hikes_25bp
        else:
            probabilities["hike_25"] = 1.0
            probabilities["hike_50"] = min(hikes_25bp - 1.0, 1.0)

    month_name = datetime(target_year, target_month, 1).strftime("%B %Y")

    log.info("Fed Funds Futures: %s = %.2f → implied rate %.3f%% (current %.2f-%.2f%%)",
             ticker, price, implied_rate, CURRENT_RATE_LOW, CURRENT_RATE_HIGH)
    log.info("Probabilities: %s", {k: f"{v:.0%}" for k, v in probabilities.items()})

    return {
        "meeting_month": month_name,
        "futures_ticker": ticker,
        "futures_price": price,
        "implied_rate": round(float(implied_rate), 4),
        "current_rate": f"{CURRENT_RATE_LOW:.2f}-{CURRENT_RATE_HIGH:.2f}",
        "probabilities": probabilities,
    }


# ─── Kalshi Fed Markets ─────────────────────────────────────────────────────

FED_KEYWORDS = ["FED", "FOMC", "RATE", "BASIS POINTS", "INTEREST RATE",
                "FEDERAL RESERVE", "RATE CUT", "RATE HIKE"]


def get_fed_markets() -> list[dict]:
    """Fetch Kalshi markets related to Fed rate decisions."""
    markets: list[dict] = []
    cursor = None

    for _ in range(10):
        params = "?status=open&limit=200"
        if cursor:
            params += f"&cursor={cursor}"

        data = kalshi_request("GET", f"/markets{params}")
        if not data:
            break

        for m in data.get("markets", []):
            ticker: str = m.get("ticker", "").upper()
            title: str = m.get("title", "").upper()
            if any(kw in ticker or kw in title for kw in FED_KEYWORDS):
                markets.append(m)

        cursor = data.get("cursor")
        if not cursor:
            break

    log.info("Found %d Fed-related markets", len(markets))
    return markets


def match_market_to_outcome(title: str) -> str | None:
    """
    Match a Kalshi market title to a futures-derived outcome.

    Returns one of: 'hold', 'cut_25', 'cut_50', 'hike_25' or None.
    """
    title_lower = title.lower()

    if any(w in title_lower for w in ["no change", "hold", "unchanged", "maintain"]):
        return "hold"
    elif "50" in title_lower and any(w in title_lower for w in ["cut", "decrease", "lower"]):
        return "cut_50"
    elif "25" in title_lower and any(w in title_lower for w in ["cut", "decrease", "lower"]):
        return "cut_25"
    elif "25" in title_lower and any(w in title_lower for w in ["hike", "increase", "raise"]):
        return "hike_25"
    elif any(w in title_lower for w in ["cut", "decrease", "lower"]):
        return "cut_25"  # default cut to 25bp
    elif any(w in title_lower for w in ["hike", "increase", "raise"]):
        return "hike_25"  # default hike to 25bp

    return None


# ─── Main Fed Scanner ───────────────────────────────────────────────────────

def run_fed_scan():
    """Run one FedWatch scan cycle using Fed Funds Futures."""
    log.info("═══ Starting FedWatch scan (futures-based) ═══")

    # 1. Compute probabilities from futures
    fed_data = compute_fed_probabilities()
    if not fed_data:
        log.warning("Could not compute Fed probabilities — skipping")
        log_trade({"event": "no_edges", "scanner": "fed",
                    "reason": "no_futures_data"})
        return

    # 2. Get bankroll
    bankroll = get_balance()
    if bankroll <= 0:
        log.warning("Bankroll is $%.2f — skipping fed scan", bankroll)
        return

    # 3. Get Fed markets from Kalshi
    markets = get_fed_markets()
    if not markets:
        log.info("No Fed markets found on Kalshi")
        log_trade({"event": "no_edges", "scanner": "fed",
                    "reason": "no_fed_markets"})
        return

    edges_found = 0

    # 4. Match and detect edges
    for market in markets:
        ticker: str = market.get("ticker", "UNKNOWN")
        title: str = market.get("title", "")
        yes_ask: float = float(market.get("yes_ask", 0)) / 100.0
        no_ask: float = float(market.get("no_ask", 0)) / 100.0

        outcome = match_market_to_outcome(title)
        if not outcome:
            log.debug("Could not match Fed market: %s", title)
            continue

        futures_prob = fed_data["probabilities"].get(outcome)
        if futures_prob is None:
            log.debug("No futures probability for outcome '%s'", outcome)
            continue

        # Compute edge
        edge_yes: float = float(futures_prob) - yes_ask
        edge_no: float = (1.0 - float(futures_prob)) - no_ask

        if edge_yes >= edge_no and edge_yes >= MIN_EDGE:
            side, edge, ask = "yes", edge_yes, yes_ask
        elif edge_no > edge_yes and edge_no >= MIN_EDGE:
            side, edge, ask = "no", edge_no, no_ask
        else:
            log.debug("No edge on %s (yes=%.3f, no=%.3f)", ticker, edge_yes, edge_no)
            continue

        # Size and EV
        odds: float = (1.0 / ask) - 1.0 if ask > 0 else 0.0
        position: float = quarter_kelly_size(edge, odds, bankroll)
        payout: float = 1.0 - ask
        ev: float = round(float(position) * (float(futures_prob) * payout - (1 - float(futures_prob)) * ask), 2)

        if position < 1.0:
            continue

        edges_found += 1

        log.info("FED EDGE: %s | Futures=%.0f%% Ask=%.0f%% Edge=%.0f%% EV=$%.2f",
                 ticker, futures_prob * 100, ask * 100, edge * 100, ev)

        # Build trade log entry
        trade_entry: dict = {
            "event": "edge_found",
            "scanner": "fed",
            "ticker": ticker,
            "title": title,
            "futures_prob": round(float(futures_prob), 4),
            "kalshi_ask": round(float(ask), 4),
            "edge": round(float(edge), 4),
            "position_size": round(float(position), 2),
            "ev": ev,
            "side": side,
            "bankroll_before": round(float(bankroll), 2),
            "order_id": None,
            "meeting_month": fed_data["meeting_month"],
            "futures_ticker": fed_data["futures_ticker"],
            "implied_rate": fed_data["implied_rate"],
        }

        alert_msg = (
            f"🏦 *FED EDGE DETECTED*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"*{title}*\n"
            f"Ticker: `{ticker}`\n\n"
            f"📊 Futures Prob: *{futures_prob:.0%}*\n"
            f"💰 Kalshi Ask: *{ask:.0%}*\n"
            f"📈 Edge: *{edge:.0%}*\n"
            f"🎯 Side: *{side.upper()}*\n"
            f"💵 Position: *${position:.2f}*\n"
            f"📐 EV: *${ev:.2f}*\n"
            f"📅 Meeting: *{fed_data['meeting_month']}*\n"
            f"📉 Implied Rate: *{fed_data['implied_rate']:.3f}%*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Mode: {'🔔 ALERT ONLY' if ALERT_ONLY else '⚡ AUTO-EXECUTE'}"
        )

        if ALERT_ONLY:
            send_telegram(alert_msg)
            log_trade(trade_entry)
        else:
            count = max(1, int(position / ask))
            price_cents = int(ask * 100)
            order = place_order(ticker, side, count, price_cents)
            if order:
                trade_entry["event"] = "executed"
                trade_entry["order_id"] = order.get("order_id")
                send_telegram(alert_msg + f"\n\n✅ *ORDER PLACED*\nID: `{order.get('order_id')}`")
            else:
                trade_entry["event"] = "failed"
                send_telegram(alert_msg + "\n\n❌ *ORDER FAILED*")
            log_trade(trade_entry)

    if edges_found == 0:
        log.info("No Fed edges found")
        log_trade({"event": "no_edges", "scanner": "fed",
                    "markets_scanned": len(markets),
                    "implied_rate": fed_data["implied_rate"],
                    "futures_ticker": fed_data["futures_ticker"]})

    log.info("═══ FedWatch scan complete — %d edges found ═══", edges_found)


if __name__ == "__main__":
    run_fed_scan()
