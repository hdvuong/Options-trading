"""
Options Monitor
===============
Scans a watchlist every 30 minutes during market hours,
scores each ticker using the Tastytrade API, and fires
Telegram alerts when a setup scores above your threshold.

Usage (local):
    python monitor.py                    # scan all tickers in watchlist.json
    python monitor.py --ticker NVDA      # scan one ticker
    python monitor.py --dry-run          # score without sending alerts
    python monitor.py --force            # ignore cooldown, re-alert everything

In GitHub Actions this runs automatically — see .github/workflows/monitor.yml

Environment variables (set in .env or GitHub Secrets):
    TASTYTRADE_CLIENT_SECRET
    TASTYTRADE_REFRESH_TOKEN
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import asyncio
import json
import logging
import math
import os
import sys
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Dependencies ──────────────────────────────────────────────────────────────
try:
    import pandas as pd
    import numpy as np
    import requests
    from scipy.stats import norm
    from dotenv import load_dotenv
except ImportError:
    print("Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    from tastytrade import Session, DXLinkStreamer
    from tastytrade.instruments import Equity, NestedOptionChain
    from tastytrade.metrics import get_market_metrics
    from tastytrade.dxfeed import Quote, Greeks, Trade, Profile, Summary, Candle
    from tastytrade.utils import is_market_open_now
except ImportError:
    print("Run: pip install tastytrade")
    sys.exit(1)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("monitor.log", mode="a"),
    ],
)
log = logging.getLogger("monitor")


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

ROOT       = Path(__file__).parent
WATCHLIST  = ROOT / "watchlist.json"
STATE_FILE = ROOT / "alert_state.json"   # tracks last alert time per ticker+strategy


def load_config() -> dict:
    with open(WATCHLIST) as f:
        return json.load(f)


# ─────────────────────────────────────────────
# MARKET STATUS
# ─────────────────────────────────────────────

def market_status() -> tuple[bool, str]:
    """
    Returns (is_open, label) using Tastytrade's own calendar.
    When closed, we fall back to last-close prices so the scan
    still runs and produces useful weekend research scores.
    """
    try:
        open_now = is_market_open_now()
    except Exception:
        # If the SDK call fails, assume closed and continue anyway
        open_now = False

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    label   = "OPEN" if open_now else "CLOSED (using last-close prices)"
    return open_now, f"{now_str} — market {label}"


# ─────────────────────────────────────────────
# ALERT STATE (cooldown tracking)
# ─────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def state_key(ticker: str, strategy: str) -> str:
    return f"{ticker}::{strategy}"


def should_alert(state: dict, ticker: str, strategy: str, score: int,
                 cooldown_minutes: int, rescore_threshold: int) -> tuple[bool, str]:
    """
    Returns (should_alert, reason).
    Fires if: never alerted before, cooldown expired, or score changed significantly.
    """
    key = state_key(ticker, strategy)
    entry = state.get(key)

    if not entry:
        return True, "first alert"

    # Check if score changed significantly
    last_score = entry.get("score", 0)
    if abs(score - last_score) >= rescore_threshold:
        return True, f"score changed {last_score}→{score}"

    # Check cooldown
    last_alert = datetime.fromisoformat(entry["last_alert"])
    elapsed_min = (datetime.now(timezone.utc) - last_alert).total_seconds() / 60
    if elapsed_min >= cooldown_minutes:
        return True, f"cooldown expired ({elapsed_min:.0f}m ago)"

    return False, f"in cooldown ({elapsed_min:.0f}/{cooldown_minutes}m)"


def record_alert(state: dict, ticker: str, strategy: str, score: int):
    key = state_key(ticker, strategy)
    state[key] = {
        "last_alert": datetime.now(timezone.utc).isoformat(),
        "score": score,
    }


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def send_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    """Send a Markdown-formatted message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       message,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info(f"  Telegram sent OK")
            return True
        else:
            log.warning(f"  Telegram error {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        log.warning(f"  Telegram exception: {e}")
        return False


def format_alert(ticker: str, setup, reason: str) -> str:
    """Format a trade alert as a Telegram Markdown message."""
    now = datetime.now().strftime("%b %d %H:%M ET")
    score_emoji = "✅" if setup.score >= 65 else "⚠️"
    strat_emoji = {
        "Long Call":        "📈",
        "Bull Call Spread": "📊",
        "Cash-Secured Put": "💰",
        "Iron Condor":      "🦅",
    }.get(setup.name, "📌")

    cost_label = "Debit"  if setup.strategy_type == "buy_debit" else "Credit"
    mg_str     = f"${setup.max_gain:.2f}" if setup.max_gain != float("inf") else "unlimited"

    lines = [
        f"{score_emoji} *{ticker} — {setup.name}* {strat_emoji}",
        f"Score: *{setup.score}/100* · {reason}",
        f"",
        f"*Setup:* `{' / '.join(setup.legs)}`",
        f"*Expiry:* {setup.expiry} ({setup.dte} DTE)",
        f"",
        f"*{cost_label}:* ${setup.premium:.2f}",
        f"*Max Gain:* {mg_str}  |  *Max Loss:* ${setup.max_loss:.2f}",
        f"*Breakeven:* ${setup.breakeven:.2f}",
        f"*Delta:* {setup.delta_net:+.2f}  |  *IV:* {setup.iv_used * 100:.1f}%",
        f"",
        f"*Exit rules:*",
    ]
    for k, v in list(setup.exit_rules.items())[:3]:
        lines.append(f"  • {v}")
    lines.append(f"")
    lines.append(f"_{now}_")
    return "\n".join(lines)


def format_summary(results: list, scan_time: str, market_open: bool = True) -> str:
    """Format a scan summary message (sent every scan)."""
    status_icon = "🟢" if market_open else "🔴"
    status_note = "" if market_open else " _(last-close prices)_"
    lines = [f"📋 *Options Scan — {scan_time}* {status_icon}{status_note}", ""]
    if not results:
        lines.append("No tickers scanned.")
        return "\n".join(lines)

    for ticker, setups in results:
        if not setups:
            lines.append(f"• {ticker}: no data")
            continue
        top = max(setups, key=lambda s: s.score)
        bar = "█" * int(top.score / 10) + "░" * (10 - int(top.score / 10))
        lines.append(f"• *{ticker}* `{bar}` {top.score}/100 — {top.name}")

    lines.append("")
    lines.append(f"_Threshold: {CONFIG.get('alert', {}).get('score_threshold', 65)}_")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# SCORER (self-contained copy of the core logic)
# ─────────────────────────────────────────────
# Keeps monitor.py standalone — no import dependency on scorer.py

from dataclasses import dataclass


@dataclass
class TradeSetup:
    name: str
    strategy_type: str
    expiry: str
    dte: int
    legs: list
    premium: float
    max_gain: float
    max_loss: float
    breakeven: float
    delta_net: float
    p50: float
    iv_used: float
    open_interest: int
    score: int
    score_breakdown: dict
    verdict: str
    exit_rules: dict


def days_to_expiry(exp_str: str) -> int:
    return max(0, (datetime.strptime(exp_str, "%Y-%m-%d") - datetime.now()).days)


def find_best_expiry(expirations: list, target_dte: int = 35) -> Optional[str]:
    best, best_diff = None, float("inf")
    for exp in expirations:
        dte = days_to_expiry(exp)
        if dte < 7: continue
        diff = abs(dte - target_dte)
        if diff < best_diff:
            best_diff, best = diff, exp
    return best


def bs_price(S, K, T, r, sigma, opt="call"):
    if T <= 0 or sigma <= 0:
        return max(0, S - K) if opt == "call" else max(0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "call": return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_delta(S, K, T, r, sigma, opt="call"):
    if T <= 0 or sigma <= 0:
        return 1.0 if (opt == "call" and S > K) else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return norm.cdf(d1) if opt == "call" else norm.cdf(d1) - 1


def find_strike(df, price, offset_pct, direction):
    target = price * (1 + offset_pct) if direction == "above" else price * (1 - offset_pct)
    return float(df["strike"].values[np.argmin(np.abs(df["strike"].values - target))])


def mid(df, strike):
    row = df[df["strike"] == strike]
    if row.empty: return 0.0
    bid, ask = float(row["bid"].values[0]), float(row["ask"].values[0])
    return (bid + ask) / 2 if ask > 0 else float(row["lastPrice"].values[0])


def iv_at(df, strike, fallback=0.3):
    row = df[df["strike"] == strike]
    return float(row["impliedVolatility"].values[0]) if not row.empty else fallback


def d_at(df, strike):
    row = df[df["strike"] == strike]
    return float(row["delta"].values[0]) if not row.empty else 0.0


def score_setup(iv_rank, iv_current, hv_30d, trend_30d, strategy_type, dte,
                max_gain, max_loss, p50, next_earnings_date=None,
                open_interest=0) -> tuple[int, dict]:
    """Score a trade setup 0-100 across 7 factors + earnings penalty."""

    # IV Rank (30pts)
    iv = iv_rank
    if strategy_type == "buy_debit":
        iv_pts = 28 if iv < 20 else 20 if iv < 35 else 12 if iv < 50 else 4
        iv_note = f"IV Rank {iv:.0f} — {'great for buying' if iv < 20 else 'ok' if iv < 35 else 'elevated' if iv < 50 else 'avoid buying'}"
    else:
        iv_pts = 28 if iv >= 50 else 20 if iv >= 35 else 10 if iv >= 20 else 3
        iv_note = f"IV Rank {iv:.0f} — {'great for selling' if iv >= 50 else 'decent' if iv >= 35 else 'thin credit' if iv >= 20 else 'avoid selling'}"

    # Risk/Reward (20pts)
    if max_loss <= 0:
        rr_pts, rr_note = 10, "Undefined risk"
    else:
        rr = max_gain / max_loss
        rr_pts  = 20 if rr >= 2 else 14 if rr >= 1 else 8 if rr >= 0.5 else 3
        rr_note = f"R/R {rr:.1f}x"

    # P50 (15pts)
    p50_pts  = 15 if p50 >= 0.6 else 10 if p50 >= 0.45 else 5 if p50 >= 0.3 else 2
    p50_note = f"P50 ~{p50*100:.0f}%"

    # IV vs HV (15pts)
    ratio    = iv_current / max(hv_30d, 0.01)
    hv_pts   = 15 if ratio >= 1.3 else 10 if ratio >= 1.0 else 7 if ratio >= 0.8 else 12
    hv_note  = f"IV/HV {ratio:.2f}x"

    # Trend (5pts)
    if trend_30d == 0:
        tr_pts, tr_note = 3, "Trend n/a (no candle data)"
    elif strategy_type == "buy_debit":
        tr_pts  = 5 if trend_30d >= 3 else 3 if trend_30d >= -2 else 1
        tr_note = f"Trend {trend_30d:+.1f}%"
    else:
        tr_pts  = 5 if abs(trend_30d) < 3 else 2
        tr_note = f"Trend {trend_30d:+.1f}%"

    # Liquidity (5pts) — from Summary OI
    if open_interest >= 500:
        lq_pts, lq_note = 5, f"OI {open_interest:,} — good liquidity"
    elif open_interest >= 100:
        lq_pts, lq_note = 3, f"OI {open_interest:,} — moderate liquidity"
    elif open_interest > 0:
        lq_pts, lq_note = 1, f"OI {open_interest:,} — thin, watch spreads"
    else:
        lq_pts, lq_note = 2, "OI unavailable — verify spreads before entry"

    # DTE Fit (10pts)
    if strategy_type == "buy_debit":
        dt_pts  = 10 if 25 <= dte <= 60 else 6 if (15 <= dte < 25 or 60 < dte <= 90) else 2
    else:
        dt_pts  = 10 if 21 <= dte <= 45 else 6 if (14 <= dte < 21 or 45 < dte <= 60) else 2
    dt_note = f"{dte} DTE"

    total = min(100, iv_pts + rr_pts + p50_pts + hv_pts + tr_pts + lq_pts + dt_pts)

    # Earnings penalty — applied AFTER total (can push score below threshold)
    earnings_note = None
    if next_earnings_date:
        from datetime import date as date_type
        today    = datetime.now().date()
        earn_dt  = next_earnings_date if isinstance(next_earnings_date, date_type) else next_earnings_date
        days_to_earn = (earn_dt - today).days
        if 0 <= days_to_earn <= dte:
            # Earnings fall WITHIN the trade's lifespan
            if strategy_type == "sell_credit":
                # Sellers hate earnings — unexpected moves can blow through spreads
                penalty = 20
                earnings_note = f"⚠️ EARNINGS in {days_to_earn}d (exp {earn_dt}) — high risk for sellers, -{penalty}pts"
            else:
                # Buyers can benefit from IV expansion into earnings
                penalty = -5  # slight bonus
                earnings_note = f"📅 EARNINGS in {days_to_earn}d (exp {earn_dt}) — IV may expand, +5pts for buyers"
            total = max(0, min(100, total - penalty))

    breakdown = {
        "IV Rank (30pts)":     (iv_pts,  iv_note),
        "Risk/Reward (20pts)": (rr_pts,  rr_note),
        "P50 (15pts)":         (p50_pts, p50_note),
        "IV vs HV (15pts)":    (hv_pts,  hv_note),
        "Trend (5pts)":        (tr_pts,  tr_note),
        "Liquidity (5pts)":    (lq_pts,  lq_note),
        "DTE Fit (10pts)":     (dt_pts,  dt_note),
    }
    if earnings_note:
        breakdown["Earnings"] = (0, earnings_note)

    return total, breakdown


def exit_rules(setup: TradeSetup) -> dict:
    exp_dt    = datetime.strptime(setup.expiry, "%Y-%m-%d")
    dte21     = (exp_dt - timedelta(days=21)).strftime("%b %d")
    if setup.strategy_type == "buy_debit":
        return {
            "take_profit": f"Close at 2x-3x premium (~${setup.premium*2:.2f}-${setup.premium*3:.2f})",
            "stop_loss":   f"Exit if down 50% (<${setup.premium*0.5:.2f})",
            "time_stop":   f"Re-evaluate at 21 DTE ({dte21})",
        }
    return {
        "take_profit": f"Buy back at 50% credit (~${setup.premium*0.5:.2f})",
        "stop_loss":   f"Exit at 2x credit loss (${setup.premium*2:.2f})",
        "time_stop":   f"Close by 21 DTE ({dte21})",
    }


def build_setups(snap: dict) -> list[TradeSetup]:
    """Build and score all 4 strategies from a MarketSnapshot dict."""
    setups    = []
    price     = snap["price"]
    iv_rank   = snap["iv_rank"]
    iv_cur    = snap["iv_current"]
    hv        = snap["hist_volatility_30d"]
    trend     = snap["trend_30d"]
    expiry    = snap["expiry"]
    dte       = snap["dte"]
    T         = dte / 365
    calls     = snap["calls"]
    puts      = snap["puts"]
    earnings  = snap.get("next_earnings_date")

    def make(name, stype, legs, premium, max_gain, max_loss, breakeven,
             delta_net, p50, iv_used, oi=0):
        score, bd = score_setup(iv_rank, iv_cur, hv, trend, stype, dte,
                                max_gain, max_loss, p50,
                                next_earnings_date=earnings, open_interest=oi)
        verdict   = ("STRONG SETUP" if score >= 65 else "NEUTRAL" if score >= 45 else "AVOID")
        s = TradeSetup(
            name=name, strategy_type=stype, expiry=expiry, dte=dte, legs=legs,
            premium=premium, max_gain=max_gain, max_loss=max_loss, breakeven=breakeven,
            delta_net=delta_net, p50=p50, iv_used=iv_used, open_interest=oi,
            score=score, score_breakdown=bd, verdict=verdict, exit_rules={},
        )
        s.exit_rules = exit_rules(s)
        return s

    try:  # Long Call
        K  = find_strike(calls, price, 0.04, "above")
        iv = iv_at(calls, K, iv_cur)
        p  = mid(calls, K) or bs_price(price, K, T, 0.04, iv, "call")
        d  = d_at(calls, K) or bs_delta(price, K, T, 0.04, iv, "call")
        oi = int(calls[calls["strike"] == K]["openInterest"].values[0]) if not calls[calls["strike"] == K].empty else 0
        setups.append(make("Long Call", "buy_debit",
            [f"BUY ${K:.0f}C exp {expiry}"],
            p, price * 0.5, p, K + p, d, max(0.1, d * 0.85), iv, oi))
    except Exception as e:
        log.debug(f"Long Call skipped: {e}")

    try:  # Bull Call Spread
        K1, K2 = find_strike(calls, price, 0.01, "above"), find_strike(calls, price, 0.09, "above")
        iv1, iv2 = iv_at(calls, K1, iv_cur), iv_at(calls, K2, iv_cur)
        p1 = mid(calls, K1) or bs_price(price, K1, T, 0.04, iv1, "call")
        p2 = mid(calls, K2) or bs_price(price, K2, T, 0.04, iv2, "call")
        debit = max(0.01, p1 - p2)
        d1 = d_at(calls, K1) or bs_delta(price, K1, T, 0.04, iv1, "call")
        d2 = d_at(calls, K2) or bs_delta(price, K2, T, 0.04, iv2, "call")
        oi = min(
            int(calls[calls["strike"] == K1]["openInterest"].values[0]) if not calls[calls["strike"] == K1].empty else 0,
            int(calls[calls["strike"] == K2]["openInterest"].values[0]) if not calls[calls["strike"] == K2].empty else 0,
        )
        setups.append(make("Bull Call Spread", "buy_debit",
            [f"BUY ${K1:.0f}C / SELL ${K2:.0f}C exp {expiry}"],
            debit, (K2 - K1) - debit, debit, K1 + debit, d1 - d2, max(0.1, d1 * 0.8), iv1, oi))
    except Exception as e:
        log.debug(f"Bull Call Spread skipped: {e}")

    try:  # Cash-Secured Put
        K  = find_strike(puts, price, 0.06, "below")
        iv = iv_at(puts, K, iv_cur)
        p  = mid(puts, K) or bs_price(price, K, T, 0.04, iv, "put")
        d  = d_at(puts, K) or bs_delta(price, K, T, 0.04, iv, "put")
        oi = int(puts[puts["strike"] == K]["openInterest"].values[0]) if not puts[puts["strike"] == K].empty else 0
        setups.append(make("Cash-Secured Put", "sell_credit",
            [f"SELL ${K:.0f}P exp {expiry}"],
            p, p, K - p, K - p, d, max(0.1, (1 + d) * 0.85), iv, oi))
    except Exception as e:
        log.debug(f"CSP skipped: {e}")

    try:  # Iron Condor
        Kps = find_strike(puts,  price, 0.05, "below")
        Kpb = find_strike(puts,  price, 0.10, "below")
        Kcs = find_strike(calls, price, 0.05, "above")
        Kcb = find_strike(calls, price, 0.10, "above")
        credit = ((mid(puts, Kps) - mid(puts, Kpb)) + (mid(calls, Kcs) - mid(calls, Kcb)))
        ml = max(Kps - Kpb, Kcb - Kcs) - credit
        iv_ps = iv_at(puts, Kps, iv_cur)
        dcs = d_at(calls, Kcs) or bs_delta(price, Kcs, T, 0.04, iv_at(calls, Kcs, iv_cur), "call")
        dps = d_at(puts, Kps) or bs_delta(price, Kps, T, 0.04, iv_ps, "put")
        p50 = max(0.1, (1 - abs(dcs)) * (1 - abs(dps)) * 0.85)
        oi = min(
            int(puts[puts["strike"]  == Kps]["openInterest"].values[0]) if not puts[puts["strike"]  == Kps].empty else 0,
            int(calls[calls["strike"] == Kcs]["openInterest"].values[0]) if not calls[calls["strike"] == Kcs].empty else 0,
        )
        s = make("Iron Condor", "sell_credit",
            [f"SELL ${Kps:.0f}P/BUY ${Kpb:.0f}P  SELL ${Kcs:.0f}C/BUY ${Kcb:.0f}C exp {expiry}"],
            credit, credit, max(ml, 0.01), Kps - credit, dcs + dps, p50, iv_ps, oi)
        s.exit_rules["profit_zone"] = f"${Kps - credit:.2f} – ${Kcs + credit:.2f}"
        setups.append(s)
    except Exception as e:
        log.debug(f"Iron Condor skipped: {e}")

    return setups


# ─────────────────────────────────────────────
# DATA FETCHER
# ─────────────────────────────────────────────

async def fetch_ticker(ticker: str, session: Session, cfg: dict) -> Optional[dict]:
    """
    Fetches live snapshot for a ticker and returns a dict with price,
    IV rank, chain DataFrames, etc. Returns None on failure.
    """
    scan_cfg     = cfg.get("scan", {})
    max_exps     = scan_cfg.get("max_expirations", 8)
    timeout      = scan_cfg.get("stream_timeout_seconds", 40)   # raised default
    target_dte   = scan_cfg.get("target_dte", 35)

    try:
        # Market metrics
        mlist = await get_market_metrics(session, [ticker])
        if not mlist:
            log.warning(f"  {ticker}: no market metrics")
            return None
        m = mlist[0]
        iv_rank    = float(m.tw_implied_volatility_index_rank or 50)
        iv_current = float(m.implied_volatility_30_day        or 0.30)
        hv_30d     = float(m.historical_volatility_30_day     or 0.25)

        # Earnings — extract next expected report date
        next_earnings_date = None
        if m.earnings and m.earnings.expected_report_date:
            next_earnings_date = m.earnings.expected_report_date  # a date object

        # Option chain structure
chain_list = await NestedOptionChain.get(session, ticker)

if not chain_list:
    log.warning(f"  {ticker}: no option chain returned")
    return None

chain = chain_list[0]

if not chain.expirations:
    log.warning(f"  {ticker}: no expirations")
    return None

        expirations, exp_map = [], {}
        for exp in chain.expirations:
            exp_str = exp.expiration_date.strftime("%Y-%m-%d")
            expirations.append(exp_str)
            calls_m, puts_m = {}, {}
            for s in exp.strikes:
                if s.call: calls_m[s.call] = float(s.strike_price)
                if s.put:  puts_m[s.put]   = float(s.strike_price)
            exp_map[exp_str] = {"calls": calls_m, "puts": puts_m}

        # Best expiry
        best_exp = find_best_expiry(expirations[:max_exps], target_dte)
        if not best_exp:
            log.warning(f"  {ticker}: no suitable expiry")
            return None

        # Streamer symbols — only stream the ONE expiry we need for scoring
        # This reduces symbol count from ~800 to ~100, critical for weekend reliability
        equity     = await Equity.get(session, ticker)
        equity_sym = equity.streamer_symbol
        option_syms = []
        em_best = exp_map.get(best_exp, {})
        option_syms.extend(em_best.get("calls", {}).keys())
        option_syms.extend(em_best.get("puts",  {}).keys())
        log.info(f"  {ticker}: streaming {len(option_syms)} contracts for {best_exp}")

        # Stream live data
        price = week52_high = week52_low = 0.0
        prev_close = 0.0
        trend_30d  = 0.0
        quotes, greeks_map, summary_map = {}, {}, {}
        candle_closes: list[float] = []

        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Trade,   [equity_sym])
            await streamer.subscribe(Profile, [equity_sym])
            await streamer.subscribe(Summary, [equity_sym])
            if option_syms:
                await streamer.subscribe(Quote,   option_syms)
                await streamer.subscribe(Greeks,  option_syms)
                await streamer.subscribe(Summary, option_syms)

            # Daily candles for trend — last 32 days (covers 30 trading days)
            candle_start = datetime.now(timezone.utc) - timedelta(days=32)
            await streamer.subscribe_candle([equity_sym], interval="1d",
                                            start_time=candle_start)

            deadline        = time.time() + timeout
            got_price       = got_profile = False
            pending_g       = set(option_syms)
            pending_q       = set(option_syms)
            pending_s       = set(option_syms)

            while time.time() < deadline:
                if not got_price:
                    try:
                        ev = streamer.get_event_nowait(Trade)
                        if ev.event_symbol == equity_sym and ev.price:
                            price     = float(ev.price)
                            got_price = price > 0
                    except Exception: pass

                # Also drain Summary for equity (prev close fallback + equity OI)
                try:
                    ev = streamer.get_event_nowait(Summary)
                    if ev.event_symbol == equity_sym:
                        if not prev_close and ev.prev_day_close_price:
                            prev_close = float(ev.prev_day_close_price)
                    else:
                        summary_map[ev.event_symbol] = ev
                        pending_s.discard(ev.event_symbol)
                except Exception: pass

                if not got_profile:
                    try:
                        ev = streamer.get_event_nowait(Profile)
                        if ev.event_symbol == equity_sym:
                            week52_high = float(ev.high_52_week_price or 0)
                            week52_low  = float(ev.low_52_week_price  or 0)
                            got_profile = week52_high > 0
                    except Exception: pass

                while True:
                    try:
                        ev = streamer.get_event_nowait(Greeks)
                        greeks_map[ev.event_symbol] = ev
                        pending_g.discard(ev.event_symbol)
                    except Exception: break

                while True:
                    try:
                        ev = streamer.get_event_nowait(Quote)
                        quotes[ev.event_symbol] = ev
                        pending_q.discard(ev.event_symbol)
                    except Exception: break

                # Candle events — collect daily closes for trend
                while True:
                    try:
                        ev = streamer.get_event_nowait(Candle)
                        if ev.event_symbol.startswith(equity_sym) and ev.close:
                            candle_closes.append(float(ev.close))
                    except Exception: break

                if got_price and got_profile and not pending_g and not pending_q:
                    break
                await asyncio.sleep(0.15)

        # Price fallback: use prev_day_close when market is closed (weekends/holidays)
        market_open, market_label = market_status()
        if price <= 0:
            if prev_close > 0:
                price = prev_close
                log.info(f"  {ticker}: market closed — using last close ${price:.2f}")
            else:
                log.warning(f"  {ticker}: no price available")
                return None

        # 30-day trend from candles
        if len(candle_closes) >= 2:
            # candles arrive oldest→newest; take first and last valid closes
            close_old  = candle_closes[0]
            close_new  = candle_closes[-1]
            if close_old > 0:
                trend_30d = (close_new - close_old) / close_old * 100
        log.debug(f"  {ticker}: trend_30d={trend_30d:+.1f}% ({len(candle_closes)} candles)")

        if week52_high <= 0: week52_high = price * 1.3
        if week52_low  <= 0: week52_low  = price * 0.7

        # Build chain DataFrames for best_exp only
        em = exp_map.get(best_exp, {})
        call_rows, put_rows = [], []

        for sym, strike in em.get("calls", {}).items():
            q  = quotes.get(sym)
            g  = greeks_map.get(sym)
            sm = summary_map.get(sym)
            bid = float(q.bid_price or 0) if q else 0.0
            ask = float(q.ask_price or 0) if q else 0.0

            # Weekend/after-hours fallback: use last close price from Summary
            last_price = (bid + ask) / 2 if ask > 0 else bid
            if last_price <= 0 and sm:
                last_price = float(sm.day_close_price or sm.prev_day_close_price or 0)
            if last_price <= 0:
                continue  # truly no data for this contract

            call_rows.append({
                "strike":            strike,
                "bid":               bid if bid > 0 else last_price * 0.98,
                "ask":               ask if ask > 0 else last_price * 1.02,
                "lastPrice":         last_price,
                "impliedVolatility": max(0.01, float(g.volatility or iv_current) if g else iv_current),
                "delta":             float(g.delta or 0.5) if g else 0.5,
                "openInterest":      int(sm.open_interest or 0) if sm else 0,
            })

        for sym, strike in em.get("puts", {}).items():
            q  = quotes.get(sym)
            g  = greeks_map.get(sym)
            sm = summary_map.get(sym)
            bid = float(q.bid_price or 0) if q else 0.0
            ask = float(q.ask_price or 0) if q else 0.0

            last_price = (bid + ask) / 2 if ask > 0 else bid
            if last_price <= 0 and sm:
                last_price = float(sm.day_close_price or sm.prev_day_close_price or 0)
            if last_price <= 0:
                continue

            put_rows.append({
                "strike":            strike,
                "bid":               bid if bid > 0 else last_price * 0.98,
                "ask":               ask if ask > 0 else last_price * 1.02,
                "lastPrice":         last_price,
                "impliedVolatility": max(0.01, float(g.volatility or iv_current) if g else iv_current),
                "delta":             float(g.delta or -0.5) if g else -0.5,
                "openInterest":      int(sm.open_interest or 0) if sm else 0,
            })

        if not call_rows or not put_rows:
            log.warning(f"  {ticker}: insufficient options data for {best_exp}")
            return None

        return {
            "ticker":              ticker,
            "price":               price,
            "week52_high":         week52_high,
            "week52_low":          week52_low,
            "iv_rank":             iv_rank,
            "iv_current":          iv_current,
            "hist_volatility_30d": hv_30d,
            "trend_30d":           trend_30d,
            "next_earnings_date":  next_earnings_date,
            "expiry":              best_exp,
            "dte":                 days_to_expiry(best_exp),
            "calls":               pd.DataFrame(call_rows).sort_values("strike").reset_index(drop=True),
            "puts":                pd.DataFrame(put_rows).sort_values("strike").reset_index(drop=True),
            "expirations":         expirations,
            "market_open":         market_open,
        }

    except Exception as e:
        log.error(f"  {ticker}: fetch failed — {e}")
        return None


# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────

async def run_scan(tickers: list[str], session: Session, cfg: dict,
                   dry_run: bool = False, force: bool = False):
    alert_cfg  = cfg.get("alert", {})
    threshold  = alert_cfg.get("score_threshold", 65)
    cooldown   = alert_cfg.get("cooldown_minutes", 120)
    rescore    = alert_cfg.get("rescore_if_change", 10)

    bot_token  = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id    = os.getenv("TELEGRAM_CHAT_ID",   "")
    has_tg     = bool(bot_token and chat_id)

    state = load_state()
    scan_time = datetime.now().strftime("%b %d %H:%M ET")
    results   = []
    alerts_sent = 0
    scan_market_open = True  # updated from first successful fetch

    log.info(f"Scanning {len(tickers)} tickers | threshold={threshold} | dry_run={dry_run}")

    for ticker in tickers:
        log.info(f"  → {ticker}")
        snap = await fetch_ticker(ticker, session, cfg)
        if snap is None:
            results.append((ticker, []))
            continue

        setups = build_setups(snap)
        results.append((ticker, setups))
        scan_market_open = snap.get("market_open", True)

        log.info(f"    {ticker} @ ${snap['price']:.2f} | IV Rank {snap['iv_rank']:.0f} | "
                 f"exp {snap['expiry']} ({snap['dte']} DTE)")

        for setup in setups:
            log.info(f"      {setup.name:<22} score={setup.score:>3}/100  {setup.verdict}")

            if setup.score < threshold:
                continue

            fire, reason = should_alert(state, ticker, setup.name, setup.score, cooldown, rescore)
            if force:
                fire, reason = True, "forced"

            if not fire:
                log.info(f"      ↳ skipped ({reason})")
                continue

            log.info(f"      ↳ ALERTING — {reason}")

            if not dry_run and has_tg:
                msg = format_alert(ticker, setup, reason)
                sent = send_telegram(msg, bot_token, chat_id)
                if sent:
                    record_alert(state, ticker, setup.name, setup.score)
                    alerts_sent += 1
            elif dry_run:
                log.info(f"      [DRY RUN] Would send alert: {ticker} {setup.name} {setup.score}/100")
                record_alert(state, ticker, setup.name, setup.score)
                alerts_sent += 1
            else:
                log.warning("      Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

        # Brief pause between tickers to avoid rate limits
        await asyncio.sleep(2)

    # Save updated state
    if not dry_run:
        save_state(state)

    # Send scan summary to Telegram
    if has_tg and not dry_run:
        summary = format_summary(results, scan_time, market_open=scan_market_open)
        send_telegram(summary, bot_token, chat_id)

    log.info(f"Scan complete — {alerts_sent} alerts sent")
    return results


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Options Monitor — Tastytrade + Telegram")
    parser.add_argument("--ticker",   help="Scan a single ticker (overrides watchlist)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Score and log without sending Telegram alerts")
    parser.add_argument("--force",    action="store_true",
                        help="Ignore cooldown — re-alert everything above threshold")
    parser.add_argument("--sandbox",  action="store_true",
                        help="Use Tastytrade sandbox environment")
    args = parser.parse_args()

    load_dotenv()
    cfg = load_config()

    # Log market status — but always continue (weekends use last-close prices)
    market_open, market_label = market_status()
    log.info(market_label)

    # Auth
    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token  = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not client_secret or not refresh_token:
        log.error("Missing TASTYTRADE_CLIENT_SECRET or TASTYTRADE_REFRESH_TOKEN")
        sys.exit(1)

    try:
        session = Session(client_secret, refresh_token, is_test=args.sandbox)
        log.info("Authenticated with Tastytrade")
    except Exception as e:
        log.error(f"Auth failed: {e}")
        sys.exit(1)

    tickers = [args.ticker.upper()] if args.ticker else cfg.get("tickers", [])
    if not tickers:
        log.error("No tickers configured. Add tickers to watchlist.json or use --ticker")
        sys.exit(1)

    asyncio.run(run_scan(tickers, session, cfg, dry_run=args.dry_run, force=args.force))


CONFIG = load_config() if WATCHLIST.exists() else {}

if __name__ == "__main__":
    main()
