# Data model — portfolio-advisor

All tables live in the project's Supabase Postgres instance. The DDL is
in `supabase_schema.sql` and is **idempotent** — every `create table`
is `if not exists` and every column addition is guarded by
`add column if not exists`. Every timestamp column is `timestamptz` and
stored in **UTC**.

## Tables

There are 8 tables:

1. `holdings` — current portfolio state (source of truth)
2. `transactions` — immutable BUY / SELL log
3. `portfolio_snapshots` — raw state captured at each run
4. `advisor_recommendations` — Sonnet 4.5 output (9 AM IND + 7:30 PM US)
5. `midday_checks` — legacy 12:30 PM rows (no longer written)
6. `eod_recommendations` — Haiku 4.5 output at 3 PM
7. `backtest_results` — outcome / alpha rows, one per recommendation
8. `run_log` — every scheduler invocation for cost + error tracking

---

### 1. `holdings`

Current portfolio. Mutated **only** by the Telegram bot
(`bot/portfolio_manager.py`). Indian + US holdings co-exist; the
`market` column splits them.

| Column | Type | Constraint / Default | Notes |
|---|---|---|---|
| `id` | `uuid` | PK, `default gen_random_uuid()` | |
| `ticker` | `varchar(20)` | NOT NULL, UNIQUE | NSE symbol for IND, Nasdaq/NYSE symbol for US. |
| `exchange` | `varchar(10)` | default `'NSE'` | `'NSE'` / `'BSE'` / `'NASDAQ'` / `'NYSE'`. |
| `market` | `varchar(5)` | default `'IND'`, **check** `market in ('IND','US')` | Routing key for prices and currency. |
| `currency` | `varchar(5)` | default `'INR'` | `'INR'` for IND, `'USD'` for US. |
| `quantity` | `numeric(14,4)` | NOT NULL, **check** `quantity > 0` | Fractional shares supported (e.g. NVDA 12.07). |
| `average_price` | `numeric(12,4)` | NOT NULL | Weighted-avg cost in `currency`. |
| `current_price` | `numeric(12,4)` | nullable | Last LTP written by the price refresh. |
| `current_value` | `numeric(12,4)` | nullable | `quantity × current_price`. |
| `unrealised_pnl` | `numeric(12,4)` | nullable | `current_value − quantity × average_price`. |
| `unrealised_pnl_pct` | `numeric(8,4)` | nullable | percent. |
| `date_added` | `timestamptz` | default `now()` | First time this row was inserted. |
| `last_updated` | `timestamptz` | default `now()` | Touched on every refresh. |
| `is_active` | `boolean` | default `true` | Soft-delete: `SELL` to zero flips this to `false`. |
| `notes` | `text` | nullable | Free-form user note. |

**Indexes:** `idx_holdings_ticker(ticker)`, `idx_holdings_active(is_active)`, `idx_holdings_market(market)`.

---

### 2. `transactions`

Immutable BUY / SELL log. Every Telegram `BUY` or `SELL` appends a row.
Drives realised P&L.

| Column | Type | Constraint / Default | Notes |
|---|---|---|---|
| `id` | `uuid` | PK, `default gen_random_uuid()` | |
| `ticker` | `varchar(20)` | NOT NULL | |
| `action` | `varchar(10)` | NOT NULL, **check** `action in ('BUY','SELL')` | |
| `quantity` | `numeric(14,4)` | NOT NULL, **check** `quantity > 0` | |
| `price` | `numeric(12,4)` | NOT NULL | Trade price in the holding's currency. |
| `total_value` | `numeric(12,4)` | nullable | `quantity × price`. |
| `realised_pnl` | `numeric(12,4)` | nullable | Only populated on SELL: `quantity × (price − avg_price_at_trade)`. |
| `avg_price_at_trade` | `numeric(12,4)` | nullable | Snapshot of `holdings.average_price` at trade time. |
| `executed_at` | `timestamptz` | default `now()` | |
| `notes` | `text` | nullable | |

