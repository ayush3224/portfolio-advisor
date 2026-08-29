"""Supabase wrapper — one client per process, helpers for every table.

All writes are no-ops when config.DRY_RUN is true.
All timestamps are written in UTC. The DB defaults already enforce this
(timestamptz default now()), but we pass explicit UTC timestamps where
the caller cares about the value (e.g. snapshot_time).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from supabase import Client, create_client

import config


def _sanitize_for_json(value: Any) -> Any:
    """Recursively replace NaN/Inf floats with None so Postgres JSONB accepts the payload."""
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, dict):
        return {k: _sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_json(v) for v in value]
    return value

log = logging.getLogger(__name__)

_client: Client | None = None


def get_client() -> Client | None:
    """Singleton Supabase client. Returns None if creds are missing."""
    global _client
    if _client is not None:
        return _client
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        log.warning("Supabase creds missing — DB operations will be skipped")
        return None
    _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _client


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Portfolio snapshots (also serves as the 5-minute portfolio cache)
# ---------------------------------------------------------------------------

def insert_portfolio_snapshot(snapshot: dict[str, Any]) -> str | None:
    """Insert a portfolio snapshot row. Returns row id, None on DRY_RUN or failure.

    Pipeline must treat None as fatal — caller decides whether to abort.
    """
    if config.DRY_RUN:
        log.info("[DRY_RUN] insert_portfolio_snapshot skipped")
        return None
    client = get_client()
    if client is None:
        return None
    payload = _sanitize_for_json({
        "snapshot_time": snapshot.get("snapshot_time", _utcnow_iso()),
        "run_type": snapshot["run_type"],
        "holdings_json": snapshot.get("holdings", []),
        "positions_json": snapshot.get("positions", []),
        "total_value": snapshot.get("total_value"),
        "available_margin": snapshot.get("available_margin"),
        "used_margin": snapshot.get("used_margin"),
        "realised_pnl_today": snapshot.get("realised_pnl_today"),
        "sector_allocation_json": snapshot.get("sector_allocation"),
        "project": config.PROJECT_NAME,
    })
    try:
        res = client.table("portfolio_snapshots").insert(payload).execute()
    except Exception as exc:
        # Older portfolio_snapshots tables may not have the `project` column.
        log.warning("portfolio_snapshots insert with project failed (%s) — retrying without", exc)
        payload.pop("project", None)
        try:
            res = client.table("portfolio_snapshots").insert(payload).execute()
        except Exception as exc2:
            log.error("insert_portfolio_snapshot failed: %s", exc2)
            return None
    return res.data[0]["id"] if res.data else None


def get_cached_portfolio(max_age_seconds: int = config.PORTFOLIO_CACHE_TTL) -> dict[str, Any] | None:
    """Return the most recent snapshot if within TTL, else None.

    Any DB error (missing table, transport failure) is logged and treated as
    a cache miss — the caller falls through to a live fetch.
    """
    client = get_client()
    if client is None:
        return None
    try:
        res = (
            client.table("portfolio_snapshots")
            .select("*")
            .order("snapshot_time", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        log.warning("portfolio cache lookup failed (%s) — treating as cache miss", exc)
        return None
    if not res.data:
        return None
    row = res.data[0]
    snapshot_time = datetime.fromisoformat(row["snapshot_time"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - snapshot_time > timedelta(seconds=max_age_seconds):
        return None
    return row


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def insert_recommendation(rec: dict[str, Any]) -> str | None:
    if config.DRY_RUN:
        log.info("[DRY_RUN] insert_recommendation %s/%s skipped", rec.get("ticker"), rec.get("action"))
        return None
    client = get_client()
    if client is None:
        return None
    payload = {
        "snapshot_id": rec.get("snapshot_id"),
        "ticker": rec["ticker"],
        "action": rec["action"],
        "confidence_score": rec["confidence_score"],
        "entry_price": rec.get("entry_price"),
        "target_price": rec.get("target_price"),
        "stop_loss": rec.get("stop_loss"),
        "leverage_multiplier": rec.get("leverage_multiplier"),
        "capital_deployed": rec.get("capital_deployed"),
        "shares_qty": rec.get("shares_qty"),
        "reasoning": rec.get("reasoning"),
        "primary_driver": rec.get("primary_driver"),
        "user_executed": rec.get("user_executed", False),
        "paper_trade": bool(rec.get("paper_trade", config.PAPER_TRADING)),
    }
    if rec.get("run_id"):
        payload["run_id"] = rec["run_id"]
    try:
        res = client.table("advisor_recommendations").insert(payload).execute()
    except Exception as exc:
        # Pre-migration tables have no run_id column — retry without it.
        log.warning("recommendation insert with run_id failed (%s) — retrying without", exc)
        payload.pop("run_id", None)
        res = client.table("advisor_recommendations").insert(payload).execute()
    return res.data[0]["id"] if res.data else None


_BUY_ACTIONS = ("BUY", "BUY-MOMENTUM", "BUY-EVENT", "ADD")
_SELL_ACTIONS = ("SELL", "PARTIAL-EXIT", "FULL-EXIT", "EXIT-PARTIAL",
                 "EXIT-FULL", "BOOK-PROFIT", "TIGHTEN-SL")
_HOLD_ACTIONS = ("HOLD", "TIGHTEN-SL")

# A trade can also be attributed to a weaker-fitting call when no direct one
# exists — buying a name Claude said to HOLD still counts as acting on the
# advice. Searched only after the primary family comes up empty, so a real ADD
# is never outranked by a higher-confidence HOLD.
_FALLBACK_ACTIONS = {"BUY": ("HOLD",), "SELL": (), "HOLD": ()}


def _todays_recommendation_for(ticker: str, action_family: str) -> dict[str, Any] | None:
    """Return today's (UTC) advisor_recommendation for `ticker` whose action
    belongs to the given family ('BUY' | 'SELL' | 'HOLD'), highest confidence
    first. None if nothing matches."""
    client = get_client()
    if client is None:
        return None
    family = action_family.upper()
    families = {"BUY": _BUY_ACTIONS, "SELL": _SELL_ACTIONS, "HOLD": _HOLD_ACTIONS}
    actions = families.get(family)
    if not actions:
        return None
    today = datetime.now(timezone.utc).date().isoformat()
    start_iso = f"{today}T00:00:00+00:00"
    end_iso = f"{today}T23:59:59+00:00"

    def _lookup(candidates: tuple[str, ...]) -> dict[str, Any] | None:
        if not candidates:
            return None
        res = (
            client.table("advisor_recommendations")
            .select("*")
            .eq("ticker", ticker)
            .in_("action", list(candidates))
            .gte("created_at", start_iso)
            .lte("created_at", end_iso)
            .order("confidence_score", desc=True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None

    try:
        return _lookup(actions) or _lookup(_FALLBACK_ACTIONS.get(family, ()))
    except Exception as exc:
        log.warning("recommendation lookup failed for %s/%s: %s", ticker, action_family, exc)
        return None


def mark_recommendation_executed(
    rec_id: str,
    *,
    executed_price: float | None = None,
    notes: str = "matched from bot command",
) -> bool:
    """Stamp a recommendation as user_executed=true. Returns True on success."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] mark_recommendation_executed %s skipped", rec_id)
        return False
    client = get_client()
    if client is None:
        return False
    payload: dict[str, Any] = {
        "user_executed": True,
        "execution_notes": notes,
        "executed_at": _utcnow_iso(),
    }
    if executed_price is not None and not (isinstance(executed_price, float) and math.isnan(executed_price)):
        payload["executed_price"] = round(float(executed_price), 4)
    try:
        client.table("advisor_recommendations").update(payload).eq("id", rec_id).execute()
    except Exception as exc:
        log.warning("mark_recommendation_executed failed for %s: %s", rec_id, exc)
        return False

    # Mirror onto backtest_results so weekly/monthly execution stats see it.
    bt_payload: dict[str, Any] = {"user_executed": True}
    if "executed_price" in payload:
        bt_payload["executed_price"] = payload["executed_price"]
    try:
        client.table("backtest_results").update(bt_payload).eq("recommendation_id", rec_id).execute()
    except Exception as exc:
        log.warning("backtest_results execution stamp failed for %s: %s", rec_id, exc)
    return True


