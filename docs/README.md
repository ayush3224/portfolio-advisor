# Portfolio Advisor

Personal AI portfolio advisor for an Indian retail investor on Upstox.
Runs three live cron jobs per trading day, plus a 4 PM outcome logger,
all on a Hostinger VPS. Delivers BUY / ADD / HOLD / EXIT / TIGHTEN-SL
recommendations with explicit position sizing and leverage to a private
Telegram chat. **Execution is always manual — the system never places
orders.**

## Daily flow

| Time (IST) | Run | Model | Purpose |
|---|---|---|---|
| 9:00 AM  | `scheduler/premarket.py`       | Sonnet 4.5 | Full portfolio review, sized recommendations |
| 12:30 PM | `scheduler/midday.py`          | Haiku 4.5  | Intraday leveraged-position triage |
| 3:00 PM  | `scheduler/eod.py`             | Haiku 4.5  | Exit / hold decisions; force MIS exit after 14:45 IST |
| 4:00 PM  | `scheduler/outcome_logger.py`  | none       | Persist day's outcomes; alpha vs Nifty |

A weekly scorecard (`backtest/weekly_scorecard.py`) runs Mondays.

## Setup

```bash
cd /root/portfolio-advisor
pip install -r requirements.txt
cp .env.example .env             # then fill in credentials
python scripts/export_telethon_session.py   # one-time, copy result into .env
psql "$SUPABASE_URL" -f supabase_schema.sql # apply schema once
```

## Local testing

```bash
# import-only smoke test
python -c "from storage.supabase_client import get_client; print(get_client() is not None)"

# Full pipeline against mock portfolio (no Upstox needed)
DRY_RUN=true USE_MOCK_PORTFOLIO=true PYTHONPATH=. python scheduler/premarket.py
```

`DRY_RUN=true` skips Telegram sends and DB writes.
`USE_MOCK_PORTFOLIO=true` returns a fixed 5-holding portfolio from
`tests/mock_portfolio.py` instead of hitting Upstox.

## Capital model

Anchored on a daily ₹10,000 capital budget with a ₹2,000 hard loss stop.

| Confidence | Capital fraction | Leverage | Notional |
|---|---|---|---|
| 9-10 | 40% (₹4,000) | 3x | ₹12,000 |
| 7-8  | 30% (₹3,000) | 2x | ₹6,000  |
| 6    | 20% (₹2,000) | 1x | ₹2,000  |
| <6   | skip         | —  | —       |

Other guardrails:
- Max 3 leveraged positions/day.
- Max single position ≤ 20% of portfolio value.
- Max sector concentration ≤ 30% of portfolio value.
- Max total leverage exposure ≤ 60% of portfolio value.
- Realised loss > ₹2,000 today → conservative mode (EXIT-only).
- High-severity event tomorrow → halve all sizes.
- Medium-severity event tomorrow → reduce leverage by 1x.
- After 14:45 IST → all MIS positions forced to EXIT-FULL.

## Repo layout

```
config.py                # env loader + capital model + INSTRUMENT_KEYS + KNOWN_EVENTS
scheduler/               # 4 cron entry points
ingestion/               # Upstox, Tavily news, Telethon, market context
processing/              # context build, sizing, guardrails
analysis/                # Claude client + prompts (premarket / midday / eod)
storage/                 # Supabase wrapper
delivery/                # Telegram formatters + sender
backtest/                # outcome tracker + weekly scorecard
scripts/                 # one-off helpers (Telethon session export)
tests/                   # mock_portfolio
supabase_schema.sql      # 6-table DDL
```

See `PRD.md` for product requirements and `ERD.md` for the data model.
