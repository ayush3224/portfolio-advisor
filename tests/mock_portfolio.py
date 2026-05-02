"""Mock portfolio for offline / pre-Upstox testing.

Activated by USE_MOCK_PORTFOLIO=true. Returns a fully-formed portfolio
snapshot in the same shape `ingestion.upstox_portfolio.get_portfolio_snapshot`
produces, plus enriched price blocks compatible with `upstox_prices.enrich_price_block`.
"""

from __future__ import annotations

import config

# 5 sample holdings — sized for a ~₹5L portfolio
_HOLDINGS = [
    {
        "trading_symbol": "ICICIBANK", "instrument_token": config.instrument_key_for("ICICIBANK"),
        "quantity": 50, "average_price": 1290.0, "last_price": 1380.0,
        "instrument_sector": "Financial Services",
        "vwap": 1372.0, "week_52_high": 1410.0, "week_52_low": 980.0,
        "open": 1365.0, "high": 1390.0, "low": 1360.0, "close_prev": 1368.0,
        "volume": 6_500_000,
    },
    {
        "trading_symbol": "SUNPHARMA", "instrument_token": config.instrument_key_for("SUNPHARMA"),
        "quantity": 30, "average_price": 1720.0, "last_price": 1650.0,
        "instrument_sector": "Healthcare",
        "vwap": 1660.0, "week_52_high": 1850.0, "week_52_low": 1430.0,
        "open": 1655.0, "high": 1672.0, "low": 1640.0, "close_prev": 1668.0,
        "volume": 1_200_000,
    },
    {
        "trading_symbol": "RELIANCE", "instrument_token": config.instrument_key_for("RELIANCE"),
        "quantity": 20, "average_price": 2750.0, "last_price": 2840.0,
        "instrument_sector": "Energy",
        "vwap": 2825.0, "week_52_high": 3020.0, "week_52_low": 2350.0,
        "open": 2810.0, "high": 2855.0, "low": 2805.0, "close_prev": 2818.0,
        "volume": 4_800_000,
    },
    {
        "trading_symbol": "TATASTEEL", "instrument_token": config.instrument_key_for("TATASTEEL"),
        "quantity": 100, "average_price": 165.0, "last_price": 158.0,
        "instrument_sector": "Metals",
        "vwap": 159.5, "week_52_high": 184.0, "week_52_low": 122.0,
        "open": 159.0, "high": 161.0, "low": 156.5, "close_prev": 160.0,
        "volume": 22_000_000,
    },
    {
        "trading_symbol": "HDFCBANK", "instrument_token": config.instrument_key_for("HDFCBANK"),
        "quantity": 40, "average_price": 1800.0, "last_price": 1920.0,
        "instrument_sector": "Financial Services",
        "vwap": 1908.0, "week_52_high": 1955.0, "week_52_low": 1430.0,
        "open": 1905.0, "high": 1928.0, "low": 1900.0, "close_prev": 1902.0,
        "volume": 5_200_000,
    },
]


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
        "id": "mock-snapshot-id",
        "run_type": run_type,
        "holdings": holdings,
        "positions": [],
        "total_value": total_value,
        "available_margin": 300_000.0,
        "used_margin": 200_000.0,
        "realised_pnl_today": -450.0,
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
