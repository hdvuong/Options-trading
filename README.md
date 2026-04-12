# Options Monitor

Scans your watchlist every 30 minutes during market hours via GitHub Actions,
scores each ticker using the live Tastytrade API, and sends Telegram alerts
when a setup scores above your threshold.

---

## Setup (20 minutes total)

### Step 1 — Telegram Bot (~5 min)

1. Open Telegram, search **@BotFather**
2. Send `/newbot`, follow prompts, copy your **bot token** (looks like `123456:ABCdef...`)
3. Search **@userinfobot**, start it, copy your **Chat ID** (a number like `987654321`)
4. Send any message to your new bot to activate the chat

### Step 2 — GitHub Repo (~5 min)

1. Create a new GitHub repo (can be private)
2. Push all files in this folder to the repo root
3. Go to **Settings → Secrets and variables → Actions → New repository secret**
4. Add these 4 secrets:

| Secret name                 | Value                              |
|-----------------------------|------------------------------------|
| `TASTYTRADE_CLIENT_SECRET`  | Your Tastytrade OAuth client secret |
| `TASTYTRADE_REFRESH_TOKEN`  | Your Tastytrade refresh token       |
| `TELEGRAM_BOT_TOKEN`        | Your bot token from BotFather       |
| `TELEGRAM_CHAT_ID`          | Your chat ID from userinfobot       |

**Get Tastytrade credentials:**
- Go to `my.tastytrade.com → Manage → API Access → OAuth Applications`
- Create an app (check all scopes), save the **client secret**
- Under your app → **Create Grant** → copy the **refresh token**

### Step 3 — Configure your watchlist (~2 min)

Edit `watchlist.json`:
```json
{
  "tickers": ["NVDA", "AAPL", "MSFT", "SPY", "QQQ"],
  "alert": {
    "score_threshold": 65,
    "cooldown_minutes": 120,
    "rescore_if_change": 10
  }
}
```

### Step 4 — Enable GitHub Actions (~2 min)

1. Go to your repo → **Actions** tab
2. Enable workflows if prompted
3. The workflow runs automatically on schedule — or click **Run workflow** to test now

---

## How it works

Every 30 minutes (Mon–Fri, 9am–4pm ET):

1. Connects to Tastytrade API
2. Fetches live quotes, Greeks, and IV Rank for each ticker
3. Scores 4 strategies per ticker (Long Call, Bull Call Spread, CSP, Iron Condor)
4. Sends Telegram alert for any setup scoring ≥ threshold
5. Sends a summary message showing all ticker scores
6. Respects cooldown — won't re-alert the same setup for 2 hours (configurable)
7. Overrides cooldown if the score changes by 10+ points (configurable)

---

## Local testing

```bash
# Install dependencies
pip install -r requirements.txt

# Create .env file
cp .env.example .env
# Fill in your credentials

# Dry run scan (no alerts sent)
python monitor.py --dry-run

# Test a single ticker
python monitor.py --ticker NVDA --dry-run

# Force alerts (ignore cooldown)
python monitor.py --force

# Check open positions
python positions.py --dry-run

# Use sandbox environment
python monitor.py --sandbox --dry-run
python positions.py --sandbox --dry-run
```

---

## Telegram message examples

**Positions summary (every scan):**
```
📊 Positions — Apr 13 09:30 ET

• NVDA L $195C exp 2026-05-15 (33d)  +$420 (+65%)
• AAPL S $180P exp 2026-04-24 (12d) ⏰  +$88 (+55%)

Total unrealized: +$508
Apr 13 09:30 ET
```

**Position alert — take profit:**
```
🎯 NVDA — TAKE PROFIT

Position: LONG 2x NVDA  260515C00195000
Strike: $195 Call  ·  Exp: 2026-05-15 (33 DTE)

Avg Open:  $6.50  ($650/contract)
Mark Now:  $13.20  ($1,320/contract)
Unrealized P&L: +$1,340 (+103%)

Signal: Take profit hit — mark $13.20 ≥ target $13.00

Greeks: Δ +0.62  θ -0.18  IV 41%

Apr 13 10:00 ET
```

**Position alert — time stop:**
```
⏰ AAPL — TIME STOP

Position: SHORT 1x AAPL  260424P00180000
Strike: $180 Put  ·  Exp: 2026-04-24 (12 DTE)

Avg Open:  $3.20  ($320/contract)
Mark Now:  $1.45  ($145/contract)
Unrealized P&L: +$175 (+55%)

Signal: 12 DTE — theta accelerates, evaluate closing position

Greeks: Δ -0.24  θ -0.31  IV 38%

Apr 13 10:00 ET
```

**Trade scan alert (score ≥ threshold):**
```
✅ NVDA — Bull Call Spread 📊
Score: 74/100 · first alert

Setup: BUY $190C / SELL $205C exp 2026-05-15
Expiry: 2026-05-15 (33 DTE)

Debit: $4.50
Max Gain: $10.50  |  Max Loss: $4.50
Breakeven: $194.50
Delta: +0.42  |  IV: 41.0%

Exit rules:
  • Close at 2x-3x premium (~$9.00-$13.50)
  • Exit if down 50% (<$2.25)
  • Re-evaluate at 21 DTE (Apr 24)

Apr 12 09:30 ET
```

---

## Roadmap

- [x] Live IV Rank, Greeks, bid/ask from Tastytrade
- [x] 30-day trend via daily candle history
- [x] Open interest from Summary streaming event
- [x] Earnings detection — penalty for sellers, bonus for buyers
- [x] Weekend/after-hours mode (last-close prices)
- [x] Position monitor with take profit / stop loss / time stop alerts
- [ ] Portfolio net Greeks tracker (total delta, theta, vega across all positions)
- [ ] Earnings calendar overlay on watchlist scan
- [ ] Multi-leg position grouping (show spread P&L as one unit)
