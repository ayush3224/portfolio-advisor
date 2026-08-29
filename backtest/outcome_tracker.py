"""Outcome tracking for advisor recommendations — T+5 and T+15 horizons.

Two responsibilities:

1. *Daily* (4 PM IST cron): open a backtest_results row for every recommendation
   made today, then walk every still-open row forward and fill in the T+5 / T+15
   verdicts as those dates arrive.
2. *Backfill* (`--backfill`): open and score rows for every historical
   recommendation that never got one.

A verdict is alpha-based: the stock's return minus its benchmark's return over
the same window (Nifty 50 for IND, S&P 500 for US). Beating the index is the
bar, not merely going up.

Bad runs are excluded, not scored. A run that died on an API credit gap, a
timeout, or an overload emitted recommendations that reflect the outage rather
than the model's judgement, and averaging them into the win rate makes the
scorecard lie. Recommendations written after the `run_id` column landed
(2026-08-29) are matched to their run exactly; older ones are matched to a
failed run by timestamp window.

This module also still serves the legacy same-day path used by
scheduler/outcome_logger.py — `fetch_benchmarks()` and `compute_outcome()` are
unchanged in behaviour and signature.

backtest_results is shared with the sibling StockSage app, so every read and
write here is scoped to project='portfolio-advisor'.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests
import yfinance as yf

import config
from storage import supabase_client

log = logging.getLogger(__name__)

PROJECT = config.PROJECT_NAME
_UPSTOX_BASE = "https://api.upstox.com/v2"
_NIFTY_KEY = "NSE_INDEX|Nifty 50"

# How far back price history is pulled in one shot. Recommendations start
# 2026-05-11, so a year covers every horizon we score.
_HISTORY_DAYS = 400

# A bad run's recommendations are the ones written inside its window. When a
# run never wrote completed_at (it died), assume this much wall-clock.
_ASSUMED_RUN_MINUTES = 45

# claude_client stamps completed_at the moment the API responds, and the
# scheduler only then sizes, risk-checks and inserts the rows — measured at
# 0-3s after completed_at, but the persistence phase is unbounded on a slow
# DB. Extend each window past completed_at by this much so those rows are
# still attributed to their run. Scheduled runs sit hours apart, so a 10
# minute tail cannot reach into the next run.
_PERSIST_GRACE_MINUTES = 10


# ---------------------------------------------------------------------------
# Trading calendars
# ---------------------------------------------------------------------------

NSE_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 26), date(2026, 2, 26), date(2026, 3, 14), date(2026, 3, 31),
    date(2026, 4, 10), date(2026, 4, 14), date(2026, 4, 18), date(2026, 5, 1),
    date(2026, 8, 15), date(2026, 8, 27), date(2026, 10, 2), date(2026, 10, 24),
    date(2026, 11, 5), date(2026, 11, 20), date(2026, 12, 25),
}

US_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
}


def _holidays_for(market: str) -> set[date]:
    return US_HOLIDAYS_2026 if (market or "IND").upper() == "US" else NSE_HOLIDAYS_2026


def is_trading_day(day: date, market: str = "IND") -> bool:
    return day.weekday() < 5 and day not in _holidays_for(market)


def add_trading_days(start_date: date, n_days: int, market: str = "IND") -> date:
    """Return the date `n_days` trading sessions after `start_date`.

    Weekends are skipped for both markets; exchange holidays come from the
    2026 calendars above. Dates beyond 2026 only skip weekends — the holiday
    sets need extending each year.
    """
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date[:10])
    holidays = _holidays_for(market)
    day = start_date
    counted = 0
    while counted < n_days:
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in holidays:
            counted += 1
    return day


# ---------------------------------------------------------------------------
# Price history — one fetch per symbol per process, then date lookups
# ---------------------------------------------------------------------------

_yf_cache: dict[str, dict[date, float]] = {}
_upstox_cache: dict[str, dict[date, float]] = {}


def _as_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _yf_closes(symbol: str) -> dict[date, float]:
    """Daily closes for a Yahoo symbol, keyed by date. Cached per process."""
    if symbol in _yf_cache:
        return _yf_cache[symbol]
    closes: dict[date, float] = {}
    try:
        start = (date.today() - timedelta(days=_HISTORY_DAYS)).isoformat()
        hist = yf.Ticker(symbol).history(start=start, auto_adjust=False)
        if hist is not None and not hist.empty:
            for idx, row in hist.iterrows():
                close = float(row["Close"])
                if not math.isnan(close):
                    closes[idx.date()] = close
    except Exception as exc:
        log.warning("yfinance history for %s failed: %s", symbol, exc)
    _yf_cache[symbol] = closes
    return closes


def _upstox_closes(instrument_key: str) -> dict[date, float]:
    """Daily closes from the Upstox historical-candle API. Empty dict on failure."""
    if instrument_key in _upstox_cache:
        return _upstox_cache[instrument_key]
    closes: dict[date, float] = {}
    if not config.UPSTOX_ANALYTICS_TOKEN:
        _upstox_cache[instrument_key] = closes
        return closes
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=_HISTORY_DAYS)).isoformat()
    url = (
        f"{_UPSTOX_BASE}/historical-candle/"
        f"{quote(instrument_key, safe='')}/day/{to_date}/{from_date}"
    )
    try:
        r = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.UPSTOX_ANALYTICS_TOKEN}",
            },
            timeout=20,
        )
        r.raise_for_status()
        # candle = [timestamp, open, high, low, close, volume, oi]
        for candle in ((r.json().get("data") or {}).get("candles") or []):
            day = _as_date(candle[0])
            if day is not None:
                closes[day] = float(candle[4])
    except Exception as exc:
        log.warning("Upstox history for %s failed (%s) — falling back to yfinance",
                    instrument_key, exc)
    _upstox_cache[instrument_key] = closes
    return closes


def _close_on_or_before(closes: dict[date, float], target: date, *, window: int = 7) -> float | None:
    """Close for `target`, or the most recent session within `window` days before
    it. Covers holidays we do not have in the calendar and half-days with no bar."""
    for back in range(window + 1):
        hit = closes.get(target - timedelta(days=back))
        if hit is not None:
            return hit
    return None


def fetch_closing_price(ticker: str, target_date: Any, market: str = "IND") -> float | None:
    """Closing price for `ticker` on `target_date`. None on any failure."""
    target = _as_date(target_date)
    if not ticker or target is None:
        return None
    market = (market or "IND").upper()
    try:
        if market == "IND":
            instrument_key = config.instrument_key_for(ticker)
            if instrument_key:
                hit = _close_on_or_before(_upstox_closes(instrument_key), target)
                if hit is not None:
                    return round(hit, 4)
            symbol = config.yf_ind_symbol(ticker)
        else:
            symbol = ticker.upper().replace(".", "-")
        if not symbol:
            return None
        hit = _close_on_or_before(_yf_closes(symbol), target)
        return round(hit, 4) if hit is not None else None
    except Exception as exc:
        log.warning("fetch_closing_price %s @ %s failed: %s", ticker, target, exc)
        return None


def fetch_benchmark_close(target_date: Any, market: str = "IND") -> float | None:
    """Benchmark close on `target_date` — Nifty 50 for IND, S&P 500 for US."""
    target = _as_date(target_date)
    if target is None:
        return None
    market = (market or "IND").upper()
    try:
        if market == "IND":
            hit = _close_on_or_before(_upstox_closes(_NIFTY_KEY), target)
            if hit is not None:
                return round(hit, 4)
            hit = _close_on_or_before(_yf_closes("^NSEI"), target)
            return round(hit, 4) if hit is not None else None
        hit = _close_on_or_before(_yf_closes("^GSPC"), target)
        return round(hit, 4) if hit is not None else None
    except Exception as exc:
        log.warning("fetch_benchmark_close %s (%s) failed: %s", target, market, exc)
        return None


# ---------------------------------------------------------------------------
# Bad-run exclusion
# ---------------------------------------------------------------------------

_ERROR_PATTERNS = ("credit", "overloaded", "timeout", "error")

_excluded_cache: tuple[set[str], list[tuple[datetime, datetime]]] | None = None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_bad_run(row: dict[str, Any]) -> bool:
    status = (row.get("status") or "").lower()
    if status != "success":
        return True
    tokens = row.get("output_tokens")
    if tokens is not None and int(tokens) < 100:
        return True
    err = (row.get("error_message") or "").lower()
    return bool(err) and any(p in err for p in _ERROR_PATTERNS)


def _load_excluded_runs() -> tuple[set[str], list[tuple[datetime, datetime]]]:
    """(bad run ids, bad run time windows) for this project. Cached per process."""
    global _excluded_cache
    if _excluded_cache is not None:
        return _excluded_cache
    ids: set[str] = set()
    windows: list[tuple[datetime, datetime]] = []
    client = supabase_client.get_client()
    if client is None:
        _excluded_cache = (ids, windows)
        return _excluded_cache
    try:
        res = (
            client.table("run_log")
            .select("id,run_type,started_at,completed_at,status,output_tokens,error_message")
            .eq("project", PROJECT)
            .order("started_at")
            .execute()
        )
        rows = res.data or []
    except Exception as exc:
        log.warning("run_log lookup failed (%s) — excluding nothing", exc)
        rows = []
    for row in rows:
        if not _is_bad_run(row):
            continue
        ids.add(str(row["id"]))
        start = _parse_ts(row.get("started_at"))
        if start is None:
            continue
        end = _parse_ts(row.get("completed_at")) or (start + timedelta(minutes=_ASSUMED_RUN_MINUTES))
        windows.append((start, end + timedelta(minutes=_PERSIST_GRACE_MINUTES)))
    log.info("Excluded runs: %d bad of %d %s runs", len(ids), len(rows), PROJECT)
    _excluded_cache = (ids, windows)
    return _excluded_cache


def get_excluded_run_ids() -> set[str]:
    """run_log ids for this project whose output is not worth scoring."""
    return _load_excluded_runs()[0]


def is_excluded_recommendation(rec: dict[str, Any], excluded: set[str] | None = None) -> bool:
    """True if `rec` came out of a failed or credit-gapped run.

    Exact when the recommendation carries run_id; otherwise the created_at
    timestamp is matched against the windows of known-bad runs.
    """
    ids, windows = _load_excluded_runs()
    if excluded is not None:
        ids = excluded
    run_id = rec.get("run_id")
    if run_id:
        return str(run_id) in ids
    created = _parse_ts(rec.get("created_at"))
    if created is None:
        return False
    return any(start <= created <= end for start, end in windows)


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

_BUY_FAMILY = {"ADD", "BUY", "BUY-MOMENTUM", "BUY-EVENT"}
_HOLD_FAMILY = {"HOLD", "TIGHTEN-SL"}
_EXIT_FAMILY = {"EXIT-FULL", "EXIT-PARTIAL", "BOOK-PROFIT",
                "FULL-EXIT", "PARTIAL-EXIT", "SELL"}


def classify_outcome(
    action: str,
    alpha: float | None,
    current_price: float | None = None,
    stop_loss: float | None = None,
) -> str:
    """win / loss / neutral / not_scored for an action given its alpha.

    Thresholds differ by intent. A BUY has to earn its entry cost, so it needs
    a full point of alpha. A HOLD only has to not lose you anything, so any
    positive alpha counts and the loss bar sits at -2%. EXIT calls invert: the
    stock falling after you sold is the call working.
    """
    act = (action or "").upper().strip()
    if act == "WATCH" or alpha is None:
        return "not_scored"
    if act in _BUY_FAMILY:
        if alpha > 1.0:
            return "win"
        if alpha < -1.0:
            return "loss"
        return "neutral"
    if act in _HOLD_FAMILY:
        if alpha > 0.0:
            return "win"
        if alpha < -2.0:
            return "loss"
        return "neutral"
    if act in _EXIT_FAMILY:
        if alpha < -1.0:
            return "win"
        if alpha > 1.0:
            return "loss"
        return "neutral"
    return "not_scored"


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

_holdings_market_cache: dict[str, str] | None = None


def resolve_market(ticker: str) -> str:
    t = (ticker or "").upper().strip()
    if config.instrument_key_for(t):
        return "IND"
    if t in _KNOWN_US_TICKERS:
        return "US"
    return "IND"


def get_holding_market(ticker: str) -> str:
    """Market for a ticker, from the holdings table. 'IND' when unknown."""
    global _holdings_market_cache
    if _holdings_market_cache is None:
        _holdings_market_cache = {}
        client = supabase_client.get_client()
        if client is not None:
            try:
                res = client.table("holdings").select("ticker,market").execute()
                for row in (res.data or []):
                    tick = (row.get("ticker") or "").upper()
                    mkt = (row.get("market") or "").upper()
                    if tick and mkt in ("IND", "US"):
                        _holdings_market_cache[tick] = mkt
            except Exception as exc:
                log.warning("holdings market lookup failed (%s) — using heuristics", exc)
    t = (ticker or "").upper().strip()
    return _holdings_market_cache.get(t) or resolve_market(t)


# ---------------------------------------------------------------------------
# backtest_results upsert (no unique index on recommendation_id — 31 legacy
# ids carry duplicate rows — so conflicts are resolved read-then-write)
# ---------------------------------------------------------------------------

def _existing_row_id(recommendation_id: str) -> str | None:
    client = supabase_client.get_client()
    if client is None or not recommendation_id:
        return None
    try:
        res = (
            client.table("backtest_results")
            .select("id")
            .eq("recommendation_id", recommendation_id)
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0]["id"] if rows else None
    except Exception as exc:
        log.warning("backtest_results lookup for %s failed: %s", recommendation_id, exc)
        return None


def upsert_backtest_row(row: dict[str, Any]) -> str | None:
    """Insert, or update the existing row for this recommendation_id."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] upsert_backtest_row %s skipped", row.get("ticker"))
        return None
    client = supabase_client.get_client()
    if client is None:
        return None
    payload = dict(row)
    payload["project"] = PROJECT
    rec_id = payload.get("recommendation_id")
    existing = _existing_row_id(rec_id) if rec_id else None
    try:
        if existing:
            client.table("backtest_results").update(payload).eq("id", existing).execute()
            return existing
        res = client.table("backtest_results").insert(payload).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as exc:
        log.error("backtest_results upsert failed for %s: %s", payload.get("ticker"), exc)
        return None


