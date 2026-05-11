"""Sunday 8:00 PM IST orchestrator — weekly recap.

Aggregates the trailing 7 days of advisor_recommendations and backtest_results
into a single Telegram message. Deterministic — no Claude call. The Haiku
critique already lives in backtest/weekly_scorecard.py and is invoked
separately.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from delivery import telegram_bot
from storage import supabase_client

log = logging.getLogger(__name__)


def _aggregate(recs: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    by_action: dict[str, int] = {}
    for r in recs:
        action = r.get("action") or "?"
        by_action[action] = by_action.get(action, 0) + 1

    executed = [r for r in recs if r.get("user_executed")]
    paper = [r for r in recs if r.get("paper_trade")]

    wins = [o for o in outcomes if (o.get("actual_pnl_inr") or 0) > 0]
    losses = [o for o in outcomes if (o.get("actual_pnl_inr") or 0) < 0]
    total_pnl = sum((o.get("actual_pnl_inr") or 0) for o in outcomes)

    alphas = [o.get("alpha_pct") for o in outcomes if o.get("alpha_pct") is not None]
    avg_alpha = (sum(alphas) / len(alphas)) if alphas else None

    best = max(outcomes, key=lambda o: o.get("actual_pnl_inr") or 0, default=None)
    worst = min(outcomes, key=lambda o: o.get("actual_pnl_inr") or 0, default=None)

    return {
        "recs_total": len(recs),
        "executed": len(executed),
        "paper": len(paper),
        "by_action": by_action,
        "trades": len(outcomes),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(outcomes)) if outcomes else 0.0,
        "total_pnl": total_pnl,
        "avg_alpha": avg_alpha,
        "best": best,
        "worst": worst,
    }


def run() -> dict[str, Any]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)

    recs = supabase_client.get_recommendations_between(start.isoformat(), end.isoformat())
    outcomes = supabase_client.get_backtest_results_between(
        start.date().isoformat(), end.date().isoformat()
    )

    summary = _aggregate(recs, outcomes)
    summary["start"] = start.date().isoformat()
    summary["end"] = end.date().isoformat()

    body = telegram_bot.format_weekly_review(summary)
    if config.PAPER_TRADING:
        body = telegram_bot.PAPER_TRADING_BANNER + "\n\n" + body
    telegram_bot.send_alert(body)

    log.info(
        "weekly_review complete — recs=%d trades=%d pnl=%s",
        summary["recs_total"], summary["trades"], summary["total_pnl"],
    )
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
