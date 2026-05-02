# Data model — Portfolio Advisor

All tables live in the project's Supabase Postgres instance. Schema
DDL is in `supabase_schema.sql`. Every timestamp column is `timestamptz`
and stored in UTC.

```
                       ┌────────────────────────┐
                       │  portfolio_snapshots   │
                       ├────────────────────────┤
                       │ id (uuid, PK)          │◀─┐
                       │ snapshot_time          │  │
                       │ run_type               │  │
                       │ holdings_json          │  │
                       │ positions_json         │  │
                       │ total_value            │  │
                       │ available_margin       │  │
                       │ used_margin            │  │
                       │ realised_pnl_today     │  │
                       │ sector_allocation_json │  │
                       └────────────────────────┘  │
                                                   │ snapshot_id (FK)
                       ┌─────────────────────────┐ │
                       │ advisor_recommendations │ │
                       ├─────────────────────────┤ │
                       │ id (uuid, PK)           │◀┼┐
                       │ snapshot_id ────────────┼─┘│
                       │ ticker                  │  │
                       │ action                  │  │
                       │ confidence_score        │  │
                       │ entry_price             │  │
                       │ target_price            │  │
                       │ stop_loss               │  │
                       │ leverage_multiplier     │  │
                       │ capital_deployed        │  │
                       │ shares_qty              │  │
                       │ reasoning               │  │
                       │ primary_driver          │  │
                       │ user_executed           │  │
                       │ created_at              │  │
                       └─────────────────────────┘  │
                                                    │ recommendation_id (FK)
                       ┌────────────────────────┐   │
                       │   backtest_results     │   │
                       ├────────────────────────┤   │
                       │ id (uuid, PK)          │   │
                       │ recommendation_id ─────┼───┘
                       │ ticker                 │
                       │ run_date               │
                       │ action                 │
                       │ confidence_score       │
                       │ leverage_multiplier    │
                       │ price_at_recommendation│
                       │ price_at_close         │
                       │ return_pct             │
                       │ nifty_return_pct       │
                       │ alpha_pct              │
                       │ outcome  (win|loss|flat)│
                       │ capital_deployed       │
                       │ actual_pnl_inr         │
                       │ created_at             │
                       └────────────────────────┘

    ┌──────────────────┐    ┌──────────────────────┐    ┌──────────────────┐
    │  midday_checks   │    │ eod_recommendations  │    │     run_log      │
    ├──────────────────┤    ├──────────────────────┤    ├──────────────────┤
    │ id (uuid, PK)    │    │ id (uuid, PK)        │    │ id (uuid, PK)    │
    │ ticker           │    │ ticker               │    │ run_type         │
    │ entry_price      │    │ action               │    │ started_at       │
    │ cmp              │    │ reasoning            │    │ completed_at     │
    │ pnl_unrealised   │    │ hold_overnight       │    │ status           │
    │ vwap_position    │    │ created_at           │    │ model_used       │
    │ volume_ratio     │    └──────────────────────┘    │ input_tokens     │
    │ action           │                                │ output_tokens    │
    │ new_sl           │                                │ estimated_cost_usd│
    │ reasoning        │                                │ error_message    │
    │ created_at       │                                └──────────────────┘
    └──────────────────┘
```

## Relationship notes

- `advisor_recommendations.snapshot_id` → `portfolio_snapshots.id`
  (`on delete set null`). Lets us trace any rec back to the exact
  market state it was made against.
- `backtest_results.recommendation_id` → `advisor_recommendations.id`
  (`on delete set null`). One-to-one in practice (one outcome per rec
  per day).
- `midday_checks`, `eod_recommendations`, and `run_log` are independent
  log tables — no foreign keys.

## Indexes

Each table has an index on its primary timestamp column (descending)
plus an index on `ticker` where useful, so dashboards / weekly
scorecards can range-scan recent activity efficiently.

## Lifecycle

1. Pre-market run → insert one `portfolio_snapshots` row, then N
   `advisor_recommendations` rows pointing to it, then a `run_log` row.
2. Midday → one `portfolio_snapshots` row (cache miss > 5 min) + N
   `midday_checks` + a `run_log` row.
3. EOD → one `portfolio_snapshots` row + N `eod_recommendations` + a
   `run_log` row.
4. Outcome logger → N `backtest_results` rows (no Claude call, no
   `run_log` Claude entry, but optionally a status row).

A trading day produces ≈ 1-5 advisor recs + 0-3 midday checks + 0-5
EOD recs + matching backtest rows. Storage growth is trivial; no
partitioning needed.
