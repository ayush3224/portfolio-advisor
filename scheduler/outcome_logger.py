"""4:00 PM IST orchestrator — outcome logger + EOD scorecard. No Claude calls.

Three jobs:
  1. For every recommendation made today, look up the closing price, compute
     return vs entry & alpha vs benchmark (Nifty for IND, S&P 500 for US),
     write a backtest_results row.
  2. Build an "AI cost today" block from run_log.
  3. Emit a per-pick scorecard split by market into the daily Telegram.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

import pytz

import config
from delivery import telegram_bot
from storage import supabase_client
from backtest import outcome_tracker

log = logging.getLogger(__name__)

_IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _ist_day_bounds_utc(d: date) -> tuple[str, str]:
    start_ist = _IST.localize(datetime.combine(d, dtime.min))
    end_ist = _IST.localize(datetime.combine(d, dtime.max))
    return start_ist.astimezone(timezone.utc).isoformat(), end_ist.astimezone(timezone.utc).isoformat()


def _ist_month_bounds_utc(d: date) -> tuple[str, str]:
    first = d.replace(day=1)
    last_day = calendar.monthrange(d.year, d.month)[1]
    last = d.replace(day=last_day)
    start_ist = _IST.localize(datetime.combine(first, dtime.min))
    end_ist = _IST.localize(datetime.combine(last, dtime.max))
    return start_ist.astimezone(timezone.utc).isoformat(), end_ist.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# AI cost block
# ---------------------------------------------------------------------------

def _short_model(name: str | None) -> str:
    if not name:
        return "Other"
    if "sonnet" in name:
        return "Sonnet"
    if "haiku" in name:
        return "Haiku"
    if "opus" in name:
        return "Opus"
    return name


# Map run_type → user-facing IST time label so each row reads like the mockup
# ("9AM Sonnet: $0.017"). Falls back to the actual IST start time on unknown
# run_types.
_RUN_TYPE_TIME_LABEL = {
    "premarket":         "9AM",
    "midday":            "12:30PM",
    "eod":               "3PM",
    "outcome_logger":    "4PM",
    "us_advisory":       "7:30PM",
    "us_premarket":      "7:30PM",
    "weekly_scorecard":  "Sun 8PM",
}


def _ist_label_for(run_type: str | None, started_at_iso: str | None) -> str:
    if run_type and run_type in _RUN_TYPE_TIME_LABEL:
        return _RUN_TYPE_TIME_LABEL[run_type]
    if not started_at_iso:
        return run_type or "?"
    try:
        dt = datetime.fromisoformat(started_at_iso.replace("Z", "+00:00")).astimezone(_IST)
        return dt.strftime("%-I:%M%p").replace(":00", "")
    except Exception:
        return run_type or "?"


def build_cost_block(today_ist: date) -> dict[str, Any]:
    """Group today's run_log entries by IST time-of-day + model. One row per
    distinct (time-label, model) so the message reads like:
        9AM Sonnet:     $0.017
        7:30PM Sonnet:  $0.133
        3PM Haiku:      $0.002
    """
    today_start, today_end = _ist_day_bounds_utc(today_ist)
    todays_runs = supabase_client.get_run_log_between(today_start, today_end)

    grouped: dict[tuple[str, str], float] = {}
    total_cost = 0.0
    for r in todays_runs:
        cost = float(r.get("estimated_cost_usd") or 0.0)
        if cost == 0.0:
            continue
        total_cost += cost
        model = _short_model(r.get("model_used"))
        label = _ist_label_for(r.get("run_type"), r.get("started_at"))
        key = (label, model)
        grouped[key] = grouped.get(key, 0.0) + cost

    rows = sorted(grouped.items(), key=lambda kv: -kv[1])
    return {
        "model_rows": [
            {"label": label, "model": model, "cost_usd": cost}
            for (label, model), cost in rows
        ],
        "total_cost_usd": total_cost,
        "total_cost_inr": total_cost * config.USD_INR_RATE,
    }


# ---------------------------------------------------------------------------
# Scorecard aggregation
# ---------------------------------------------------------------------------

def aggregate_scorecard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ind_rows = [r for r in rows if r.get("market") == "IND"]
    us_rows = [r for r in rows if r.get("market") == "US"]

    wins = sum(1 for r in rows if r.get("outcome") == "win")
    losses = sum(1 for r in rows if r.get("outcome") == "loss")
    executed = sum(1 for r in rows if r.get("user_executed"))
    alpha_vals = [float(r.get("alpha_pct") or 0) for r in rows]
    avg_alpha = sum(alpha_vals) / len(alpha_vals) if alpha_vals else 0.0
    total_pnl = sum(float(r.get("actual_pnl_inr") or 0) for r in rows if r.get("user_executed"))

    ind_bench = ind_rows[0].get("nifty_return_pct") if ind_rows else None
    us_bench = us_rows[0].get("nifty_return_pct") if us_rows else None

    return {
        "ind_rows": ind_rows,
        "us_rows": us_rows,
        "wins": wins,
        "losses": losses,
        "total": len(rows),
        "executed": executed,
        "avg_alpha": avg_alpha,
        "total_pnl": total_pnl,
        "ind_benchmark": ind_bench,
        "us_benchmark": us_bench,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> dict[str, Any]:
    today_ist = datetime.now(_IST).date()
    today_str = today_ist.isoformat()

    today_start, today_end = _ist_day_bounds_utc(today_ist)
    recs = supabase_client.get_recommendations_between(today_start, today_end)
    benchmarks = outcome_tracker.fetch_benchmarks()
    log.info("Benchmarks today: %s", benchmarks)

    outcome_rows: list[dict[str, Any]] = []
    for rec in recs:
        try:
            row = outcome_tracker.compute_outcome(rec, benchmarks=benchmarks)
            if row is None:
                continue
            supabase_client.insert_backtest_result(row)
            outcome_rows.append(row)
        except Exception as exc:
            log.exception("outcome compute failed for %s: %s", rec.get("ticker"), exc)

    scorecard = aggregate_scorecard(outcome_rows)
    cost = build_cost_block(today_ist)

    body = telegram_bot.format_daily_scorecard(today_str, scorecard, cost)
    if config.PAPER_TRADING:
        body = telegram_bot.PAPER_TRADING_BANNER + "\n\n" + body
    telegram_bot.send_alert(body)
    return {"date": today_str, "scorecard": scorecard, "cost": cost,
            "outcome_rows": outcome_rows, "benchmarks": benchmarks}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
