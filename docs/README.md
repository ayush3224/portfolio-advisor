# portfolio-advisor

A personal, single-user AI portfolio advisor for an Indian retail investor
who holds **Indian equities + ETFs** and **US equities + ETFs**. It reads
the user's real portfolio from Supabase, fetches live prices, market
context, news, analyst signals, and prediction-market probabilities, runs
Claude for per-holding recommendations, and delivers them to a private
Telegram chat on a fixed daily schedule. Execution is always manual —
the system never places orders.

Standalone project. Not a fork of, nor a derivative of, any other
codebase.

---

## 1. What it is

- **One user, one chat.** Recommendations go to a single Telegram chat
  ID. Inbound commands from any other chat are silently ignored.
- **Two markets, one model.** Indian holdings and US holdings live in the
  same `holdings` table, distinguished by `market in ('IND','US')`.
  Quantities are `numeric(14,4)` so fractional US shares work natively.
- **Four daily report cadences.** 9:00 AM IND advisory, 3:00 PM IND EOD
  check, 4:00 PM outcome logger, 7:30 PM US advisory. Plus a Sunday-8 PM
  weekly review.
- **You maintain portfolio state.** The Telegram bot exposes `BUY`,
  `SELL`, `PORTFOLIO`, etc. There is no broker-side write integration.
- **Advisory only.** Every output is a recommendation. No order ever
  leaves the box.

## 2. Architecture

| Layer | Component | Purpose |
|---|---|---|
| Portfolio truth | Supabase `holdings` / `transactions` | Mutated only by the Telegram bot. |
| IND prices | Upstox Analytics Token (v3 LTP) | Live quotes for NSE holdings; long-lived bearer. |
| IND fallback | yfinance (`.NS` / `.BO` suffix) | Used when Upstox is unreachable or unmapped. |
| US prices | yfinance | LTP + OHLC + 52w range for US tickers. |
| IND macro | yfinance + NSE public API | `^NSEI`, `^NSEBANK` via yfinance; FII/DII via NSE `fiidiiTradeReact`. |
| US macro | yfinance | `^GSPC`, `^IXIC`, `^VIX`, `INR=X`, `^TNX`. |
| News | Tavily semantic search + RSS | Tavily per-holding queries; ET Markets / Moneycontrol / Livemint RSS as free supplement. |
| Analyst channels | Telethon | 9 weighted Indian Telegram channels (`config.TELEGRAM_CHANNELS`). |
| Prediction markets | Polymarket Gamma API | Free, no auth; 2 h cache; India / US topic filters. |
| Reasoning | Anthropic API | Sonnet 4.5 for advisories; Haiku 4.5 for EOD + weekly. |
| Delivery (send) | Telegram Bot HTTP API | Cron jobs POST `sendMessage` directly. |
| Delivery (receive) | `python-telegram-bot` long-poll | `portfolio-bot.service` runs as systemd unit. |

## 3. Daily schedule

| IST | UTC | Job | Model | Output |
|---|---|---|---|---|
| 09:00 | 03:30 | `scheduler/premarket.py` | Sonnet 4.5 | IND portfolio recommendations |
| 15:00 | 09:30 | `scheduler/eod.py` | Haiku 4.5 | IND hold/exit decisions (CNC delivery) |
| 16:00 | 10:30 | `scheduler/outcome_logger.py` | none (free) | Outcomes vs Nifty + token-cost report |
| 19:30 | 14:00 | `scheduler/us_premarket.py` | Sonnet 4.5 | US portfolio recommendations (after US open) |
| Sun 20:00 | Sun 14:30 | `scheduler/weekly_review.py` | Sonnet 4.5 | Weekly win-rate roll-up |

The midday (12:30 PM) slot has been **removed** — leverage is no longer
the focus and the IndMoney / yfinance pricing layer makes a midday check
redundant. `scheduler/midday.py` is retained but not in the active
crontab.

## 4. Telegram commands

The bot responds only to `TELEGRAM_CHAT_ID`; everything else is silently
ignored.

| Command | Aliases | Example | Effect |
|---|---|---|---|
| `BUY <T> <Q> <P>` | — | `BUY ICICIBANK 50 1380` | Add to (or open) an IND position. Recomputes weighted-avg cost. Logs a `transactions` row. |
| `BUY-US <T> <Q> <P>` | — | `BUY-US NVDA 5 243.20` | Same, forces `market='US'`, `currency='USD'`. |
| `SELL <T> <Q> <P>` | — | `SELL ICICIBANK 10 1420` | Reduce (or close) a position. Computes realised P&L vs the stored avg cost. |
| `PORTFOLIO` | `PORT`, `P` | — | IND + US holdings with live mark-to-market. INR + USD subtotals shown side by side. |
| `QUOTE <T>` | `Q` | `QUOTE RELIANCE` | Live LTP via Upstox (IND) or yfinance (US). |
| `STATUS` | `S` | — | Bot health, last cron run, active-holdings count. |
| `HISTORY <T>` | `HIST` | `HISTORY ICICIBANK` | Last 5 transactions for the ticker. |
| `HELP` | `H` | — | Lists all commands. |

