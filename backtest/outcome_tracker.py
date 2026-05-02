"""Per-recommendation outcome computation for the 4 PM logger.

Looks up CMP for each recommended ticker, compares to entry_price, computes
return %, alpha vs Nifty, and the actual rupee P&L given the leveraged
notional that would have been deployed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import config
from ingestion import upstox_prices

log = logging.getLogger(__name__)


def _resolve_instrument_key(rec: dict[str, Any]) -> str | None:
    """Recover instrument_key for a recommendation.

    Order: explicit field on the row → config.INSTRUMENT_KEYS lookup by ticker.
    Returns None if the ticker isn't in the map; caller logs and skips.
    """
    explicit = rec.get("instrument_key")
    if explicit:
        return explicit
    return config.instrument_key_for(rec.get("ticker"))


def compute_outcome(rec: dict[str, Any], *, nifty_return_pct: float = 0.0) -> dict[str, Any] | None:
    """Build a backtest_results row from a recommendation. None on failure."""
    ticker = rec.get("ticker")
    if not ticker:
        return None
    entry = rec.get("entry_price")
    if entry is None:
        return None

    instrument_key = _resolve_instrument_key(rec)
    if not instrument_key:
        log.warning("Skipping outcome for %s — ticker not in INSTRUMENT_KEYS map", ticker)
        return None
    cmp_ = upstox_prices.cmp_for(instrument_key)
    if cmp_ is None:
        log.warning("Skipping outcome for %s — Upstox returned no quote", ticker)
        return None

    entry_f = float(entry)
    return_pct = (float(cmp_) - entry_f) / entry_f * 100 if entry_f else 0.0
    alpha = return_pct - float(nifty_return_pct or 0.0)
    capital = float(rec.get("capital_deployed") or 0)
    leverage = int(rec.get("leverage_multiplier") or 1)
    actual_pnl = capital * leverage * (return_pct / 100)
    outcome = "win" if return_pct > 0.5 else "loss" if return_pct < -0.5 else "flat"

    return {
        "recommendation_id": rec.get("id"),
        "ticker": ticker,
        "run_date": datetime.now(timezone.utc).date().isoformat(),
        "action": rec.get("action"),
        "confidence_score": rec.get("confidence_score"),
        "leverage_multiplier": leverage,
        "price_at_recommendation": entry_f,
        "price_at_close": float(cmp_),
        "return_pct": round(return_pct, 4),
        "nifty_return_pct": round(float(nifty_return_pct or 0.0), 4),
        "alpha_pct": round(alpha, 4),
        "outcome": outcome,
        "capital_deployed": capital,
        "actual_pnl_inr": round(actual_pnl, 2),
    }
