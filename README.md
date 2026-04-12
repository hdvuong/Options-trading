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

# Dry run (no alerts sent)
python monitor.py --dry-run --skip-hours-check

# Test a single ticker
python monitor.py --ticker NVDA --dry-run --skip-hours-check

# Force alerts (ignore cooldown)
python monitor.py --force --skip-hours-check

# Real run with alerts
python monitor.py --skip-hours-check
```

---

## Telegram message examples

**Alert (score ≥ threshold):**
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

**Summary (every scan):**
```
📋 Options Scan — Apr 12 09:30 ET

• NVDA  ████████░░  74/100 — Bull Call Spread
• AAPL  ██████░░░░  62/100 — Long Call
• MSFT  █████░░░░░  55/100 — Bull Call Spread
• SPY   ████░░░░░░  48/100 — Cash-Secured Put
• QQQ   ████░░░░░░  46/100 — Cash-Secured Put

Threshold: 65
```

---

## Roadmap

- [ ] Add earnings date detection (avoid selling into earnings)
- [ ] Add 30-day trend via Tastytrade candle history
- [ ] Add open interest from REST API (not in streaming)
- [ ] Add portfolio Greeks tracker (net delta, theta)
- [ ] Add position-aware alerts (e.g. "your NVDA call is up 80%")
