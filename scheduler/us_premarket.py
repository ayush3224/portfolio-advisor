"""7:30 PM IST US-stock portfolio advisory.

Pipeline:
  1. Read US holdings from Supabase (market='US')
  2. Fetch US macro via yfinance: ^GSPC, ^IXIC, ^VIX, INR=X, ^TNX
  3. Live USD prices + day OHLC for each holding (yfinance)
  4. Tavily news per holding (max 3 each)
  5. Polymarket US signals
  6. Sonnet 4.5 → action per ticker
  7. Format Telegram message, split at ~3,800 chars per part
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any

import yfinance as yf

import config
from analysis import us_premarket_prompt
from delivery import telegram_bot
from ingestion import news as news_mod
from ingestion import polymarket, upstox_portfolio
from processing import technicals as technicals_mod

log = logging.getLogger(__name__)

_TELEGRAM_CHUNK = 3_800  # below Telegram's 4096 hard cap, leaves headroom


# ---------------------------------------------------------------------------
# US macro context
# ---------------------------------------------------------------------------

def _drop_incomplete(hist: Any) -> Any:
    """Drop rows whose Close is NaN.

    Yahoo emits a row for the in-progress session with a null Close, so a bare
    iloc[-1] reads NaN and poisons every derived figure (this is what rendered
    the whole US report as `$nan`)."""
    if hist is None or hist.empty:
        return hist
    return hist.dropna(subset=["Close"])


def _yf_close(symbol: str) -> dict[str, Any] | None:
    try:
        hist = _drop_incomplete(yf.Ticker(symbol).history(period="5d", auto_adjust=False))
        if hist is None or hist.empty or len(hist) < 2:
            return None
        last = hist.iloc[-1]
        prev = hist.iloc[-2]
        last_close = float(last["Close"])
        prev_close = float(prev["Close"])
        return {
            "last": round(last_close, 2),
            "prev_close": round(prev_close, 2),
            "open": round(float(last["Open"]), 2),
            "high": round(float(last["High"]), 2),
            "low": round(float(last["Low"]), 2),
            "gap_pct": round((float(last["Open"]) - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
            "change_pct": round((last_close - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
        }
    except Exception as exc:
        log.warning("yf macro %s failed: %s", symbol, exc)
        return None


def build_us_macro() -> dict[str, Any]:
    return {
        "sp500":     _yf_close("^GSPC"),
        "nasdaq":    _yf_close("^IXIC"),
        "vix":       _yf_close("^VIX"),
        "usd_inr":   _yf_close("INR=X"),
        "tnx_10y":   _yf_close("^TNX"),
    }


# ---------------------------------------------------------------------------
# Per-holding context
# ---------------------------------------------------------------------------

def _yf_symbol(ticker: str) -> str:
    """Convert a stored ticker into the Yahoo-compatible variant.
    Berkshire's class-B shares are stored as BRK.B but Yahoo serves BRK-B."""
    return ticker.replace(".", "-").upper()