US tickers are auto-detected from a known-ticker set in
`bot/portfolio_manager._KNOWN_US_TICKERS`. Use `BUY-US` for anything
ambiguous (e.g. a US ticker that collides with an NSE symbol).

## 5. Portfolio coverage

- **13 Indian holdings** (equity + sector / index ETFs) in
  `holdings WHERE market='IND'`.
- **24 US holdings** (equity + commodity / index ETFs) in
  `holdings WHERE market='US'`, seeded from IndMoney with verified
  average prices (see `scripts/load_us_portfolio.py`).
- The `holdings` table is the **source of truth**. Every BUY / SELL goes
  through the Telegram bot, which updates the row and writes an immutable
  `transactions` log entry.

## 6. Environment variables

Required:

| Var | Used by | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `analysis/claude_client.py` | Anthropic API auth. |
| `SUPABASE_URL` | `storage/supabase_client.py` | Supabase REST endpoint. |
| `SUPABASE_KEY` | `storage/supabase_client.py` | Service-role key. |
| `TELEGRAM_BOT_TOKEN` | `delivery/telegram_bot.py`, `scheduler/bot_runner.py` | Bot identity for send + receive. |
| `TELEGRAM_CHAT_ID` | both | Whitelisted chat — every inbound command is filtered on this. |
| `UPSTOX_ANALYTICS_TOKEN` | `ingestion/upstox_market_data.py`, `ingestion/upstox_prices.py` | Bearer for Upstox v3 endpoints. Long-lived (~1 year). |
| `TAVILY_API_KEY` | `ingestion/news.py` | Per-holding news semantic search. |
| `TELETHON_API_ID` | `ingestion/telegram_scraper.py` | MTProto API auth (from my.telegram.org). |
| `TELETHON_API_HASH` | same | same |
| `TELETHON_PHONE` | same | Phone for first-time login. |
| `TELETHON_SESSION_STRING` *or* `TELETHON_SESSION_FILE` | same | Persistent session for headless cron. String form preferred. |

Optional / tuning:

| Var | Default | Effect |
|---|---|---|
| `DAILY_CAPITAL_BUDGET` | `10000` (₹) | Position-sizing cap for the IND advisory. |
| `DAILY_LOSS_LIMIT` | `2000` (₹) | If today's realised loss exceeds this, subsequent runs switch to conservative mode. |
| `DRY_RUN` | `false` | Skip every Telegram send and Supabase write. Pure read-only. |
| `PAPER_TRADING` | `false` | Banner outgoing Telegram messages and tag `advisor_recommendations.paper_trade=true`. |
| `USE_MOCK_PORTFOLIO` | `false` | Use `tests/mock_portfolio.py` instead of Supabase. |

USD/INR is **not** an env var. It is fetched at process start in
`config.py` via `yfinance INR=X` (`fetch_usd_inr_rate`) with a `95.31`
fallback. The rate is exposed as `config.USD_INR_RATE` and reused
across all $→₹ conversions for that process.

## 7. Setup

```bash
# 1. Clone
git clone <repo> /root/portfolio-advisor
cd /root/portfolio-advisor

# 2. Dependencies
pip install -r requirements.txt

# 3. Configuration
cp .env.example .env
# Edit .env with your secrets.

# 4. Database schema
psql "$SUPABASE_URL" -f supabase_schema.sql
# (or paste supabase_schema.sql into the Supabase SQL editor)

# 5. Telethon one-time auth (interactive — needs a TTY)
python scripts/export_telethon_session.py
# Copy the printed StringSession into .env as TELETHON_SESSION_STRING.

# 6. (Optional) Seed US holdings from IndMoney
python scripts/load_us_portfolio.py

# 7. Bot service
sudo cp portfolio-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now portfolio-bot

# 8. Verify
# In Telegram: DM your bot the word  STATUS
# Expect: bot health + last cron run + active holdings count.
```

### Cron entries

Add to `crontab -e` (all times UTC):

```
30 03 * * 1-5 cd /root/portfolio-advisor && /usr/bin/python3 -m scheduler.premarket      >> logs/premarket.log 2>&1
30 09 * * 1-5 cd /root/portfolio-advisor && /usr/bin/python3 -m scheduler.eod            >> logs/eod.log 2>&1
30 10 * * 1-5 cd /root/portfolio-advisor && /usr/bin/python3 -m scheduler.outcome_logger >> logs/outcome.log 2>&1
00 14 * * 1-5 cd /root/portfolio-advisor && /usr/bin/python3 -m scheduler.us_premarket   >> logs/us.log 2>&1
30 14 * * 0   cd /root/portfolio-advisor && /usr/bin/python3 -m scheduler.weekly_review  >> logs/weekly.log 2>&1
```

