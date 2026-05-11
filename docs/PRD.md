# Product Requirements — portfolio-advisor

## 1. Overview and goals

A personal, AI-powered portfolio advisor for a single Indian retail
investor whose holdings span:

- **Indian stocks + ETFs** held in an Upstox-cleared account, and
- **US stocks + ETFs** held via IndMoney.

The system reads the user's *actual* holdings from a Supabase
`holdings` table and produces per-position recommendations grounded in:

1. Current portfolio context (size, sector mix, P&L).
2. Live prices and intraday market context.
3. Filtered news relevant to held tickers.
4. Indian analyst signals from weighted Telegram channels.
5. Polymarket prediction-market probabilities for macro events.

Recommendations are delivered to a private Telegram chat on a fixed
schedule. **Execution is always manual** — the system never places
orders. Portfolio state is updated through a Telegram bot that exposes
`BUY`, `SELL`, `PORTFOLIO`, `QUOTE`, `STATUS`, `HISTORY`, `HELP`.

## 2. Non-goals

- **No order execution.** Every output is a recommendation. No broker
  write path of any kind.
- **No multi-user support.** All credentials, capital limits, and
  channel preferences are configured for one user. The bot ignores
  messages from any other Telegram chat ID.
- **No web UI.** Telegram is the entire delivery surface.
- **No real-time streaming.** Snapshots at fixed cron times only.
- **No intraday trading workflow.** No more midday MIS checks; the
  product is now positional / delivery-only.

## 3. Features (F1–F15)

### F1. Indian portfolio ingestion
- **Source:** Supabase `holdings WHERE market='IND' AND is_active=true`.
- **Live prices:** Upstox Analytics Token (`v3 market-quote/ltp`),
  resolved via `config.INSTRUMENT_KEYS` (`NSE_EQ|<ISIN>`).
- **Fallback:** yfinance with `.NS` suffix on Upstox failure or
  unmapped ticker.
- **Enrichment:** current_value, unrealised P&L (₹ + %), today's OHLC
  via Upstox historical.
- Implemented in `ingestion/upstox_portfolio.py`,
  `ingestion/upstox_market_data.py`, `ingestion/upstox_prices.py`.

### F2. US portfolio ingestion
- **Source:** Supabase `holdings WHERE market='US' AND is_active=true`.
- **Live prices:** yfinance per ticker.
- **Currency:** Prices in USD; INR equivalents computed using
  `config.USD_INR_RATE` (fetched at process start).
- Implemented in `bot/portfolio_manager.py` (`_get_us_quote`) and
  `ingestion/market_context.py`.

### F3. Indian market context
- Nifty 50 (`^NSEI`) and Bank Nifty (`^NSEBANK`) via yfinance: spot,
  day change %, gap vs previous close.
- FII / DII flows via NSE public API `fiidiiTradeReact` (no auth).
- Pre-market gap analysis vs previous close.
- Implemented in `ingestion/market_context.py`.

### F4. US market context
- S&P 500 (`^GSPC`), Nasdaq 100 (`^IXIC`), VIX (`^VIX`) via yfinance.
- USD / INR via yfinance `INR=X` (live rate, single fetch per
  process).
- 10-year US Treasury yield (`^TNX`).
- Implemented in `ingestion/market_context.py`.

### F5. News ingestion
- **Indian holdings:** Tavily semantic search with NSE-focused query
  template per ticker. RSS supplements: ET Markets, Moneycontrol,
  Livemint.
- **US holdings:** Tavily semantic search with US-company query
  template.
- **Filtering:** only items mentioning a currently-held ticker are
  kept. In-process cache for 1 h.
- Implemented in `ingestion/news.py`.

### F6. Telegram channel signals (Indian stocks)
- 9 channels (`config.TELEGRAM_CHANNELS`) with weights:
  - `institutional` weight = 3: religarebrokingofficial,
    ICICIdirectMARKETSstocks, Equity99Official_Equity_999, equity99,
    nooreshtech.
  - `mid` weight = 2: STOCKGAINERSS.
  - `retail` weight = 1: aakankshatrading, rawattraderss,
    STOCK_MARKET_SEBI_R.
