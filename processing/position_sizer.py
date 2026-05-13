"""Confidence → capital fraction → share quantity. CNC swing only (1x).

Capital model (anchored on DAILY_CAPITAL_BUDGET=10000):
  Conf 9-10  → 40% capital  (₹4,000)
  Conf 7-8   → 30% capital  (₹3,000)
  Conf 6     → 20% capital  (₹2,000)
  Conf <6    → skip (return None)

`leverage_multiplier` is kept on outputs for schema continuity but is always 1.
`notional_exposure` == `capital_deployed`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import config

log = logging.getLogger(__name__)


@dataclass
class PositionSize:
    confidence: int
    leverage_multiplier: int           # always 1 — CNC
    capital_deployed: float
    notional_exposure: float           # == capital_deployed
    shares_qty: int
    is_paper: bool = False


def size_for(confidence: int, entry_price: float | None) -> PositionSize | None:
    """Return PositionSize for a recommendation, or None to skip."""
    sizing = config.sizing_for_confidence(int(confidence))
    if sizing is None:
        log.info("Confidence %s below threshold — skipping recommendation", confidence)
        return None
    _, capital_fraction = sizing
    capital_deployed = config.DAILY_CAPITAL_BUDGET * capital_fraction
    if not entry_price or entry_price <= 0:
        shares = 0
    else:
        shares = int(capital_deployed // float(entry_price))
    return PositionSize(
        confidence=int(confidence),
        leverage_multiplier=1,
        capital_deployed=capital_deployed,
        notional_exposure=capital_deployed,
        shares_qty=max(shares, 0),
        is_paper=bool(config.PAPER_TRADING),
    )


def calculate_position_size(
    *,
    confidence: int,
    entry_price: float,
    stop_loss: float | None = None,
    daily_capital: int | None = None,
    daily_loss_used: float = 0.0,
) -> dict[str, Any]:
    """Friendly wrapper exposing the full sizing decision as a plain dict."""
    if daily_capital is not None and daily_capital != config.DAILY_CAPITAL_BUDGET:
        log.warning("calculate_position_size: ignoring daily_capital=%s (config is %s)",
                    daily_capital, config.DAILY_CAPITAL_BUDGET)
    if daily_loss_used >= config.DAILY_LOSS_LIMIT:
        return {"skip": True, "reason": "daily loss limit reached", "shares_qty": 0,
                "leverage_multiplier": 1, "capital_deployed": 0.0}

    sized = size_for(confidence, entry_price)
    if sized is None:
        return {"skip": True, "reason": f"confidence {confidence} below threshold",
                "shares_qty": 0, "leverage_multiplier": 1, "capital_deployed": 0.0}

    risk_per_share = (entry_price - stop_loss) if stop_loss else None
    return {
        "skip": False,
        "confidence": sized.confidence,
        "leverage_multiplier": sized.leverage_multiplier,
        "capital_deployed": sized.capital_deployed,
        "notional_exposure": sized.notional_exposure,
        "shares_qty": sized.shares_qty,
        "is_paper": sized.is_paper,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "risk_per_share": risk_per_share,
        "rupee_risk": (risk_per_share * sized.shares_qty) if risk_per_share is not None else None,
    }
