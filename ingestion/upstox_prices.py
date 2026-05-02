"""Upstox live prices — quotes, VWAP, 52-week levels.

Lightweight in-memory cache per process (cron jobs are short-lived, so this
keeps multiple lookups inside a single run from hammering the API).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

import config

log = logging.getLogger(__name__)

_UPSTOX_BASE = "https://api.upstox.com/v2"
_QUOTE_TTL = 60  # seconds — within a run, treat quote as fresh for 1 min
_quote_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.UPSTOX_ANALYTICS_TOKEN}",
    }


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not config.UPSTOX_ANALYTICS_TOKEN:
        return None
    try:
        r = requests.get(f"{_UPSTOX_BASE}{path}", headers=_headers(), params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.exception("Upstox GET %s failed: %s", path, exc)
        return None


def fetch_quote(instrument_key: str) -> dict[str, Any] | None:
    """Return latest quote for an Upstox instrument key (e.g. NSE_EQ|INE002A01018)."""
    now = time.time()
    cached = _quote_cache.get(instrument_key)
    if cached and now - cached[0] < _QUOTE_TTL:
        return cached[1]
    body = _get("/market-quote/quotes", params={"instrument_key": instrument_key})
    if not body:
        return None
    data = (body.get("data") or {})
    if not data:
        return None
    quote = next(iter(data.values()))
    _quote_cache[instrument_key] = (now, quote)
    return quote


def cmp_for(instrument_key: str) -> float | None:
    if config.PAPER_TRADING or config.USE_MOCK_PORTFOLIO:
        from tests.mock_portfolio import get_mock_cmp
        return get_mock_cmp(instrument_key)
    q = fetch_quote(instrument_key)
    if not q:
        return None
    return q.get("last_price") or q.get("ltp")


def vwap_for(instrument_key: str) -> float | None:
    q = fetch_quote(instrument_key)
    if not q:
        return None
    ohlc = q.get("ohlc") or {}
    return q.get("average_price") or q.get("vwap") or ohlc.get("vwap")


def fetch_52w_levels(instrument_key: str) -> tuple[float | None, float | None]:
    """Return (52W high, 52W low) from quote payload if present."""
    q = fetch_quote(instrument_key)
    if not q:
        return None, None
    return q.get("week_52_high") or q.get("upper_circuit_limit"), q.get("week_52_low") or q.get("lower_circuit_limit")


def enrich_price_block(instrument_key: str) -> dict[str, Any]:
    """One-shot bundle for use in per-holding context."""
    if config.PAPER_TRADING or config.USE_MOCK_PORTFOLIO:
        from tests.mock_portfolio import get_mock_price_block
        return get_mock_price_block(instrument_key)
    try:
        q = fetch_quote(instrument_key) or {}
        ohlc = q.get("ohlc") or {}
        return {
            "cmp": q.get("last_price") or q.get("ltp"),
            "open": ohlc.get("open"),
            "high": ohlc.get("high"),
            "low": ohlc.get("low"),
            "close_prev": ohlc.get("close"),
            "vwap": q.get("average_price") or q.get("vwap"),
            "volume": q.get("volume"),
            "week_52_high": q.get("week_52_high"),
            "week_52_low": q.get("week_52_low"),
        }
    except Exception as exc:
        log.exception("enrich_price_block failed for %s: %s", instrument_key, exc)
        return {}