def _yf_holding_block(ticker: str, qty: float, avg_usd: float, usd_inr: float) -> dict[str, Any] | None:
    """Enrich a single US holding with live price + 52w range + day change."""
    try:
        t = yf.Ticker(_yf_symbol(ticker))
        day_hist = _drop_incomplete(t.history(period="5d", auto_adjust=False))
        yr_hist = _drop_incomplete(t.history(period="1y", auto_adjust=False))
        if day_hist is None or day_hist.empty:
            return None
        last = day_hist.iloc[-1]
        prev = day_hist.iloc[-2] if len(day_hist) >= 2 else last
        cmp_usd = float(last["Close"])
        prev_close = float(prev["Close"])
        wk52_high = float(yr_hist["High"].max()) if yr_hist is not None and not yr_hist.empty else None
        wk52_low = float(yr_hist["Low"].min()) if yr_hist is not None and not yr_hist.empty else None
        cmp_inr = round(cmp_usd * usd_inr, 2) if usd_inr else None
        avg_inr = round(avg_usd * usd_inr, 2) if usd_inr else None
        unreal_usd = round((cmp_usd - avg_usd) * qty, 2)
        unreal_inr = round(unreal_usd * usd_inr, 2) if usd_inr else None
        unreal_pct = round((cmp_usd - avg_usd) / avg_usd * 100, 2) if avg_usd else 0.0
        # Tier 1 technicals — enrichment only; None on any failure, never fatal.
        tech = technicals_mod.compute_technicals(ticker, "US")
        block = {
            "ticker": ticker,
            "company": (t.info or {}).get("longName") if hasattr(t, "info") else None,
            "qty": qty,
            "avg_price_usd": round(avg_usd, 2),
            "avg_price_inr": avg_inr,
            "current_price_usd": round(cmp_usd, 2),
            "current_price_inr": cmp_inr,
            "unrealised_pnl_usd": unreal_usd,
            "unrealised_pnl_inr": unreal_inr,
            "unrealised_pnl_pct": unreal_pct,
            "day_change_pct": round((cmp_usd - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
            "week_high": round(float(day_hist["High"].max()), 2),
            "week_low": round(float(day_hist["Low"].min()), 2),
            "wk52_high": round(wk52_high, 2) if wk52_high else None,
            "wk52_low": round(wk52_low, 2) if wk52_low else None,
        }
        if tech:
            block["technicals"] = tech
            block["technicals_text"] = technicals_mod.format_technicals_block(tech)
        return block
    except Exception as exc:
        log.warning("holding block %s failed: %s", ticker, exc)
        return None


def _attach_news(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tickers = [b["ticker"] for b in blocks]
    try:
        news_map = news_mod.news_for_holdings(tickers)
    except Exception as exc:
        log.warning("news fetch failed: %s", exc)
        news_map = {}
    for b in blocks:
        b["news"] = news_map.get(b["ticker"], [])[:2]
    return blocks


# ---------------------------------------------------------------------------
# Sonnet helpers
# ---------------------------------------------------------------------------

def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


# ---------------------------------------------------------------------------
# Telegram formatting
# ---------------------------------------------------------------------------

_ACTION_ORDER = ["FULL-EXIT", "PARTIAL-EXIT", "ADD", "WATCH", "HOLD"]
_ACTION_EMOJI = {
    "FULL-EXIT": "🔴", "PARTIAL-EXIT": "🟡", "ADD": "🟢",
    "WATCH": "⚠️", "HOLD": "✅",
}


def _fmt_inr(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "-"
    return f"{sign}₹{abs(v):,.0f}"


def _format_holding_line(rec: dict[str, Any], block: dict[str, Any]) -> str:
    emoji = _ACTION_EMOJI.get(rec.get("action") or "HOLD", "•")
    conf = rec.get("confidence_score", "?")
    ticker = block["ticker"]
    cmp_usd = block["current_price_usd"]
    qty = block["qty"]
    unreal_usd = block.get("unrealised_pnl_usd") or 0
    unreal_pct = block.get("unrealised_pnl_pct") or 0
    unreal_inr = block.get("unrealised_pnl_inr")
    action_label = (rec.get("action") or "HOLD")
    lines = [
        f"{emoji} <b>{action_label} — {ticker}</b> | ⭐ {conf}/10",
        f"   ${cmp_usd:,.2f} | {qty:g}sh | "
        f"{('+' if unreal_usd>=0 else '-')}${abs(unreal_usd):,.2f} "
        f"({unreal_pct:+.1f}%) | {_fmt_inr(unreal_inr)}",
    ]
    if rec.get("reasoning"):
        lines.append(f"   <i>{rec['reasoning']}</i>")
    return "\n".join(lines)


def format_us_advisory(
    blocks_by_ticker: dict[str, dict[str, Any]],
    recs: list[dict[str, Any]],
    macro: dict[str, Any],
    polymarket_text: str,
) -> list[str]:
    """Build one-or-more Telegram messages. Splits at action-group boundaries."""
    sp = macro.get("sp500") or {}
    nq = macro.get("nasdaq") or {}
    vix = macro.get("vix") or {}
    usdinr = macro.get("usd_inr") or {}
    tnx = macro.get("tnx_10y") or {}

    header_lines = [
        "🌐 <b>US Portfolio Advisory — 7:30 PM IST</b>",
        f"S&amp;P 500: {sp.get('last','—')} ({(sp.get('change_pct') or 0):+.2f}%) | "
        f"Nasdaq: {nq.get('last','—')} ({(nq.get('change_pct') or 0):+.2f}%)",
        f"VIX: {vix.get('last','—')} | USD/INR: ₹{config.USD_INR_RATE} (live) | "
        f"10Y: {tnx.get('last','—')}%",
        "",
    ]
    if polymarket_text:
        header_lines.append("🎯 <b>PREDICTION MARKETS</b>")
        # Strip the header line of the prompt-style block and indent
        for line in polymarket_text.splitlines()[1:]:
            header_lines.append(line)
        header_lines.append("")
    header_lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    header_lines.append("<b>YOUR US POSITIONS</b>")
    header_lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    # Group recs by action for stable ordering
    recs_by_ticker = {r.get("ticker"): r for r in recs}

    body_groups: list[list[str]] = []
    for action in _ACTION_ORDER:
        group: list[str] = []
        for ticker, block in blocks_by_ticker.items():
            rec = recs_by_ticker.get(ticker) or {"action": "HOLD", "confidence_score": 5,
                                                 "reasoning": "no model decision — default HOLD"}
            if rec.get("action") != action:
                continue
            group.append(_format_holding_line(rec, block))
        if group:
            body_groups.append(group)

    # Totals footer
    total_usd_value = sum(b["current_price_usd"] * b["qty"] for b in blocks_by_ticker.values())
    total_usd_cost = sum(b["avg_price_usd"] * b["qty"] for b in blocks_by_ticker.values())
    total_usd_pnl = total_usd_value - total_usd_cost
    usd_inr_rate = config.USD_INR_RATE or (usdinr.get("last") or 95.31)
    total_inr_value = total_usd_value * usd_inr_rate
    total_inr_pnl = total_usd_pnl * usd_inr_rate
    pnl_pct = (total_usd_pnl / total_usd_cost * 100) if total_usd_cost else 0.0
    footer = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"Total US: ${total_usd_value:,.0f} (₹{total_inr_value/100_000:,.1f}L) | "
        f"P&amp;L {('+' if total_usd_pnl>=0 else '-')}${abs(total_usd_pnl):,.0f} "
        f"({pnl_pct:+.1f}%) | {_fmt_inr(total_inr_pnl)}",
        "<i>⚠️ Manual execution only — system never places orders.</i>",
    ]

    # Pack into messages, never splitting a holding's multi-line block.
    messages: list[str] = []
    current = "\n".join(header_lines)
    for group in body_groups:
        for block_text in group:
            candidate = (current + "\n\n" + block_text) if current.strip() else block_text
            if len(candidate) > _TELEGRAM_CHUNK:
                messages.append(current.rstrip())
                current = block_text
            else:
                current = candidate
    footer_text = "\n".join(footer)
    if len(current + "\n\n" + footer_text) > _TELEGRAM_CHUNK:
        messages.append(current.rstrip())
        messages.append(footer_text)
    else:
        messages.append((current + "\n\n" + footer_text).rstrip())
    return messages


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> dict[str, Any]:
    portfolio = upstox_portfolio.fetch_portfolio(market="US")
    us_holdings = portfolio.get("holdings") or []
    if not us_holdings:
        log.info("No US holdings — sending empty-state notice")
        telegram_bot.send_alert(
            "🌐 <b>US Portfolio Advisory</b>\n\nNo active US holdings. "
            "Add positions with: <code>BUY-US NVDA 5 243.20</code>"
        )
        return {"holdings": 0, "messages": 0}

    macro = build_us_macro()
    # Prefer the centrally-cached config rate (fetched once at process start)
    # over the per-run yfinance call — they're the same source so the values
    # match, but config.USD_INR_RATE is the single source of truth.
    usd_inr = config.USD_INR_RATE or (macro.get("usd_inr") or {}).get("last") or 95.31

    blocks: list[dict[str, Any]] = []
    for h in us_holdings:
        b = _yf_holding_block(h["ticker"], float(h["quantity"]), float(h["average_price"]), usd_inr)
        if b:
            blocks.append(b)
    blocks = _attach_news(blocks)

    poly_markets = _run_async(polymarket.fetch_relevant_markets("us"))
    poly_text = polymarket.format_for_prompt(poly_markets)

    result = us_premarket_prompt.run(blocks, macro, poly_text)
    recs = result.get("recommendations") or []
    blocks_by_ticker = {b["ticker"]: b for b in blocks}

    messages = format_us_advisory(blocks_by_ticker, recs, macro, poly_text)
    for msg in messages:
        telegram_bot.send_alert(msg)
    log.info("us_premarket complete — %d holdings, %d telegram parts", len(blocks), len(messages))
    return {"holdings": len(blocks), "recommendations": recs, "messages": len(messages)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
