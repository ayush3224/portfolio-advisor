"""3:00 PM IST orchestrator — EOD exit / hold decisions.

CNC swing only. Asks Haiku whether each holding should hold overnight, trim,
or fully exit into the close. No intraday MIS positions to square off.
"""

from __future__ import annotations

import logging
from typing import Any

import config
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


def _maybe_banner(body: str) -> str:
    if config.PAPER_TRADING:
        return telegram_bot.PAPER_TRADING_BANNER + "\n\n" + body
    return body


def run() -> dict[str, Any]:
    if not risk_guardrails.check_market_hours() and not config.PAPER_TRADING:
        log.info("Market closed — eod run aborted")
        return {"decisions": [], "skipped": "market_closed"}

    snapshot = upstox_portfolio.get_portfolio_snapshot("eod")
    holdings = snapshot.get("holdings") or []

    blocks: list[dict[str, Any]] = []
    for h in holdings:
        b = _to_block(h, "CNC")
        if b:
            blocks.append(b)

    if not blocks:
        telegram_bot.send_alert(_maybe_banner(telegram_bot.format_eod([])))
        return {"decisions": []}

    result = eod_prompt.run(blocks)
    decisions = result.get("decisions") or []

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

    telegram_bot.send_alert(_maybe_banner(telegram_bot.format_eod(persisted)))
    return {"decisions": persisted}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
