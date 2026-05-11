"""Polymarket prediction-market signals — free, no auth.

We pull the Gamma API's top ~500 active markets in one request (sorted by
liquidity), cache for 2 hours, then filter client-side against context-specific
keyword sets. The Gamma `search=` query parameter is ignored by the API in
practice, so client-side filtering is the reliable path.

Returns a tight per-market dict suitable for both Telegram rendering and
Sonnet prompts. Any network failure returns an empty list — the pipeline
must never crash on a third-party outage.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_CACHE_TTL = 2 * 60 * 60  # 2 hours
_TIMEOUT = 15.0
_FETCH_LIMIT = 500

_cache: tuple[float, list[dict[str, Any]]] | None = None


# Context → list of keyword tokens. Each token is matched case-insensitively
# against the market `question` field; ANY match qualifies the market.
_CONTEXT_QUERIES: dict[str, list[str]] = {
    "india": [
        "RBI", "Modi", "rupee", "INR ", "Pakistan",
        "India GDP", "India inflation", "India election",
        "crude oil", "Brent", "WTI", "OPEC",
    ],
    "us": [
        "Federal Reserve", "Fed rate", "Fed cut", "FOMC", "rate cut",
        "US recession", "S&P 500", "SPX", "SPY", "Nasdaq", "NDX",
        "NVIDIA", "NVDA", "Apple", "AAPL", "Tesla", "TSLA",
        "inflation", "CPI", "US GDP", "Treasury yield", "VIX",
        "tariff", "China invade",
    ],
}

# Tokens that immediately disqualify a market regardless of keyword match.
# Sports / entertainment / crypto dominate Polymarket volume but aren't useful
# for an equity advisor.
_EXCLUDE_TOKENS: list[str] = [
    "premier league", "ipl ", "football", "cricket", "tennis", "nba",
    "mlb", "nfl", "soccer", "match", "fantasy", " vs ", " vs.",
    "gta vi", "album", "movie", "oscar", "song", "rihanna",
    "microstrategy", "bitcoin", "ethereum", "solana",
]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

async def _fetch_all_markets() -> list[dict[str, Any]]:
    """Return cached or freshly-fetched batch of active markets."""
    global _cache
    now = time.time()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_GAMMA_BASE}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": _FETCH_LIMIT,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                log.warning("Polymarket Gamma returned non-list: %r", type(data))
                data = []
            _cache = (now, data)
            return data
    except Exception as exc:
        log.warning("Polymarket fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_yes_probability(market: dict[str, Any]) -> float | None:
    """Return the YES outcome probability as a float in [0,1], or None."""
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if not prices:
            return None
        # Convention: index 0 is the YES (or affirmative) outcome.
        return float(prices[0])
    except Exception:
        return None


def _parse_outcomes(market: dict[str, Any]) -> list[str]:
    raw = market.get("outcomes")
    if not raw:
        return []
    try:
        return json.loads(raw) if isinstance(raw, str) else list(raw)
    except Exception:
        return []


def _closes_within(market: dict[str, Any], days: int) -> bool:
    end = market.get("endDate") or market.get("endDateIso")
    if not end:
        return False
    try:
        dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except Exception:
        return False
    cutoff = datetime.now(timezone.utc) + timedelta(days=days)
    return datetime.now(timezone.utc) <= dt <= cutoff


def _summarise(market: dict[str, Any]) -> dict[str, Any] | None:
    prob = _parse_yes_probability(market)
    if prob is None:
        return None
    volume = float(market.get("volumeNum") or 0.0)
    end_date = market.get("endDate") or market.get("endDateIso")
    closes_iso = end_date.split("T")[0] if end_date else None
    slug = market.get("slug") or ""
    return {
        "question": market.get("question") or "—",
        "probability": round(prob, 4),
        "volume_usd": round(volume),
        "closes": closes_iso,
        "url": f"https://polymarket.com/event/{slug}" if slug else None,
        "id": market.get("id"),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_relevant_markets(
    context: str = "india",
    *,
    min_volume: float = 10_000.0,
    horizon_days: int = 90,
    prob_band: tuple[float, float] = (0.05, 0.95),
    max_results: int = 6,
) -> list[dict[str, Any]]:
    """Return up to `max_results` Polymarket markets relevant to the context.

    Filters:
      - market is active & not closed
      - market volume (USD) > min_volume
      - market closes within horizon_days
      - YES probability is within prob_band (excludes near-certain outcomes)
    """
    queries = _CONTEXT_QUERIES.get(context.lower())
    if not queries:
        log.warning("Unknown polymarket context %r", context)
        return []

    markets = await _fetch_all_markets()
    if not markets:
        return []

    seen_ids: set[Any] = set()
    out: list[dict[str, Any]] = []
    lower_queries = [q.lower() for q in queries]

    # Sort source list by volume descending so the highest-signal markets win
    # when we hit max_results.
    markets_sorted = sorted(markets, key=lambda m: float(m.get("volumeNum") or 0.0), reverse=True)

    for m in markets_sorted:
        q = (m.get("question") or "").lower()
        if any(bad in q for bad in _EXCLUDE_TOKENS):
            continue
        if not any(token in q for token in lower_queries):
            continue
        if float(m.get("volumeNum") or 0.0) < min_volume:
            continue
        if not _closes_within(m, horizon_days):
            continue
        prob = _parse_yes_probability(m)
        if prob is None or not (prob_band[0] <= prob <= prob_band[1]):
            continue
        summary = _summarise(m)
        if summary is None or summary["id"] in seen_ids:
            continue
        seen_ids.add(summary["id"])
        out.append(summary)
        if len(out) >= max_results:
            break
    return out


def format_for_prompt(markets: list[dict[str, Any]]) -> str:
    """Render Polymarket items as a compact block for Sonnet (or Telegram)."""
    if not markets:
        return ""
    lines = ["PREDICTION MARKET SIGNALS (Polymarket):"]
    for m in markets:
        prob_pct = round(m["probability"] * 100)
        vol_usd = m.get("volume_usd") or 0
        if vol_usd >= 1_000_000:
            vol_label = f"${vol_usd/1_000_000:.1f}M"
        elif vol_usd >= 1_000:
            vol_label = f"${vol_usd/1_000:.0f}K"
        else:
            vol_label = f"${vol_usd:.0f}"
        lines.append(f"• {m['question']} → {prob_pct}% YES ({vol_label} volume)")
    return "\n".join(lines)
