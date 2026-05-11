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

import logging
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

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


def detect_market(ticker: str) -> str:
    """Heuristic: ticker maps to NSE if in INSTRUMENT_KEYS, else US if known,
    else best-guess by name length (NSE symbols are usually ≤10 chars). For
    ambiguity, callers should pass an explicit market via BUY-US."""
    t = ticker.upper().strip()
    if config.instrument_key_for(t):
        return "IND"
    if t in _KNOWN_US_TICKERS:
        return "US"
    return "IND"  # safest default — most users hold Indian stocks


def _yf_quote_for_ticker(ticker: str) -> dict[str, Any] | None:
    """Quote for a US ticker via yfinance (no .NS suffix).
    Berkshire's BRK.B is served as BRK-B on Yahoo — normalise dots to hyphens."""
    try:
        t = yf.Ticker(ticker.upper().replace(".", "-"))
        hist = t.history(period="2d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        last = hist.iloc[-1]
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
        return {
            "ticker": ticker.upper(),
            "ltp": round(float(last["Close"]), 2),
            "prev_close": prev_close,
            "open": round(float(last["Open"]), 2),
            "high": round(float(last["High"]), 2),
            "low": round(float(last["Low"]), 2),
            "volume": int(last["Volume"]) if last["Volume"] else None,
            "source": "yfinance",
        }
    except Exception as exc:
        log.warning("yfinance US quote for %s failed: %s", ticker, exc)
        return None


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

    if resolved_market == "IND":
        quote = await upstox_market_data.get_live_quote(ticker)
    else:
        quote = _yf_quote_for_ticker(ticker)
    live_price = float(quote["ltp"]) if quote and quote.get("ltp") else new_avg
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
        "current_price": round(live_price, 2),
        "unrealised_pnl": unrealised,
        "unrealised_pnl_pct": unrealised_pct,
        "buy_total": round(quantity * price, 2),
        "quote_source": (quote or {}).get("source"),
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

    market_v = (existing.get("market") or detect_market(ticker)).upper()
    if market_v == "IND":
        quote = await upstox_market_data.get_live_quote(ticker)
    else:
        quote = _yf_quote_for_ticker(ticker)
    live_price = float(quote["ltp"]) if quote and quote.get("ltp") else price

    return {
        "success": True,
        "ticker": ticker,
        "action": "SELL",
        "quantity_sold": quantity,
        "remaining_quantity": remaining,
        "sell_price": round(price, 2),
        "sell_total": round(quantity * price, 2),
        "avg_price": round(avg_price, 2),
        "realised_pnl": realised_pnl,
        "realised_pnl_pct": realised_pct,
        "current_price": round(live_price, 2),
        "message": "SELL confirmed",
    }


def get_portfolio() -> dict[str, Any]:
    return upstox_portfolio.fetch_portfolio()


async def get_quote(ticker: str) -> dict[str, Any] | None:
    ticker = ticker.upper().strip()
    if detect_market(ticker) == "US":
        return _yf_quote_for_ticker(ticker)
    return await upstox_market_data.get_live_quote(ticker)


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
