"""Mock portfolio for paper-trading / pre-Upstox testing.

Activated by PAPER_TRADING=true (preferred) or USE_MOCK_PORTFOLIO=true.
Returns a fully-formed portfolio snapshot in the shape
`ingestion.upstox_portfolio.get_portfolio_snapshot` produces, plus enriched
price blocks compatible with `upstox_prices.enrich_price_block`.

Realistic ~₹5.2L Indian retail portfolio:
  ICICIBANK  50 @ 1290  Banking
  HDFCBANK   25 @ 1850  Banking
  RELIANCE   15 @ 2780  Energy
  INFY       40 @ 1580  IT
  SUNPHARMA  20 @ 1700  Pharma

Price proxies (no live data dependency):
  cmp / close_prev = average_price
  vwap             = close_prev × 1.002
  week_52_high     = average_price × 1.25
  week_52_low      = average_price × 0.75
"""

from __future__ import annotations

import config

# (ticker, qty, avg_price, sector, volume)
_BASE = [
    ("ICICIBANK", 50, 1290.0, "Banking", 6_500_000),
    ("HDFCBANK",  25, 1850.0, "Banking", 5_200_000),
    ("RELIANCE",  15, 2780.0, "Energy",  4_800_000),
    ("INFY",      40, 1580.0, "IT",      3_900_000),
    ("SUNPHARMA", 20, 1700.0, "Pharma",  1_200_000),
]


def _build_holding(ticker: str, qty: int, avg: float, sector: str, volume: int) -> dict:
    cmp_ = avg
    close_prev = avg
    vwap = round(close_prev * 1.002, 2)
    return {
        "trading_symbol": ticker,
        "instrument_token": config.instrument_key_for(ticker),
        "quantity": qty,
        "average_price": avg,
        "last_price": cmp_,
        "instrument_sector": sector,
        "vwap": vwap,
        "week_52_high": round(avg * 1.25, 2),
        "week_52_low": round(avg * 0.75, 2),
        "open": close_prev,
        "high": round(cmp_ * 1.005, 2),
        "low": round(cmp_ * 0.995, 2),
        "close_prev": close_prev,
        "volume": volume,
    }


_HOLDINGS = [_build_holding(*row) for row in _BASE]


def _sector_allocation(holdings: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for h in holdings:
        sector = h.get("instrument_sector", "Unknown")
        v = float(h["last_price"]) * float(h["quantity"])
        totals[sector] = totals.get(sector, 0.0) + v
    return totals


def get_mock_snapshot(run_type: str = "premarket") -> dict:
    holdings = [dict(h) for h in _HOLDINGS]
    total_value = sum(h["last_price"] * h["quantity"] for h in holdings)
    return {
        "run_type": run_type,
        "holdings": holdings,
        "positions": [],
        "total_value": total_value,
        "available_margin": 200_000.0,
        "used_margin": 0.0,
        "realised_pnl_today": 0.0,
        "sector_allocation": _sector_allocation(holdings),
    }


def get_mock_price_block(instrument_key: str | None) -> dict:
    """Return a price block matching upstox_prices.enrich_price_block."""
    if not instrument_key:
        return {}
    for h in _HOLDINGS:
        if h["instrument_token"] == instrument_key:
            return {
                "cmp": h["last_price"],
                "open": h["open"],
                "high": h["high"],
                "low": h["low"],
                "close_prev": h["close_prev"],
                "vwap": h["vwap"],
                "volume": h["volume"],
                "week_52_high": h["week_52_high"],
                "week_52_low": h["week_52_low"],
            }
    return {}


def get_mock_cmp(instrument_key: str | None) -> float | None:
    block = get_mock_price_block(instrument_key)
    return block.get("cmp")
