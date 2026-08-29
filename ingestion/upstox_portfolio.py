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
import math
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
    """Attach live LTP to each holding row via upstox_market_data (batched
    for IND tickers). US tickers fall through to yfinance. Per-ticker
    exceptions never crash the pipeline."""
    if not rows:
        return []

    ind_tickers = [r["ticker"] for r in rows if (r.get("market") or "IND") == "IND" and r.get("ticker")]
    us_rows = [r for r in rows if (r.get("market") or "IND") == "US"]

    ind_quotes: dict[str, dict[str, Any]] = {}
    if ind_tickers:
        # One batched Upstox LTP call covers every mapped IND ticker; anything
        # without an instrument key silently drops to yfinance inside
        # get_live_quotes, so name those explicitly here — an unmapped ticker
        # is a config gap worth fixing, not a permanent fallback.
        unmapped = [t for t in ind_tickers if not config.instrument_key_for(t)]
        if unmapped:
            log.warning("no Upstox instrument key for %s — falling back to yfinance; "
                        "add them to config.INSTRUMENT_KEYS", ", ".join(sorted(unmapped)))
        try:
            ind_quotes = _run_async(upstox_market_data.get_live_quotes(ind_tickers))
        except Exception as exc:
            log.warning("batch quote (IND) failed (%s) — using avg_price fallback", exc)
        else:
            via_upstox = sum(1 for t in ind_tickers if (ind_quotes.get(t) or {}).get("source") == "upstox")
            log.info("IND quotes: %d/%d via upstox (1 batched call)", via_upstox, len(ind_tickers))

    us_quotes: dict[str, dict[str, Any]] = {}
    if us_rows:
        # US names never go to Upstox — it carries no US instruments. The
        # shared helper reads fast_info rather than the last daily candle,
        # which is NaN before the US session prints one (i.e. all morning IST).
        for r in us_rows:
            t = r["ticker"]
            try:
                q = upstox_market_data.get_us_quote(t)
            except Exception as exc:
                log.warning("yfinance quote for %s failed: %s", t, exc)
                continue
            if q and q.get("ltp") is not None:
                us_quotes[t] = q
            else:
                log.warning("no live US price for %s — showing average price", t)

    enriched: list[dict[str, Any]] = []
    for r in rows:
        ticker = r["ticker"]
        qty = float(r["quantity"])
        avg_price = float(r["average_price"])
        market = (r.get("market") or "IND").upper()
        currency = r.get("currency") or ("USD" if market == "US" else "INR")
        q = (us_quotes if market == "US" else ind_quotes).get(ticker) or {}
        ltp = q.get("ltp")
        # NaN is truthy in Python, so an explicit isnan check is needed before falling back.
        if ltp is None or (isinstance(ltp, float) and math.isnan(ltp)):
            ltp = None
        current_price = float(ltp if ltp is not None else avg_price)
        prev_close = q.get("prev_close")
        try:
            prev_close = float(prev_close) if prev_close is not None else None
            if prev_close is not None and math.isnan(prev_close):
                prev_close = None
        except (TypeError, ValueError):
            prev_close = None
        # Today's move, used by ingestion.news to decide whether a holding is
        # quiet enough to skip the (metered) Tavily call.
        day_change_pct = (
            round((current_price - prev_close) / prev_close * 100, 4)
            if prev_close and ltp is not None else None
        )
        current_value = round(qty * current_price, 2)
        cost = qty * avg_price
        unrealised_pnl = round((current_price - avg_price) * qty, 2)
        unrealised_pnl_pct = (
            round((current_price - avg_price) / avg_price * 100, 4) if avg_price else 0.0
        )
        # For US holdings expose an INR-converted P&L using the live rate so
        # downstream consumers (Telegram formatters, weekly recap) can render
        # ₹ totals without each one re-fetching the rate.
        if market == "US":
            unrealised_pnl_inr = round(unrealised_pnl * config.USD_INR_RATE, 2)
            current_value_inr = round(current_value * config.USD_INR_RATE, 2)
        else:
            unrealised_pnl_inr = unrealised_pnl
            current_value_inr = current_value
        enriched.append({
            "trading_symbol": ticker,
            "instrument_token": config.instrument_key_for(ticker) if market == "IND" else None,
            "quantity": qty,
            "average_price": avg_price,
            "last_price": current_price,
            "instrument_sector": r.get("notes") or None,
            "ticker": ticker,
            "exchange": r.get("exchange") or ("NSE" if market == "IND" else "US"),
            "market": market,
            "currency": currency,
            "current_price": current_price,
            "prev_close": prev_close,
            "day_change_pct": day_change_pct,
            "current_value": current_value,
            "current_value_inr": current_value_inr,
            "cost_value": round(cost, 2),
            "unrealised_pnl": unrealised_pnl,
            "unrealised_pnl_inr": unrealised_pnl_inr,
            "unrealised_pnl_pct": unrealised_pnl_pct,
            "quote_source": q.get("source"),
            "is_active": bool(r.get("is_active", True)),
            "id": r.get("id"),
            "date_added": r.get("date_added"),
            "last_updated": r.get("last_updated"),
        })
    return enriched


def fetch_portfolio(market: str | None = None) -> dict[str, Any]:
    """Read active holdings from Supabase, enrich with live prices, compute totals.

    `market`: optional filter — 'IND' or 'US' — for callers that want only one
    region (e.g. the IND premarket scheduler ignores US holdings).
    """
    rows = _read_active_holdings()
    if market:
        market_u = market.upper()
        rows = [r for r in rows if (r.get("market") or "IND").upper() == market_u]
    if not rows:
        log.warning("No holdings in database (market=%s). Add positions via Telegram bot.", market or "ALL")
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
