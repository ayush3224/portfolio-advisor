"""Per-recommendation outcome computation for the 4 PM logger.

Computes return vs entry, alpha vs market benchmark (Nifty for IND, S&P 500 for
US), classifies win/loss/neutral, and produces a backtest_results row.

Action semantics:
  - BUY family (BUY, BUY-MOMENTUM, BUY-EVENT, ADD): win if alpha > +0.5%
  - HOLD / TIGHTEN-SL: same alpha logic — recommendation succeeds if the stock
    you kept beat the benchmark
  - EXIT family (EXIT-PARTIAL, EXIT-FULL, PARTIAL-EXIT, FULL-EXIT, SELL): win
    if price fell more than 0.5% after the call (you got out ahead of a drop);
    raw return drives the verdict, not alpha
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Yahoo helpers
# ---------------------------------------------------------------------------

def _yf_today_open_close(symbol: str) -> tuple[float, float] | None:
    """Return (open, close) for the latest available trading day. None if the
    symbol has no usable rows — falls back to the prior day when today's bar is
    still NaN (common right after market close for NSE tickers and ^NSEI)."""
    try:
        hist = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
    except Exception as exc:
        log.warning("yfinance fetch %s failed: %s", symbol, exc)
        return None
    if hist is None or hist.empty:
        return None
    # Walk backwards from the latest row; take the first non-NaN pair.
    for i in range(len(hist) - 1, -1, -1):
        row = hist.iloc[i]
        o = float(row["Open"]); c = float(row["Close"])
        if not math.isnan(o) and not math.isnan(c):
            return o, c
    return None


def _bench_return_pct(symbol: str) -> float | None:
    oc = _yf_today_open_close(symbol)
    if oc is None:
        return None
    o, c = oc
    return (c - o) / o * 100 if o else None


def fetch_benchmarks() -> dict[str, float]:
    """Latest-day benchmark returns. Empty dict if yfinance is unreachable."""
    out: dict[str, float] = {}
    nifty = _bench_return_pct("^NSEI")
    if nifty is not None:
        out["IND"] = nifty
    sp500 = _bench_return_pct("^GSPC")
    if sp500 is not None:
        out["US"] = sp500
    return out


def _close_for(ticker: str, market: str) -> float | None:
    symbol = f"{ticker}.NS" if market == "IND" else ticker.replace(".", "-")
    oc = _yf_today_open_close(symbol)
    return oc[1] if oc else None


# ---------------------------------------------------------------------------
# Market resolution
# ---------------------------------------------------------------------------

_KNOWN_US_TICKERS = {
    "AAPL", "MSFT", "AMZN", "GOOG", "GOOGL", "META", "NVDA", "TSLA",
    "NFLX", "AMD", "INTC", "IBM", "ORCL", "CRM", "ADBE", "PEP", "KO",
    "WMT", "JPM", "BAC", "GS", "MS", "C", "BRK.A", "BRK.B", "BRK-B",
    "V", "MA", "DIS", "HD", "MCD", "NKE", "XOM", "CVX", "BP", "BKR",
    "DUK", "PLTR", "PANW", "SOXX", "EQIX", "RTX", "TSM", "PSI",
    "IBKR", "IAU", "GLD", "SLV", "AAAU",
}


def resolve_market(ticker: str) -> str:
    t = (ticker or "").upper().strip()
    if config.instrument_key_for(t):
        return "IND"
    if t in _KNOWN_US_TICKERS:
        return "US"
    return "IND"


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

_EXIT_ACTIONS = ("EXIT-PARTIAL", "EXIT-FULL", "PARTIAL-EXIT", "FULL-EXIT", "SELL")
_THRESHOLD = 0.5  # percent


def classify_outcome(action: str, return_pct: float, alpha_pct: float) -> str:
    act = (action or "").upper()
    if act in _EXIT_ACTIONS:
        # Win if the price dropped after the exit (call was right to cut).
        if return_pct < -_THRESHOLD:
            return "win"
        if return_pct > _THRESHOLD:
            return "loss"
        return "neutral"
    # BUY / ADD / HOLD / TIGHTEN-SL → alpha-driven verdict
    if alpha_pct > _THRESHOLD:
        return "win"
    if alpha_pct < -_THRESHOLD:
        return "loss"
    return "neutral"


# ---------------------------------------------------------------------------
# Per-recommendation row builder
# ---------------------------------------------------------------------------

def compute_outcome(
    rec: dict[str, Any],
    *,
    benchmarks: dict[str, float],
) -> dict[str, Any] | None:
    """Build a backtest_results row from a recommendation. None on failure.

    `benchmarks` maps "IND"/"US" → today's benchmark return %.
    """
    ticker = rec.get("ticker")
    entry = rec.get("entry_price")
    if not ticker or entry is None:
        return None

    market = resolve_market(ticker)
    close = _close_for(ticker, market)
    if close is None:
        log.warning("No close price for %s (%s) — skipping outcome", ticker, market)
        return None

    entry_f = float(entry)
    return_pct = (close - entry_f) / entry_f * 100 if entry_f else 0.0
    bench = benchmarks.get(market, 0.0)
    alpha = return_pct - bench
    outcome = classify_outcome(rec.get("action") or "", return_pct, alpha)

    user_executed = bool(rec.get("user_executed"))
    shares = rec.get("shares_qty") or 0
    leverage = int(rec.get("leverage_multiplier") or 1)
    capital = float(rec.get("capital_deployed") or 0)

    # P&L only matters if the call was actually executed.
    actual_pnl_inr: float = 0.0
    if user_executed:
        if shares:
            per_share = (close - entry_f)
            actual_pnl_inr = per_share * float(shares)
            if market == "US":
                actual_pnl_inr *= config.USD_INR_RATE
        elif capital:
            actual_pnl_inr = capital * leverage * (return_pct / 100)

    return {
        "recommendation_id": rec.get("id"),
        "ticker": ticker,
        "market": market,
        "run_date": datetime.now(timezone.utc).date().isoformat(),
        # `action` is the portfolio-advisor column; `recommended_action` is the
        # legacy NOT NULL column from the StockSage table that the schema still
        # carries — populate both so inserts succeed on either layout.
        "action": rec.get("action"),
        "recommended_action": rec.get("action"),
        "confidence_score": rec.get("confidence_score"),
        "leverage_multiplier": leverage,
        "price_at_recommendation": entry_f,
        "price_at_close": round(close, 4),
        "return_pct": round(return_pct, 4),
        "nifty_return_pct": round(bench, 4),
        "alpha_pct": round(alpha, 4),
        "outcome": outcome,
        "capital_deployed": capital,
        "actual_pnl_inr": round(actual_pnl_inr, 2),
        "user_executed": user_executed,
    }