def _excluded_row(rec: dict[str, Any], run_date: str, market: str) -> dict[str, Any]:
    return {
        "recommendation_id": rec.get("id"),
        "ticker": rec.get("ticker"),
        "market": market,
        "run_date": run_date,
        "action": rec.get("action"),
        "recommended_action": rec.get("action"),
        "confidence_score": rec.get("confidence_score"),
        "price_at_recommendation": rec.get("entry_price"),
        "excluded": True,
        "exclusion_reason": "Failed or credit-gap run",
        "days_tracked": 0,
    }


def _horizon_update(
    *,
    prefix: str,
    ticker: str,
    market: str,
    action: str,
    horizon_date: Any,
    rec_price: float,
    bench_at_rec: float | None,
) -> dict[str, Any]:
    """Price/return/alpha/outcome fields for one horizon. Empty when a fetch fails."""
    close = fetch_closing_price(ticker, horizon_date, market)
    bench = fetch_benchmark_close(horizon_date, market)
    if close is None or bench is None or not bench_at_rec or not rec_price:
        return {}
    stock_ret = (close - rec_price) / rec_price * 100
    bench_ret = (bench - bench_at_rec) / bench_at_rec * 100
    alpha = stock_ret - bench_ret
    return {
        f"{prefix}_close": round(close, 4),
        f"{prefix}_return_pct": round(stock_ret, 4),
        f"{prefix}_benchmark_return": round(bench_ret, 4),
        f"{prefix}_alpha": round(alpha, 4),
        f"{prefix}_outcome": classify_outcome(action, alpha),
    }



