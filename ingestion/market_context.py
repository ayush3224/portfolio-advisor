"""Macro / market context — Nifty, BankNifty, FII/DII flows, GIFT Nifty proxy.

Primary source for the indices: Upstox (Analytics Token) — same feed the
portfolio and technicals now use, so the Nifty level in the prompt matches the
level the holdings were priced against. yfinance stays as the fallback when the
token is missing or Upstox errors, and still serves USD/INR. NSE/Moneycontrol
scrapers were replaced because the public endpoints either bot-block or return
empty payloads.

Contract: every function returns either a populated dict or — for the index
helpers — a degraded dict with explicit None fields. We never return None
itself for the top-level bundle, so the caller can always render a partial
report.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
import yfinance as yf

import config
from ingestion import upstox_market_data

log = logging.getLogger(__name__)

_UPSTOX_LTP_URL = "https://api.upstox.com/v3/market-quote/ltp"

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


def fetch_nifty_from_upstox() -> dict[str, Any] | None:
    """Nifty 50 + Bank Nifty spot in a single Upstox LTP call.

    The v3 LTP payload carries `last_price` and `cp` (previous close) but no
    net_change field, so the percentage move is derived from those two.
    Returns None when the token is missing or the call fails."""
    if not config.UPSTOX_ANALYTICS_TOKEN:
        return None
    keys = f"{config.INDEX_KEYS['NIFTY50']},{config.INDEX_KEYS['BANKNIFTY']}"
    try:
        resp = requests.get(
            _UPSTOX_LTP_URL,
            params={"instrument_key": keys},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.UPSTOX_ANALYTICS_TOKEN}",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
    except Exception as exc:
        log.warning("Upstox index LTP failed: %s", exc)
        return None

    by_key = {v.get("instrument_token"): v for v in data.values()}
    nifty = by_key.get(config.INDEX_KEYS["NIFTY50"]) or {}
    banknifty = by_key.get(config.INDEX_KEYS["BANKNIFTY"]) or {}
    if not nifty.get("last_price"):
        return None

    def _pct(entry: dict[str, Any]) -> float | None:
        ltp, prev = entry.get("last_price"), entry.get("cp")
        if not ltp or not prev:
            return None
        return round((float(ltp) - float(prev)) / float(prev) * 100, 2)

    return {
        "nifty_spot": nifty.get("last_price"),
        "nifty_prev_close": nifty.get("cp"),
        "nifty_change_pct": _pct(nifty),
        "banknifty_spot": banknifty.get("last_price"),
        "banknifty_prev_close": banknifty.get("cp"),
        "banknifty_change_pct": _pct(banknifty),
        "source": "upstox",
    }


def _upstox_index_block(alias: str, label: str) -> dict[str, Any] | None:
    """Today's OHLC for an index from Upstox daily candles + live LTP.

    Candles give open/high/low and the previous session's close (so gap_pct is
    real, not derived); the LTP call keeps `spot` live during market hours."""
    try:
        df = upstox_market_data.get_historical_candles_upstox(alias, days=10)
        if df is None or len(df) < 2:
            log.warning("%s: Upstox returned <2 candles", label)
            return None
        today, prev = df.iloc[-1], df.iloc[-2]
        prev_close = float(prev["Close"])
        spot = float(today["Close"])
        quote = upstox_market_data.get_live_quote_sync(alias)
        if quote and quote.get("ltp"):
            spot = float(quote["ltp"])
        return {
            "spot": round(spot, 2),
            "prev_close": round(prev_close, 2),
            "open": round(float(today["Open"]), 2),
            "high": round(float(today["High"]), 2),
            "low": round(float(today["Low"]), 2),
            "gap_pct": round((float(today["Open"]) - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
            "change_pct": round((spot - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
            "source": "upstox",
        }
    except Exception as exc:
        log.warning("%s Upstox fetch failed: %s", label, exc)
        return None


def _index_block(alias: str, yf_symbol: str, label: str) -> dict[str, Any] | None:
    """Upstox first, yfinance as fallback — same shape either way."""
    block = _upstox_index_block(alias, label)
    if block:
        return block
    log.info("%s: falling back to yfinance %s", label, yf_symbol)
    block = _yf_index_block(yf_symbol, label)
    if block:
        block["source"] = "yfinance"
    return block


def _yf_index_block(symbol: str, label: str) -> dict[str, Any] | None:
    """Return today vs prev-day OHLC for a Yahoo index symbol."""
    try:
        hist = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
        if hist is None or hist.empty or len(hist) < 2:
            log.warning("%s: yfinance returned <2 rows", label)
            return None
        today = hist.iloc[-1]
        prev = hist.iloc[-2]
        today_open = float(today["Open"])
        today_close = float(today["Close"])
        today_high = float(today["High"])
        today_low = float(today["Low"])
        prev_close = float(prev["Close"])
        gap_pct = (today_open - prev_close) / prev_close * 100 if prev_close else 0.0
        change_pct = (today_close - prev_close) / prev_close * 100 if prev_close else 0.0
        return {
            "spot": round(today_close, 2),
            "prev_close": round(prev_close, 2),
            "open": round(today_open, 2),
            "high": round(today_high, 2),
            "low": round(today_low, 2),
            "gap_pct": round(gap_pct, 2),
            "change_pct": round(change_pct, 2),
        }
    except Exception as exc:
        log.warning("%s yfinance fetch failed: %s", label, exc)
        return None


def fetch_nifty_spot() -> dict[str, Any] | None:
    """Nifty 50 today + prev-day comparison."""
    block = _index_block("NIFTY50", "^NSEI", "Nifty 50")
    if not block:
        return None
    return {
        "nifty_spot": block["spot"],
        "nifty_prev_close": block["prev_close"],
        "nifty_open": block["open"],
        "nifty_high": block["high"],
        "nifty_low": block["low"],
        "nifty_gap_pct": block["gap_pct"],
        "nifty_change_pct": block["change_pct"],
        "nifty_source": block.get("source"),
    }


def fetch_banknifty_spot() -> dict[str, Any] | None:
    """Bank Nifty today + prev-day comparison."""
    block = _index_block("BANKNIFTY", "^NSEBANK", "Bank Nifty")
    if not block:
        return None
    return {
        "banknifty_spot": block["spot"],
        "banknifty_prev_close": block["prev_close"],
        "banknifty_open": block["open"],
        "banknifty_high": block["high"],
        "banknifty_low": block["low"],
        "banknifty_gap_pct": block["gap_pct"],
        "banknifty_change_pct": block["change_pct"],
        "banknifty_source": block.get("source"),
    }


def fetch_sgx_nifty_proxy() -> dict[str, Any] | None:
    """GIFT Nifty proxy. Yahoo doesn't host SGX/GIFT directly, so we use the
    Nifty 50 spot itself as a same-trend proxy and label it explicitly."""
    nifty = _index_block("NIFTY50", "^NSEI", "GIFT Nifty proxy")
    if not nifty:
        return None
    return {
        "proxy": True,
        "source": nifty.get("source", "^NSEI"),
        "last": nifty["spot"],
        "change_pct": nifty["change_pct"],
        "note": "GIFT Nifty proxy — using Nifty 50 spot",
    }


def fetch_fii_dii_flows() -> dict[str, Any]:
    """Latest FII/DII cash flows from NSE's react endpoint.

    NSE requires a session cookie (set by first hitting the homepage) and a
    realistic UA. On failure we return a degraded dict with `available=False`
    so downstream prompt builders can render an honest "data unavailable"
    rather than misleading zeros.
    """
    try:
        with requests.Session() as s:
            s.headers.update(_NSE_HEADERS)
            s.get("https://www.nseindia.com/", timeout=10)
            r = s.get(
                "https://www.nseindia.com/api/fiidiiTradeReact",
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or not data:
                raise ValueError("empty payload")
            # NSE returns one row per category. Match by substring because
            # the exact key has historically wobbled ("FII/FPI", "FII/FPI *", "FII").
            fii: dict[str, Any] = {}
            dii: dict[str, Any] = {}
            for row in data:
                cat = (row.get("category") or "").upper()
                if "FII" in cat or "FPI" in cat:
                    fii = row
                elif "DII" in cat:
                    dii = row
            return {
                "available": True,
                "date": (fii.get("date") or dii.get("date")),
                "fii_net_cr": fii.get("netValue"),
                "dii_net_cr": dii.get("netValue"),
                "fii_buy_cr": fii.get("buyValue"),
                "fii_sell_cr": fii.get("sellValue"),
                "dii_buy_cr": dii.get("buyValue"),
                "dii_sell_cr": dii.get("sellValue"),
            }
    except Exception as exc:
        log.warning("FII/DII fetch failed: %s — falling back to price-action note", exc)
        return {
            "available": False,
            "note": "Institutional flow data unavailable — using price action",
        }


def fetch_usd_inr_rate() -> float:
    """Latest USD/INR spot. Delegates to config, which owns the single fetch
    performed at import time (kept here for callers using the old name)."""
    return config.fetch_usd_inr_rate()


def get_market_context() -> dict[str, Any]:
    """Bundle all market-context signals. Always returns a dict — never None."""
    nifty = fetch_nifty_spot()
    bn = fetch_banknifty_spot()
    return {
        # Flat fields preferred by downstream prompt formatters
        **(nifty or {"nifty_spot": None}),
        **(bn or {"banknifty_spot": None}),
        "gift_nifty": fetch_sgx_nifty_proxy(),
        "fii_dii": fetch_fii_dii_flows(),
    }


# Alias — preferred external name
fetch_market_context = get_market_context
