"""Portfolio ingestion — Supabase `holdings` table is the source of truth.

We no longer call Upstox's `/portfolio/long-term-holdings` or `/user/get-funds-and-margin`
endpoints (they require an OAuth-rotated access token, not the Analytics Token).
Holdings are managed by the user via the Telegram bot (bot/portfolio_manager.py)
and live prices are enriched per-run via ingestion.upstox_market_data (Upstox v3
LTP with yfinance fallback).

Public surface kept stable for callers in scheduler/{premarket,midday,eod}.py:
  - get_portfolio_snapshot(run_type) → dict (with persisted Supabase row id)
  - held_tickers(snapshot)            → list[str]
  - fetch_portfolio()                 → raw portfolio dict (no DB insert)
  - fetch_analytics_data()            → alias for fetch_portfolio
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any

import config
from ingestion import upstox_market_data
from storage import supabase_client


def _run_async(coro: Any) -> Any:
    """Run an awaitable from sync code, regardless of whether an outer event
    loop is already running. When called from inside an async handler (the
    Telegram bot) we hand the coroutine to a worker thread that owns its own
    loop — `asyncio.run` would otherwise raise."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()

log = logging.getLogger(__name__)


def _empty_portfolio() -> dict[str, Any]:
    return {
        "holdings": [],
        "positions": [],
        "total_value": 0.0,
        "total_cost": 0.0,
        "total_pnl": 0.0,
        "total_pnl_pct": 0.0,
        "available_margin": 0.0,
        "used_margin": 0.0,
        "realised_pnl_today": 0.0,
        "sector_allocation": {},
    }


def _read_active_holdings() -> list[dict[str, Any]]:
    client = supabase_client.get_client()
    if client is None:
        log.warning("Supabase unavailable — returning empty holdings")
        return []
    try:
        res = (
            client.table("holdings")
            .select("*")
            .eq("is_active", True)
            .order("ticker")
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.exception("holdings read failed: %s", exc)
        return []


def _enrich_with_quotes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach live LTP to each holding row via upstox_market_data (batched).
    Falls through to yfinance if Upstox is unavailable. Per-ticker exceptions
    never crash the pipeline."""
    if not rows:
        return []
    tickers = [r["ticker"] for r in rows if r.get("ticker")]
    quotes: dict[str, dict[str, Any]] = {}
    try:
        quotes = _run_async(upstox_market_data.get_live_quotes(tickers))
    except Exception as exc:
        log.warning("batch quote fetch failed (%s) — using avg_price fallback", exc)

    enriched: list[dict[str, Any]] = []
    for r in rows:
        ticker = r["ticker"]
        qty = int(r["quantity"])
        avg_price = float(r["average_price"])
        q = quotes.get(ticker) or {}
        current_price = float(q.get("ltp") or avg_price)
        current_value = round(qty * current_price, 2)
        cost = qty * avg_price
        unrealised_pnl = round((current_price - avg_price) * qty, 2)
        unrealised_pnl_pct = (
            round((current_price - avg_price) / avg_price * 100, 4) if avg_price else 0.0
        )
        enriched.append({
            # Fields consumed by processing/portfolio_context.build_holding_block
            "trading_symbol": ticker,
            "instrument_token": config.instrument_key_for(ticker),
            "quantity": qty,
            "average_price": avg_price,
            "last_price": current_price,
            "instrument_sector": r.get("notes") or None,  # sector not tracked yet
            # Convenience fields for bot replies / dashboards
            "ticker": ticker,
            "exchange": r.get("exchange") or "NSE",
            "current_price": current_price,
            "current_value": current_value,
            "cost_value": round(cost, 2),
            "unrealised_pnl": unrealised_pnl,
            "unrealised_pnl_pct": unrealised_pnl_pct,
            "quote_source": q.get("source"),
            "is_active": bool(r.get("is_active", True)),
            "id": r.get("id"),
            "date_added": r.get("date_added"),
            "last_updated": r.get("last_updated"),
        })
    return enriched


def fetch_portfolio() -> dict[str, Any]:
    """Read active holdings from Supabase, enrich with live prices, compute totals."""
    rows = _read_active_holdings()
    if not rows:
        log.warning("No holdings in database. Add positions via Telegram bot.")
        return _empty_portfolio()

    enriched = _enrich_with_quotes(rows)
    total_cost = sum(h["cost_value"] for h in enriched)
    total_value = sum(h["current_value"] for h in enriched)
    total_pnl = round(total_value - total_cost, 2)
    total_pnl_pct = round((total_pnl / total_cost * 100), 4) if total_cost else 0.0

    return {
        "holdings": enriched,
        "positions": [],
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "available_margin": 0.0,
        "used_margin": 0.0,
        "realised_pnl_today": 0.0,
        "sector_allocation": {},
    }


# Alias kept for any future caller that prefers this naming.
fetch_analytics_data = fetch_portfolio


def get_portfolio_snapshot(run_type: str, *, use_cache: bool = True) -> dict[str, Any]:
    """Build a snapshot for the scheduler, persist to portfolio_snapshots.

    Cache: when a fresh row exists (< PORTFOLIO_CACHE_TTL) we reuse it so
    consecutive runs in the same window don't re-quote Upstox.
    """
    if use_cache:
        cached = supabase_client.get_cached_portfolio()
        if cached:
            log.info("Using cached portfolio snapshot from %s", cached.get("snapshot_time"))
            return {
                "id": cached["id"],
                "run_type": cached["run_type"],
                "holdings": cached.get("holdings_json") or [],
                "positions": cached.get("positions_json") or [],
                "total_value": cached.get("total_value"),
                "available_margin": cached.get("available_margin"),
                "used_margin": cached.get("used_margin"),
                "realised_pnl_today": cached.get("realised_pnl_today"),
                "sector_allocation": cached.get("sector_allocation_json") or {},
            }

    portfolio = fetch_portfolio()
    snapshot = {
        "run_type": run_type,
        "holdings": portfolio["holdings"],
        "positions": portfolio["positions"],
        "total_value": portfolio["total_value"],
        "available_margin": portfolio["available_margin"],
        "used_margin": portfolio["used_margin"],
        "realised_pnl_today": portfolio["realised_pnl_today"],
        "sector_allocation": portfolio["sector_allocation"],
    }
    snapshot_id = supabase_client.insert_portfolio_snapshot(snapshot)
    if snapshot_id:
        snapshot["id"] = snapshot_id
    return snapshot


def held_tickers(snapshot: dict[str, Any]) -> list[str]:
    """All distinct tickers held (holdings + positions)."""
    seen: set[str] = set()
    for row in (snapshot.get("holdings") or []) + (snapshot.get("positions") or []):
        sym = row.get("trading_symbol") or row.get("ticker") or row.get("symbol")
        if sym:
            seen.add(sym)
    return sorted(seen)