# An entry price should be the market price on the day of the call. Some early
# recommendations recorded the holding's average cost instead (RELIANCE at
# 2780 against a 1388 close, GOLDBEES at 73 against 131), which manufactures a
# +100% "return" that has nothing to do with the advice. Anything this far
# from the day's actual close is a recording error, not a trade, so the row is
# flagged rather than scored — a fabricated win is worse than a missing one.
_ENTRY_PRICE_TOLERANCE_PCT = 15.0
_BAD_ENTRY_REASON = "Entry price is cost basis, not the market price that day"


def entry_price_is_market(
    ticker: str, run_date: Any, market: str, rec_price: float,
) -> bool:
    """True if `rec_price` plausibly is the market price on `run_date`.

    Unverifiable prices pass — a missing quote is not evidence of a bad entry.
    """
    if not rec_price:
        return False
    close = fetch_closing_price(ticker, run_date, market)
    if close is None or not close:
        return True
    return abs(rec_price - close) / close * 100 <= _ENTRY_PRICE_TOLERANCE_PCT


# ---------------------------------------------------------------------------
# 1. Open today's rows
# ---------------------------------------------------------------------------

def initialise_todays_recommendations() -> dict[str, int]:
    """Open a backtest_results row for every recommendation made today."""
    excluded = get_excluded_run_ids()
    today = date.today()
    start = f"{today.isoformat()}T00:00:00+00:00"
    end = f"{today.isoformat()}T23:59:59+00:00"

    recs = supabase_client.get_recommendations_between(start, end)
    opened = skipped = 0
    for rec in recs:
        try:
            market = get_holding_market(rec.get("ticker") or "")
            if is_excluded_recommendation(rec, excluded):
                upsert_backtest_row(_excluded_row(rec, today.isoformat(), market))
                skipped += 1
                continue
            upsert_backtest_row({
                "recommendation_id": rec.get("id"),
                "ticker": rec.get("ticker"),
                "market": market,
                "run_date": today.isoformat(),
                "action": rec.get("action"),
                "recommended_action": rec.get("action"),
                "confidence_score": rec.get("confidence_score"),
                "price_at_recommendation": rec.get("entry_price"),
                "t5_date": add_trading_days(today, 5, market).isoformat(),
                "t15_date": add_trading_days(today, 15, market).isoformat(),
                "excluded": False,
                "days_tracked": 0,
                "user_executed": bool(rec.get("user_executed")),
            })
            opened += 1
        except Exception as exc:
            log.exception("initialise failed for %s: %s", rec.get("ticker"), exc)
    log.info("Initialised %d rows for %s (%d excluded)", opened, today, skipped)
    print(f"Initialised: {opened} | Excluded: {skipped}")
    return {"opened": opened, "excluded": skipped}


