-- Backtesting system: T+5 / T+15 horizons + bad-run exclusion.
-- Target: Supabase project iykwsuosvxcwipopzhfk (shared with the StockSage app).
-- Every statement is additive and idempotent — no existing column is dropped
-- and no row is deleted.
--
-- Apply in the Supabase SQL editor, or let Claude apply it via MCP.

-- ---------------------------------------------------------------------------
-- 1. backtest_results — T+5 / T+15 horizon columns
-- ---------------------------------------------------------------------------
alter table public.backtest_results
  add column if not exists t5_date              date,
  add column if not exists t5_close             numeric,
  add column if not exists t5_return_pct        numeric,
  add column if not exists t5_benchmark_return  numeric,
  add column if not exists t5_alpha             numeric,
  add column if not exists t5_outcome           text,
  add column if not exists t15_date             date,
  add column if not exists t15_return_pct       numeric,
  add column if not exists t15_benchmark_return numeric,
  add column if not exists t15_alpha            numeric,
  add column if not exists t15_outcome          text,
  add column if not exists excluded             boolean not null default false,
  add column if not exists exclusion_reason     text,
  add column if not exists executed_price       numeric,
  add column if not exists project              text;

-- t15_close, days_tracked, market, user_executed already exist.

-- ---------------------------------------------------------------------------
-- 2. Relax legacy NOT NULLs so an excluded row (no price/score) can be written
-- ---------------------------------------------------------------------------
alter table public.backtest_results alter column confidence_score   drop not null;
alter table public.backtest_results alter column recommended_action drop not null;

-- ---------------------------------------------------------------------------
-- 3. Scope the shared table by project
--    Exactly the 236 rows whose recommendation_id resolves against
--    advisor_recommendations belong to portfolio-advisor; the remaining 637
--    are StockSage's and are left untouched (project stays NULL).
-- ---------------------------------------------------------------------------
update public.backtest_results
   set project = 'portfolio-advisor'
 where project is null
   and recommendation_id in (select id from public.advisor_recommendations);

-- ---------------------------------------------------------------------------
-- 4. Link a recommendation to the run that produced it, so bad runs can be
--    excluded exactly rather than by time window. NULL for the 276 historical
--    rows — those fall back to run-window matching in outcome_tracker.
-- ---------------------------------------------------------------------------
alter table public.advisor_recommendations
  add column if not exists run_id uuid;

-- ---------------------------------------------------------------------------
-- 5. Indexes for the tracker's hot paths
-- ---------------------------------------------------------------------------
create index if not exists backtest_results_rec_id_idx
  on public.backtest_results (recommendation_id);
create index if not exists backtest_results_open_positions_idx
  on public.backtest_results (run_date desc) where excluded = false;
create index if not exists advisor_recommendations_created_idx
  on public.advisor_recommendations (created_at desc);

-- ---------------------------------------------------------------------------
-- 6. OPTIONAL — only if you choose the dedupe path.
--    31 recommendation_ids currently carry 2-3 backtest rows each (74 rows,
--    43 would be deleted, keeping the most recently fetched per rec).
--    Without this, outcome_tracker upserts read-then-write in Python instead
--    of relying on on_conflict.
-- ---------------------------------------------------------------------------
-- delete from public.backtest_results b
--  using (
--    select id, row_number() over (
--             partition by recommendation_id order by fetched_at desc, id desc
--           ) rn
--      from public.backtest_results
--     where recommendation_id is not null
--  ) d
--  where b.id = d.id and d.rn > 1;
--
-- create unique index if not exists backtest_results_rec_id_uniq
--   on public.backtest_results (recommendation_id)
--   where recommendation_id is not null;
