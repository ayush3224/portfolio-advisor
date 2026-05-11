"""Upstox v3 market data — live LTP + historical daily candles.

Async-first because the typical caller (premarket pipeline) fetches data for
multiple tickers concurrently. Each function:
  1. Tries the Upstox v3 API with the Analytics Token.
  2. Falls back to yfinance (Yahoo `<TICKER>.NS`) if Upstox fails.
  3. Returns dicts that include a `source` field ("upstox" | "yfinance").

Returning a `source` makes it trivial to verify in tests / logs which path
actually served a given quote — important after token rotation incidents.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import yfinance as yf

import config

log = logging.getLogger(__name__)

_BASE = "https://api.upstox.com/v3"
_TIMEOUT = 15.0


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.UPSTOX_ANALYTICS_TOKEN}",
    }


# ---------------------------------------------------------------------------
# LTP (live last-traded-price)
# ---------------------------------------------------------------------------

async def _upstox_ltp(instrument_keys: list[str]) -> dict[str, dict[str, Any]] | None:
    """POST-like batched fetch via query string; Upstox accepts comma-separated keys."""
    if not config.UPSTOX_ANALYTICS_TOKEN or not instrument_keys:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_BASE}/market-quote/ltp",
                params={"instrument_key": ",".join(instrument_keys)},
                headers=_headers(),
            )
            r.raise_for_status()
            payload = r.json()
            if payload.get("status") != "success":
                log.warning("Upstox v3 LTP non-success: %s", payload)
                return None
            return payload.get("data") or {}
    except Exception as exc:
        log.warning("Upstox v3 LTP failed: %s", exc)
        return None


def _yfinance_quote(ticker: str) -> dict[str, Any] | None:
    """Single-ticker fallback via yfinance (`<TICKER>.NS`)."""
    try:
        t = yf.Ticker(f"{ticker.upper()}.NS")
        hist = t.history(period="2d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        last = hist.iloc[-1]
        return {
            "ltp": round(float(last["Close"]), 2),
            "open": round(float(last["Open"]), 2),
            "high": round(float(last["High"]), 2),
            "low": round(float(last["Low"]), 2),
            "close": round(float(last["Close"]), 2),
            "volume": int(last["Volume"]) if last["Volume"] else None,
            "timestamp": hist.index[-1].isoformat(),
            "source": "yfinance",
        }
    except Exception as exc:
        log.warning("yfinance fallback for %s failed: %s", ticker, exc)
        return None


async def get_live_quote(ticker: str) -> dict[str, Any] | None:
    """Single-ticker live quote. Tries Upstox first, yfinance as fallback."""
    key = config.instrument_key_for(ticker)
    if key:
        data = await _upstox_ltp([key])
        if data:
            entry = next(iter(data.values()), None)
            if entry and entry.get("last_price") is not None:
                log.debug("LTP %s via upstox", ticker)
                return {
                    "ticker": ticker,
                    "ltp": float(entry["last_price"]),
                    "prev_close": entry.get("cp"),
                    "volume": entry.get("volume"),
                    "ltq": entry.get("ltq"),
                    "instrument_key": entry.get("instrument_token"),
                    "timestamp": datetime.utcnow().isoformat(),
                    "source": "upstox",
                }
    # Fallback
    fallback = await asyncio.to_thread(_yfinance_quote, ticker)
    if fallback:
        fallback["ticker"] = ticker
    return fallback


async def get_live_quotes(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Batch live quotes. Upstox is one HTTP call; yfinance fallback runs concurrently."""
    keys = [config.instrument_key_for(t) for t in tickers if config.instrument_key_for(t)]
    key_to_ticker = {config.instrument_key_for(t): t for t in tickers if config.instrument_key_for(t)}

    out: dict[str, dict[str, Any]] = {}
    if keys:
        data = await _upstox_ltp(keys) or {}
        for entry in data.values():
            ikey = entry.get("instrument_token")
            t = key_to_ticker.get(ikey)
            if not t:
                continue
            out[t] = {
                "ticker": t,
                "ltp": float(entry["last_price"]),
                "prev_close": entry.get("cp"),
                "volume": entry.get("volume"),
                "ltq": entry.get("ltq"),
                "instrument_key": ikey,
                "timestamp": datetime.utcnow().isoformat(),
                "source": "upstox",
            }

    missing = [t for t in tickers if t not in out]
    if missing:
        results = await asyncio.gather(*(asyncio.to_thread(_yfinance_quote, t) for t in missing))
        for t, q in zip(missing, results):
            if q:
                q["ticker"] = t
                out[t] = q
    return out


# ---------------------------------------------------------------------------
# Historical candles
# ---------------------------------------------------------------------------

async def _upstox_historical(
    instrument_key: str, unit: str, interval: int, to_d: date, from_d: date,
) -> list[dict[str, Any]] | None:
    if not config.UPSTOX_ANALYTICS_TOKEN:
        return None
    # Upstox encodes the instrument key path segment; the pipe must be %7C.
    encoded = instrument_key.replace("|", "%7C")
    url = (
        f"{_BASE}/historical-candle/{encoded}/{unit}/{interval}/"
        f"{to_d.isoformat()}/{from_d.isoformat()}"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers=_headers())
            r.raise_for_status()
            payload = r.json()
            if payload.get("status") != "success":
                log.warning("Upstox v3 historical non-success: %s", payload)
                return None
            candles = (payload.get("data") or {}).get("candles") or []
            return [
                {
                    "timestamp": c[0],
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": int(c[5]),
                }
                for c in candles
            ]
    except Exception as exc:
        log.warning("Upstox v3 historical failed for %s: %s", instrument_key, exc)
        return None


def _yfinance_history(ticker: str, days: int) -> list[dict[str, Any]] | None:
    try:
        t = yf.Ticker(f"{ticker.upper()}.NS")
        hist = t.history(period=f"{max(days, 1)}d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        rows: list[dict[str, Any]] = []
        for ts, row in hist.iterrows():
            rows.append({
                "timestamp": ts.isoformat(),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"]) if row["Volume"] else 0,
            })
        return rows
    except Exception as exc:
        log.warning("yfinance history fallback for %s failed: %s", ticker, exc)
        return None


async def get_historical_candles(
    ticker: str, interval: str = "day", *, days: int = 60,
) -> list[dict[str, Any]]:
    """Return daily candles for the last `days` sessions, Upstox-first.

    `interval` accepts "day" / "days" interchangeably; intra-day intervals are
    not wired (we currently only need EOD candles for trend context).
    """
    if interval not in ("day", "days"):
        log.warning("get_historical_candles: only 'day' interval supported, got %r", interval)
    to_d = date.today()
    from_d = to_d - timedelta(days=days)

    key = config.instrument_key_for(ticker)
    candles = None
    if key:
        candles = await _upstox_historical(key, "days", 1, to_d, from_d)

    if candles:
        for c in candles:
            c["source"] = "upstox"
        return candles

    fb = await asyncio.to_thread(_yfinance_history, ticker, days)
    if fb:
        for c in fb:
            c["source"] = "yfinance"
    return fb or []