# ---------------------------------------------------------------------------
# 2. Walk open rows forward
# ---------------------------------------------------------------------------

def update_open_positions() -> dict[str, int]:
    """Fill T+5 / T+15 verdicts on every open row whose horizon has arrived."""
    client = supabase_client.get_client()
    if client is None:
        return {"updated": 0, "scored_t5": 0, "scored_t15": 0}
    today = date.today()
    cutoff = (today - timedelta(days=30)).isoformat()
    try:
        res = (
            client.table("backtest_results")
            .select("*")
            .eq("project", PROJECT)
            .eq("excluded", False)
            .is_("t15_outcome", None)
            .gte("run_date", cutoff)
            .execute()
        )
        rows = res.data or []
    except Exception as exc:
        log.error("open-position lookup failed: %s", exc)
        return {"updated": 0, "scored_t5": 0, "scored_t15": 0}

    updated = scored_t5 = scored_t15 = 0
    for row in rows:
        try:
            rec_price = row.get("price_at_recommendation")
            if rec_price is None:
                continue
            rec_price = float(rec_price)
            if not rec_price:
                continue
            ticker = row.get("ticker") or ""
            market = (row.get("market") or "IND").upper()
            action = row.get("action") or row.get("recommended_action") or ""
            run_date = _as_date(row.get("run_date"))
            if run_date is None:
                continue

            if not entry_price_is_market(ticker, run_date, market, rec_price):
                if not config.DRY_RUN:
                    client.table("backtest_results").update({
                        "excluded": True, "exclusion_reason": _BAD_ENTRY_REASON,
                    }).eq("id", row["id"]).execute()
                log.warning("%s @ %s: entry %.2f is not a market price — flagged",
                            ticker, run_date, rec_price)
                continue

            bench_at_rec = fetch_benchmark_close(run_date, market)
            bench_now = fetch_benchmark_close(today, market)
            current = fetch_closing_price(ticker, today, market)
            if bench_at_rec is None or bench_now is None or current is None:
                log.warning("price fetch incomplete for %s — skipping", ticker)
                continue

            stock_ret = (current - rec_price) / rec_price * 100
            bench_ret = (bench_now - bench_at_rec) / bench_at_rec * 100 if bench_at_rec else 0.0
            alpha = stock_ret - bench_ret

            # price_at_close / return_pct / nifty_return_pct / alpha_pct belong
            # to the 4 PM same-day path and are left alone here — overwriting
            # them with since-recommendation figures would leave `outcome`
            # (same-day) and `alpha_pct` (cumulative) describing different
            # windows in the same row. The running alpha is logged instead.
            log.debug("%s day %d: stock %+.2f%% bench %+.2f%% alpha %+.2f%%",
                      ticker, (today - run_date).days, stock_ret, bench_ret, alpha)
            update: dict[str, Any] = {"days_tracked": (today - run_date).days}

            t5_date = _as_date(row.get("t5_date"))
            if t5_date and today >= t5_date and row.get("t5_outcome") is None:
                fields = _horizon_update(
                    prefix="t5", ticker=ticker, market=market, action=action,
                    horizon_date=t5_date, rec_price=rec_price, bench_at_rec=bench_at_rec,
                )
                if fields:
                    update.update(fields)
                    scored_t5 += 1

            t15_date = _as_date(row.get("t15_date"))
            if t15_date and today >= t15_date and row.get("t15_outcome") is None:
                fields = _horizon_update(
                    prefix="t15", ticker=ticker, market=market, action=action,
                    horizon_date=t15_date, rec_price=rec_price, bench_at_rec=bench_at_rec,
                )
                if fields:
                    update.update(fields)
                    scored_t15 += 1

            if config.DRY_RUN:
                log.info("[DRY_RUN] update %s %s", ticker, update)
                continue
            client.table("backtest_results").update(update).eq("id", row["id"]).execute()
            updated += 1
        except Exception as exc:
            log.exception("update failed for %s: %s", row.get("ticker"), exc)

    log.info("Updated %d open rows (T+5 scored %d, T+15 scored %d)",
             updated, scored_t5, scored_t15)
    print(f"Updated: {updated} | T+5 scored: {scored_t5} | T+15 scored: {scored_t15}")
    return {"updated": updated, "scored_t5": scored_t5, "scored_t15": scored_t15}


