"""12:30 PM IST orchestrator — leveraged-position intraday check.

Only inspects positions opened today (Upstox short-term-positions). For each
open position we build a tiny live snapshot (CMP, VWAP, P&L, volume ratio)
and ask Haiku for HOLD / TIGHTEN-SL / EXIT-PARTIAL / EXIT-FULL.
"""

from __future__ import annotations

import logging
from typing import Any

from analysis import midday_prompt
from delivery import telegram_bot
from ingestion import upstox_portfolio, upstox_prices
from processing import risk_guardrails
from storage import supabase_client

log = logging.getLogger(__name__)


def _build_position_block(pos: dict[str, Any]) -> dict[str, Any] | None:
    try:
        ticker = pos.get("trading_symbol") or pos.get("tradingsymbol") or pos.get("symbol")
        instrument_key = pos.get("instrument_token") or pos.get("instrument_key")
        if not ticker:
            return None
        entry = float(pos.get("average_price") or pos.get("avg_price") or 0)
        qty = int(pos.get("quantity") or 0)
        price = upstox_prices.enrich_price_block(instrument_key) if instrument_key else {}
        cmp_ = price.get("cmp") or pos.get("last_price")
        pnl = (float(cmp_) - entry) * qty if cmp_ and entry else None
        vwap = price.get("vwap")
        vwap_pos = "above" if (cmp_ and vwap and float(cmp_) > float(vwap)) else "below" if (cmp_ and vwap) else None
        return {
            "ticker": ticker,
            "entry_price": entry,
            "cmp": cmp_,
            "qty": qty,
            "pnl_unrealised": pnl,
            "vwap": vwap,
            "vwap_position": vwap_pos,
            "volume": price.get("volume"),
            "high": price.get("high"),
            "low": price.get("low"),
        }
    except Exception as exc:
        log.exception("position block failed: %s", exc)
        return None


def run() -> dict[str, Any]:
    if not risk_guardrails.check_market_hours():
        log.info("Market closed — midday run aborted")
        return {"decisions": [], "checks": [], "skipped": "market_closed"}

    snapshot = upstox_portfolio.get_portfolio_snapshot("midday")
    positions = snapshot.get("positions") or []
    leveraged = [p for p in positions if int(p.get("quantity") or 0) != 0]
    blocks = [b for b in (_build_position_block(p) for p in leveraged) if b]

    if not blocks:
        telegram_bot.send_alert(telegram_bot.format_midday([]))
        return {"decisions": [], "checks": []}

    result = midday_prompt.run(blocks)
    decisions = result.get("decisions") or []
    by_ticker = {d.get("ticker"): d for d in decisions}

    persisted: list[dict[str, Any]] = []
    for b in blocks:
        try:
            d = by_ticker.get(b["ticker"], {"action": "HOLD", "reasoning": "no model decision"})
            check = {
                "ticker": b["ticker"],
                "entry_price": b["entry_price"],
                "cmp": b["cmp"],
                "pnl_unrealised": b["pnl_unrealised"],
                "vwap_position": b["vwap_position"],
                "volume_ratio": None,
                "action": d.get("action", "HOLD"),
                "new_sl": d.get("new_sl"),
                "reasoning": d.get("reasoning"),
            }
            supabase_client.insert_midday_check(check)
            persisted.append(check)
        except Exception as exc:
            log.exception("midday persist failed for %s: %s", b.get("ticker"), exc)

    telegram_bot.send_alert(telegram_bot.format_midday(persisted))
    return {"decisions": decisions, "checks": persisted}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
