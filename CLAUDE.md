# Portfolio Advisor — Personal AI Trading Advisor

## Project overview
A personal AI-powered portfolio advisor for an Indian retail investor
using Upstox as the primary broker. Runs 3 times daily on a Hostinger
VPS (Linux) via cron. Delivers actionable BUY/SELL/HOLD recommendations
with position sizing to a private Telegram chat. Execution is always
manual — the system never places orders.

**Trading product: CNC (delivery / swing). No leverage, no intraday MIS.**
Every recommendation is sized 1x against capital_deployed only.

## Core philosophy
This is a portfolio advisor, not a stock screener.
It knows what you actually own and advises on YOUR positions.
It does not scan a universe of 50 stocks — it analyses your
real holdings from Upstox and tells you what to do with them.

## Daily schedule
- 9:00 AM IST  → Pre-market full portfolio advisory (Claude Sonnet 4.5)
- (12:30 PM IST midday MIS check is retired — CNC swing has no intraday positions)
- 3:00 PM IST  → EOD exit/hold decisions (Claude Haiku 3.5)
- 4:00 PM IST  → Outcome logger — no Claude, free

## Models
- 9:00 AM run: claude-sonnet-4-5 (complex multi-factor reasoning)
- 12:30 PM run: claude-haiku-4-5-20251001 (simple, rule-based)
- 3:00 PM run: claude-haiku-4-5-20251001 (simple, rule-based)
- Weekly scorecard: claude-haiku-4-5-20251001

## Capital model
- Daily capital budget: ₹10,000
- Daily loss hard stop: ₹2,000 (20% of daily capital)
- Max positions per day: 3
- Position sizing by confidence (1x CNC — no leverage):
  Conf 9-10: 40% of daily capital (₹4,000)
  Conf 7-8:  30% of daily capital (₹3,000)
  Conf 6:    20% of daily capital (₹2,000)
  Conf <6:   Skip — do not recommend

## Position rules
- Max single position: 20% of portfolio value
- Max sector concentration: 30% of portfolio value
- Max 3 new BUY/ADD positions per day
- If today realised loss > ₹2,000: no new BUY/ADD positions (EXIT still allowed)
- Day before major event (RBI, budget, earnings): halve new-entry position sizes

## Action types
- BUY: Fresh entry into a stock not currently held
- ADD: Add to an existing position
- HOLD: No action — maintain current position
- EXIT-PARTIAL: Sell a portion of current holding
- EXIT-FULL: Exit entire position
- TIGHTEN-SL: Keep position but move stop loss up

## Data sources
- Upstox Analytics Token (primary): live holdings, positions,
  VWAP, 52W high/low, available margin, used margin,
  today's realised P&L, sector allocation
- Tavily API: news filtered to current holdings only
- Telegram channels (Telethon): analyst signals on holdings
- Nifty futures, SGX Nifty, FII/DII data: market context

## Telegram channels (weighted)
- religarebrokingofficial: weight=3
- ICICIdirectMARKETSstocks: weight=3
- equity99: weight=3
- nooreshtech: weight=3
- STOCKGAINERSS: weight=2
- aakankshatrading: weight=1

Three handles were removed 2026-08-29 because they no longer
resolve on Telegram. `config.TELEGRAM_CHANNELS` is the source of truth.

## Project structure
portfolio-advisor/
├── CLAUDE.md
├── .env.example
├── requirements.txt
├── config.py
├── scheduler/
│   ├── __init__.py
│   ├── premarket.py        # 9:00 AM orchestrator
│   ├── midday.py           # 12:30 PM orchestrator
│   ├── eod.py              # 3:00 PM orchestrator
│   └── outcome_logger.py   # 4:00 PM — no Claude
├── ingestion/
│   ├── __init__.py
│   ├── upstox_portfolio.py # Holdings, positions, margins
│   ├── upstox_prices.py    # Live quotes, VWAP, 52W levels
│   ├── news.py             # Tavily — filtered to holdings
│   ├── market_context.py   # Nifty futures, FII/DII, SGX
│   └── telegram_scraper.py # Telethon channel reader
├── processing/
│   ├── __init__.py
│   ├── portfolio_context.py # Builds per-holding context block
│   ├── technicals.py       # Tier 1 indicators: RSI, EMA 20/50, volume, VWAP
│   ├── position_sizer.py   # Confidence → capital fraction → qty
│   └── risk_guardrails.py  # Enforce all capital rules
├── analysis/
│   ├── __init__.py
│   ├── claude_client.py    # API calls with model selection
│   ├── premarket_prompt.py # Sonnet prompt for 9 AM
│   ├── midday_prompt.py    # Haiku prompt for 12:30 PM
│   └── eod_prompt.py       # Haiku prompt for 3 PM
├── storage/
│   ├── __init__.py
│   └── supabase_client.py  # All DB reads and writes
├── delivery/
│   ├── __init__.py
│   └── telegram_bot.py     # Formatter + sender
├── backtest/
│   ├── __init__.py
│   ├── outcome_tracker.py  # Daily alpha tracking
│   └── weekly_scorecard.py # Monday report
└── docs/
    ├── README.md
    ├── PRD.md
    └── ERD.md

## Supabase tables
- portfolio_snapshots (id, snapshot_time, run_type, holdings_json,
  positions_json, total_value, available_margin, used_margin,
  realised_pnl_today, sector_allocation_json)
- advisor_recommendations (id, snapshot_id, ticker, action,
  confidence_score, entry_price, target_price, stop_loss,
  leverage_multiplier, capital_deployed, shares_qty, reasoning,
  primary_driver, user_executed, created_at)
- midday_checks (id, ticker, entry_price, cmp, pnl_unrealised,
  vwap_position, volume_ratio, action, new_sl, reasoning,
  created_at)
- eod_recommendations (id, ticker, action, reasoning,
  hold_overnight, created_at)
- backtest_results (id, recommendation_id, ticker, run_date,
  action, confidence_score, leverage_multiplier,
  price_at_recommendation, price_at_close, return_pct,
  nifty_return_pct, alpha_pct, outcome, capital_deployed,
  actual_pnl_inr, created_at)
- run_log (id, run_type, started_at, completed_at, status,
  model_used, input_tokens, output_tokens, estimated_cost_usd,
  error_message)

## Environment variables
ANTHROPIC_API_KEY
UPSTOX_ANALYTICS_TOKEN
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TAVILY_API_KEY
TELETHON_API_ID
TELETHON_API_HASH
TELETHON_PHONE
SUPABASE_URL
SUPABASE_KEY
DAILY_CAPITAL_BUDGET=10000
DAILY_LOSS_LIMIT=2000

## Important rules — always follow
- Never place orders — recommendation only, execution is manual
- Always check Supabase cache before external API calls
- News cache staleness: 1 hour (intraday moves fast)
- Portfolio cache staleness: 5 minutes
- Catch all per-ticker exceptions — never crash the full pipeline
- Log all token usage and cost to run_log after every Claude call
- If daily loss limit hit: switch all subsequent runs to conservative
  mode (no new BUY/ADD; EXIT recommendations only)
- All timestamps in UTC in Supabase
- DRY_RUN=true flag skips Telegram send and DB writes