# ---------------------------------------------------------------------------
# 3. Backfill
# ---------------------------------------------------------------------------

def backfill_historical() -> dict[str, int]:
    """Open and score a row for every recommendation that has none yet."""
    excluded = get_excluded_run_ids()
    client = supabase_client.get_client()
    if client is None:
        print("Backfilled: 0 | Excluded: 0 | IND: 0 | US: 0  (no DB)")
        return {"backfilled": 0, "excluded": 0, "IND": 0, "US": 0}

    try:
        recs = (
            client.table("advisor_recommendations")
            .select("*")
            .order("created_at")
            .execute()
        ).data or []
    except Exception as exc:
        log.error("recommendation fetch failed: %s", exc)
        return {"backfilled": 0, "excluded": 0, "IND": 0, "US": 0}

    # One pass to find which recommendations already have a row.
    have: set[str] = set()
    try:
        offset, step = 0, 1000
        while True:
            page = (
                client.table("backtest_results")
                .select("recommendation_id")
                .range(offset, offset + step - 1)
                .execute()
            ).data or []
            have.update(str(r["recommendation_id"]) for r in page if r.get("recommendation_id"))
            if len(page) < step:
                break
            offset += step
    except Exception as exc:
        log.warning("existing backtest row scan failed (%s) — may re-open rows", exc)

    today = date.today()
    backfilled = excluded_count = 0
    by_market = {"IND": 0, "US": 0}

    for rec in recs:
        try:
            rec_id = str(rec.get("id"))
            if rec_id in have:
                continue
            ticker = rec.get("ticker") or ""
            market = get_holding_market(ticker)
            run_date = (rec.get("created_at") or "")[:10]
            if not run_date:
                continue

            if is_excluded_recommendation(rec, excluded):
                upsert_backtest_row(_excluded_row(rec, run_date, market))
                excluded_count += 1
                continue

            run_day = date.fromisoformat(run_date)
            t5 = add_trading_days(run_day, 5, market)
            t15 = add_trading_days(run_day, 15, market)
            entry = rec.get("entry_price")
            row: dict[str, Any] = {
                "recommendation_id": rec_id,
                "ticker": ticker,
                "market": market,
                "run_date": run_date,
                "action": rec.get("action"),
                "recommended_action": rec.get("action"),
                "confidence_score": rec.get("confidence_score"),
                "price_at_recommendation": entry,
                "t5_date": t5.isoformat(),
                "t15_date": t15.isoformat(),
                "excluded": False,
                "days_tracked": 0,
                "user_executed": bool(rec.get("user_executed")),
            }

            if entry is not None and float(entry):
                rec_price = float(entry)
                if not entry_price_is_market(ticker, run_day, market, rec_price):
                    row["excluded"] = True
                    row["exclusion_reason"] = _BAD_ENTRY_REASON
                    upsert_backtest_row(row)
                    excluded_count += 1
                    continue
                bench_at_rec = fetch_benchmark_close(run_day, market)
                row["days_tracked"] = (min(today, t15) - run_day).days
                action = rec.get("action") or ""
                if today >= t5:
                    row.update(_horizon_update(
                        prefix="t5", ticker=ticker, market=market, action=action,
                        horizon_date=t5, rec_price=rec_price, bench_at_rec=bench_at_rec,
                    ))
                if today >= t15:
                    row.update(_horizon_update(
                        prefix="t15", ticker=ticker, market=market, action=action,
                        horizon_date=t15, rec_price=rec_price, bench_at_rec=bench_at_rec,
                    ))

            upsert_backtest_row(row)
            backfilled += 1
            by_market[market] = by_market.get(market, 0) + 1
        except Exception as exc:
            log.exception("backfill failed for %s: %s", rec.get("ticker"), exc)

    print(f"Backfilled: {backfilled} | Excluded: {excluded_count} | "
          f"IND: {by_market.get('IND', 0)} | US: {by_market.get('US', 0)}")

    # Second pass — rows that existed before this schema have no horizons yet.
    horizons = backfill_existing_horizons()
    return {"backfilled": backfilled, "excluded": excluded_count,
            "horizons": horizons, **by_market}