**Indexes:** `idx_transactions_ticker(ticker)`.

**No FK** to `holdings(ticker)` — `transactions` survives soft-deletes
and ticker renames.

---

### 3. `portfolio_snapshots`

Raw state captured at each scheduler run. Used to anchor each
`advisor_recommendations` row to the exact market context it was
generated against.

| Column | Type | Constraint / Default | Notes |
|---|---|---|---|
| `id` | `uuid` | PK, `default gen_random_uuid()` | |
| `snapshot_time` | `timestamptz` | NOT NULL, default `now()` | |
| `run_type` | `text` | NOT NULL | `'premarket'` / `'midday'` / `'eod'` / `'outcome'` / `'us_premarket'` / `'weekly'`. |
| `holdings_json` | `jsonb` | NOT NULL | Full enriched holdings list. |
| `positions_json` | `jsonb` | NOT NULL | Open positions (intraday — empty in current product). |
| `total_value` | `numeric(14,2)` | nullable | Portfolio value at snapshot. |
| `available_margin` | `numeric(14,2)` | nullable | Upstox-reported; nullable for US. |
| `used_margin` | `numeric(14,2)` | nullable | |
| `realised_pnl_today` | `numeric(14,2)` | nullable | |
| `sector_allocation_json` | `jsonb` | nullable | `{sector: weight_pct}`. |
| `project` | `text` | default `'portfolio-advisor'` | Scopes rows for hosts that run multiple apps. |

**Indexes:** `idx_portfolio_snapshots_time(snapshot_time desc)`, `idx_portfolio_snapshots_run_type(run_type)`.

---

### 4. `advisor_recommendations`

Output of the 9 AM IND advisory and the 7:30 PM US advisory.

| Column | Type | Constraint / Default | Notes |
|---|---|---|---|
| `id` | `uuid` | PK, `default gen_random_uuid()` | |
| `snapshot_id` | `uuid` | **FK** → `portfolio_snapshots(id)` `on delete set null` | |
| `ticker` | `text` | NOT NULL | |
| `action` | `text` | NOT NULL | One of: `BUY` / `ADD` / `HOLD` / `EXIT-PARTIAL` / `EXIT-FULL` / `TIGHTEN-SL` / `BUY-MOMENTUM` / `BUY-EVENT` / `PARTIAL-EXIT` / `FULL-EXIT` / `WATCH`. |
| `confidence_score` | `int` | NOT NULL | 1–10. |
| `entry_price` | `numeric(12,2)` | nullable | |
| `target_price` | `numeric(12,2)` | nullable | |
| `stop_loss` | `numeric(12,2)` | nullable | |
| `leverage_multiplier` | `int` | nullable | 1 / 2 / 3, or NULL for US (no leverage). |
| `capital_deployed` | `numeric(14,2)` | nullable | In INR even for US recs (converted at `USD_INR_RATE`). |
| `shares_qty` | `int` | nullable | |
| `reasoning` | `text` | nullable | Sonnet's prose justification. |
| `primary_driver` | `text` | nullable | The dominant factor: `news` / `momentum` / `event` / `signal` / `macro`. |
| `user_executed` | `boolean` | default `false` | Flipped by the user (no automation). |
| `paper_trade` | `boolean` | default `false` | Set when `PAPER_TRADING=true`. |
| `created_at` | `timestamptz` | NOT NULL, default `now()` | |

**Indexes:** `idx_advisor_recs_created(created_at desc)`, `idx_advisor_recs_ticker(ticker)`, `idx_advisor_recs_paper_trade(paper_trade)`.

---

### 5. `midday_checks` (legacy)

Output of the previous 12:30 PM midday MIS-check run. **No longer
written** — the midday slot was removed from the schedule. The table
is retained for historical rows.

