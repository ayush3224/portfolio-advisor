"""Macro / market context — Nifty, BankNifty, FII/DII flows, GIFT Nifty proxy.

Primary source: yfinance (reliable, no auth). NSE/Moneycontrol scrapers replaced
because the public endpoints either bot-block or return empty payloads.

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

log = logging.getLogger(__name__)

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


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
    block = _yf_index_block("^NSEI", "Nifty 50")
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
    }


def fetch_banknifty_spot() -> dict[str, Any] | None:
    """Bank Nifty today + prev-day comparison."""
    block = _yf_index_block("^NSEBANK", "Bank Nifty")
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
    }


def fetch_sgx_nifty_proxy() -> dict[str, Any] | None:
    """GIFT Nifty proxy. Yahoo doesn't host SGX/GIFT directly, so we use the
    Nifty 50 spot itself as a same-trend proxy and label it explicitly."""
    nifty = _yf_index_block("^NSEI", "GIFT Nifty proxy")
    if not nifty:
        return None
    return {
        "proxy": True,
        "source": "^NSEI",
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