## 8. Project structure

```
.
├── CLAUDE.md                              -- project guardrails for Claude Code
├── config.py                              -- env, capital model, leverage map, USD_INR_RATE, NSE instrument keys
├── supabase_schema.sql                    -- DDL for all 8 tables (idempotent)
├── portfolio-bot.service                  -- systemd unit for the bot
├── requirements.txt                       -- pip dependencies
│
├── analysis/
│   ├── claude_client.py                   -- Anthropic call wrapper + run_log cost tracking
│   ├── premarket_prompt.py                -- IND 9 AM Sonnet system + user prompts
│   ├── us_premarket_prompt.py             -- US 7:30 PM Sonnet prompts
│   ├── eod_prompt.py                      -- 3 PM Haiku exit/hold prompts
│   └── midday_prompt.py                   -- (legacy; midday scheduler removed)
│
├── ingestion/
│   ├── upstox_portfolio.py                -- reads holdings from Supabase, enriches with live IND prices
│   ├── upstox_market_data.py              -- Upstox v3 LTP + historical OHLC (async), yfinance fallback
│   ├── upstox_prices.py                   -- legacy v2 quote wrapper, kept for cmp_for / enrich_price_block
│   ├── market_context.py                  -- Nifty, BankNifty, FII/DII, USD/INR, US indices helpers
│   ├── news.py                            -- Tavily semantic search + RSS fallback; filters to current holdings
│   ├── telegram_scraper.py                -- Telethon channel signals + join helper
│   └── polymarket.py                      -- Gamma API client, 2 h cache, India / US topic filters
│
├── processing/
│   ├── portfolio_context.py               -- joins holdings + prices + news + signals per ticker
│   ├── position_sizer.py                  -- confidence → leverage + capital → shares
│   └── risk_guardrails.py                 -- single-position / sector / event guardrails
│
├── scheduler/
│   ├── premarket.py                       -- 9 AM IND orchestrator
│   ├── us_premarket.py                    -- 7:30 PM US orchestrator (Telegram split at 4000 chars)
│   ├── eod.py                             -- 3 PM exit/hold orchestrator
│   ├── outcome_logger.py                  -- 4 PM outcomes + token-cost report
│   ├── weekly_review.py                   -- Sunday 8 PM weekly roll-up
│   ├── midday.py                          -- (legacy; not scheduled)
│   └── bot_runner.py                      -- python-telegram-bot polling loop (systemd entrypoint)
│
├── bot/
│   ├── portfolio_manager.py               -- add_position / close_position / get_quote / list_active
│   └── command_handler.py                 -- parse + dispatch BUY / SELL / PORTFOLIO / QUOTE / STATUS / HISTORY / HELP
│
├── delivery/
│   └── telegram_bot.py                    -- HTTP API sender + report formatters
│
├── storage/
│   └── supabase_client.py                 -- typed wrappers around all 8 tables
│
├── backtest/
│   ├── outcome_tracker.py                 -- per-rec close-price outcome row writer
│   └── weekly_scorecard.py                -- Haiku-generated 7-day critique
│
├── scripts/
│   ├── export_telethon_session.py         -- one-time interactive Telethon session export
│   └── load_us_portfolio.py               -- seed 24 US holdings with verified IndMoney avg prices
│
└── tests/
    └── mock_portfolio.py                  -- offline mock for USE_MOCK_PORTFOLIO=true
```

## 9. Cost

Per-run costs from `run_log` (Sonnet 4.5: $3 / $15 per MTok in / out;
Haiku 4.5: $1 / $5 per MTok):

| Job | Model | Avg in / out (tok) | $ / run | Runs / mo | $ / mo |
|---|---|---|---|---|---|
| IND premarket | Sonnet 4.5 | 13 000 / 500 | $0.046 | 22 | $1.01 |
| EOD hold/exit | Haiku 4.5 | 800 / 300 | $0.0024 | 22 | $0.05 |
| US premarket | Sonnet 4.5 | 11 000 / 1 100 | $0.050 | 22 | $1.10 |
| Outcome logger | none | — | $0 | 22 | $0.00 |
| Weekly review | Sonnet 4.5 | 6 000 / 600 | $0.027 | 4 | $0.11 |
| **Total** | | | | | **≈ $2.27 / mo** |

Telegram, yfinance, NSE public API, Polymarket, and RSS feeds are all
**free**. Tavily's free tier is sufficient for this volume.

> Earlier estimates pegged this at ~$1.37 / mo with smaller prompts; the
> current US prompt includes a richer macro + Polymarket block. To trim
> back, drop max news-per-stock from 3 → 1 in
> `processing/portfolio_context.py`.
