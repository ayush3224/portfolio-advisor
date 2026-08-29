"""Portfolio manager — BUY/SELL/QUOTE/PORTFOLIO primitives backed by Supabase.

This module is the *only* place that mutates the holdings/transactions tables.
All write paths preserve invariants:
  - holdings.quantity reflects open shares for is_active=true rows
  - transactions is append-only; one row per BUY or SELL leg
  - SELL of full quantity flips is_active=false (history retained)

Live prices for the post-trade snapshot come from ingestion.upstox_market_data
(Upstox v3 LTP → yfinance fallback).
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any

import config
from ingestion import upstox_market_data, upstox_portfolio
from storage import supabase_client

log = logging.getLogger(__name__)


# Common US tickers we recognise without an explicit market flag.
# Adding to this list is cheap; the source of truth is still the `market`
# column stored on each holding row.
_KNOWN_US_TICKERS = {
    "AAPL", "MSFT", "AMZN", "GOOG", "GOOGL", "META", "NVDA", "TSLA",
    "NFLX", "AMD", "INTC", "IBM", "ORCL", "CRM", "ADBE", "PEP", "KO",
    "WMT", "JPM", "BAC", "GS", "MS", "C", "BRK.A", "BRK.B", "BRK-B",
    "V", "MA", "DIS", "HD", "MCD", "NKE", "XOM", "CVX", "BP", "BKR",
    "DUK", "PLTR", "PANW", "SOXX", "EQIX", "RTX", "TSM", "PSI",
    "IBKR", "IAU", "GLD", "SLV", "AAAU",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_market_cache: dict[str, str] = {}


def _market_from_holdings(ticker: str) -> str | None:
    """`market` as recorded on the ticker's holding row — the source of truth
    for anything already owned, which covers every US name the hardcoded set
    below doesn't know about. Cached per process; never fatal."""
    if ticker in _market_cache:
        return _market_cache[ticker]
    client = supabase_client.get_client()
    if client is None:
        return None
    try:
        res = (
            client.table("holdings")
            .select("market")
            .eq("ticker", ticker)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
    except Exception as exc:
        log.warning("market lookup failed for %s: %s", ticker, exc)
        return None
    if not rows or not rows[0].get("market"):
        return None
    market = str(rows[0]["market"]).upper()
    _market_cache[ticker] = market
    return market


def detect_market(ticker: str) -> str:
    """Resolve a ticker to 'IND' or 'US'.

    Order: Upstox instrument key (definitively NSE) → the `market` column on
    an existing holding → the known-US set → default IND. For a genuinely
    ambiguous new symbol, callers should pass an explicit market via BUY-US.
    """
    t = ticker.upper().strip()
    if config.instrument_key_for(t):
        return "IND"
    held = _market_from_holdings(t)
    if held in ("IND", "US"):
        return held
    if t in _KNOWN_US_TICKERS:
        return "US"
    return "IND"  # safest default — most users hold Indian stocks


def _yf_quote_for_ticker(ticker: str) -> dict[str, Any] | None:
    """Quote for a US ticker — delegates to the shared yfinance path so the
    bot and the schedulers agree on what "live US price" means."""
    return upstox_market_data.get_us_quote(ticker)


def get_cmp_for_display(ticker: str, market: str | None = None) -> float | None:
    """Live CMP for a bot reply, from the right source for the market.

    US → yfinance (Upstox carries no US instruments, so asking it returns
    nothing and the reply used to fall back to a NaN / the entry price).
    IND → Upstox v3 LTP, with the module's own yfinance `.NS` fallback.

    Returns None when no live price is available; callers decide what to
    show instead (usually the average price).
    """
    ticker = ticker.upper().strip()
    resolved = (market or detect_market(ticker)).upper()
    if resolved == "US":
        quote = upstox_market_data.get_us_quote(ticker)
    else:
        quote = upstox_market_data.get_live_quote_sync(ticker)
    if not quote:
        return None
    ltp = quote.get("ltp")
    if ltp is None:
        return None
    try:
        value = float(ltp)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


# --- Currency sanity check for US BUYs -------------------------------------
# A US price typed in rupees (e.g. "BUY NVDA 0.1 18000") silently books a
# position at ~190x its real cost basis and poisons every P&L downstream.
# Anything above this threshold is rejected as a suspected INR price...
US_PRICE_SANITY_LIMIT = 5000.0

# ...except for the handful of US names that legitimately trade above it.
# GOOGL is here defensively (post-split it trades ~$200, but the list is
# cheap insurance against a future split-adjusted spike).
HIGH_PRICE_US_TICKERS = {
    "EQIX", "BRK-B", "BRK.B", "BRK-A", "BRK.A", "GOOGL", "NVR", "BKNG",
}

# Shown on HELP and on every US BUY confirmation — the whole class of bug
# above starts with typing a rupee price into a dollar-denominated trade.
CURRENCY_HINT = (
    "💡 US stocks: price in USD ($)\n"
    "Indian stocks: price in INR (₹)"
)


def _currency_rejection(ticker: str, quantity: float, price: float) -> dict[str, Any]:
    """Reply payload for a US BUY whose price looks like INR. Nothing is
    written to the database — the command is refused outright."""
    live = get_cmp_for_display(ticker, "US")
    live_str = f"${live:,.2f}" if live is not None else "unavailable"
    suggested = f"{live:.2f}" if live is not None else "<USD_PRICE>"
    reply = (
        f"❌ Price ₹{price:,.2f} looks like INR.\n"
        f"US stocks need price in USD ($).\n"
        f"Example: BUY {ticker} {quantity:g} {suggested}\n\n"
        f"Current {ticker} price: {live_str}\n\n"
        f"Resend with USD price to confirm.\n\n"
        f"{CURRENCY_HINT}"
    )
    return {
        "success": False,
        "error": f"{ticker}: price {price:g} looks like INR, not USD",
        "reply": reply,
        "rejected": "currency",
        "current_price": live,
    }


def _quote_ltp(quote: dict[str, Any] | None) -> float | None:
    """LTP out of a quote payload, with NaN treated as missing."""
    if not quote or quote.get("ltp") is None:
        return None
    try:
        value = float(quote["ltp"])
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


def _fetch_existing(ticker: str) -> dict[str, Any] | None:
    client = supabase_client.get_client()
    if client is None:
        return None
    res = (
        client.table("holdings")
        .select("*")
        .eq("ticker", ticker)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def _insert_transaction(payload: dict[str, Any]) -> None:
    client = supabase_client.get_client()
    if client is None:
        return
    try:
        client.table("transactions").insert(payload).execute()
    except Exception as exc:
        log.exception("transaction insert failed: %s", exc)


def match_to_recommendation(ticker: str, action: str, price: float | None = None) -> str | None:
    """Attribute a manual trade to today's matching recommendation.

    Stamps the recommendation (and its backtest row) as executed and returns a
    line for the bot reply, or None when nothing matches. The scorecard's
    followed-vs-skipped split is only meaningful if this fires on every trade.
    """
    try:
        rec = supabase_client.match_and_mark_execution(
            ticker.upper().strip(), action, executed_price=price,
        )
    except Exception as exc:
        log.warning("recommendation match failed for %s/%s: %s", ticker, action, exc)
        return None
    if not rec:
        return None
    conf = rec.get("confidence_score")
    conf_str = f" (conf {conf:g}/10)" if conf is not None else ""
    return f"📋 Matched to today's {rec.get('action')} recommendation{conf_str}"


async def add_position(
    ticker: str, quantity: float, price: float, *, market: str | None = None,
) -> dict[str, Any]:
    ticker = ticker.upper().strip()
    if quantity <= 0 or price <= 0:
        return {"success": False, "error": "quantity and price must be positive"}

    client = supabase_client.get_client()
    if client is None:
        return {"success": False, "error": "Database unavailable"}

    resolved_market = (market or detect_market(ticker)).upper()
    if resolved_market not in ("IND", "US"):
        return {"success": False, "error": f"unknown market {resolved_market!r}"}
    currency = "USD" if resolved_market == "US" else "INR"

    # Currency guard runs before every write path: a rejected BUY must leave
    # holdings and transactions untouched.
    if (
        resolved_market == "US"
        and price > US_PRICE_SANITY_LIMIT
        and ticker not in HIGH_PRICE_US_TICKERS
    ):
        log.warning("rejected US BUY %s %g @ %g — price looks like INR", ticker, quantity, price)
        return _currency_rejection(ticker, quantity, price)

    existing = _fetch_existing(ticker)
    if existing:
        old_qty = float(existing["quantity"])
        old_avg = float(existing["average_price"])
        new_qty = old_qty + quantity
        new_avg = round((old_qty * old_avg + quantity * price) / new_qty, 4)
        try:
            client.table("holdings").update({
                "quantity": new_qty,
                "average_price": new_avg,
                "last_updated": _now_iso(),
            }).eq("id", existing["id"]).execute()
        except Exception as exc:
            return {"success": False, "error": f"update failed: {exc}"}
    else:
        new_qty = quantity
        new_avg = round(float(price), 4)
        try:
            client.table("holdings").insert({
                "ticker": ticker,
                "quantity": quantity,
                "average_price": new_avg,
                "market": resolved_market,
                "currency": currency,
                "is_active": True,
            }).execute()
        except Exception as exc:
            return {"success": False, "error": f"insert failed: {exc}"}

    _insert_transaction({
        "ticker": ticker,
        "action": "BUY",
        "quantity": quantity,
        "price": round(price, 4),
        "total_value": round(quantity * price, 4),
        "avg_price_at_trade": new_avg,
    })

    match_message = match_to_recommendation(ticker, "BUY", price)

    if resolved_market == "IND":
        quote = await upstox_market_data.get_live_quote(ticker)
    else:
        quote = await asyncio.to_thread(upstox_market_data.get_us_quote, ticker)
    cmp_ = _quote_ltp(quote)
    live_price = cmp_ if cmp_ is not None else new_avg
    unrealised = round((live_price - new_avg) * new_qty, 2)
    unrealised_pct = round((live_price - new_avg) / new_avg * 100, 4) if new_avg else 0.0

    return {
        "success": True,
        "ticker": ticker,
        "action": "BUY",
        "market": resolved_market,
        "currency": currency,
        "quantity_added": quantity,
        "total_quantity": new_qty,
        "average_price": new_avg,
        "buy_price": round(price, 4),
        "current_price": round(live_price, 2),
        "unrealised_pnl": unrealised,
        "unrealised_pnl_pct": unrealised_pct,
        "buy_total": round(quantity * price, 2),
        "quote_source": (quote or {}).get("source"),
        "match_message": match_message,
        "message": "BUY confirmed",
    }


async def close_position(ticker: str, quantity: float, price: float) -> dict[str, Any]:
    ticker = ticker.upper().strip()
    if quantity <= 0 or price <= 0:
        return {"success": False, "error": "quantity and price must be positive"}

    existing = _fetch_existing(ticker)
    if not existing:
        return {"success": False, "error": f"No holding found for {ticker}"}

    held_qty = float(existing["quantity"])
    if quantity > held_qty:
        return {
            "success": False,
            "error": f"Cannot sell {quantity} — only {held_qty} held",
        }

    avg_price = float(existing["average_price"])
    realised_pnl = round((price - avg_price) * quantity, 2)
    realised_pct = round((price - avg_price) / avg_price * 100, 4) if avg_price else 0.0

    client = supabase_client.get_client()
    if client is None:
        return {"success": False, "error": "Database unavailable"}

    if quantity == held_qty:
        remaining = 0
        try:
            client.table("holdings").update({
                "quantity": held_qty,  # leave as-is so audits can see the closed size
                "is_active": False,
                "last_updated": _now_iso(),
            }).eq("id", existing["id"]).execute()
        except Exception as exc:
            return {"success": False, "error": f"close failed: {exc}"}
    else:
        remaining = held_qty - quantity
        try:
            client.table("holdings").update({
                "quantity": remaining,
                "last_updated": _now_iso(),
            }).eq("id", existing["id"]).execute()
        except Exception as exc:
            return {"success": False, "error": f"partial-sell update failed: {exc}"}

    _insert_transaction({
        "ticker": ticker,
        "action": "SELL",
        "quantity": quantity,
        "price": round(price, 4),
        "total_value": round(quantity * price, 4),
        "realised_pnl": realised_pnl,
        "avg_price_at_trade": round(avg_price, 4),
    })

    match_message = match_to_recommendation(ticker, "SELL", price)

    market_v = (existing.get("market") or detect_market(ticker)).upper()
    if market_v == "IND":
        quote = await upstox_market_data.get_live_quote(ticker)
    else:
        quote = await asyncio.to_thread(upstox_market_data.get_us_quote, ticker)
    cmp_ = _quote_ltp(quote)
    live_price = cmp_ if cmp_ is not None else price

    return {
        "success": True,
        "ticker": ticker,
        "action": "SELL",
        "market": market_v,
        "currency": existing.get("currency") or ("USD" if market_v == "US" else "INR"),
        "quantity_sold": quantity,
        "remaining_quantity": remaining,
        "sell_price": round(price, 2),
        "sell_total": round(quantity * price, 2),
        "avg_price": round(avg_price, 2),
        "realised_pnl": realised_pnl,
        "realised_pnl_pct": realised_pct,
        "current_price": round(live_price, 2),
        "quote_source": (quote or {}).get("source"),
        "match_message": match_message,
        "message": "SELL confirmed",
    }


def get_portfolio() -> dict[str, Any]:
    """Live portfolio for the PORTFOLIO reply, split by market.

    IND and US holdings are aggregated in their own currency and only combined
    after the US leg is converted at config.USD_INR_RATE — adding a dollar
    subtotal straight onto a rupee one understates the portfolio by roughly
    the FX rate. The aggregation lives in upstox_portfolio._currency_totals so
    the schedulers, which render the same `total_value` with a ₹ sign, get the
    corrected number too.

    Alongside `holdings` the dict carries:
      ind_value_inr / ind_cost_inr / ind_pnl_inr / ind_pnl_pct
      us_value_usd  / us_cost_usd  / us_pnl_usd  / us_pnl_pct
      us_value_inr  / us_cost_inr  / us_pnl_inr
      total_value_inr / total_cost_inr / total_pnl_inr / total_pnl_pct
      usd_inr_rate, ind_count, us_count
    """
    return upstox_portfolio.fetch_portfolio()


async def get_quote(ticker: str) -> dict[str, Any] | None:
    """Live quote for QUOTE <TICKER>, routed by market.

    US tickers go to yfinance — Upstox has no US instruments, so routing them
    there returns nothing and the reply renders an empty/NaN price.
    """
    ticker = ticker.upper().strip()
    market = detect_market(ticker)
    if market == "US":
        quote = await asyncio.to_thread(upstox_market_data.get_us_quote, ticker)
    else:
        quote = await upstox_market_data.get_live_quote(ticker)
    if quote:
        quote.setdefault("market", market)
        quote.setdefault("currency", "USD" if market == "US" else "INR")
    return quote


def get_transactions(ticker: str, limit: int = 5) -> list[dict[str, Any]]:
    client = supabase_client.get_client()
    if client is None:
        return []
    ticker = ticker.upper().strip()
    try:
        res = (
            client.table("transactions")
            .select("*")
            .eq("ticker", ticker)
            .order("executed_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.exception("transactions read failed: %s", exc)
        return []


def count_active_holdings() -> int:
    client = supabase_client.get_client()
    if client is None:
        return 0
    try:
        res = (
            client.table("holdings")
            .select("id", count="exact")
            .eq("is_active", True)
            .execute()
        )
        return res.count or 0
    except Exception:
        return 0


def last_run_started_at(run_type: str = "premarket") -> str | None:
    client = supabase_client.get_client()
    if client is None:
        return None
    try:
        res = (
            client.table("run_log")
            .select("started_at")
            .eq("run_type", run_type)
            .order("started_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0]["started_at"] if rows else None
    except Exception:
        return None
