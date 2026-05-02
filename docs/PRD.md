# Product Requirements — Portfolio Advisor

## Problem

A single retail investor in India needs disciplined, context-aware
guidance on what to do with their *actual* Upstox holdings on each
trading day. Generic stock screeners and Telegram analyst signals are
noisy; a personal advisor that knows the user's positions, risk budget,
and capital model is more useful.

## Non-goals

- Not a stock screener. The system never proposes tickers the user
  doesn't already hold or that aren't part of a curated watchlist.
- Not an order-execution system. Every recommendation is reviewed and
  acted on manually by the user.
- Not a financial-advice product for third parties. Strictly personal.

## Users

A single user (the project owner). All credentials, capital limits,
and channel preferences are configured for this one user.

## Functional requirements

### 9:00 AM IST — Pre-market full advisory
Inputs: live Upstox holdings + positions + margins, per-ticker live
prices / VWAP / 52W levels, Tavily news filtered to current holdings,
weighted Telegram channel signals, Nifty / GIFT Nifty / FII-DII context.

Output: per-holding recommendation in `{BUY, ADD, HOLD, EXIT-PARTIAL,
EXIT-FULL, TIGHTEN-SL}` with confidence (1-10), entry, target, stop
loss, reasoning, primary driver. Sized using the leverage map. Filtered
through guardrails. Persisted to `advisor_recommendations`. Posted to
Telegram.

### 12:30 PM IST — Midday leveraged-position check
Inputs: open intraday (MIS) positions only.
Output: per-position `{HOLD, TIGHTEN-SL, EXIT-PARTIAL, EXIT-FULL}` with
optional new SL and one-line reasoning. Persisted to `midday_checks`.

### 3:00 PM IST — EOD exit/hold
Inputs: open positions (MIS + CNC).
Output: per-position decision; MIS forced to `EXIT-FULL` if run after
14:45 IST regardless of model output. Persisted to `eod_recommendations`.

### 4:00 PM IST — Outcome logger
Inputs: today's `advisor_recommendations`.
Output: per-rec `backtest_results` row with return %, alpha vs Nifty,
rupee P&L computed against the configured leverage. Telegram scorecard.

### Weekly — Monday morning scorecard
Aggregate of trailing-7-day `backtest_results`. Optional Haiku-generated
critique (≤120 words). Posted to Telegram.

## Non-functional requirements

- **Reliability**: per-ticker exceptions never crash a run. External
  fetch failures degrade to None / empty rather than aborting.
- **Cost discipline**: each Claude call is logged to `run_log` with
  input/output tokens and estimated USD cost. Daily expected spend at
  steady state is ≈ $0.05-0.10 (one Sonnet + two Haiku + one weekly).
- **Caching**: portfolio snapshots cached 5 min in Supabase; news cached
  1 hour in-process; quotes cached 60 s in-process.
- **Headless ops**: VPS cron runs unattended. Telethon session is a
  string in `.env`, not an interactive prompt.
- **DRY_RUN mode**: the entire pipeline runs without sending Telegram
  or writing to Supabase, for safe dev/test.
- **USE_MOCK_PORTFOLIO mode**: pipeline runs against a fixed mock
  snapshot when Upstox is unavailable, for offline acceptance tests.

## Capital & risk model

(See README.) Codified in `config.LEVERAGE_MAP`, enforced by
`processing.risk_guardrails.apply`.

## Success metrics

- Win rate > 55% over a rolling 30-day window.
- Average alpha vs Nifty > 0.5% per day on traded recommendations.
- Zero unattended-loss-limit breaches per quarter.
- Telegram delivery success ≥ 99% on trading days.

## Out-of-scope (today)

- Options strategies.
- Multi-account / multi-user.
- Auto-execution.
- Backtesting against historical universes (we only backtest what we
  actually recommended).
