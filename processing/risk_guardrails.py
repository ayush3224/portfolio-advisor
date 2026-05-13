"""Enforce capital / position-size rules before a rec leaves the system.

Trading product: CNC delivery. No leverage, no MIS. The historical MIS
square-off / leverage-cap rules from the original CLAUDE.md no longer apply.

Returns a (passing, rejected_with_reasons) split so the caller can:
  - send only passing recs to Telegram
  - log rejected ones with a clear reason for audit

Rules enforced:
  - Skip confidence < 6 entirely (for new-entry actions: BUY/ADD)
  - Max single position ≤ MAX_SINGLE_POSITION_PCT of portfolio value
  - Max sector concentration ≤ MAX_SECTOR_CONCENTRATION_PCT of portfolio value
  - Max MAX_POSITIONS_PER_DAY new-entry positions per day
  - If today's realised loss ≤ -DAILY_LOSS_LIMIT: conservative mode
    (new BUY/ADD positions blocked; EXIT actions still allowed)
  - Day before high-severity event: halve all position sizes
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta
from typing import Any

import pytz

import config
from storage import supabase_client

log = logging.getLogger(__name__)

_NEW_ENTRY_ACTIONS = {"BUY", "ADD"}
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


# ---------------------------------------------------------------------------
# Event calendar
# ---------------------------------------------------------------------------

def check_event_tomorrow(today: datetime | None = None) -> dict[str, Any] | None:
    """Return tomorrow's event entry from KNOWN_EVENTS, or None.

    severity values:
      'high'   → caller should halve position sizes and surface a warning
      'medium' → informational only (CNC is already 1x; no leverage to cut)
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
    return f"ℹ️ {name} tomorrow ({event['date']})."


def _apply_event_adjustment(rec: dict[str, Any], event: dict[str, Any] | None) -> dict[str, Any]:
    """Return a new rec with high-severity size halving applied (CNC: no leverage)."""
    if not event or event.get("severity") != "high":
        return rec
    out = dict(rec)
    out["capital_deployed"] = float(out.get("capital_deployed") or 0) * 0.5
    out["shares_qty"] = int((out.get("shares_qty") or 0) * 0.5)
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
    added = float(rec.get("capital_deployed") or 0)
    return sector, sector_alloc.get(sector, 0.0) + added


def apply(
    recommendations: list[dict[str, Any]],
    *,
    portfolio_value: float,
    sector_allocation: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter recs against guardrails. Returns (passing, rejected).

    Event adjustments are applied to a copy of each rec before evaluation, so
    the persisted record reflects the actual capital that would have been
    deployed.
    """
    passing: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    sector_alloc = dict(sector_allocation or {})
    conservative = conservative_mode_active()
    event = check_event_tomorrow()

    if conservative:
        log.warning("Conservative mode active — daily loss limit breached. New BUY/ADD entries blocked.")
    if event:
        log.warning("Event tomorrow: %s (%s) — severity=%s",
                    event.get("event"), event.get("date"), event.get("severity"))

    for raw in recommendations:
        rec = _apply_event_adjustment(raw, event)
        action = rec.get("action")
        confidence = int(rec.get("confidence_score") or 0)
        capital = float(rec.get("capital_deployed") or 0)

        reason: str | None = None

        if conservative and action in _NEW_ENTRY_ACTIONS:
            reason = "conservative mode active (daily loss limit breached)"
        elif confidence < 6 and action in _NEW_ENTRY_ACTIONS:
            reason = f"confidence {confidence} below floor"
        elif portfolio_value and capital > portfolio_value * config.MAX_SINGLE_POSITION_PCT and action in _NEW_ENTRY_ACTIONS:
            reason = f"single-position cap {config.MAX_SINGLE_POSITION_PCT:.0%} breached"
        else:
            sector, projected = _sector_value_after(rec, sector_alloc)
            if portfolio_value and projected > portfolio_value * config.MAX_SECTOR_CONCENTRATION_PCT and action in _NEW_ENTRY_ACTIONS:
                reason = f"sector cap {config.MAX_SECTOR_CONCENTRATION_PCT:.0%} breached for {sector}"

        if reason:
            rejected.append({**rec, "rejection_reason": reason})
            log.info("REJECT %s %s — %s", action, rec.get("ticker"), reason)
            continue

        passing.append(rec)
        if action in _NEW_ENTRY_ACTIONS:
            sector, projected = _sector_value_after(rec, sector_alloc)
            if sector:
                sector_alloc[sector] = projected

    # Cap to MAX_POSITIONS_PER_DAY across new-entry actions only
    new_entries = [r for r in passing if r.get("action") in _NEW_ENTRY_ACTIONS]
    if len(new_entries) > config.MAX_POSITIONS_PER_DAY:
        new_entries.sort(key=lambda r: int(r.get("confidence_score") or 0), reverse=True)
        kept = set(id(r) for r in new_entries[: config.MAX_POSITIONS_PER_DAY])
        new_passing: list[dict[str, Any]] = []
        for r in passing:
            if r.get("action") in _NEW_ENTRY_ACTIONS and id(r) not in kept:
                rejected.append({**r, "rejection_reason": f"daily position cap {config.MAX_POSITIONS_PER_DAY} reached"})
            else:
                new_passing.append(r)
        passing = new_passing

    return passing, rejected
