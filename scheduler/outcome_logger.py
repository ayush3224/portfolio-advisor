"""4:00 PM IST orchestrator — outcome logger. No Claude calls (free).

For every recommendation made today, look up the closing price, compute
return vs entry, compute Nifty's same-day return for alpha, and write a
backtest_results row. Then post a quick scorecard to Telegram.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from delivery import telegram_bot
from ingestion import market_context, upstox_prices
from storage import supabase_client
from backtest import outcome_tracker

log = logging.getLogger(__name__)


def run() -> dict[str, Any]:
    today = datetime.now(timezone.utc).date().isoformat()
    recs = supabase_client.get_recommendations_for_date(today)
    if not recs:
        telegram_bot.send_alert(telegram_bot.format_outcome([]))
        return {"rows": []}

    nifty = market_context.fetch_nifty_spot() or {}
    nifty_return = float(nifty.get("pct_change") or 0.0)

    rows: list[dict[str, Any]] = []
    for rec in recs:
        try:
            row = outcome_tracker.compute_outcome(rec, nifty_return_pct=nifty_return)
            if row is None:
                continue
            supabase_client.insert_backtest_result(row)
            rows.append(row)
        except Exception as exc:
            log.exception("outcome compute failed for %s: %s", rec.get("ticker"), exc)

    telegram_bot.send_alert(telegram_bot.format_outcome(rows))
    return {"rows": rows}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