def match_and_mark_execution(
    ticker: str, action_family: str, *, executed_price: float | None = None,
) -> dict[str, Any] | None:
    """Convenience wrapper for the Telegram bot: look up today's matching
    recommendation, stamp it executed, return the matched row (or None)."""
    rec = _todays_recommendation_for(ticker, action_family)
    if not rec:
        return None
    ok = mark_recommendation_executed(
        rec["id"], executed_price=executed_price,
        notes="matched from bot command",
    )
    return rec if ok else None


def get_recommendations_for_date(run_date: str) -> list[dict[str, Any]]:
    """Return all recommendations created on a given UTC date (YYYY-MM-DD)."""
    client = get_client()
    if client is None:
        return []
    start = f"{run_date}T00:00:00+00:00"
    end = f"{run_date}T23:59:59+00:00"
    res = (
        client.table("advisor_recommendations")
        .select("*")
        .gte("created_at", start)
        .lte("created_at", end)
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Midday checks
# ---------------------------------------------------------------------------

def insert_midday_check(check: dict[str, Any]) -> str | None:
    if config.DRY_RUN:
        log.info("[DRY_RUN] insert_midday_check %s skipped", check.get("ticker"))
        return None
    client = get_client()
    if client is None:
        return None
    res = client.table("midday_checks").insert(check).execute()
    return res.data[0]["id"] if res.data else None


# ---------------------------------------------------------------------------
# EOD recommendations
# ---------------------------------------------------------------------------

def insert_eod_recommendation(rec: dict[str, Any]) -> str | None:
    if config.DRY_RUN:
        log.info("[DRY_RUN] insert_eod_recommendation %s skipped", rec.get("ticker"))
        return None
    client = get_client()
    if client is None:
        return None
    res = client.table("eod_recommendations").insert(rec).execute()
    return res.data[0]["id"] if res.data else None


# ---------------------------------------------------------------------------
# Backtest results
# ---------------------------------------------------------------------------

def insert_backtest_result(row: dict[str, Any]) -> str | None:
    if config.DRY_RUN:
        log.info("[DRY_RUN] insert_backtest_result %s skipped", row.get("ticker"))
        return None
    client = get_client()
    if client is None:
        return None
    res = client.table("backtest_results").insert(row).execute()
    return res.data[0]["id"] if res.data else None


def get_backtest_results_between(start_date: str, end_date: str) -> list[dict[str, Any]]:
    client = get_client()
    if client is None:
        return []
    res = (
        client.table("backtest_results")
        .select("*")
        .gte("run_date", start_date)
        .lte("run_date", end_date)
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Run log (Claude cost & operational tracking)
# ---------------------------------------------------------------------------

def start_run(run_type: str) -> str | None:
    if config.DRY_RUN:
        log.info("[DRY_RUN] start_run %s skipped", run_type)
        return None
    client = get_client()
    if client is None:
        return None
    payload = {
        "project": config.PROJECT_NAME,
        "run_type": run_type,
        "started_at": _utcnow_iso(),
        "status": "running",
    }
    try:
        res = client.table("run_log").insert(payload).execute()
    except Exception as exc:
        # Older run_log tables may not have the `project` column; retry without it.
        log.warning("run_log insert with project failed (%s) — retrying without project column", exc)
        payload.pop("project", None)
        res = client.table("run_log").insert(payload).execute()
    return res.data[0]["id"] if res.data else None


def finish_run(
    run_id: str | None,
    *,
    status: str,
    model_used: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    estimated_cost_usd: float | None = None,
    error_message: str | None = None,
) -> None:
    if config.DRY_RUN or run_id is None:
        log.info(
            "[DRY_RUN/skip] finish_run status=%s model=%s in=%s out=%s cost=%s",
            status, model_used, input_tokens, output_tokens, estimated_cost_usd,
        )
        return
    client = get_client()
    if client is None:
        return
    client.table("run_log").update(
        {
            "completed_at": _utcnow_iso(),
            "status": status,
            "model_used": model_used,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": estimated_cost_usd,
            "error_message": error_message,
        }
    ).eq("id", run_id).execute()


def get_run_log_between(start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Return run_log rows whose started_at is within [start_iso, end_iso].

    Scopes results to this project: `project = 'portfolio-advisor'` OR
    (project IS NULL AND run_type IN PROJECT_RUN_TYPES). The fallback
    catches rows written before the `project` column was added.
    """
    client = get_client()
    if client is None:
        return []
    run_types_csv = ",".join(config.PROJECT_RUN_TYPES)
    or_clause = (
        f"project.eq.{config.PROJECT_NAME},"
        f"and(project.is.null,run_type.in.({run_types_csv}))"
    )
    try:
        res = (
            client.table("run_log")
            .select("*")
            .gte("started_at", start_iso)
            .lte("started_at", end_iso)
            .or_(or_clause)
            .order("started_at", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        # `project` column may not exist in older tables — fall back to run_type only.
        log.warning("run_log lookup with project filter failed (%s) — falling back to run_type", exc)
        try:
            res = (
                client.table("run_log")
                .select("*")
                .gte("started_at", start_iso)
                .lte("started_at", end_iso)
                .in_("run_type", list(config.PROJECT_RUN_TYPES))
                .order("started_at", desc=False)
                .execute()
            )
            return res.data or []
        except Exception as exc2:
            log.warning("run_log fallback lookup failed (%s) — returning []", exc2)
            return []


def get_recommendations_between(start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Return advisor_recommendations created within [start_iso, end_iso]."""
    client = get_client()
    if client is None:
        return []
    try:
        res = (
            client.table("advisor_recommendations")
            .select("*")
            .gte("created_at", start_iso)
            .lte("created_at", end_iso)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.warning("advisor_recommendations lookup failed (%s) — returning []", exc)
        return []


def get_backtest_results_for_run_date(run_date: str) -> list[dict[str, Any]]:
    """Return backtest_results dated exactly run_date (YYYY-MM-DD)."""
    client = get_client()
    if client is None:
        return []
    try:
        res = (
            client.table("backtest_results")
            .select("*")
            .eq("run_date", run_date)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.warning("backtest_results lookup failed (%s) — returning []", exc)
        return []


def realised_pnl_today() -> float:
    """Sum of actual_pnl_inr for backtest_results dated today (UTC)."""
    client = get_client()
    if client is None:
        return 0.0
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        res = (
            client.table("backtest_results")
            .select("actual_pnl_inr")
            .eq("run_date", today)
            .execute()
        )
    except Exception as exc:
        log.warning("realised_pnl_today lookup failed (%s) — assuming 0", exc)
        return 0.0
    return float(sum((r.get("actual_pnl_inr") or 0) for r in (res.data or [])))
