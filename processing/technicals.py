"""Tier 1 technical indicators for a single ticker (Indian or US).

Tier 1: RSI(14), the EMA 20/50 trend + fresh-crossover flag, volume vs its
20-day average, a 20-day VWAP.
Tier 2: Bollinger Bands (position + squeeze), MACD (12/26/9 crossover +
histogram momentum), and pivot support/resistance with proximity.
Both tiers roll up into a 0-6 signal-alignment score. All of it comes from
60 days of daily OHLCV pulled via yfinance.

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
MIN_ROWS = 20             # below this RSI/EMA20/BB/volume-avg are meaningless
MIN_ROWS_MACD = 35        # MACD(12,26,9) needs 26+9 candles before it reads

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


def _col(df: pd.DataFrame, prefix: str) -> pd.Series:
    """First column starting with `prefix`.

    pandas-ta suffixes indicator columns with their parameters and the exact
    suffix moves between versions (BBU_20_2.0 vs BBU_20_2.0_2.0), so match on
    the stable prefix instead of a hardcoded name."""
    for name in df.columns:
        if str(name).startswith(prefix):
            return df[name]
    raise KeyError(f"no column starting with {prefix!r} in {list(df.columns)}")


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


def _crossed_recently(diff: pd.Series, bars: int = 3) -> int:
    """Sign flip of `diff` within the last `bars` candles.

    Returns +1 for an up-cross (fast crossed above slow), -1 for a down-cross,
    0 for no crossover. Needs bars+1 points to look across bars intervals."""
    diff = diff.dropna()
    if len(diff) < bars + 1:
        return 0
    signs = [1 if v > 0 else -1 for v in diff.iloc[-(bars + 1):]]
    for i in range(len(signs) - 1):
        if signs[i] != signs[i + 1]:
            return signs[-1]
    return 0


def _fresh_crossover(ema20: pd.Series, ema50: pd.Series) -> bool:
    """True if EMA20 crossed EMA50 within the last 3 candles."""
    return _crossed_recently(ema20 - ema50) != 0


def _bb_signal(bb_position: float) -> str:
    """Band position → label.

    The spec's bands overlap (0.4-0.6 'neutral' sits inside both halves), so
    'neutral' is tested before the halves — otherwise it could never fire."""
    if bb_position > 0.85:
        return "overbought"
    if bb_position < 0.15:
        return "oversold"
    if 0.4 <= bb_position <= 0.6:
        return "neutral"
    if bb_position >= 0.5:
        return "upper_half"
    return "lower_half"


def _sr_signal(dist_resistance: float, dist_support: float) -> str:
    """Proximity label — the tighter 'at_*' bands are tested first."""
    if dist_resistance < 0.5:
        return "at_resistance"
    if dist_support < 0.5:
        return "at_support"
    if dist_resistance < 2:
        return "near_resistance"
    if dist_support < 2:
        return "near_support"
    return "middle"


def _alignment(rsi_signal: str, ema_signal: str, volume_signal: str,
               vwap_position: str, bb_signal: str, bb_squeeze: bool,
               macd_signal: str, sr_signal: str) -> tuple[int, str]:
    """Tier 1 (0-4) + Tier 2 (0-2) → 0-6 alignment score and its label."""
    score = 0
    # Tier 1
    if rsi_signal in ("bullish", "oversold"):
        score += 1
    if ema_signal == "bullish":
        score += 1
    if volume_signal in ("high", "above_avg"):
        score += 1
    if vwap_position == "above":
        score += 1
    # Tier 2
    if (bb_signal in ("oversold", "upper_half")
            and macd_signal in ("bullish", "bullish_crossover")):
        score += 1
    if (sr_signal in ("near_support", "at_support")
            or (sr_signal == "middle" and not bb_squeeze)):
        score += 1

    if score >= 5:
        label = "strong bullish"
    elif score == 4:
        label = "bullish"
    elif score == 3:
        label = "moderate"
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

        # --- Tier 2 ---------------------------------------------------------
        # Bollinger Bands (20, 2σ): position within the band + squeeze
        bb = ta.bbands(close, length=20, std=2)
        bb_upper = float(_col(bb, "BBU_").iloc[-1])
        bb_middle = float(_col(bb, "BBM_").iloc[-1])
        bb_lower = float(_col(bb, "BBL_").iloc[-1])
        band_range = bb_upper - bb_lower
        bb_position = (last_close - bb_lower) / band_range if band_range else 0.5
        bb_signal = _bb_signal(bb_position)

        width_series = ((_col(bb, "BBU_") - _col(bb, "BBL_")) / _col(bb, "BBM_")).dropna()
        band_width = float(width_series.iloc[-1])
        avg_width = float(width_series.tail(20).mean())
        bb_squeeze = bool(avg_width and band_width < avg_width * 0.75)

        # MACD (12, 26, 9): crossover state + histogram momentum.
        # Short-history tickers keep their Tier 1 + BB/S&R readings rather than
        # losing the whole block — MACD just reports as unavailable.
        macd_line = signal_line = histogram = None
        macd_signal = macd_momentum = "unavailable"
        if len(df) >= MIN_ROWS_MACD:
            macd_df = ta.macd(close, fast=12, slow=26, signal=9)
            macd_line = float(_col(macd_df, "MACD_").iloc[-1])
            signal_line = float(_col(macd_df, "MACDs_").iloc[-1])
            hist_series = _col(macd_df, "MACDh_").dropna()
            histogram = float(hist_series.iloc[-1])
            prev_histogram = float(hist_series.iloc[-2]) if len(hist_series) >= 2 else histogram

            cross = _crossed_recently(_col(macd_df, "MACD_") - _col(macd_df, "MACDs_"))
            if cross > 0:
                macd_signal = "bullish_crossover"
            elif cross < 0:
                macd_signal = "bearish_crossover"
            else:
                macd_signal = "bullish" if macd_line > signal_line else "bearish"
            macd_momentum = "increasing" if abs(histogram) > abs(prev_histogram) else "decreasing"

        # Support / resistance: pivot quantiles over the 60-day window
        resistance = float(df["High"].tail(60).quantile(0.90))
        support = float(close.tail(60).quantile(0.10))
        distance_to_resistance = (resistance - last_close) / last_close * 100
        distance_to_support = (last_close - support) / last_close * 100
        sr_signal = _sr_signal(distance_to_resistance, distance_to_support)

        score, label = _alignment(rsi_signal, ema_signal, volume_signal, vwap_position,
                                  bb_signal, bb_squeeze, macd_signal, sr_signal)

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
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
            "bb_position": bb_position,
            "bb_signal": bb_signal,
            "bb_squeeze": bb_squeeze,
            "band_width": band_width,
            "macd": macd_line,
            "macd_signal_line": signal_line,
            "macd_histogram": histogram,
            "macd_signal": macd_signal,
            "macd_momentum": macd_momentum,
            "resistance": resistance,
            "support": support,
            "distance_to_resistance": distance_to_resistance,
            "distance_to_support": distance_to_support,
            "sr_signal": sr_signal,
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
    squeeze = "  ⚡ SQUEEZE — breakout imminent" if tech.get("bb_squeeze") else ""
    macd_cross = ("  ⚡ FRESH CROSSOVER"
                  if tech.get("macd_signal") in ("bullish_crossover", "bearish_crossover")
                  else "")
    return "\n".join([
        "TECHNICALS:",
        f"RSI(14): {tech['rsi']:.1f} — {tech['rsi_signal']}",
        f"EMA: 20-day {tech['ema_signal']} 50-day "
        f"{tech['ema20']:.2f} vs {tech['ema50']:.2f}{crossover}",
        f"Volume: {tech['volume_ratio']:.1f}x avg — {tech['volume_signal']}",
        f"VWAP: Price {tech['vwap_position']} VWAP ({tech['vwap']:.2f})",
        f"BB: {tech['bb_position']:.2f} — {tech['bb_signal']}{squeeze}",
        f"MACD: {tech['macd_signal']} | momentum {tech['macd_momentum']}{macd_cross}"
        + ("" if tech.get("macd") is None else f" (hist {tech['macd_histogram']:+.2f})"),
        f"S&R: {tech['sr_signal']} | resistance {tech['resistance']:.2f} "
        f"support {tech['support']:.2f}",
        f"Signal alignment: {tech['signal_alignment']}/6 — {tech['alignment_label']}",
    ])


def clear_cache() -> None:
    """Drop the in-memory cache (tests / long-lived processes)."""
    _cache.clear()