- Telethon `StringSession` for headless cron auth (preferred over
  file sessions on a VPS).
- Filtered to the currently-held ticker / sector set.
- Implemented in `ingestion/telegram_scraper.py`.

### F7. Polymarket prediction signals
- Free public Gamma API — no auth required.
- **India bucket:** RBI policy, crude oil, geopolitics keywords.
- **US bucket:** Fed rates, US macro / election keywords.
- Filters: `volume > $10 000`, probability between 5 % and 95 %,
  market closes within 90 days.
- 2-hour in-process cache.
- Implemented in `ingestion/polymarket.py`.

### F8. Indian portfolio advisory (9:00 AM IST)
- **Model:** Claude Sonnet 4.5 (`claude-sonnet-4-5`).
- **Action set:** `BUY-MOMENTUM`, `BUY-EVENT`, `HOLD`,
  `PARTIAL-EXIT`, `FULL-EXIT`, `WATCH`.
- **Inputs:** per-holding context block (F1 + F3 + F5 + F6 + F7),
  capital model (`config.DAILY_CAPITAL_BUDGET = 10 000 ₹`,
  `DAILY_LOSS_LIMIT = 2 000 ₹`).
- **Output:** structured per-ticker recommendations persisted to
  `advisor_recommendations` and posted to Telegram, max 3 messages.
- Implemented in `scheduler/premarket.py`, `analysis/premarket_prompt.py`.

### F9. US portfolio advisory (7:30 PM IST, after US open)
- **Model:** Claude Sonnet 4.5.
- **Action set:** `HOLD`, `PARTIAL-EXIT`, `FULL-EXIT`, `ADD`, `WATCH`.
  **No leverage** — positional holds only.
- **Inputs:** F2 + F4 + F5 (US) + F7 (US bucket).
- **Output:** per-ticker recommendations persisted to
  `advisor_recommendations`. Telegram split at 4 000 chars to respect
  the Telegram message size limit.
- Implemented in `scheduler/us_premarket.py`,
  `analysis/us_premarket_prompt.py`.

### F10. EOD check (3:00 PM IST)
- **Model:** Claude Haiku 4.5 (`claude-haiku-4-5-20251001`).
- Checks stop-loss / target breaches on Indian holdings only.
- **CNC delivery** decisions only — no MIS / leverage actions.
- Persisted to `eod_recommendations`. Posted to Telegram.
- Implemented in `scheduler/eod.py`, `analysis/eod_prompt.py`.

### F11. Outcome logger (4:00 PM IST)
- **No Claude call — free.**
- Fetches closing prices for today's recommendations via yfinance.
- Computes alpha vs Nifty 50 (Indian) or S&P 500 (US).
- Classifies each rec as `win` / `loss` / `flat`.
- Persists to `backtest_results`.
- Sends an EOD summary to Telegram including the day's token-cost
  report read from `run_log`.
- Implemented in `scheduler/outcome_logger.py`,
  `backtest/outcome_tracker.py`.

### F12. Telegram portfolio-management bot
- Runs as a systemd service (`portfolio-bot.service`), always on,
  `Restart=always`.
- **Commands** (full list in README §4): `BUY`, `BUY-US`, `SELL`,
  `PORTFOLIO`, `QUOTE`, `STATUS`, `HISTORY`, `HELP`.
- **Security model:** every incoming update is filtered on
  `TELEGRAM_CHAT_ID` before any dispatch; non-whitelisted senders are
  silently ignored.
- `BUY` recomputes weighted-average cost: `(old_qty × old_avg + qty × price) / new_qty`.
- `SELL` computes realised P&L vs the stored `average_price` and writes
  a `transactions` row.
- `PORTFOLIO` calls live-price layer (Upstox or yfinance) per holding.
- Implemented in `scheduler/bot_runner.py`, `bot/command_handler.py`,
  `bot/portfolio_manager.py`.

### F13. Weekly review (Sunday 8:00 PM IST)
- **Model:** Claude Sonnet 4.5.
- Aggregates the trailing 7 days of recommendations and outcomes from
  `advisor_recommendations` ⨝ `backtest_results`.
