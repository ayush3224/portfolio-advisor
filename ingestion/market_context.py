"""Macro / market context — Nifty futures, SGX Nifty proxy, FII/DII flows.

Pulls from public sources (NSE / Moneycontrol / Investing-style RSS) with
graceful fall-back. Each function returns None on failure rather than
raising, so the caller can still build a report with partial data.
"""

from __future__ import annotations

import logging
from typing import Any

import feedparser
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; portfolio-advisor/1.0)"


def _safe_get(url: str, timeout: int = 10) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as exc:
        log.warning("market_context fetch %s failed: %s", url, exc)
        return None


def fetch_nifty_spot() -> dict[str, Any] | None:
    """Latest Nifty 50 spot from NSE public quote API."""
    url = "https://www.nseindia.com/api/quote-equity?symbol=NIFTY%2050"
    body = _safe_get(url)
    if not body:
        return None
    try:
        import json
        data = json.loads(body)
        info = data.get("priceInfo", {})
        return {
            "last": info.get("lastPrice"),
            "change": info.get("change"),
            "pct_change": info.get("pChange"),
        }
    except Exception as exc:
        log.warning("nifty parse failed: %s", exc)
        return None


def fetch_sgx_nifty_proxy() -> dict[str, Any] | None:
    """SGX Nifty proxy via investing.com RSS / GIFT Nifty quote.

    SGX Nifty migrated to GIFT Nifty in mid-2023 — we just pull GIFT Nifty
    headlines to get a directional read pre-market.
    """
    feed = feedparser.parse("https://www.moneycontrol.com/rss/gift-nifty.xml")
    if not feed.entries:
        return None
    headline = feed.entries[0]
    return {"headline": headline.get("title"), "link": headline.get("link")}


def fetch_fii_dii_flows() -> dict[str, Any] | None:
    """Latest FII/DII cash market flows scraped from Moneycontrol."""
    body = _safe_get("https://www.moneycontrol.com/stocks/marketstats/fii_dii_activity/index.php")
    if not body:
        return None
    try:
        soup = BeautifulSoup(body, "html.parser")
        table = soup.find("table")
        if not table:
            return None
        first_row = table.find_all("tr")[1].find_all("td")
        return {
            "date": first_row[0].get_text(strip=True),
            "fii_net": first_row[3].get_text(strip=True),
            "dii_net": first_row[6].get_text(strip=True),
        }
    except Exception as exc:
        log.warning("fii/dii parse failed: %s", exc)
        return None


def get_market_context() -> dict[str, Any]:
    """Bundle all market-context signals; missing pieces become None."""
    return {
        "nifty_spot": fetch_nifty_spot(),
        "gift_nifty": fetch_sgx_nifty_proxy(),
        "fii_dii": fetch_fii_dii_flows(),
    }


# Alias — preferred external name
fetch_market_context = get_market_context
