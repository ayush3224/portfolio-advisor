"""Upstox live prices — quotes, VWAP, 52-week levels.

Lightweight in-memory cache per process (cron jobs are short-lived, so this
keeps multiple lookups inside a single run from hammering the API).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
import yfinance as yf

import config

log = logging.getLogger(__name__)

_UPSTOX_BASE = "https://api.upstox.com/v2"
_QUOTE_TTL = 60  # seconds — within a run, treat quote as fresh for 1 min
_quote_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _ticker_from_instrument_key(instrument_key: str) -> str | None:
    """Reverse-lookup ticker symbol from instrument_key (for yfinance fallback)."""
    for t, k in config.INSTRUMENT_KEYS.items():
        if k == instrument_key:
            return t
    return None


def _yf_last_close(ticker: str) -> float | None:
    try:
        hist = yf.Ticker(f"{ticker.upper()}.NS").history(period="2d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception as exc:
        log.warning("yfinance fallback for %s failed: %s", ticker, exc)
        return None


def _yf_price_block(ticker: str) -> dict[str, Any] | None:
    try:
        t = yf.Ticker(f"{ticker.upper()}.NS")
        hist = t.history(period="2d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        last = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) >= 2 else None
        # 52-week range — single extra call; ok within fallback path
        try:
            wk_hist = t.history(period="1y", auto_adjust=False)
            wk52_high = float(wk_hist["High"].max()) if not wk_hist.empty else None
            wk52_low = float(wk_hist["Low"].min()) if not wk_hist.empty else None
        except Exception:
            wk52_high = wk52_low = None
        return {
            "cmp": round(float(last["Close"]), 2),
            "open": round(float(last["Open"]), 2),
            "high": round(float(last["High"]), 2),
            "low": round(float(last["Low"]), 2),
            "close_prev": round(float(prev["Close"]), 2) if prev is not None else None,
            "vwap": None,
            "volume": int(last["Volume"]) if last["Volume"] else None,
            "week_52_high": round(wk52_high, 2) if wk52_high else None,
            "week_52_low": round(wk52_low, 2) if wk52_low else None,
            "source": "yfinance",
        }
    except Exception as exc:
        log.warning("yfinance enrich for %s failed: %s", ticker, exc)
        return None


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
    if q:
        ltp = q.get("last_price") or q.get("ltp")
        if ltp is not None:
            log.debug("cmp_for %s via upstox", instrument_key)
            return ltp
    # yfinance fallback — recover the ticker from the reverse map and try Yahoo.
    ticker = _ticker_from_instrument_key(instrument_key)
    if ticker:
        price = _yf_last_close(ticker)
        if price is not None:
            log.info("cmp_for %s via yfinance fallback", instrument_key)
            return price
    return None


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
        if q.get("last_price") or q.get("ltp"):
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
                "source": "upstox",
            }
    except Exception as exc:
        log.exception("enrich_price_block upstox path failed for %s: %s", instrument_key, exc)
    # yfinance fallback
    ticker = _ticker_from_instrument_key(instrument_key)
    if ticker:
        fb = _yf_price_block(ticker)
        if fb:
            log.info("enrich_price_block %s via yfinance fallback", instrument_key)
            return fb
    return {}
