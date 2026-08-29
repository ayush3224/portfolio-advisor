-- Track Tavily API usage per run so the free-tier credit burn is visible in
-- run_log alongside Claude token cost. Written by scheduler.premarket /
-- scheduler.us_premarket via supabase_client.record_tavily_calls().
alter table run_log add column if not exists tavily_calls int;
