"""Monday morning weekly scorecard — Haiku 4.5, summarises the trailing week.

Reads backtest_results for the prior 7 days, builds aggregate metrics, and
posts a one-screen recap to Telegram. Falls back to a deterministic summary
if Claude is unavailable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from analysis import claude_client
from delivery import telegram_bot
from storage import supabase_client

log = logging.getLogger(__name__)

MODEL = config.HAIKU_MODEL

SYSTEM = """You are summarising the previous week's trading results for a retail
investor. You receive a JSON array of backtest_result rows. Produce a brief,
honest critique:
  - what worked, what did not
  - any pattern in winners vs losers (sector, confidence, leverage)
  - one specific behavioural recommendation for next week
Keep it under 120 words. Plain prose, no markdown headers."""


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"trades": 0, "win_rate": 0, "total_pnl": 0, "avg_alpha": 0,
                "best_trade": "—", "worst_trade": "—"}
    wins = [r for r in rows if (r.get("actual_pnl_inr") or 0) > 0]
    total_pnl = sum((r.get("actual_pnl_inr") or 0) for r in rows)
    avg_alpha = sum((r.get("alpha_pct") or 0) for r in rows) / len(rows)
    best = max(rows, key=lambda r: (r.get("actual_pnl_inr") or 0))
    worst = min(rows, key=lambda r: (r.get("actual_pnl_inr") or 0))
    return {
        "trades": len(rows),
        "win_rate": len(wins) / len(rows),
        "total_pnl": total_pnl,
        "avg_alpha": avg_alpha,
        "best_trade": f"{best.get('ticker')} ₹{best.get('actual_pnl_inr', 0):,.0f}",
        "worst_trade": f"{worst.get('ticker')} ₹{worst.get('actual_pnl_inr', 0):,.0f}",
    }


def run() -> dict[str, Any]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    rows = supabase_client.get_backtest_results_between(start.isoformat(), end.isoformat())
    summary = _aggregate(rows)
    summary["start"] = start.isoformat()
    summary["end"] = end.isoformat()

    critique = ""
    if rows:
        result = claude_client.call_claude(
            model=MODEL,
            system=SYSTEM,
            user="## Trailing-week trades\n" + json.dumps(rows, default=str, indent=2),
            run_type="weekly_scorecard",
            max_tokens=512,
        )
        critique = result["text"].strip()

    body = telegram_bot.format_weekly_scorecard(summary)
    if critique:
        body += "\n<i>" + critique + "</i>"
    telegram_bot.send_alert(body)
    return {"summary": summary, "critique": critique}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
