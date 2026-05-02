"""Enforce all capital / leverage rules from CLAUDE.md before a rec leaves the system.

Returns a (passing, rejected_with_reasons) split so the caller can:
  - send only passing recs to Telegram
  - log rejected ones with a clear reason for audit

Rules enforced (per CLAUDE.md):
  - Never leverage below confidence 7
  - Skip confidence < 6 entirely
  - Max single position ≤ 20% of portfolio value
  - Max sector concentration ≤ 30% of portfolio value
  - Max total leverage exposure ≤ 60% of portfolio value
  - Max 3 positions per day
  - If today's realised loss > DAILY_LOSS_LIMIT: no new leveraged positions
    AND switch to conservative mode (EXIT-only)
  - Day before high-severity event: halve all position sizes
  - Day before medium-severity event: reduce leverage by 1x
  - Outside 9:15-15:30 IST: market closed (caller may abort)
  - After 14:45 IST: all MIS positions flagged MUST-EXIT regardless of P&L
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta
from typing import Any

import pytz

import config
from storage import supabase_client

log = logging.getLogger(__name__)

_LEVERAGED_ACTIONS = {"BUY", "ADD"}
_EXIT_ACTIONS = {"EXIT-PARTIAL", "EXIT-FULL", "TIGHTEN-SL", "HOLD"}
_IST = pytz.timezone("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(_IST)


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


# ---------------------------------------------------------------------------
# Time-based gates
# ---------------------------------------------------------------------------

def check_market_hours(now: datetime | None = None) -> bool:
    """True iff IST time is within 9:15-15:30 on a weekday."""
    now = now or _now_ist()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = _parse_hhmm(config.MARKET_OPEN_IST)
    close_t = _parse_hhmm(config.MARKET_CLOSE_IST)
    return open_t <= now.time() <= close_t


def must_force_exit_mis(now: datetime | None = None) -> bool:
    """After MIS_FORCE_EXIT_AFTER_IST, every MIS position is MUST-EXIT."""
    now = now or _now_ist()
    threshold = _parse_hhmm(config.MIS_FORCE_EXIT_AFTER_IST)
    return now.time() >= threshold


# ---------------------------------------------------------------------------
# Event calendar
# ---------------------------------------------------------------------------

def check_event_tomorrow(today: datetime | None = None) -> dict[str, Any] | None:
    """Return tomorrow's event entry from KNOWN_EVENTS, or None.

    severity values:
      'high'   → caller should halve position sizes and surface a warning
      'medium' → caller should reduce leverage by 1x
    """
    today = today or _now_ist()
    tomorrow_str = (today.date() + timedelta(days=1)).isoformat()
    for ev in config.KNOWN_EVENTS:
        if ev.get("date") == tomorrow_str:
            return ev
    return None


def event_warning_text(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None
    sev = event.get("severity", "medium")
    name = event.get("event", "event")
    if sev == "high":
        return f"⚠️ {name} tomorrow ({event['date']}) — position sizes HALVED."
    if sev == "medium":
        return f"⚠️ {name} tomorrow ({event['date']}) — leverage reduced by 1x."
    return f"ℹ️ {name} tomorrow ({event['date']})."


def _apply_event_adjustment(rec: dict[str, Any], event: dict[str, Any] | None) -> dict[str, Any]:
    """Return a new rec with event-based size/leverage adjustment applied."""
    if not event:
        return rec
    sev = event.get("severity")
    out = dict(rec)
    if sev == "high":
        out["capital_deployed"] = float(out.get("capital_deployed") or 0) * 0.5
        out["shares_qty"] = int((out.get("shares_qty") or 0) * 0.5)
    elif sev == "medium":
        new_lev = max(1, int(out.get("leverage_multiplier") or 1) - 1)
        out["leverage_multiplier"] = new_lev
    return out


# ---------------------------------------------------------------------------
# Loss-limit / conservative mode
# ---------------------------------------------------------------------------

def conservative_mode_active() -> bool:
    """True once realised P&L for the day is below -DAILY_LOSS_LIMIT."""
    try:
        pnl = supabase_client.realised_pnl_today()
    except Exception as exc:
        log.warning("realised_pnl_today lookup failed: %s", exc)
        pnl = 0.0
    return pnl <= -float(config.DAILY_LOSS_LIMIT)


# ---------------------------------------------------------------------------
# Main filter
# ---------------------------------------------------------------------------

def _sector_value_after(rec: dict[str, Any], sector_alloc: dict[str, float]) -> tuple[str | None, float]:
    sector = (rec.get("sector") or "Unknown")
    added = float(rec.get("capital_deployed") or 0) * int(rec.get("leverage_multiplier") or 1)
    return sector, sector_alloc.get(sector, 0.0) + added


def apply(
    recommendations: list[dict[str, Any]],
    *,
    portfolio_value: float,
    sector_allocation: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter recs against all guardrails. Returns (passing, rejected).

    Event adjustments are applied to a copy of each rec before evaluation, so
    the persisted record reflects the actual capital/leverage that would have
    been deployed.
    """
    passing: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    sector_alloc = dict(sector_allocation or {})
    cumulative_leverage_exposure = 0.0
    conservative = conservative_mode_active()
    event = check_event_tomorrow()

    if conservative:
        log.warning("Conservative mode active — daily loss limit breached. Allowing EXIT actions only.")
    if event:
        log.warning("Event tomorrow: %s (%s) — applying %s adjustment",
                    event.get("event"), event.get("date"), event.get("severity"))

    for raw in recommendations:
        rec = _apply_event_adjustment(raw, event)
        action = rec.get("action")
        confidence = int(rec.get("confidence_score") or 0)
        leverage = int(rec.get("leverage_multiplier") or 0)
        capital = float(rec.get("capital_deployed") or 0)
        notional = capital * max(leverage, 1)

        reason: str | None = None

        if conservative and action in _LEVERAGED_ACTIONS:
            reason = "conservative mode active (daily loss limit breached)"
        elif confidence < 6 and action in _LEVERAGED_ACTIONS:
            reason = f"confidence {confidence} below floor"
        elif leverage > 1 and confidence < 7:
            reason = "leverage requested below confidence 7"
        elif portfolio_value and notional > portfolio_value * config.MAX_SINGLE_POSITION_PCT and action in _LEVERAGED_ACTIONS:
            reason = f"single-position cap {config.MAX_SINGLE_POSITION_PCT:.0%} breached"
        elif portfolio_value and (cumulative_leverage_exposure + notional) > portfolio_value * config.MAX_LEVERAGE_EXPOSURE_PCT and leverage > 1:
            reason = f"total-leverage cap {config.MAX_LEVERAGE_EXPOSURE_PCT:.0%} breached"
        else:
            sector, projected = _sector_value_after(rec, sector_alloc)
            if portfolio_value and projected > portfolio_value * config.MAX_SECTOR_CONCENTRATION_PCT and action in _LEVERAGED_ACTIONS:
                reason = f"sector cap {config.MAX_SECTOR_CONCENTRATION_PCT:.0%} breached for {sector}"

        if reason:
            rejected.append({**rec, "rejection_reason": reason})
            log.info("REJECT %s %s — %s", action, rec.get("ticker"), reason)
            continue

        passing.append(rec)
        if leverage > 1:
            cumulative_leverage_exposure += notional
            sector, projected = _sector_value_after(rec, sector_alloc)
            if sector:
                sector_alloc[sector] = projected

    # Cap to MAX_POSITIONS_PER_DAY across leveraged actions only
    leveraged_passing = [r for r in passing if r.get("action") in _LEVERAGED_ACTIONS]
    if len(leveraged_passing) > config.MAX_POSITIONS_PER_DAY:
        leveraged_passing.sort(key=lambda r: int(r.get("confidence_score") or 0), reverse=True)
        kept = set(id(r) for r in leveraged_passing[: config.MAX_POSITIONS_PER_DAY])
        new_passing: list[dict[str, Any]] = []
        for r in passing:
            if r.get("action") in _LEVERAGED_ACTIONS and id(r) not in kept:
                rejected.append({**r, "rejection_reason": f"daily position cap {config.MAX_POSITIONS_PER_DAY} reached"})
            else:
                new_passing.append(r)
        passing = new_passing

    return passing, rejected