| Column | Type | Constraint / Default | Notes |
|---|---|---|---|
| `id` | `uuid` | PK, `default gen_random_uuid()` | |
| `ticker` | `text` | NOT NULL | |
| `entry_price` | `numeric(12,2)` | nullable | |
| `cmp` | `numeric(12,2)` | nullable | Current market price at check. |
| `pnl_unrealised` | `numeric(14,2)` | nullable | |
| `vwap_position` | `text` | nullable | `'above'` / `'below'` / `'at'`. |
| `volume_ratio` | `numeric(8,2)` | nullable | Today vs avg. |
| `action` | `text` | NOT NULL | |
| `new_sl` | `numeric(12,2)` | nullable | |
| `reasoning` | `text` | nullable | |
| `created_at` | `timestamptz` | NOT NULL, default `now()` | |

**Indexes:** `idx_midday_created(created_at desc)`, `idx_midday_ticker(ticker)`.

---

### 6. `eod_recommendations`

Output of the 3 PM Haiku exit/hold check on Indian CNC holdings.

| Column | Type | Constraint / Default | Notes |
|---|---|---|---|
| `id` | `uuid` | PK, `default gen_random_uuid()` | |
| `ticker` | `text` | NOT NULL | |
| `action` | `text` | NOT NULL | `'HOLD'` / `'EXIT-PARTIAL'` / `'EXIT-FULL'` / `'TIGHTEN-SL'`. |
| `reasoning` | `text` | nullable | |
| `hold_overnight` | `boolean` | default `false` | True if Claude judges the position safe to carry. |
| `created_at` | `timestamptz` | NOT NULL, default `now()` | |

**Indexes:** `idx_eod_created(created_at desc)`.

---

### 7. `backtest_results`

One row per recommendation, written by the 4 PM outcome logger.