- Computes win-rate split by signal type (BUY-MOMENTUM, BUY-EVENT,
  PARTIAL-EXIT, FULL-EXIT, ADD).
- Highlights best and worst calls of the week.
- Telegram summary, ~120 words.
- Implemented in `scheduler/weekly_review.py`,
  `backtest/weekly_scorecard.py`.

### F14. Backtesting framework
- Daily outcome tracking populated by F11.
- Alpha vs **Nifty 50** for Indian recommendations, vs **S&P 500**
  for US.
- `outcome ∈ {win, loss, flat}` classification with a small dead-band
  to absorb noise.
- All rows stored in `backtest_results` (FK to
  `advisor_recommendations.id`).

### F15. USD / INR live rate
- Fetched once per process at `config.py` import time via
  yfinance `INR=X` ticker (`market_context.fetch_usd_inr_rate`).
- Used for every $ ↔ ₹ conversion in advisories and the `PORTFOLIO`
  command.
- **Fallback:** `95.31` if yfinance is unreachable. Logged at WARNING.
- Exposed as `config.USD_INR_RATE` (and the legacy alias
  `config.USD_TO_INR`).

## 4. Success metrics

| Metric | Target |
|---|---|
| Alpha win-rate over rolling 30 trading days | **> 50 %** |
| IND premarket cost per run | **< $0.05** |
| US premarket cost per run | **< $0.15** |
| Total monthly cost | **< $2** |
| Missed scheduled cron runs per week | **0** |
| Bot response time (`PORTFOLIO` end-to-end) | **< 3 s** |

## 5. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Upstox Analytics Token expiry (1-year max). | All IND price fetches fail. | yfinance `.NS` fallback inside `upstox_market_data`; STATUS warns when token age > 11 months. |
| NSE public API blocks the VPS IP for FII/DII. | Lose one input signal. | Soft-fail to None; advisory prompt is robust to a missing FII/DII line. |
| Telethon session expiry. | Lose analyst-channel signals. | `TELETHON_SESSION_STRING` for portable headless restore; `scripts/export_telethon_session.py` re-runnable. |
| Polymarket has sparse US-specific signals on quiet days. | F7 returns 0 markets. | Treated as "no signal" — advisory continues without it. |
| yfinance rate-limits the 24-stock US batch. | US advisory degrades. | Single-ticker fetches are serialised with short sleeps; failures degrade per-ticker, not whole run. |
| IndMoney US average prices need manual verification. | Wrong P&L. | One-time `scripts/load_us_portfolio.py` seeds verified avgs; subsequent BUYs update via the bot. |
| Anthropic API outage at cron time. | No advisory that day. | Cron logs the failure to `run_log`; no auto-retry — manual replay is acceptable. |
| Telegram outage. | No delivery. | Recommendations are still persisted to Supabase — replay manually via `delivery/telegram_bot.py` if needed. |

## 6. Data sources summary

| Source | What | Cost | Fallback |
|---|---|---|---|
| Supabase `holdings` / `transactions` | Portfolio state (source of truth) | Supabase free tier | None — required. |
| Upstox v3 LTP | IND live prices | Free with Analytics Token | yfinance `.NS` |
| yfinance | IND fallback prices + US prices + macro (`^GSPC` / `^IXIC` / `^VIX` / `^NSEI` / `^NSEBANK` / `INR=X` / `^TNX`) | Free | None (degrade per ticker) |
| NSE public `fiidiiTradeReact` | IND FII / DII flows | Free | None (skip) |
| Tavily API | Per-holding news semantic search | Free tier | RSS feeds |
| ET Markets / Moneycontrol / Livemint RSS | News supplement | Free | None |
| Telethon (9 channels) | Indian analyst signals | Free | None (skip) |
| Polymarket Gamma API | Prediction-market probabilities | Free, no auth | None (skip) |
| Anthropic API | Claude Sonnet / Haiku | Paid (~$2.27 / mo) | None — required for advisory. |
| Telegram Bot API | Delivery + command intake | Free | None — required. |
