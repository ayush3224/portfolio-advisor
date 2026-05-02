"""News ingestion via Tavily — filtered to current portfolio holdings only.

Per-process in-memory cache keyed on (ticker, query); cache TTL from
config.NEWS_CACHE_TTL (1 hour). Within a single cron run this is the right
scope — across runs the cache is naturally cold, which is fine because
news is cron-cycle-fresh anyway.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import config

log = logging.getLogger(__name__)

try:
    from tavily import TavilyClient
except Exception:  # pragma: no cover — SDK optional at import time
    TavilyClient = None  # type: ignore[assignment]


_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_client: Any | None = None


def _get_client() -> Any | None:
    global _client
    if _client is not None:
        return _client
    if not config.TAVILY_API_KEY or TavilyClient is None:
        log.warning("Tavily client unavailable — news fetch will return []")
        return None
    _client = TavilyClient(api_key=config.TAVILY_API_KEY)
    return _client


def _cache_key(ticker: str, query: str) -> str:
    return f"{ticker}::{query}"


def search_for_ticker(ticker: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    """Return up to max_results news items for a single ticker. Cached for 1h."""
    query = f"{ticker} stock NSE India news today"
    key = _cache_key(ticker, query)
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < config.NEWS_CACHE_TTL:
        return cached[1]

    client = _get_client()
    if client is None:
        return []
    try:
        resp = client.search(
            query=query,
            search_depth="basic",
            topic="news",
            max_results=max_results,
            days=2,
        )
        items = [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "snippet": r.get("content"),
                "published": r.get("published_date"),
                "score": r.get("score"),
            }
            for r in (resp.get("results") or [])
        ]
        _cache[key] = (now, items)
        return items
    except Exception as exc:
        log.warning("Tavily search failed for %s: %s", ticker, exc)
        return []


def news_for_holdings(tickers: list[str], *, max_results: int = 5) -> dict[str, list[dict[str, Any]]]:
    """Per-ticker news map. Per-ticker exceptions are caught — never crash the run."""
    out: dict[str, list[dict[str, Any]]] = {}
    for t in tickers:
        try:
            out[t] = search_for_ticker(t, max_results=max_results)
        except Exception as exc:
            log.exception("news_for_holdings failed for %s: %s", t, exc)
            out[t] = []
    return out


# Alias — preferred external name
fetch_news = news_for_holdings
