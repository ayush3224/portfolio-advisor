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


def build_cost_block(today_ist: date) -> dict[str, Any]:
    """Group today's run_log entries by model. Returns dict for the formatter."""
    today_start, today_end = _ist_day_bounds_utc(today_ist)
    todays_runs = supabase_client.get_run_log_between(today_start, today_end)

    grouped: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    for r in todays_runs:
        model = _short_model(r.get("model_used"))
        cost = float(r.get("estimated_cost_usd") or 0.0)
        total_cost += cost
        slot = grouped.setdefault(model, {"cost_usd": 0.0, "run_types": []})
        slot["cost_usd"] += cost
        run_type = r.get("run_type") or "?"
        if run_type not in slot["run_types"]:
            slot["run_types"].append(run_type)

    rows = sorted(grouped.items(), key=lambda kv: -kv[1]["cost_usd"])
    return {
        "model_rows": [
            {
                "model": m,
                "cost_usd": s["cost_usd"],
                "run_types": s["run_types"],
            }
            for m, s in rows
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
