"""3:00 PM IST orchestrator — EOD exit / hold decisions.

By 3:00 PM all leveraged MIS positions need to be in exit mode (full flat by
3:15). For delivery (CNC) holdings we also ask Haiku whether to hold overnight
or trim into the close.
"""

from __future__ import annotations

import logging
from typing import Any

from analysis import eod_prompt
from delivery import telegram_bot
from ingestion import upstox_portfolio, upstox_prices
from processing import risk_guardrails
from storage import supabase_client

log = logging.getLogger(__name__)


def _to_block(row: dict[str, Any], product: str) -> dict[str, Any] | None:
    try:
        ticker = row.get("trading_symbol") or row.get("tradingsymbol") or row.get("symbol")
        instrument_key = row.get("instrument_token") or row.get("instrument_key")
        if not ticker:
            return None
        price = upstox_prices.enrich_price_block(instrument_key) if instrument_key else {}
        return {
            "ticker": ticker,
            "product": product,            # MIS or CNC
            "qty": int(row.get("quantity") or 0),
            "entry_price": float(row.get("average_price") or row.get("avg_price") or 0),
            "cmp": price.get("cmp") or row.get("last_price"),
            "vwap": price.get("vwap"),
            "high": price.get("high"),
            "low": price.get("low"),
        }
    except Exception as exc:
        log.exception("EOD block failed for %s: %s", row.get("trading_symbol"), exc)
        return None


def run() -> dict[str, Any]:
    if not risk_guardrails.check_market_hours():
        log.info("Market closed — eod run aborted")
        return {"decisions": [], "skipped": "market_closed"}

    force_exit_mis = risk_guardrails.must_force_exit_mis()
    snapshot = upstox_portfolio.get_portfolio_snapshot("eod")
    positions = snapshot.get("positions") or []
    holdings = snapshot.get("holdings") or []

    blocks: list[dict[str, Any]] = []
    for p in positions:
        if int(p.get("quantity") or 0) == 0:
            continue
        b = _to_block(p, "MIS")
        if b:
            blocks.append(b)
    for h in holdings:
        b = _to_block(h, "CNC")
        if b:
            blocks.append(b)

    if not blocks:
        telegram_bot.send_alert(telegram_bot.format_eod([]))
        return {"decisions": []}

    result = eod_prompt.run(blocks)
    decisions = result.get("decisions") or []

    # MIS force-exit override: after MIS_FORCE_EXIT_AFTER_IST every MIS row
    # MUST be EXIT-FULL regardless of what the model decided.
    by_ticker = {b["ticker"]: b for b in blocks}
    if force_exit_mis:
        log.info("Past MIS force-exit threshold — overriding all MIS decisions to EXIT-FULL")
        for d in decisions:
            block = by_ticker.get(d.get("ticker"))
            if block and block.get("product") == "MIS":
                d["action"] = "EXIT-FULL"
                d["hold_overnight"] = False
                d["reasoning"] = (d.get("reasoning") or "") + " [forced: past MIS exit threshold]"
        # Cover any MIS rows the model omitted entirely
        decided = {d.get("ticker") for d in decisions}
        for b in blocks:
            if b.get("product") == "MIS" and b["ticker"] not in decided:
                decisions.append({
                    "ticker": b["ticker"],
                    "action": "EXIT-FULL",
                    "hold_overnight": False,
                    "reasoning": "forced: past MIS exit threshold; no model decision",
                })

    persisted: list[dict[str, Any]] = []
    for d in decisions:
        try:
            row = {
                "ticker": d.get("ticker"),
                "action": d.get("action"),
                "reasoning": d.get("reasoning"),
                "hold_overnight": bool(d.get("hold_overnight", False)),
            }
            supabase_client.insert_eod_recommendation(row)
            persisted.append(row)
        except Exception as exc:
            log.exception("eod persist failed for %s: %s", d.get("ticker"), exc)

    telegram_bot.send_alert(telegram_bot.format_eod(persisted))
    return {"decisions": persisted, "mis_forced_exit": force_exit_mis}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
