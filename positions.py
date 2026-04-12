"""
Positions Monitor
=================
Pulls your open Tastytrade option positions, evaluates each one
against its exit rules, and sends Telegram alerts when a trigger
is hit (take profit, stop loss, 21 DTE time stop).

Usage:
    python positions.py              # check all open positions
    python positions.py --dry-run    # check without sending alerts
    python positions.py --sandbox    # use sandbox environment

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
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── Dependencies ──────────────────────────────────────────────────────────────
try:
    import requests
    from dotenv import load_dotenv
except ImportError:
    print("Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    from tastytrade import Session, DXLinkStreamer
    from tastytrade.account import Account, CurrentPosition
    from tastytrade.dxfeed import Quote, Greeks, Summary
    from tastytrade.order import InstrumentType
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
        logging.FileHandler("positions.log", mode="a"),
    ],
)
log = logging.getLogger("positions")

ROOT            = Path(__file__).parent
POS_STATE_FILE  = ROOT / "position_state.json"
STREAM_TIMEOUT  = 20   # seconds


# ─────────────────────────────────────────────
# STATE (track which alerts have fired)
# ─────────────────────────────────────────────

def load_state() -> dict:
    if POS_STATE_FILE.exists():
        try:
            with open(POS_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    with open(POS_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─────────────────────────────────────────────
# POSITION DATA MODEL
# ─────────────────────────────────────────────

@dataclass
class PositionSnapshot:
    symbol:           str    # OCC option symbol e.g. NVDA  260515C00195000
    underlying:       str    # NVDA
    option_type:      str    # "call" | "put"
    strike:           float
    expiry:           str    # YYYY-MM-DD
    dte:              int
    quantity:         int    # positive = long, negative = short
    avg_open_price:   float  # per share (divide by 100 for per-contract cost)
    mark_price:       float  # current mid price per share
    cost_per_contract: float # avg_open_price * 100
    value_per_contract: float  # mark_price * 100
    pnl_dollars:      float  # unrealized P&L
    pnl_pct:          float  # as decimal e.g. 0.50 = 50%
    is_long:          bool
    # Live greeks
    delta:  float
    theta:  float
    iv:     float
    # Exit rule thresholds (computed at open price)
    tp_price:   float   # take-profit mark price trigger
    sl_price:   float   # stop-loss mark price trigger
    dte_warn:   int     # DTE at which to send time warning (21)


# ─────────────────────────────────────────────
# PARSE OPTION SYMBOL
# ─────────────────────────────────────────────

def parse_occ_symbol(symbol: str) -> Optional[dict]:
    """
    Parse OCC option symbol: NVDA  260515C00195000
    Returns {underlying, expiry, option_type, strike} or None.
    
    OCC format: <underlying padded to 6><YYMMDD><C/P><strike * 1000 padded to 8>
    Tastytrade may include spaces — strip them.
    """
    try:
        s = symbol.replace(" ", "")
        # Find where date starts: look for 6-char block then 6-digit date
        # Underlying is variable length — find the numeric date portion
        import re
        m = re.match(r"^([A-Z/\.]+)(\d{6})([CP])(\d{8})$", s)
        if not m:
            return None
        underlying  = m.group(1)
        date_str    = m.group(2)   # YYMMDD
        opt_type    = "call" if m.group(3) == "C" else "put"
        strike_raw  = int(m.group(4))
        strike      = strike_raw / 1000.0
        expiry_dt   = datetime.strptime("20" + date_str, "%Y%m%d")
        expiry      = expiry_dt.strftime("%Y-%m-%d")
        return {
            "underlying": underlying,
            "expiry":     expiry,
            "option_type": opt_type,
            "strike":     strike,
        }
    except Exception:
        return None


# ─────────────────────────────────────────────
# EXIT RULE THRESHOLDS
# ─────────────────────────────────────────────

def compute_thresholds(avg_open: float, is_long: bool) -> tuple[float, float]:
    """
    Returns (take_profit_mark_price, stop_loss_mark_price).

    Long options:
        Take profit at 2x avg open price (100% gain)
        Stop loss  at 0.5x avg open price (50% loss)

    Short options (avg_open is credit received):
        Take profit when mark drops to 0.5x (50% of credit captured)
        Stop loss  when mark rises to 2x (2x credit = max pain exit)
    """
    if is_long:
        return avg_open * 2.0, avg_open * 0.5
    else:
        return avg_open * 0.5, avg_open * 2.0


# ─────────────────────────────────────────────
# EXIT TRIGGER DETECTION
# ─────────────────────────────────────────────

@dataclass
class ExitSignal:
    signal_type: str   # "take_profit" | "stop_loss" | "time_stop" | "expiry_warn"
    message:     str
    urgency:     str   # "high" | "medium"


def check_exit_triggers(pos: PositionSnapshot) -> list[ExitSignal]:
    signals = []

    if pos.is_long:
        # Take profit: mark >= 2x cost
        if pos.mark_price >= pos.tp_price:
            pnl_str = f"+${pos.pnl_dollars:,.0f} ({pos.pnl_pct*100:.0f}%)"
            signals.append(ExitSignal(
                "take_profit",
                f"Take profit hit — mark ${pos.mark_price:.2f} ≥ target ${pos.tp_price:.2f}  {pnl_str}",
                "high",
            ))
        # Stop loss: mark <= 0.5x cost
        elif pos.mark_price <= pos.sl_price:
            pnl_str = f"-${abs(pos.pnl_dollars):,.0f} ({pos.pnl_pct*100:.0f}%)"
            signals.append(ExitSignal(
                "stop_loss",
                f"Stop loss hit — mark ${pos.mark_price:.2f} ≤ floor ${pos.sl_price:.2f}  {pnl_str}",
                "high",
            ))
    else:
        # Short: take profit when mark drops to 50% of credit
        if pos.mark_price <= pos.tp_price:
            captured = pos.avg_open_price - pos.mark_price
            pct = captured / pos.avg_open_price * 100
            signals.append(ExitSignal(
                "take_profit",
                f"50% credit captured — buy back at ${pos.mark_price:.2f}  ({pct:.0f}% of ${pos.avg_open_price:.2f} credit)",
                "high",
            ))
        # Short: stop loss when mark rises to 2x credit
        elif pos.mark_price >= pos.sl_price:
            loss = (pos.mark_price - pos.avg_open_price) * 100 * abs(pos.quantity)
            signals.append(ExitSignal(
                "stop_loss",
                f"Stop loss — mark ${pos.mark_price:.2f} ≥ 2× credit ${pos.sl_price:.2f}  (-${loss:,.0f})",
                "high",
            ))

    # Time stop: 21 DTE
    if pos.dte <= 21 and pos.dte > 0:
        signals.append(ExitSignal(
            "time_stop",
            f"{pos.dte} DTE — theta accelerates, evaluate closing position",
            "medium" if pos.dte > 7 else "high",
        ))

    # Expiry warning: 5 DTE
    if pos.dte <= 5 and pos.dte > 0:
        signals.append(ExitSignal(
            "expiry_warn",
            f"⚠️ {pos.dte} DTE — assignment/pin risk, close immediately",
            "high",
        ))

    return signals


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def send_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": chat_id, "text": message, "parse_mode": "Markdown"
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        log.warning(f"Telegram error: {e}")
        return False


def format_position_alert(pos: PositionSnapshot, signal: ExitSignal) -> str:
    direction  = "LONG" if pos.is_long else "SHORT"
    emoji_map  = {
        "take_profit": "🎯",
        "stop_loss":   "🛑",
        "time_stop":   "⏰",
        "expiry_warn": "🚨",
    }
    emoji = emoji_map.get(signal.signal_type, "📌")
    pnl_sign = "+" if pos.pnl_dollars >= 0 else ""
    now = datetime.now().strftime("%b %d %H:%M ET")

    return "\n".join([
        f"{emoji} *{pos.underlying} — {signal.signal_type.replace('_',' ').upper()}*",
        f"",
        f"*Position:* {direction} {abs(pos.quantity)}x `{pos.symbol}`",
        f"*Strike:* ${pos.strike:.0f} {pos.option_type.title()}  ·  Exp: {pos.expiry} ({pos.dte} DTE)",
        f"",
        f"*Avg Open:*  ${pos.avg_open_price:.2f}  (${pos.cost_per_contract:.0f}/contract)",
        f"*Mark Now:*  ${pos.mark_price:.2f}  (${pos.value_per_contract:.0f}/contract)",
        f"*Unrealized P&L:* {pnl_sign}${pos.pnl_dollars:,.0f}  ({pnl_sign}{pos.pnl_pct*100:.0f}%)",
        f"",
        f"*Signal:* {signal.message}",
        f"",
        f"*Greeks:* Δ {pos.delta:+.2f}  θ {pos.theta:.2f}  IV {pos.iv*100:.0f}%",
        f"",
        f"_{now}_",
    ])


def format_positions_summary(positions: list[PositionSnapshot], scan_time: str) -> str:
    if not positions:
        return f"📊 *Positions — {scan_time}*\n\n_No open option positions found._"

    lines = [f"📊 *Positions — {scan_time}*", ""]
    total_pnl = sum(p.pnl_dollars for p in positions)

    for p in sorted(positions, key=lambda x: abs(x.pnl_pct), reverse=True):
        direction = "L" if p.is_long else "S"
        pnl_sign  = "+" if p.pnl_dollars >= 0 else ""
        pnl_str   = f"{pnl_sign}${p.pnl_dollars:,.0f} ({pnl_sign}{p.pnl_pct*100:.0f}%)"
        warn      = " ⏰" if p.dte <= 21 else ""
        lines.append(
            f"• *{p.underlying}* {direction} ${p.strike:.0f}{p.option_type[0].upper()} "
            f"exp {p.expiry} ({p.dte}d){warn}  `{pnl_str}`"
        )

    lines += [
        "",
        f"*Total unrealized:* {'+'if total_pnl >= 0 else ''}${total_pnl:,.0f}",
        f"_{scan_time}_",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN FETCH + MONITOR LOGIC
# ─────────────────────────────────────────────

async def fetch_and_check(session: Session, dry_run: bool = False) -> list[PositionSnapshot]:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID",   "")
    has_tg    = bool(bot_token and chat_id)

    state     = load_state()
    scan_time = datetime.now().strftime("%b %d %H:%M ET")

    # ── 1. Fetch all accounts and positions ───────────────────────────────────
    log.info("Fetching accounts and positions...")
    accounts  = await Account.get(session)
    if not accounts:
        log.warning("No accounts found")
        return []

    all_positions: list[CurrentPosition] = []
    for account in accounts if isinstance(accounts, list) else [accounts]:
        pos_list = await account.get_positions(session)
        all_positions.extend(pos_list)

    # Filter to equity options only
    option_positions = [
        p for p in all_positions
        if p.instrument_type == InstrumentType.EQUITY_OPTION
        and not p.is_suppressed
    ]

    if not option_positions:
        log.info("No open option positions")
        if has_tg and not dry_run:
            send_telegram(
                format_positions_summary([], scan_time),
                bot_token, chat_id,
            )
        return []

    log.info(f"Found {len(option_positions)} option position(s)")

    # ── 2. Collect streamer symbols for live quotes/greeks ────────────────────
    streamer_syms = []
    pos_meta: dict[str, dict] = {}   # streamer_sym -> parsed option info

    for p in option_positions:
        parsed = parse_occ_symbol(p.symbol)
        if not parsed:
            log.warning(f"  Could not parse symbol: {p.symbol}")
            continue
        # Tastytrade streamer symbol for options uses a dot-prefix format
        # e.g. .NVDA260515C195  — approximate from OCC
        sym = p.symbol.strip()
        streamer_syms.append(sym)
        pos_meta[sym] = {
            "position":  p,
            "parsed":    parsed,
        }

    if not streamer_syms:
        log.warning("No parseable option positions")
        return []

    # ── 3. Stream live quotes and greeks ─────────────────────────────────────
    log.info(f"Streaming live data for {len(streamer_syms)} position(s)...")
    quotes:     dict = {}
    greeks_map: dict = {}
    summary_map:dict = {}

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote,   streamer_syms)
        await streamer.subscribe(Greeks,  streamer_syms)
        await streamer.subscribe(Summary, streamer_syms)

        deadline  = time.time() + STREAM_TIMEOUT
        pending_q = set(streamer_syms)
        pending_g = set(streamer_syms)

        while time.time() < deadline:
            while True:
                try:
                    ev = streamer.get_event_nowait(Quote)
                    quotes[ev.event_symbol] = ev
                    pending_q.discard(ev.event_symbol)
                except Exception: break

            while True:
                try:
                    ev = streamer.get_event_nowait(Greeks)
                    greeks_map[ev.event_symbol] = ev
                    pending_g.discard(ev.event_symbol)
                except Exception: break

            while True:
                try:
                    ev = streamer.get_event_nowait(Summary)
                    summary_map[ev.event_symbol] = ev
                except Exception: break

            if not pending_q and not pending_g:
                break
            await asyncio.sleep(0.15)

    # ── 4. Build PositionSnapshot for each position ───────────────────────────
    snapshots: list[PositionSnapshot] = []
    alerts_sent = 0

    for sym, meta in pos_meta.items():
        p      = meta["position"]
        parsed = meta["parsed"]

        # Prices
        q  = quotes.get(sym)
        g  = greeks_map.get(sym)
        sm = summary_map.get(sym)

        avg_open = float(p.average_open_price or 0)
        mark     = 0.0

        if q:
            bid = float(q.bid_price or 0)
            ask = float(q.ask_price or 0)
            if bid > 0 and ask > 0:
                mark = (bid + ask) / 2
            elif bid > 0:
                mark = bid

        # Fallback to close price from summary
        if mark <= 0 and sm and sm.day_close_price:
            mark = float(sm.day_close_price)
        if mark <= 0 and sm and sm.prev_day_close_price:
            mark = float(sm.prev_day_close_price)
        if mark <= 0:
            mark = float(p.close_price or avg_open)

        # Greeks
        delta = float(g.delta or 0)  if g else 0.0
        theta = float(g.theta or 0)  if g else 0.0
        iv    = float(g.volatility or 0) if g else 0.0

        # Position direction
        qty       = int(p.quantity or 1)
        direction = str(p.quantity_direction or "Long")
        is_long   = direction.lower() == "long"
        multiplier = int(p.multiplier or 100)

        # P&L
        cost_per  = avg_open * multiplier
        val_per   = mark     * multiplier
        if is_long:
            pnl_per_contract = (mark - avg_open) * multiplier
        else:
            pnl_per_contract = (avg_open - mark) * multiplier
        pnl_total = pnl_per_contract * qty
        pnl_pct   = (mark - avg_open) / avg_open if avg_open > 0 else 0.0
        if not is_long:
            pnl_pct = -pnl_pct

        # DTE
        expiry = parsed["expiry"]
        dte    = max(0, (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days)

        # Exit thresholds
        tp_price, sl_price = compute_thresholds(avg_open, is_long)

        snap = PositionSnapshot(
            symbol=sym,
            underlying=parsed["underlying"],
            option_type=parsed["option_type"],
            strike=parsed["strike"],
            expiry=expiry,
            dte=dte,
            quantity=qty,
            avg_open_price=avg_open,
            mark_price=mark,
            cost_per_contract=cost_per,
            value_per_contract=val_per,
            pnl_dollars=pnl_total,
            pnl_pct=pnl_pct,
            is_long=is_long,
            delta=delta,
            theta=theta,
            iv=iv,
            tp_price=tp_price,
            sl_price=sl_price,
            dte_warn=21,
        )
        snapshots.append(snap)

        pnl_str = f"{'+'if pnl_total>=0 else ''}${pnl_total:,.0f} ({pnl_pct*100:+.0f}%)"
        log.info(
            f"  {'L' if is_long else 'S'} {snap.underlying} "
            f"${snap.strike:.0f}{snap.option_type[0].upper()} "
            f"exp {snap.expiry} ({snap.dte} DTE) | "
            f"open=${avg_open:.2f} mark=${mark:.2f} pnl={pnl_str}"
        )

        # ── 5. Check exit triggers ────────────────────────────────────────────
        signals = check_exit_triggers(snap)
        for sig in signals:
            state_key = f"{sym}::{sig.signal_type}"
            last_alert = state.get(state_key, {}).get("last_alert")

            # Cooldown: high urgency = 30 min, medium = 120 min
            cooldown = 30 if sig.urgency == "high" else 120
            should_fire = True
            if last_alert:
                elapsed = (datetime.now(timezone.utc) -
                           datetime.fromisoformat(last_alert)).total_seconds() / 60
                if elapsed < cooldown:
                    should_fire = False
                    log.info(f"    ↳ {sig.signal_type} in cooldown ({elapsed:.0f}/{cooldown}m)")

            if should_fire:
                log.info(f"    ↳ ALERT: {sig.signal_type} [{sig.urgency}] — {sig.message}")
                if not dry_run and has_tg:
                    msg  = format_position_alert(snap, sig)
                    sent = send_telegram(msg, bot_token, chat_id)
                    if sent:
                        state[state_key] = {
                            "last_alert": datetime.now(timezone.utc).isoformat()
                        }
                        alerts_sent += 1
                elif dry_run:
                    log.info(f"    [DRY RUN] Would send: {sig.signal_type} for {sym}")
                    state[state_key] = {
                        "last_alert": datetime.now(timezone.utc).isoformat()
                    }
                    alerts_sent += 1

    # ── 6. Send positions summary ─────────────────────────────────────────────
    if has_tg and not dry_run:
        summary_msg = format_positions_summary(snapshots, scan_time)
        send_telegram(summary_msg, bot_token, chat_id)

    save_state(state)
    log.info(f"Positions scan complete — {len(snapshots)} positions, {alerts_sent} alerts")
    return snapshots


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Positions Monitor — Tastytrade + Telegram")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Check positions without sending alerts")
    parser.add_argument("--sandbox",  action="store_true",
                        help="Use Tastytrade sandbox environment")
    args = parser.parse_args()

    load_dotenv()

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

    asyncio.run(fetch_and_check(session, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