def backfill_existing_horizons() -> dict[str, int]:
    """Add T+5 / T+15 horizons to rows that predate this schema.

    The old daily logger wrote a same-day row per recommendation and nothing
    else, so ~193 rows carry a price and an action but no horizon dates. They
    are where nearly all the scorable history lives — the rows the first
    backfill pass opens are mostly the ones the old logger skipped for having
    no entry price. Without this pass the scorecard would report on a handful
    of calls and silently ignore months of them.
    """
    client = supabase_client.get_client()
    if client is None:
        return {"dated": 0, "scored_t5": 0, "scored_t15": 0}
    try:
        rows = (
            client.table("backtest_results")
            .select("*")
            .eq("project", PROJECT)
            .eq("excluded", False)
            .is_("t5_date", None)
            .execute()
        ).data or []
    except Exception as exc:
        log.error("horizon backfill lookup failed: %s", exc)
        return {"dated": 0, "scored_t5": 0, "scored_t15": 0}

    today = date.today()
    dated = scored_t5 = scored_t15 = bad_entry = 0
    for row in rows:
        try:
            run_date = _as_date(row.get("run_date"))
            if run_date is None:
                continue
            ticker = row.get("ticker") or ""
            market = (row.get("market") or get_holding_market(ticker)).upper()
            action = row.get("action") or row.get("recommended_action") or ""
            t5 = add_trading_days(run_date, 5, market)
            t15 = add_trading_days(run_date, 15, market)
            update: dict[str, Any] = {
                "t5_date": t5.isoformat(),
                "t15_date": t15.isoformat(),
                "market": market,
                "days_tracked": (min(today, t15) - run_date).days,
            }
            rec_price = row.get("price_at_recommendation")
            if rec_price is not None and float(rec_price):
                rec_price = float(rec_price)
                if not entry_price_is_market(ticker, run_date, market, rec_price):
                    update["excluded"] = True
                    update["exclusion_reason"] = _BAD_ENTRY_REASON
                    bad_entry += 1
                    if not config.DRY_RUN:
                        client.table("backtest_results").update(update).eq("id", row["id"]).execute()
                    continue
                bench_at_rec = fetch_benchmark_close(run_date, market)
                if today >= t5:
                    fields = _horizon_update(
                        prefix="t5", ticker=ticker, market=market, action=action,
                        horizon_date=t5, rec_price=rec_price, bench_at_rec=bench_at_rec,
                    )
                    if fields:
                        update.update(fields)
                        scored_t5 += 1
                if today >= t15:
                    fields = _horizon_update(
                        prefix="t15", ticker=ticker, market=market, action=action,
                        horizon_date=t15, rec_price=rec_price, bench_at_rec=bench_at_rec,
                    )
                    if fields:
                        update.update(fields)
                        scored_t15 += 1
            if config.DRY_RUN:
                log.info("[DRY_RUN] horizon backfill %s %s", ticker, update)
                continue
            client.table("backtest_results").update(update).eq("id", row["id"]).execute()
            dated += 1
        except Exception as exc:
            log.exception("horizon backfill failed for %s: %s", row.get("ticker"), exc)

    print(f"Horizons added: {dated} | T+5 scored: {scored_t5} | "
          f"T+15 scored: {scored_t15} | bad entry price: {bad_entry}")
    return {"dated": dated, "scored_t5": scored_t5,
            "scored_t15": scored_t15, "bad_entry": bad_entry}


