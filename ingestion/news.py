"""News ingestion — Tavily (paid, semantic) + RSS (free, broad coverage).

Both sources are filtered to the user's holdings. Per-process in-memory cache
keyed on (ticker, query); cache TTL from config.NEWS_CACHE_TTL (1 hour).
RSS feeds are pulled once per process and reused across tickers.

Volume is deliberately tight: news was the single largest block in the Claude
prompts (untrimmed Tavily snippets across 5 results per ticker), so each
holding gets at most TAVILY_RESULTS_PER_TICKER semantic hits plus
RSS_ITEMS_PER_TICKER headlines, each summary cut to SUMMARY_WORD_LIMIT words.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import feedparser
import requests

import config

log = logging.getLogger(__name__)

try:
    from tavily import TavilyClient
except Exception:  # pragma: no cover — SDK optional at import time
    TavilyClient = None  # type: ignore[assignment]


# Volume caps — see module docstring. Raising these raises per-run token cost
# roughly linearly, so change them together with a token measurement.
TAVILY_RESULTS_PER_TICKER = 2
RSS_ITEMS_PER_TICKER = 2
SUMMARY_WORD_LIMIT = 80

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_rss_cache: tuple[float, list[dict[str, Any]]] | None = None
_client: Any | None = None


# ---------------------------------------------------------------------------
# RSS — free, broad coverage; complements Tavily semantic search
# ---------------------------------------------------------------------------

RSS_FEEDS: list[str] = [
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/marketoutlook.xml",
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://www.livemint.com/rss/markets",
    "https://www.business-standard.com/rss/markets-106.rss",
]

# Per-ticker company-name aliases for substring matching in RSS titles/snippets.
# Default fallback is the ticker itself + lowercase.
_TICKER_ALIASES: dict[str, list[str]] = {
    "RELIANCE":   ["reliance", "ril"],
    "TCS":        ["tcs", "tata consultancy"],
    "HDFCBANK":   ["hdfc bank", "hdfcbank"],
    "ICICIBANK":  ["icici bank", "icicibank"],
    "INFY":       ["infosys", "infy"],
    "SBIN":       ["sbi ", "state bank of india", "sbin"],
    "AXISBANK":   ["axis bank", "axisbank"],
    "BAJFINANCE": ["bajaj finance", "bajfinance"],
    "WIPRO":      ["wipro"],
    "MARUTI":     ["maruti"],
    "TATAMOTORS": ["tata motors", "tatamotors"],
    "ADANIENT":   ["adani enterprises", "adanient"],
    "SUNPHARMA":  ["sun pharma", "sunpharma"],
    "LTIM":       ["ltimindtree", "ltim"],
    "TITAN":      ["titan"],
    "ONGC":       ["ongc", "oil and natural gas"],
    "NTPC":       ["ntpc"],
    "COALINDIA":  ["coal india", "coalindia"],
    "TATASTEEL":  ["tata steel", "tatasteel"],
    "BPCL":       ["bpcl", "bharat petroleum"],
    "HINDALCO":   ["hindalco"],
    "TECHM":      ["tech mahindra", "techm"],
    "DIVISLAB":   ["divis lab", "divi's lab", "divislab"],
    "PIIND":      ["pi industries", "piind"],
    "TRENT":      ["trent"],
    "ETERNAL":    ["zomato", "eternal"],
    "BHARTIARTL": ["bharti airtel", "bhartiartl", "airtel"],
    "KOTAKBANK":  ["kotak", "kotakbank"],
    "INDUSINDBK": ["indusind", "indusindbk"],
    "HDFCLIFE":   ["hdfc life", "hdfclife"],
    "BAJAJFINSV": ["bajaj finserv", "bajajfinsv"],
    "M&M":        ["mahindra", "m&m"],
    "GRASIM":     ["grasim"],
    "TATAPOWER":  ["tata power", "tatapower"],
    "NESTLEIND":  ["nestle", "nestleind"],
    "ULTRACEMCO": ["ultratech", "ultracemco"],
    "ADANIPORTS": ["adani ports", "adaniports"],
}


def trim_summary(text: str | None, *, words: int = SUMMARY_WORD_LIMIT) -> str:
    """Strip HTML and clip to `words` words.

    RSS summaries arrive as HTML fragments and Tavily snippets run to several
    hundred words; neither adds signal past the lede for a trading decision."""
    if not text:
        return ""
    clean = _WS_RE.sub(" ", _TAG_RE.sub(" ", str(text))).strip()
    parts = clean.split(" ")
    if len(parts) <= words:
        return clean
    return " ".join(parts[:words]) + "…"


def _aliases_for(ticker: str) -> list[str]:
    return _TICKER_ALIASES.get(ticker.upper()) or [ticker.lower()]


def _fetch_feed(url: str) -> list[dict[str, Any]]:
    """Try feedparser; on zero entries fall back to a raw GET so a feed served
    with a strict UA whitelist (e.g. some CDNs) still yields entries."""
    try:
        parsed = feedparser.parse(url)
        if parsed.entries:
            return parsed.entries  # feedparser entry objects act dict-like
    except Exception as exc:
        log.warning("RSS feedparser %s failed: %s", url, exc)
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        parsed = feedparser.parse(r.text)
        return parsed.entries or []
    except Exception as exc:
        log.warning("RSS raw fetch %s failed: %s", url, exc)
        return []


def _all_rss_entries() -> list[dict[str, Any]]:
    """Aggregate entries from all configured feeds, cached for NEWS_CACHE_TTL."""
    global _rss_cache
    now = time.time()
    if _rss_cache and now - _rss_cache[0] < config.NEWS_CACHE_TTL:
        return _rss_cache[1]

    out: list[dict[str, Any]] = []
    for url in RSS_FEEDS:
        for entry in _fetch_feed(url):
            out.append({
                "title": entry.get("title") or "",
                "url": entry.get("link") or "",
                "snippet": trim_summary(entry.get("summary") or entry.get("description")),
                "published": entry.get("published") or entry.get("updated"),
                "source": url,
            })
    _rss_cache = (now, out)
    return out


def fetch_rss_headlines(
    tickers: list[str],
    *,
    max_per_ticker: int = RSS_ITEMS_PER_TICKER,
) -> dict[str, list[dict[str, Any]]]:
    """Return up to max_per_ticker RSS items per ticker, filtered by alias match."""
    entries = _all_rss_entries()
    out: dict[str, list[dict[str, Any]]] = {}
    for t in tickers:
        aliases = _aliases_for(t)
        hits: list[dict[str, Any]] = []
        for e in entries:
            hay = ((e.get("title") or "") + " " + (e.get("snippet") or "")).lower()
            if any(re.search(rf"\b{re.escape(a)}\b", hay) for a in aliases):
                hits.append(e)
                if len(hits) >= max_per_ticker:
                    break
        out[t] = hits
    return out


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


def search_for_ticker(ticker: str, *, max_results: int = TAVILY_RESULTS_PER_TICKER) -> list[dict[str, Any]]:
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
                "snippet": trim_summary(r.get("content")),
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


def news_for_holdings(
    tickers: list[str], *, max_results: int = TAVILY_RESULTS_PER_TICKER,
) -> dict[str, list[dict[str, Any]]]:
    """Per-ticker news map combining Tavily (semantic) + RSS (broad).
    Per-ticker exceptions are caught — never crash the run."""
    try:
        rss_map = fetch_rss_headlines(tickers, max_per_ticker=RSS_ITEMS_PER_TICKER)
    except Exception as exc:
        log.warning("RSS aggregation failed: %s", exc)
        rss_map = {}

    out: dict[str, list[dict[str, Any]]] = {}
    for t in tickers:
        items: list[dict[str, Any]] = []
        try:
            items = search_for_ticker(t, max_results=max_results)
        except Exception as exc:
            log.exception("news_for_holdings (tavily) failed for %s: %s", t, exc)
        seen_urls = {it.get("url") for it in items if it.get("url")}
        for r in rss_map.get(t, []):
            if r.get("url") and r["url"] not in seen_urls:
                items.append(r)
                seen_urls.add(r["url"])
        out[t] = items
    return out


# Alias — preferred external name
fetch_news = news_for_holdings
