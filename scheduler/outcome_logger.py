"""4:00 PM IST orchestrator — outcome logger + EOD summary. No Claude calls.

Three jobs:
  1. For every recommendation made today, look up the closing price, compute
     return vs entry & alpha vs Nifty, write a backtest_results row.
  2. Build an "AI usage today" block from run_log (today + MTD + projected).
  3. Build a "trading performance" block from advisor_recommendations
     (executed flag) joined with backtest_results.

All three are folded into a single Telegram message via
delivery.telegram_bot.format_eod_summary.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

import pytz

import config
from delivery import telegram_bot
from ingestion import market_context
from storage import supabase_client
from backtest import outcome_tracker

log = logging.getLogger(__name__)

_IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Date helpers — translate IST calendar dates to UTC ISO bounds for queries
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
# AI usage block
# ---------------------------------------------------------------------------

def _short_model(name: str | None) -> str:
    if not name:
        return "?"
    if "sonnet" in name:
        return "Sonnet"
    if "haiku" in name:
        return "Haiku"
    if "opus" in name:
        return "Opus"
    return name[:6]


def _ist_time_label(iso_ts: str | None) -> str:
    if not iso_ts:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone(_IST)
        return dt.strftime("%H:%M")
    except Exception:
        return "—"


def build_usage_block(today_ist: date) -> dict[str, Any]:
    today_start, today_end = _ist_day_bounds_utc(today_ist)
    month_start, month_end = _ist_month_bounds_utc(today_ist)

    todays_runs = supabase_client.get_run_log_between(today_start, today_end)
    mtd_runs = supabase_client.get_run_log_between(month_start, month_end)

    runs: list[dict[str, Any]] = []
    today_tokens = 0
    today_cost = 0.0
    for r in todays_runs:
        in_t = r.get("input_tokens") or 0
        out_t = r.get("output_tokens") or 0
        cost = float(r.get("estimated_cost_usd") or 0.0)
        today_tokens += in_t + out_t
        today_cost += cost
        runs.append({
            "ist_time": _ist_time_label(r.get("started_at")),
            "run_type": r.get("run_type"),
            "model_used": r.get("model_used"),
            "model_short": _short_model(r.get("model_used")),
            "input_tokens": in_t,
            "output_tokens": out_t,
            "total_tokens": in_t + out_t,
            "cost_usd": cost,
        })

    mtd_cost = sum(float(r.get("estimated_cost_usd") or 0.0) for r in mtd_runs)
    days_elapsed = max(today_ist.day, 1)
    projected_cost = (mtd_cost / days_elapsed) * 30 if mtd_cost else 0.0

    return {
        "runs": runs,
        "today_tokens": today_tokens,
        "today_cost_usd": today_cost,
        "today_cost_inr": today_cost * config.USD_TO_INR,
        "mtd_cost_usd": mtd_cost,
        "mtd_cost_inr": mtd_cost * config.USD_TO_INR,
        "projected_cost_usd": projected_cost,
        "projected_cost_inr": projected_cost * config.USD_TO_INR,
    }


# ---------------------------------------------------------------------------
# Trading performance block
# ---------------------------------------------------------------------------

def build_perf_block(today_ist: date, *, executed_rows_with_pnl: list[dict[str, Any]]) -> dict[str, Any]:
    today_start, today_end = _ist_day_bounds_utc(today_ist)
    recs = supabase_client.get_recommendations_between(today_start, today_end)
    executed = [r for r in recs if r.get("user_executed")]
    skipped = len(recs) - len(executed)
    paper_count = sum(1 for r in recs if r.get("paper_trade"))
    real_count = len(recs) - paper_count

    # Index outcomes by ticker+action so we can attach P&L to executed recs
    by_key = {(b.get("ticker"), b.get("action")): b for b in executed_rows_with_pnl}

    rows: list[dict[str, Any]] = []
    awaiting = False
    for rec in executed:
        outcome = by_key.get((rec.get("ticker"), rec.get("action")))
        if outcome is None:
            awaiting = True
            rows.append({
                "ticker": rec.get("ticker"), "action": rec.get("action"),
                "actual_pnl_inr": None, "mark": "⏳",
            })
        else:
            pnl = outcome.get("actual_pnl_inr") or 0
            rows.append({
                "ticker": rec.get("ticker"), "action": rec.get("action"),
                "actual_pnl_inr": pnl, "mark": "✅" if pnl >= 0 else "❌",
            })

    total_pnl = sum((r.get("actual_pnl_inr") or 0) for r in rows if r.get("actual_pnl_inr") is not None)
    loss_used = max(0.0, -total_pnl)

    pnl_rows = [r for r in rows if r.get("actual_pnl_inr") is not None]
    best = max(pnl_rows, key=lambda r: r["actual_pnl_inr"], default=None)
    worst = min(pnl_rows, key=lambda r: r["actual_pnl_inr"], default=None)

    avg_alpha = None
    if executed_rows_with_pnl:
        alphas = [b.get("alpha_pct") for b in executed_rows_with_pnl if b.get("alpha_pct") is not None]
        if alphas:
            avg_alpha = sum(alphas) / len(alphas)

    return {
        "recs_count": len(recs),
        "executed_count": len(executed),
        "skipped_count": skipped,
        "paper_count": paper_count,
        "real_count": real_count,
        "executed_rows": rows,
        "total_pnl": total_pnl if rows else 0,
        "loss_limit_used": loss_used,
        "loss_limit": config.DAILY_LOSS_LIMIT,
        "alpha": avg_alpha,
        "best": best,
        "worst": worst,
        "awaiting_prices": awaiting and not pnl_rows,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> dict[str, Any]:
    today_ist = datetime.now(_IST).date()
    today_str = today_ist.isoformat()

    # 1. Compute & persist outcomes for today's recommendations
    today_start, today_end = _ist_day_bounds_utc(today_ist)
    recs = supabase_client.get_recommendations_between(today_start, today_end)
    nifty = market_context.fetch_nifty_spot() or {}
    nifty_return = float(nifty.get("pct_change") or 0.0)

    outcome_rows: list[dict[str, Any]] = []
    for rec in recs:
        try:
            row = outcome_tracker.compute_outcome(rec, nifty_return_pct=nifty_return)
            if row is None:
                continue
            supabase_client.insert_backtest_result(row)
            outcome_rows.append(row)
        except Exception as exc:
            log.exception("outcome compute failed for %s: %s", rec.get("ticker"), exc)

    # 2. Usage and 3. Perf blocks
    usage = build_usage_block(today_ist)
    perf = build_perf_block(today_ist, executed_rows_with_pnl=outcome_rows)

    body = telegram_bot.format_eod_summary(today_str, usage, perf)
    if config.PAPER_TRADING:
        body = telegram_bot.PAPER_TRADING_BANNER + "\n\n" + body
    telegram_bot.send_alert(body)
    return {"date": today_str, "usage": usage, "perf": perf, "outcome_rows": outcome_rows}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