# ---------------------------------------------------------------------------
# Legacy same-day path — used by scheduler/outcome_logger.py (4 PM run)
# ---------------------------------------------------------------------------

def _yf_today_open_close(symbol: str) -> tuple[float, float] | None:
    """(open, close) for the latest available session, walking back over NaN bars."""
    try:
        hist = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
    except Exception as exc:
        log.warning("yfinance fetch %s failed: %s", symbol, exc)
        return None
    if hist is None or hist.empty:
        return None
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
    symbol = config.yf_ind_symbol(ticker) if market == "IND" else ticker.replace(".", "-")
    if not symbol:
        return None
    oc = _yf_today_open_close(symbol)
    return oc[1] if oc else None


_SAME_DAY_THRESHOLD = 0.5  # percent


def classify_same_day_outcome(action: str, return_pct: float, alpha_pct: float) -> str:
    """Same-day verdict for the 4 PM scorecard — looser bar than the T+5 call."""
    act = (action or "").upper()
    if act in _EXIT_FAMILY:
        if return_pct < -_SAME_DAY_THRESHOLD:
            return "win"
        if return_pct > _SAME_DAY_THRESHOLD:
            return "loss"
        return "neutral"
    if alpha_pct > _SAME_DAY_THRESHOLD:
        return "win"
    if alpha_pct < -_SAME_DAY_THRESHOLD:
        return "loss"
    return "neutral"


