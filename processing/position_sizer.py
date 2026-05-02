"""Confidence → leverage → share quantity, per the capital model in CLAUDE.md.

Capital model (anchored on DAILY_CAPITAL_BUDGET=10000):
  Conf 9-10  → 40% capital × 3x leverage  (₹4,000 → ₹12,000 exposure)
  Conf 7-8   → 30% capital × 2x leverage  (₹3,000 → ₹6,000  exposure)
  Conf 6     → 20% capital × 1x           (₹2,000 → ₹2,000  exposure)
  Conf <6    → skip (return None)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import config

log = logging.getLogger(__name__)


@dataclass
class PositionSize:
    confidence: int
    leverage_multiplier: int
    capital_deployed: float       # margin actually committed (own money)
    notional_exposure: float      # capital_deployed × leverage
    shares_qty: int
    is_paper: bool = False


def size_for(confidence: int, entry_price: float | None) -> PositionSize | None:
    """Return PositionSize for a recommendation, or None to skip.

    Sizing math is identical regardless of PAPER_TRADING — we want realistic
    sizes in paper mode so the recommendations look real. The is_paper flag
    is carried through so downstream code (DB, Telegram) can tag accordingly.
    """
    sizing = config.sizing_for_confidence(int(confidence))
    if sizing is None:
        log.info("Confidence %s below threshold — skipping recommendation", confidence)
        return None
    leverage, capital_fraction = sizing
    capital_deployed = config.DAILY_CAPITAL_BUDGET * capital_fraction
    notional = capital_deployed * leverage
    if not entry_price or entry_price <= 0:
        shares = 0
    else:
        shares = int(notional // float(entry_price))
    return PositionSize(
        confidence=int(confidence),
        leverage_multiplier=leverage,
        capital_deployed=capital_deployed,
        notional_exposure=notional,
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
    """Friendly wrapper exposing the full sizing decision as a plain dict.

    If `daily_loss_used >= DAILY_LOSS_LIMIT`, the position is forcibly skipped
    (conservative mode). `stop_loss` is echoed back so the caller can compute
    rupee risk-per-share if it wants to.
    """
    if daily_capital is not None and daily_capital != config.DAILY_CAPITAL_BUDGET:
        log.warning("calculate_position_size: ignoring daily_capital=%s (config is %s)",
                    daily_capital, config.DAILY_CAPITAL_BUDGET)
    if daily_loss_used >= config.DAILY_LOSS_LIMIT:
        return {"skip": True, "reason": "daily loss limit reached", "shares_qty": 0,
                "leverage_multiplier": 0, "capital_deployed": 0.0}

    sized = size_for(confidence, entry_price)
    if sized is None:
        return {"skip": True, "reason": f"confidence {confidence} below threshold",
                "shares_qty": 0, "leverage_multiplier": 0, "capital_deployed": 0.0}

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
