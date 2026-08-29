"""Tier 1 technical indicators for a single ticker (Indian or US).

Computes RSI(14), the EMA 20/50 trend + fresh-crossover flag, volume vs its
20-day average, a 20-day VWAP and a 0-4 signal-alignment score from 60 days of
daily OHLCV pulled via yfinance.

Every failure path returns None — this is enrichment, never a hard dependency,
so a bad symbol or a yfinance hiccup must not sink a run. Results are cached
in-process for one hour so a single run never re-fetches the same ticker.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd
import pandas_ta as ta
import yfinance as yf

import config

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3600  # 1 hour — a daily-candle indicator set is stable intraday
MIN_ROWS = 20             # below this RSI/EMA20/volume-avg are meaningless

# (SYMBOL, MARKET) -> (monotonic_expiry, payload)
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def yf_symbol(ticker: str, market: str = "IND") -> str:
    """Yahoo symbol for a stored ticker.

    IND goes through config.yf_ind_symbol so the YFINANCE_IND_SYMBOL_MAP
    overrides (ICICIGOLD → GOLDIETF.NS etc.) apply. US only needs the
    class-share fix — BRK.B is served as BRK-B."""
    if (market or "IND").upper() == "US":
        return ticker.replace(".", "-").upper().strip()
    return config.yf_ind_symbol(ticker) or ticker


def _fetch_ohlcv(symbol: str) -> pd.DataFrame | None:
    """60 daily candles, columns flattened, incomplete (NaN-close) rows dropped."""
    df = yf.download(
        symbol,
        period="60d",
        interval="1d",
        auto_adjust=True,
        progress=False,
    )
    if df is None or df.empty:
        return None
    # yfinance returns MultiIndex columns (Price, Ticker) for single symbols too.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.dropna(subset=["Close", "Volume"])
    return df


def _rsi_signal(rsi: float) -> str:
    if rsi > 70:
        return "overbought"
    if rsi < 30:
        return "oversold"
    if 55 < rsi <= 70:
        return "bullish"
    if 30 <= rsi < 45:
        return "bearish"
    return "neutral"


def _volume_signal(ratio: float) -> str:
    if ratio > 1.5:
        return "high"
    if ratio > 1.2:
        return "above_avg"
    if ratio >= 0.8:
        return "normal"
    return "low"


def _vwap_position(close: float, vwap: float) -> str:
    if vwap and abs(close - vwap) / vwap <= 0.005:
        return "at"
    return "above" if close > vwap else "below"


def _fresh_crossover(ema20: pd.Series, ema50: pd.Series) -> bool:
    """True if EMA20 crossed EMA50 within the last 3 candles."""
    diff = (ema20 - ema50).dropna()
    if len(diff) < 4:
        return False
    signs = [1 if v > 0 else -1 for v in diff.iloc[-4:]]
    return any(signs[i] != signs[i + 1] for i in range(len(signs) - 1))


def _alignment(rsi_signal: str, ema_signal: str, volume_signal: str,
               vwap_position: str) -> tuple[int, str]:
    score = 0
    if rsi_signal in ("bullish", "oversold"):
        score += 1
    if ema_signal == "bullish":
        score += 1
    if volume_signal in ("high", "above_avg"):
        score += 1
    if vwap_position == "above":
        score += 1
    if score == 4:
        label = "strong bullish"
    elif score == 3:
        label = "bullish"
    elif score == 2:
        label = "mixed"
    else:
        label = "bearish"
    return score, label


def compute_technicals(ticker: str, market: str = "IND") -> dict[str, Any] | None:
    """Tier 1 indicator set for one ticker, or None if it can't be computed."""
    if not ticker:
        return None
    symbol = yf_symbol(ticker, market)
    key = (symbol, (market or "IND").upper())
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    try:
        df = _fetch_ohlcv(symbol)
        if df is None or len(df) < MIN_ROWS:
            log.warning("technicals %s: only %d usable rows — skipping",
                        symbol, 0 if df is None else len(df))
            return None

        close = df["Close"]

        # RSI(14)
        rsi = float(ta.rsi(close, length=14).iloc[-1])
        rsi_signal = _rsi_signal(rsi)

        # EMA 20 / 50 trend + fresh crossover
        ema20_s = ta.ema(close, length=20)
        ema50_s = ta.ema(close, length=50)
        ema20 = float(ema20_s.iloc[-1])
        ema50 = float(ema50_s.iloc[-1])
        ema_signal = "bullish" if ema20 > ema50 else "bearish"
        ema_crossover = _fresh_crossover(ema20_s, ema50_s)

        # Volume vs its own 20-day average
        avg_volume_20 = float(df["Volume"].tail(20).mean())
        today_volume = float(df["Volume"].iloc[-1])
        volume_ratio = today_volume / avg_volume_20 if avg_volume_20 else 0.0
        volume_signal = _volume_signal(volume_ratio)

        # 20-day VWAP on daily candles (trend level, not an intraday level)
        recent = df.tail(20)
        typical_price = (recent["High"] + recent["Low"] + recent["Close"]) / 3
        vol_sum = float(recent["Volume"].sum())
        vwap = float((typical_price * recent["Volume"]).sum() / vol_sum) if vol_sum else float(close.iloc[-1])
        last_close = float(close.iloc[-1])
        vwap_position = _vwap_position(last_close, vwap)

        score, label = _alignment(rsi_signal, ema_signal, volume_signal, vwap_position)

        result = {
            "rsi": rsi,
            "rsi_signal": rsi_signal,
            "ema20": ema20,
            "ema50": ema50,
            "ema_signal": ema_signal,
            "ema_crossover": ema_crossover,
            "volume_ratio": volume_ratio,
            "volume_signal": volume_signal,
            "vwap": vwap,
            "vwap_position": vwap_position,
            "signal_alignment": score,
            "alignment_label": label,
            "close": last_close,
            "prev_close": float(close.iloc[-2]),
        }
        _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, result)
        return result
    except Exception as exc:
        log.warning("compute_technicals failed for %s (%s): %s", ticker, symbol, exc)
        return None


def format_technicals_block(tech: dict[str, Any] | None) -> str:
    """Render the indicator set as the TECHNICALS text block Claude reads."""
    if not tech:
        return ""
    crossover = " ⚡ FRESH CROSSOVER" if tech["ema_crossover"] else ""
    return "\n".join([
        "TECHNICALS:",
        f"RSI(14): {tech['rsi']:.1f} — {tech['rsi_signal']}",
        f"EMA: 20-day {tech['ema_signal']} 50-day "
        f"{tech['ema20']:.2f} vs {tech['ema50']:.2f}{crossover}",
        f"Volume: {tech['volume_ratio']:.1f}x avg — {tech['volume_signal']}",
        f"VWAP: Price {tech['vwap_position']} VWAP ({tech['vwap']:.2f})",
        f"Signal alignment: {tech['signal_alignment']}/4 — {tech['alignment_label']}",
    ])


def clear_cache() -> None:
    """Drop the in-memory cache (tests / long-lived processes)."""
    _cache.clear()