def compute_outcome(rec: dict[str, Any], *, benchmarks: dict[str, float]) -> dict[str, Any] | None:
    """Build a same-day backtest_results row from a recommendation. None on failure."""
    ticker = rec.get("ticker")
    entry = rec.get("entry_price")
    if not ticker or entry is None:
        return None

    market = get_holding_market(ticker)
    close = _close_for(ticker, market)
    if close is None:
        log.warning("No close price for %s (%s) — skipping outcome", ticker, market)
        return None

    entry_f = float(entry)
    return_pct = (close - entry_f) / entry_f * 100 if entry_f else 0.0
    bench = benchmarks.get(market, 0.0)
    alpha = return_pct - bench
    outcome = classify_same_day_outcome(rec.get("action") or "", return_pct, alpha)

    user_executed = bool(rec.get("user_executed"))
    shares = rec.get("shares_qty") or 0
    capital = float(rec.get("capital_deployed") or 0)

    # P&L only matters if the call was actually executed. CNC = 1x, no leverage.
    actual_pnl_inr: float = 0.0
    if user_executed:
        if shares:
            actual_pnl_inr = (close - entry_f) * float(shares)
            if market == "US":
                actual_pnl_inr *= config.USD_INR_RATE
        elif capital:
            actual_pnl_inr = capital * (return_pct / 100)

    return {
        "recommendation_id": rec.get("id"),
        "ticker": ticker,
        "market": market,
        "project": PROJECT,
        "run_date": datetime.now(timezone.utc).date().isoformat(),
        # `action` is the portfolio-advisor column; `recommended_action` is the
        # legacy column the shared StockSage table still carries — populate both.
        "action": rec.get("action"),
        "recommended_action": rec.get("action"),
        "confidence_score": rec.get("confidence_score"),
        "leverage_multiplier": 1,
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


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if "--backfill" in sys.argv:
        backfill_historical()
    else:
        initialise_todays_recommendations()
        update_open_positions()
        print("Outcome tracker complete")
