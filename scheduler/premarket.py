"""9:00 AM IST orchestrator — full pre-market portfolio advisory.

Pipeline:
  1. fetch portfolio snapshot (cached if < 5min)
  2. build per-holding context (prices + news + analyst signals)
  3. fetch market context
  4. call Sonnet for recommendations
  5. attach position sizing per confidence
  6. apply risk guardrails
  7. persist passing recs to advisor_recommendations
  8. send formatted Telegram alert
"""

from __future__ import annotations

import logging
from typing import Any

from analysis import premarket_prompt
from delivery import telegram_bot
from ingestion import market_context, upstox_portfolio
from processing import portfolio_context, position_sizer, risk_guardrails
from storage import supabase_client

log = logging.getLogger(__name__)


def _attach_sizing(rec: dict[str, Any]) -> dict[str, Any] | None:
    sizing = position_sizer.size_for(int(rec.get("confidence_score") or 0), rec.get("entry_price"))
    if sizing is None:
        return None
    return {
        **rec,
        "leverage_multiplier": sizing.leverage_multiplier,
        "capital_deployed": sizing.capital_deployed,
        "shares_qty": sizing.shares_qty,
    }


def run() -> dict[str, Any]:
    snapshot = upstox_portfolio.get_portfolio_snapshot("premarket")
    context = portfolio_context.build_full_context(snapshot)
    macro = market_context.get_market_context()

    result = premarket_prompt.run(context, macro)
    raw_recs = result.get("recommendations") or []

    sized: list[dict[str, Any]] = []
    for rec in raw_recs:
        try:
            row = _attach_sizing(rec)
            if row is None:
                continue
            sized.append(row)
        except Exception as exc:
            log.exception("sizing failed for %s: %s", rec.get("ticker"), exc)

    passing, rejected = risk_guardrails.apply(
        sized,
        portfolio_value=float(context.get("total_value") or 0),
        sector_allocation=context.get("sector_allocation") or {},
    )

    snapshot_id = snapshot.get("id")
    persisted: list[dict[str, Any]] = []
    for rec in passing:
        try:
            rec["snapshot_id"] = snapshot_id
            rec_id = supabase_client.insert_recommendation(rec)
            persisted.append({**rec, "id": rec_id})
        except Exception as exc:
            log.exception("insert_recommendation failed for %s: %s", rec.get("ticker"), exc)

    body = telegram_bot.format_premarket(persisted)
    warning = risk_guardrails.event_warning_text(risk_guardrails.check_event_tomorrow())
    if warning:
        body = warning + "\n\n" + body
    telegram_bot.send_alert(body)
    log.info("premarket complete — %d passing, %d rejected", len(persisted), len(rejected))
    return {"recommendations": persisted, "rejected": rejected, "event_warning": warning}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
