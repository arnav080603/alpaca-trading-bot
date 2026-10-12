# Algorithmic Trading Bot (paper trading)

A fully automated trading system that runs 24/7 on an **[Alpaca](https://alpaca.markets) paper account (simulated money)**. Rule-based strategies find trades, a risk engine sizes them, every stop-loss lives on the broker's servers, and an optional AI layer can veto trades. Every action is pinged to Discord.

> ⚠️ Educational project. It trades paper money only, and nothing here is financial advice.

## Why it's interesting

- **Risk first.** 1% of the account at risk per trade, a 2.5% position cap, a correlation filter, no leverage, and a **5% daily-loss kill switch** that sells everything.
- **Honest backtesting.** A walk-forward backtester chooses parameters on in-sample data, then tests them on **out-of-sample** data the strategy never saw. It reuses the *same* strategy and risk code as the live bot. Results: [`BACKTEST_RESULTS.md`](BACKTEST_RESULTS.md). The S&P 500 trend leg passed both graduation gates out of sample (profit factor 2.14). The Bitcoin breakout leg failed, so I left it on paper.
- **Fault tolerant.** It auto-restarts after crashes and retries through internet drops, and on restart it reconciles with the broker (the broker is always trusted over the bot's memory). Creating a file named `STOP` halts everything within a minute.
- **AI veto layer.** Once an hour, an AI agent reads the bot's context plus market data (insider buying, congressional trades, trader positioning). It can veto a rule-based entry or propose picks, which go through the same risk engine. Every veto is logged so I can measure whether the AI actually helps.
- **Tested.** A pytest suite covers the risk engine, strategies, backtest engine, alerts, the AI layer and the live loop, plus a smoke test that does a real 1-share round trip on paper.

## Stack
Python · alpaca-py · pandas · pytest · Discord webhooks · launchd (macOS auto-start)

## Run it
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # add your Alpaca *paper* keys
.venv/bin/python -m pytest -q
.venv/bin/python run_bot.py --dry-run --once
```

---

## Full operating manual
## AI layer (added 2026-10-10)

Every hour on weekdays (08:05–15:05 ET) a Claude scheduled task called **"Trading bot AI decisions"** reads `ai/context.json` (written by the bot). It checks Co-Invest's market analysis (read-only), Coinversa trader positioning, and free Congress and insider-buying data, then writes `ai/decisions.json`. The AI can:
- **Veto** an entry the rules want to make. The trade is skipped, Discord gets a 🤖⛔ ping, and it's logged in `logs/ai_vetoes.csv` so we can check later whether the veto helped.
- **Pick** up to 10 stocks of its own from the S&P 500 list (long only). These are sized by the same risk engine, get the same 2× ATR server-side stop, and show as leg `ai_pick` in `trades.csv`.
- **Sell** its own picks early. Picks are also sold after 20 days.

If the Claude app is closed or the file is older than 2 hours, the bot simply trades on rules alone, and you get one 🤖💤 ping that day. To switch the AI off, set `ai.enabled: false` in `config.yaml` and restart. Costs nothing extra: it uses your Claude subscription, with no paid API keys.

**Active legs** (settings live in `config.yaml`):

| Leg | Symbols | Bars | Rules |
|---|---|---|---|
| ~~Commodity trend~~ (off: Bitcoin only, by choice) | GLD, USO | 4-hour (09:30–13:30, 13:30–16:00) | Buy when EMA20 crosses above EMA50. Sell on the opposite cross or at the stop (2× ATR). Long only. |
| BTC breakout | BTC/USD | Daily | Buy when the close breaks the 20-day high above the 50-day EMA. Exit on a 2× ATR trailing stop or a close below the 10-day low. |
| **Stock trend** | The S&P 500 + SpaceX (SPCX), TSMC, Palantir, Micron: 505 stocks, listed in `universe_sp500.txt` | Daily | Buy when EMA10 crosses above EMA100. Sell on the opposite cross or at the stop (2× ATR). Long only. Decides at the market open. Up to 40 positions. |
| ~~Mean reversion~~ | SPY, QQQ | 15-min | Switched off: no edge in testing. |

**Risk rules for every trade:**
- 1% of the account at risk per trade.
- No position over 2.5% of the account, at most 40 positions, and no leverage.
- No two highly correlated positions in the same direction.
- A **5% daily loss stops everything** until the next day.

## Everyday use (Mac)

All commands are run in Terminal from the `trading-bot` folder. To get there: `cd` into this folder.

| I want to… | Do this |
|---|---|
| **Check it's running** | `.venv/bin/python autostart.py status` |
| **Stop all trading right now** | Create an empty file named `STOP` in the `trading-bot` folder (`touch STOP`). The bot sells everything, cancels orders and halts within a minute. |
| **Let it trade again** | Delete the `STOP` file (`rm STOP`) |
| **Turn auto-start off / on** | `.venv/bin/python autostart.py uninstall` / `install` |
| **See what it's doing** | Open `logs/bot.log`, or `tail -f logs/bot.log` to watch live |
| **See trades** | `logs/trades.csv`, plus your Discord channel |
| **See daily results** | `logs/daily_summary.csv` (one row per day) |
| **Test without trading** | `.venv/bin/python run_bot.py --dry-run --once` |
| **Run the tests** | `.venv/bin/python -m pytest -q` |
| **Test a real round trip** (market hours) | `.venv/bin/python smoke_test.py`: buys and sells 1 share of SPY on paper through the real bot code |
| **Re-run backtests** | `.venv/bin/python backtest.py` (results in `results/summary.md`) |

Auto-start runs the bot when you log in and restarts it 60 seconds after any crash. While the Mac is **plugged in**, it's kept awake. On battery or with the lid closed it sleeps: it misses checks and alerts until it wakes, then catches up. **Every stop, stocks and Bitcoin, sits on Alpaca's servers**, so stops keep working while the Mac sleeps. Bitcoin's is a stop-limit (it sells no lower than 1% below the stop); if the price falls through that, the bot sells it as a backup once it's awake.

On Windows: copy the folder, create the venv (`py -3.12 -m venv .venv` then `.venv\Scripts\pip install -r requirements.txt`), copy `.env`, then run `.venv\Scripts\python autostart.py install`.

## Settings and secrets

- `config.yaml` holds every number the bot uses. Change it, then restart: `autostart.py uninstall` then `install`.
- `.env` holds your Alpaca keys and Discord webhook. It's never shared or committed. If you regenerate keys in Alpaca, paste the new ones here and restart.
- The bot trades your **whole paper balance** (`set_aside_extra: false`). Set it to `true` to make the bot act as a $10,000 account (`starting_equity`) inside a bigger paper account.
- **Watch alerts:** at 10:00 and 14:30 ET on weekdays (weekends: Bitcoin only), Discord gets the 5 setups closest to triggering. Each shows the exact closing price that would fire it, why it matters, and an animated chart of the top one. They never trade. Set the times with `alerts.watch_times`.
- **Daily update:** every day at 16:15 ET, Discord gets a 📊 summary covering equity, today's and all-time P&L, trades, blocks, kill-switch status, and each open position with its stop. Change the time with `alerts.daily_update_time`. Every ping starts with `@everyone` so your phone buzzes (`alerts.mention`; use `"<@your user id>"` to ping only you, or remove it to stay silent).

## What happens when…

- **The bot crashes:** Discord gets a ⚠️ ping and auto-start restarts it a minute later.
- **The internet drops:** it keeps retrying. You get one ping at the first failure, then about every 30 minutes, and a ✅ when it recovers.
- **It restarts:** it first syncs with Alpaca. The broker is always trusted over the bot's memory: unknown positions are adopted and given stops, and closed ones are logged.
- **Equity falls 5% in a day:** it sells everything, pings 🛑, and waits until tomorrow.

## Files

| File | What it is |
|---|---|
| `run_bot.py` | Starts the live bot |
| `backtest.py` | Walk-forward backtester (imports the same strategy and risk code) |
| `autostart.py` | Turns auto-start on or off |
| `bot/strategies.py` | The three legs' rules |
| `bot/risk.py` | Risk engine: sizing, caps, correlation, kill switches, cash-account mode |
| `bot/live.py` | The minute-by-minute live loop |
| `bot/broker.py` | Alpaca connection (with retries) |
| `bot/alerts.py` | Discord pings |
| `bot/ai.py` | Reads the AI's decisions, writes its context |
| `ai/` | `context.json` (bot → AI), `decisions.json` (AI → bot), `history.jsonl` (every AI decision) |
| `bot/data.py` | Price data download |
| `logs/` | `trades.csv`, `daily_summary.csv`, `bot.log`. The column formats are fixed because Claude Cowork reads them. |
| `state.json` | The bot's memory of its open positions (Alpaca always wins if they disagree) |

## Before real money

Each leg must pass the graduation checklist. Everything stays on paper until a leg passes those gates and proves itself live on paper too.
