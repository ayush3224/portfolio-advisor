"""Build a per-holding context block consumable by Claude prompts.

This is the central join point: portfolio snapshot + live price + news +
analyst-channel signals → one structured object per ticker. Each holding
is enriched independently and per-ticker exceptions are caught so a
single bad symbol can't sink the whole report.
"""

from __future__ import annotations

import logging
from typing import Any

from ingestion import news as news_mod
from ingestion import telegram_scraper, upstox_prices
from processing import technicals as technicals_mod

log = logging.getLogger(__name__)


def _row_instrument_key(row: dict[str, Any]) -> str | None:
    return (
        row.get("instrument_token")
        or row.get("instrument_key")
        or row.get("instrumentToken")
    )


def _row_ticker(row: dict[str, Any]) -> str | None:
    return row.get("trading_symbol") or row.get("tradingsymbol") or row.get("symbol")


def _row_qty(row: dict[str, Any]) -> int:
    return int(row.get("quantity") or row.get("qty") or 0)


def _row_avg_price(row: dict[str, Any]) -> float:
    return float(row.get("average_price") or row.get("avg_price") or row.get("buy_avg") or 0)


def _row_market(row: dict[str, Any]) -> str:
    return (row.get("market") or "IND").upper()


def build_holding_block(row: dict[str, Any], *, news: list[dict[str, Any]],
                        signals: dict[str, Any] | None) -> dict[str, Any]:
    ticker = _row_ticker(row) or "UNKNOWN"
    qty = _row_qty(row)
    avg = _row_avg_price(row)
    instrument_key = _row_instrument_key(row)
    price = upstox_prices.enrich_price_block(instrument_key) if instrument_key else {}
    cmp_ = price.get("cmp") or row.get("last_price")
    pnl = (float(cmp_) - avg) * qty if cmp_ and avg else None
    pnl_pct = ((float(cmp_) - avg) / avg * 100) if cmp_ and avg else None

    # Tier 1 technicals — enrichment only; None on any failure, never fatal.
    tech = technicals_mod.compute_technicals(ticker, _row_market(row))

    block = {
        "ticker": ticker,
        "instrument_key": instrument_key,
        "quantity": qty,
        "avg_price": avg,
        "cmp": cmp_,
        "unrealised_pnl": pnl,
        "unrealised_pnl_pct": pnl_pct,
        "vwap": price.get("vwap"),
        "open": price.get("open"),
        "high": price.get("high"),
        "low": price.get("low"),
        "prev_close": price.get("close_prev"),
        "volume": price.get("volume"),
        "week_52_high": price.get("week_52_high"),
        "week_52_low": price.get("week_52_low"),
        "sector": row.get("instrument_sector") or row.get("sector"),
        "news": news[:4],   # 2 Tavily + 2 RSS — see ingestion.news caps
        "analyst_signals": signals or {"bullish": 0, "bearish": 0, "score": 0},
    }
    if tech:
        block["technicals"] = tech
        block["technicals_text"] = technicals_mod.format_technicals_block(tech)
    return block


def build_full_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compose the structured context Claude sees: market + per-holding blocks."""
    rows: list[dict[str, Any]] = (snapshot.get("holdings") or []) + (snapshot.get("positions") or [])
    tickers = sorted({_row_ticker(r) for r in rows if _row_ticker(r)})

    # `holdings=rows` lets ingestion.news read each ticker's market and day
    # move, so quiet Indian names skip the metered Tavily call.
    news_map = news_mod.news_for_holdings(tickers, holdings=rows)
    try:
        signals_map = telegram_scraper.signals_for_holdings(tickers)
    except Exception as exc:
        log.warning("signals_for_holdings failed: %s", exc)
        signals_map = {}

    blocks: list[dict[str, Any]] = []
    for row in rows:
        try:
            t = _row_ticker(row) or "UNKNOWN"
            blocks.append(
                build_holding_block(
                    row,
                    news=news_map.get(t, []),
                    signals=signals_map.get(t),
                )
            )
        except Exception as exc:
            log.exception("Holding block build failed for %s: %s", _row_ticker(row), exc)

    return {
        "snapshot_id": snapshot.get("id"),
        "total_value": snapshot.get("total_value"),
        "available_margin": snapshot.get("available_margin"),
        "used_margin": snapshot.get("used_margin"),
        "realised_pnl_today": snapshot.get("realised_pnl_today"),
        "sector_allocation": snapshot.get("sector_allocation"),
        "holdings": blocks,
    }
