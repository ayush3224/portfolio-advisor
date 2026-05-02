"""Upstox portfolio ingestion — holdings, positions, margins.

Uses the Analytics Token (long-lived) as bearer auth against the Upstox v2
HTTP API. Wraps each external call so a single failure can't crash the
caller's pipeline. Cache-first: checks Supabase for a recent snapshot
before hitting Upstox.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

import config
from storage import supabase_client

log = logging.getLogger(__name__)

_UPSTOX_BASE = "https://api.upstox.com/v2"


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.UPSTOX_ANALYTICS_TOKEN}",
    }


def _get(path: str) -> dict[str, Any] | None:
    if not config.UPSTOX_ANALYTICS_TOKEN:
        log.warning("Upstox token missing — skipping %s", path)
        return None
    try:
        r = requests.get(f"{_UPSTOX_BASE}{path}", headers=_headers(), timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.exception("Upstox GET %s failed: %s", path, exc)
        return None


def fetch_holdings() -> list[dict[str, Any]]:
    body = _get("/portfolio/long-term-holdings")
    return (body or {}).get("data", []) or []


def fetch_positions() -> list[dict[str, Any]]:
    body = _get("/portfolio/short-term-positions")
    return (body or {}).get("data", []) or []


def fetch_funds_and_margin() -> dict[str, Any]:
    """Return {available_margin, used_margin, realised_pnl_today, total_value}."""
    body = _get("/user/get-funds-and-margin")
    if not body:
        return {}
    data = body.get("data", {}) or {}
    equity = data.get("equity", {}) or data
    return {
        "available_margin": equity.get("available_margin"),
        "used_margin": equity.get("used_margin"),
        "realised_pnl_today": equity.get("realised", equity.get("realised_pnl", 0)),
    }


def _sector_allocation(holdings: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for h in holdings:
        sector = h.get("instrument_sector") or h.get("sector") or "Unknown"
        value = float(h.get("last_price", 0)) * float(h.get("quantity", 0))
        totals[sector] = totals.get(sector, 0.0) + value
    return totals


def get_portfolio_snapshot(run_type: str, *, use_cache: bool = True) -> dict[str, Any]:
    """Fetch a fresh portfolio snapshot, or return the cached one if fresh.

    Persists the snapshot to portfolio_snapshots (unless DRY_RUN).
    Returns mock data when USE_MOCK_PORTFOLIO=true (offline test mode).
    """
    if config.USE_MOCK_PORTFOLIO:
        from tests.mock_portfolio import get_mock_snapshot
        log.info("USE_MOCK_PORTFOLIO=true — returning mock snapshot")
        return get_mock_snapshot(run_type)
    if use_cache:
        cached = supabase_client.get_cached_portfolio()
        if cached:
            log.info("Using cached portfolio snapshot from %s", cached.get("snapshot_time"))
            return {
                "id": cached["id"],
                "run_type": cached["run_type"],
                "holdings": cached["holdings_json"] or [],
                "positions": cached["positions_json"] or [],
                "total_value": cached.get("total_value"),
                "available_margin": cached.get("available_margin"),
                "used_margin": cached.get("used_margin"),
                "realised_pnl_today": cached.get("realised_pnl_today"),
                "sector_allocation": cached.get("sector_allocation_json") or {},
            }

    holdings = fetch_holdings()
    positions = fetch_positions()
    funds = fetch_funds_and_margin()

    total_value = sum(
        float(h.get("last_price", 0)) * float(h.get("quantity", 0)) for h in holdings
    )
    snapshot = {
        "run_type": run_type,
        "holdings": holdings,
        "positions": positions,
        "total_value": total_value,
        "available_margin": funds.get("available_margin"),
        "used_margin": funds.get("used_margin"),
        "realised_pnl_today": funds.get("realised_pnl_today"),
        "sector_allocation": _sector_allocation(holdings),
    }
    snapshot_id = supabase_client.insert_portfolio_snapshot(snapshot)
    if snapshot_id:
        snapshot["id"] = snapshot_id
    return snapshot


def held_tickers(snapshot: dict[str, Any]) -> list[str]:
    """All distinct tickers held (holdings + positions)."""
    seen: set[str] = set()
    for row in (snapshot.get("holdings") or []) + (snapshot.get("positions") or []):
        sym = row.get("trading_symbol") or row.get("tradingsymbol") or row.get("symbol")
        if sym:
            seen.add(sym)
    return sorted(seen)
