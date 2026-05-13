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

import asyncio
import concurrent.futures
import logging
from typing import Any

import config
from analysis import premarket_prompt
from delivery import telegram_bot
from ingestion import market_context, polymarket, upstox_portfolio
from processing import portfolio_context, position_sizer, risk_guardrails
from storage import supabase_client

log = logging.getLogger(__name__)


def _attach_sizing(rec: dict[str, Any]) -> dict[str, Any] | None:
    sizing = position_sizer.size_for(int(rec.get("confidence_score") or 0), rec.get("entry_price"))
    if sizing is None:
        return None
    return {
        **rec,
        "leverage_multiplier": 1,  # CNC swing — always 1x
        "capital_deployed": sizing.capital_deployed,
        "shares_qty": sizing.shares_qty,
        "is_paper": sizing.is_paper,
        "paper_trade": sizing.is_paper,
    }


def run() -> dict[str, Any]:
    snapshot = upstox_portfolio.get_portfolio_snapshot("premarket")
    # IND premarket only — strip US holdings so they don't enter the Sonnet prompt.
    snapshot["holdings"] = [
        h for h in (snapshot.get("holdings") or [])
        if (h.get("market") or "IND").upper() == "IND"
    ]
    snapshot_id = snapshot.get("id")
    if not snapshot_id and not config.DRY_RUN:
        msg = "premarket aborted — portfolio_snapshot persist failed (no UUID returned)"
        log.error(msg)
        try:
            telegram_bot.send_alert("⚠️ " + msg)
        except Exception:
            log.exception("failed to send abort alert")
        return {"recommendations": [], "rejected": [], "skipped": [], "aborted": True}

    context = portfolio_context.build_full_context(snapshot)
    macro = market_context.get_market_context()

    # Polymarket prediction-market signals — best-effort enrichment of `macro`.
    # Any network failure inside polymarket returns [] and is logged there.
    try:
        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                poly_markets = ex.submit(
                    asyncio.run, polymarket.fetch_relevant_markets("india")
                ).result()
        except RuntimeError:
            poly_markets = asyncio.run(polymarket.fetch_relevant_markets("india"))
    except Exception as exc:
        log.warning("Polymarket fetch failed: %s", exc)
        poly_markets = []
    macro["polymarket"] = poly_markets
    macro["polymarket_text"] = polymarket.format_for_prompt(poly_markets)

    result = premarket_prompt.run(context, macro)
    raw_recs = result.get("recommendations") or []

    sized: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for rec in raw_recs:
        try:
            conf = int(rec.get("confidence_score") or 0)
            if rec.get("skipped") or conf < 6:
                skipped.append({
                    "ticker": rec.get("ticker"),
                    "confidence_score": conf,
                    "reasoning": rec.get("reasoning"),
                })
                continue
            row = _attach_sizing(rec)
            if row is None:
                # Defensive: conf>=6 but sizer rejected — surface as skipped too.
                skipped.append({
                    "ticker": rec.get("ticker"),
                    "confidence_score": conf,
                    "reasoning": rec.get("reasoning"),
                })
                continue
            sized.append(row)
        except Exception as exc:
            log.exception("sizing failed for %s: %s", rec.get("ticker"), exc)

    passing, rejected = risk_guardrails.apply(
        sized,
        portfolio_value=float(context.get("total_value") or 0),
        sector_allocation=context.get("sector_allocation") or {},
    )

    persisted: list[dict[str, Any]] = []
    for rec in passing:
        try:
            rec["snapshot_id"] = snapshot_id
            rec_id = supabase_client.insert_recommendation(rec)
            persisted.append({**rec, "id": rec_id})
        except Exception as exc:
            log.exception("insert_recommendation failed for %s: %s", rec.get("ticker"), exc)

    # Enrich HOLD/TIGHTEN-SL recs with the user's actual position from the
    # snapshot so the Telegram formatter can show qty/avg/unrealised P&L.
    holdings_by_ticker = {
        h.get("ticker") or h.get("trading_symbol"): h
        for h in (snapshot.get("holdings") or [])
    }
    for rec in persisted:
        if (rec.get("action") or "").upper() in ("HOLD", "TIGHTEN-SL"):
            h = holdings_by_ticker.get(rec.get("ticker"))
            if h:
                rec["held_qty"] = h.get("quantity")
                rec["held_avg_price"] = h.get("average_price")
                rec["held_unrealised_pnl"] = h.get("unrealised_pnl")
                rec["held_unrealised_pnl_pct"] = h.get("unrealised_pnl_pct")

    body = telegram_bot.format_premarket(persisted, skipped=skipped)
    warning = risk_guardrails.event_warning_text(risk_guardrails.check_event_tomorrow())
    if warning:
        body = warning + "\n\n" + body
    if config.PAPER_TRADING:
        body = telegram_bot.PAPER_TRADING_BANNER + "\n\n" + body
    telegram_bot.send_alert(body)
    log.info("premarket complete — %d passing, %d rejected, %d skipped",
             len(persisted), len(rejected), len(skipped))
    return {
        "recommendations": persisted,
        "rejected": rejected,
        "skipped": skipped,
        "event_warning": warning,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
