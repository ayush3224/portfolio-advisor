-- Portfolio Advisor — Supabase schema
-- All timestamps stored in UTC.

create extension if not exists "pgcrypto";

-- 1. Portfolio snapshots (raw state from Upstox at each run)
create table if not exists portfolio_snapshots (
    id                      uuid primary key default gen_random_uuid(),
    snapshot_time           timestamptz not null default now(),
    run_type                text not null,            -- premarket | midday | eod | outcome
    holdings_json           jsonb not null,
    positions_json          jsonb not null,
    total_value             numeric(14, 2),
    available_margin        numeric(14, 2),
    used_margin             numeric(14, 2),
    realised_pnl_today      numeric(14, 2),
    sector_allocation_json  jsonb
);
create index if not exists idx_portfolio_snapshots_time on portfolio_snapshots (snapshot_time desc);
create index if not exists idx_portfolio_snapshots_run_type on portfolio_snapshots (run_type);

-- 2. Pre-market advisor recommendations (Sonnet output)
create table if not exists advisor_recommendations (
    id                  uuid primary key default gen_random_uuid(),
    snapshot_id         uuid references portfolio_snapshots(id) on delete set null,
    ticker              text not null,
    action              text not null,            -- BUY | ADD | HOLD | EXIT-PARTIAL | EXIT-FULL | TIGHTEN-SL
    confidence_score    int  not null,
    entry_price         numeric(12, 2),
    target_price        numeric(12, 2),
    stop_loss           numeric(12, 2),
    leverage_multiplier int,
    capital_deployed    numeric(14, 2),
    shares_qty          int,
    reasoning           text,
    primary_driver      text,
    user_executed       boolean default false,
    created_at          timestamptz not null default now()
);
create index if not exists idx_advisor_recs_created on advisor_recommendations (created_at desc);
create index if not exists idx_advisor_recs_ticker on advisor_recommendations (ticker);

-- 3. Midday checks (12:30 PM intraday review)
create table if not exists midday_checks (
    id              uuid primary key default gen_random_uuid(),
    ticker          text not null,
    entry_price     numeric(12, 2),
    cmp             numeric(12, 2),
    pnl_unrealised  numeric(14, 2),
    vwap_position   text,                         -- above | below | at
    volume_ratio    numeric(8, 2),
    action          text not null,
    new_sl          numeric(12, 2),
    reasoning       text,
    created_at      timestamptz not null default now()
);
create index if not exists idx_midday_created on midday_checks (created_at desc);
create index if not exists idx_midday_ticker on midday_checks (ticker);

-- 4. EOD recommendations (3 PM exit/hold)
create table if not exists eod_recommendations (
    id                uuid primary key default gen_random_uuid(),
    ticker            text not null,
    action            text not null,
    reasoning         text,
    hold_overnight    boolean default false,
    created_at        timestamptz not null default now()
);
create index if not exists idx_eod_created on eod_recommendations (created_at desc);

-- 5. Backtest results (4 PM outcome logger)
create table if not exists backtest_results (
    id                          uuid primary key default gen_random_uuid(),
    recommendation_id           uuid references advisor_recommendations(id) on delete set null,
    ticker                      text not null,
    run_date                    date not null,
    action                      text not null,
    confidence_score            int,
    leverage_multiplier         int,
    price_at_recommendation     numeric(12, 2),
    price_at_close              numeric(12, 2),
    return_pct                  numeric(8, 4),
    nifty_return_pct            numeric(8, 4),
    alpha_pct                   numeric(8, 4),
    outcome                     text,             -- win | loss | flat
    capital_deployed            numeric(14, 2),
    actual_pnl_inr              numeric(14, 2),
    created_at                  timestamptz not null default now()
);
create index if not exists idx_backtest_run_date on backtest_results (run_date desc);
create index if not exists idx_backtest_ticker on backtest_results (ticker);

-- 6. Run log (every scheduler invocation — for cost & error tracking)
create table if not exists run_log (
    id                      uuid primary key default gen_random_uuid(),
    project                 varchar(30) default 'portfolio-advisor',  -- scope cost reports per app
    run_type                text not null,
    started_at              timestamptz not null default now(),
    completed_at            timestamptz,
    status                  text,                 -- success | failed | partial
    model_used              text,
    input_tokens            int,
    output_tokens           int,
    estimated_cost_usd      numeric(10, 6),
    error_message           text
);
-- Idempotent for already-existing run_log tables that pre-date the project column.
alter table run_log add column if not exists project varchar(30) default 'portfolio-advisor';
create index if not exists idx_run_log_started on run_log (started_at desc);
create index if not exists idx_run_log_run_type on run_log (run_type);
create index if not exists idx_run_log_project on run_log (project);