| Column | Type | Constraint / Default | Notes |
|---|---|---|---|
| `id` | `uuid` | PK, `default gen_random_uuid()` | |
| `recommendation_id` | `uuid` | **FK** → `advisor_recommendations(id)` `on delete set null` | |
| `ticker` | `text` | NOT NULL | |
| `run_date` | `date` | NOT NULL | Trading date. |
| `action` | `text` | NOT NULL | Mirrors the rec's action. |
| `confidence_score` | `int` | nullable | |
| `leverage_multiplier` | `int` | nullable | |
| `price_at_recommendation` | `numeric(12,2)` | nullable | LTP at the time of the rec. |
| `price_at_close` | `numeric(12,2)` | nullable | Closing print. |
| `return_pct` | `numeric(8,4)` | nullable | Position-level. |
| `nifty_return_pct` | `numeric(8,4)` | nullable | Benchmark return (Nifty for IND, S&P 500 for US — same column reused). |
| `alpha_pct` | `numeric(8,4)` | nullable | `return_pct − benchmark_return_pct`. |
| `outcome` | `text` | nullable | `'win'` / `'loss'` / `'flat'`. |
| `capital_deployed` | `numeric(14,2)` | nullable | |
| `actual_pnl_inr` | `numeric(14,2)` | nullable | Always in INR (US converted at the day's USD/INR). |
| `created_at` | `timestamptz` | NOT NULL, default `now()` | |

**Indexes:** `idx_backtest_run_date(run_date desc)`, `idx_backtest_ticker(ticker)`.

---

### 8. `run_log`

Every scheduler invocation. Drives the cost report attached to the
4 PM outcome message.

| Column | Type | Constraint / Default | Notes |
|---|---|---|---|
| `id` | `uuid` | PK, `default gen_random_uuid()` | |
| `project` | `varchar(30)` | default `'portfolio-advisor'` | Scopes cost reports per app on shared hosts. |
| `run_type` | `text` | NOT NULL | `'premarket'` / `'eod'` / `'us_premarket'` / `'outcome'` / `'weekly_scorecard'`. |
| `started_at` | `timestamptz` | NOT NULL, default `now()` | |
| `completed_at` | `timestamptz` | nullable | |
| `status` | `text` | nullable | `'success'` / `'failed'` / `'partial'`. |
| `model_used` | `text` | nullable | e.g. `'claude-sonnet-4-5'`. |
| `input_tokens` | `int` | nullable | |
| `output_tokens` | `int` | nullable | |
| `estimated_cost_usd` | `numeric(10,6)` | nullable | Computed in `analysis/claude_client.py` from public per-token prices. |
| `error_message` | `text` | nullable | |

**Indexes:** `idx_run_log_started(started_at desc)`, `idx_run_log_run_type(run_type)`, `idx_run_log_project(project)`.

---

## Relationships

```
                    ┌──────────────────────────┐
                    │   portfolio_snapshots    │
                    ├──────────────────────────┤
                    │ id (uuid, PK)            │◀──────┐
                    │ snapshot_time            │       │
                    │ run_type                 │       │
                    │ holdings_json            │       │
                    │ positions_json           │       │
                    │ total_value              │       │
                    │ available_margin         │       │
                    │ used_margin              │       │
                    │ realised_pnl_today       │       │
                    │ sector_allocation_json   │       │
                    │ project                  │       │
                    └──────────────────────────┘       │
                                                       │ snapshot_id (FK,
                                                       │  on delete set null)
                    ┌─────────────────────────────┐    │
                    │  advisor_recommendations    │    │
                    ├─────────────────────────────┤    │
                    │ id (uuid, PK)               │◀─┐ │
                    │ snapshot_id ────────────────┼──┼─┘
                    │ ticker, action              │  │
                    │ confidence_score            │  │
                    │ entry_price / target / SL   │  │
                    │ leverage_multiplier         │  │
                    │ capital_deployed, shares    │  │
                    │ reasoning, primary_driver   │  │
                    │ user_executed, paper_trade  │  │
                    │ created_at                  │  │
                    └─────────────────────────────┘  │
                                                     │ recommendation_id (FK,
                                                     │  on delete set null)
                    ┌──────────────────────────┐     │
                    │     backtest_results     │     │
                    ├──────────────────────────┤     │
                    │ id (uuid, PK)            │     │
                    │ recommendation_id ───────┼─────┘
                    │ ticker, run_date         │
                    │ action                   │
                    │ price_at_recommendation  │
                    │ price_at_close           │
                    │ return_pct               │
                    │ nifty_return_pct         │
                    │ alpha_pct, outcome       │
                    │ capital_deployed         │
                    │ actual_pnl_inr           │
                    │ created_at               │
                    └──────────────────────────┘

  ┌───────────────────┐    ┌──────────────────────┐    ┌──────────────────┐
  │     holdings      │    │    transactions      │    │     run_log      │
  ├───────────────────┤    ├──────────────────────┤    ├──────────────────┤
  │ id (uuid, PK)     │    │ id (uuid, PK)        │    │ id (uuid, PK)    │
  │ ticker (UNIQUE)   │    │ ticker               │    │ project          │
  │ market in IND/US  │    │ action in BUY/SELL   │    │ run_type         │
  │ currency          │    │ quantity, price      │    │ started_at       │
  │ quantity (14,4)   │    │ total_value          │    │ completed_at     │
  │ average_price     │    │ realised_pnl         │    │ status           │
  │ current_price     │    │ avg_price_at_trade   │    │ model_used       │
  │ current_value     │    │ executed_at          │    │ input_tokens     │
  │ unrealised_pnl    │    │ notes                │    │ output_tokens    │
  │ unrealised_pnl_pct│    └──────────────────────┘    │ estimated_cost_usd│
  │ date_added        │       (no FK to holdings —     │ error_message    │
  │ last_updated      │        survives soft-deletes)  └──────────────────┘
  │ is_active         │
  │ notes             │
  └───────────────────┘

  ┌──────────────────┐    ┌──────────────────────┐
  │  midday_checks   │    │ eod_recommendations  │
  ├──────────────────┤    ├──────────────────────┤
  │ id (uuid, PK)    │    │ id (uuid, PK)        │
  │ ticker           │    │ ticker               │
  │ entry_price      │    │ action               │
  │ cmp              │    │ reasoning            │
  │ pnl_unrealised   │    │ hold_overnight       │
  │ vwap_position    │    │ created_at           │
  │ volume_ratio     │    └──────────────────────┘
  │ action           │
  │ new_sl           │
  │ reasoning        │
  │ created_at       │
  └──────────────────┘
```

### Foreign-key summary

| Child | Column | Parent | On delete |
|---|---|---|---|
| `advisor_recommendations` | `snapshot_id` | `portfolio_snapshots(id)` | `set null` |
| `backtest_results` | `recommendation_id` | `advisor_recommendations(id)` | `set null` |

`holdings` and `transactions` are deliberately **not** linked by FK —
`transactions` is an append-only event log that must survive ticker
renames and `is_active=false` soft-deletes. The bot uses
`transactions.ticker` as a string join key only.

### Index summary

| Table | Index | Column(s) |
|---|---|---|
| `holdings` | `idx_holdings_ticker` | `ticker` |
| `holdings` | `idx_holdings_active` | `is_active` |
| `holdings` | `idx_holdings_market` | `market` |
| `transactions` | `idx_transactions_ticker` | `ticker` |
| `portfolio_snapshots` | `idx_portfolio_snapshots_time` | `snapshot_time desc` |
| `portfolio_snapshots` | `idx_portfolio_snapshots_run_type` | `run_type` |
| `advisor_recommendations` | `idx_advisor_recs_created` | `created_at desc` |
| `advisor_recommendations` | `idx_advisor_recs_ticker` | `ticker` |
| `advisor_recommendations` | `idx_advisor_recs_paper_trade` | `paper_trade` |
| `midday_checks` | `idx_midday_created` | `created_at desc` |
| `midday_checks` | `idx_midday_ticker` | `ticker` |
| `eod_recommendations` | `idx_eod_created` | `created_at desc` |
| `backtest_results` | `idx_backtest_run_date` | `run_date desc` |
| `backtest_results` | `idx_backtest_ticker` | `ticker` |
| `run_log` | `idx_run_log_started` | `started_at desc` |
| `run_log` | `idx_run_log_run_type` | `run_type` |
| `run_log` | `idx_run_log_project` | `project` |

## Notes on the IND / US split

- `holdings.market` is the **only** routing key for prices and currency.
  - `market='IND'` → Upstox v3 LTP (instrument key from
    `config.INSTRUMENT_KEYS`), fallback yfinance `.NS`, currency `'INR'`.
  - `market='US'` → yfinance, currency `'USD'`.
- Every cross-market aggregation (e.g. `PORTFOLIO`) converts USD →
  INR using `config.USD_INR_RATE`, fetched at process start from
  `yfinance INR=X`. There is **no** historical FX rate stored — all
  cross-currency totals are snapshot-time conversions.
- `advisor_recommendations.capital_deployed` and
  `backtest_results.actual_pnl_inr` are always **INR**, even for US
  recs, so dashboards can sum without branching.
- `backtest_results.nifty_return_pct` is the column name historically,
  but for US-market rows it stores the **S&P 500** return on `run_date`
  (the benchmark is implicit from the rec's `market`).
- Storage growth is trivial (a few rows per trading day across all
  tables); no partitioning or retention policy is needed.

## Lifecycle (per trading day)

1. **09:00 IST premarket** → 1 `portfolio_snapshots` row + N
   `advisor_recommendations` rows (`snapshot_id` set) + 1 `run_log` row.
2. **15:00 IST EOD** → 1 `portfolio_snapshots` row + N
   `eod_recommendations` rows + 1 `run_log` row.
3. **16:00 IST outcome logger** → N `backtest_results` rows
   (`recommendation_id` set) — no Claude call. Optional `run_log`
   status row.
4. **19:30 IST US premarket** → 1 `portfolio_snapshots` row + N
   `advisor_recommendations` rows + 1 `run_log` row.
5. **Sunday 20:00 IST weekly review** → 0 new state rows, 1 `run_log` row.

Telegram `BUY` / `SELL` from the bot can happen at any time and
generate `transactions` rows + mutations to `holdings`. They do not
touch the run-driven tables.
