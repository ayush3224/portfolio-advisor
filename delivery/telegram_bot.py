"""Telegram delivery — formatters + sender.

Uses the bot HTTP API directly (no python-telegram-bot async runtime) so this
plays nicely with cron jobs that want a synchronous, short-lived process.

DRY_RUN=true short-circuits send_alert without contacting Telegram.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import requests

import config

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"

PAPER_TRADING_BANNER = (
    "<b>📋 PAPER TRADING — mock portfolio</b>\n"
    "<i>Results are simulated, not real positions</i>"
)


def send_alert(text: str, *, parse_mode: str = "HTML", disable_web_preview: bool = True) -> bool:
    """Send a Telegram message. Returns True on success, False on failure or DRY_RUN."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] telegram skip — message length=%d, body follows:\n%s",
                 len(text), text)
        return False
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram creds missing — message dropped")
        return False
    url = f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text[:4096],  # Telegram hard cap
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_preview,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as exc:
        log.exception("Telegram send failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _fmt_price(p: Any) -> str:
    if p is None:
        return "—"
    return f"₹{float(p):,.2f}"


def format_premarket(recommendations: Iterable[dict[str, Any]]) -> str:
    """Pre-market advisory message."""
    recs = list(recommendations)
    if not recs:
        return "<b>🌅 Pre-market 9:00 AM</b>\n\nNo actionable recommendations today. Hold all positions."
    lines = ["<b>🌅 Pre-market 9:00 AM</b>", ""]
    for r in recs:
        action_emoji = {
            "BUY": "🟢", "ADD": "🟢", "HOLD": "⚪",
            "EXIT-PARTIAL": "🟡", "EXIT-FULL": "🔴", "TIGHTEN-SL": "🟠",
        }.get(r["action"], "•")
        lines.append(
            f"{action_emoji} <b>{r['action']} {r['ticker']}</b> "
            f"(conf {r['confidence_score']}/10)"
        )
        if r.get("entry_price"):
            lines.append(f"   Entry: {_fmt_price(r['entry_price'])}  "
                         f"Target: {_fmt_price(r.get('target_price'))}  "
                         f"SL: {_fmt_price(r.get('stop_loss'))}")
        if r.get("leverage_multiplier"):
            lines.append(
                f"   Size: {r.get('shares_qty', '—')} shares  "
                f"Leverage: {r['leverage_multiplier']}x  "
                f"Capital: {_fmt_price(r.get('capital_deployed'))}"
            )
        if r.get("reasoning"):
            lines.append(f"   <i>{r['reasoning']}</i>")
        lines.append("")
    lines.append("<i>⚠️ Manual execution only — system never places orders.</i>")
    return "\n".join(lines)


def format_midday(checks: Iterable[dict[str, Any]]) -> str:
    checks = list(checks)
    if not checks:
        return "<b>☀️ Midday 12:30 PM</b>\n\nNo open leveraged positions to review."
    lines = ["<b>☀️ Midday 12:30 PM — leveraged position check</b>", ""]
    for c in checks:
        pnl = c.get("pnl_unrealised") or 0
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{pnl_emoji} <b>{c['ticker']}</b> — {c['action']}"
        )
        lines.append(
            f"   Entry: {_fmt_price(c.get('entry_price'))}  "
            f"CMP: {_fmt_price(c.get('cmp'))}  "
            f"P&L: {_fmt_price(pnl)}"
        )
        if c.get("new_sl"):
            lines.append(f"   New SL: {_fmt_price(c['new_sl'])}")
        if c.get("reasoning"):
            lines.append(f"   <i>{c['reasoning']}</i>")
        lines.append("")
    lines.append("<i>⏰ All MIS positions must exit by 3:15 PM.</i>")
    return "\n".join(lines)


def format_eod(recommendations: Iterable[dict[str, Any]]) -> str:
    recs = list(recommendations)
    if not recs:
        return "<b>🌇 EOD 3:00 PM</b>\n\nNo positions need an exit decision."
    lines = ["<b>🌇 EOD 3:00 PM — exit / hold decisions</b>", ""]
    for r in recs:
        emoji = "🟢" if r.get("hold_overnight") else "🔴"
        lines.append(f"{emoji} <b>{r['ticker']}</b> — {r['action']}")
        if r.get("reasoning"):
            lines.append(f"   <i>{r['reasoning']}</i>")
        lines.append("")
    return "\n".join(lines)


def format_outcome(rows: Iterable[dict[str, Any]]) -> str:
    rows = list(rows)
    if not rows:
        return "<b>📊 EOD outcome log</b>\n\nNo executed recommendations to log today."
    total_pnl = sum((r.get("actual_pnl_inr") or 0) for r in rows)
    wins = sum(1 for r in rows if (r.get("actual_pnl_inr") or 0) > 0)
    lines = [
        "<b>📊 EOD outcome log</b>",
        "",
        f"Trades: {len(rows)}  Wins: {wins}  Net P&L: {_fmt_price(total_pnl)}",
        "",
    ]
    for r in rows:
        pnl = r.get("actual_pnl_inr") or 0
        emoji = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{emoji} {r['ticker']} {r.get('action', '')}  "
            f"return {(r.get('return_pct') or 0):+.2f}%  "
            f"alpha {(r.get('alpha_pct') or 0):+.2f}%  "
            f"P&L {_fmt_price(pnl)}"
        )
    return "\n".join(lines)


def _fmt_signed_inr(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else '-'}₹{abs(float(v)):,.0f}"


def _box_top(width: int = 31) -> str:  return "┌" + "─" * width + "┐"
def _box_mid(width: int = 31) -> str:  return "├" + "─" * width + "┤"
def _box_bot(width: int = 31) -> str:  return "└" + "─" * width + "┘"
def _box_row(text: str, width: int = 31) -> str:
    inner = width - 2
    return f"│ {text:<{inner}} │"


def format_eod_summary(date_str: str, usage: dict[str, Any], perf: dict[str, Any]) -> str:
    """Compose the comprehensive 4 PM Telegram summary.

    `usage`: {runs: [{run_type, model_used, input_tokens, output_tokens,
                      total_tokens, cost_usd, ist_time}],
              today_tokens, today_cost_usd, today_cost_inr,
              mtd_cost_usd, mtd_cost_inr,
              projected_cost_usd, projected_cost_inr}
    `perf`:  {recs_count, executed_count, skipped_count,
              executed_rows: [{ticker, action, actual_pnl_inr, mark}],
              total_pnl, loss_limit_used, loss_limit, alpha,
              best, worst, awaiting_prices: bool}
    """
    lines: list[str] = []
    lines.append(f"📊 <b>End of Day Summary — {date_str}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    # ---- AI USAGE ----
    lines.append("🤖 <b>AI USAGE TODAY</b>")
    usage_lines: list[str] = [_box_top()]
    runs = usage.get("runs") or []
    if not runs:
        usage_lines.append(_box_row("No AI runs logged today"))
    else:
        for r in runs:
            t = r.get("ist_time", "—")
            model = r.get("model_short", r.get("model_used", "?")) or "?"
            tokens = r.get("total_tokens", 0)
            cost = r.get("cost_usd", 0.0) or 0.0
            usage_lines.append(_box_row(f"{t:<8} {model:<6} {tokens:>6,} tok") + f" ${cost:.3f}")
    usage_lines.append(_box_mid())
    usage_lines.append(_box_row(
        f"Today total:  {usage.get('today_tokens', 0):>5,} tokens"
    ) + f" ${usage.get('today_cost_usd', 0):.3f} / ₹{usage.get('today_cost_inr', 0):.2f}")
    usage_lines.append(_box_row(
        f"MTD total:    ${usage.get('mtd_cost_usd', 0):.2f} / ₹{usage.get('mtd_cost_inr', 0):.0f}"
    ))
    usage_lines.append(_box_row(
        f"Projected:    ${usage.get('projected_cost_usd', 0):.2f} / ₹{usage.get('projected_cost_inr', 0):.0f}"
    ))
    usage_lines.append(_box_bot())
    lines.append("<pre>" + "\n".join(usage_lines) + "</pre>")
    lines.append("")

    # ---- TRADING PERFORMANCE ----
    lines.append("💰 <b>TRADING PERFORMANCE</b>")
    perf_lines: list[str] = [_box_top()]
    recs_count = perf.get("recs_count", 0)
    perf_lines.append(_box_row(f"Recommendations: {recs_count}"))
    perf_lines.append(_box_row(
        f"Executed: {perf.get('executed_count', 0)} | Skipped: {perf.get('skipped_count', 0)}"
    ))
    paper_count = perf.get("paper_count", 0) or 0
    real_count = perf.get("real_count", 0) or 0
    if paper_count or real_count:
        perf_lines.append(_box_row(f"📋 Paper: {paper_count} | 💰 Real: {real_count}"))
    perf_lines.append(_box_mid())

    executed_rows = perf.get("executed_rows") or []
    if not executed_rows:
        perf_lines.append(_box_row("No trades executed today"))
    elif perf.get("awaiting_prices"):
        perf_lines.append(_box_row("Awaiting closing prices"))
    else:
        for row in executed_rows:
            mark = row.get("mark", "✅")
            ticker = row.get("ticker", "—")
            action = row.get("action", "—")
            pnl_str = _fmt_signed_inr(row.get("actual_pnl_inr"))
            perf_lines.append(_box_row(f"{mark} {ticker:<10} {action:<5} {pnl_str}"))
    perf_lines.append(_box_mid())

    if executed_rows and not perf.get("awaiting_prices"):
        perf_lines.append(_box_row(f"P&L today:        {_fmt_signed_inr(perf.get('total_pnl'))}"))
        perf_lines.append(_box_row(
            f"Loss limit used:  ₹{int(perf.get('loss_limit_used') or 0):,}/₹{int(perf.get('loss_limit') or 0):,}"
        ))
        alpha = perf.get("alpha")
        alpha_str = f"{alpha:+.1f}%" if alpha is not None else "—"
        perf_lines.append(_box_row(f"Alpha vs Nifty:   {alpha_str}"))
        perf_lines.append(_box_mid())
        best = perf.get("best")
        worst = perf.get("worst")
        if best:
            perf_lines.append(_box_row(f"🏆 Best:  {best['ticker']:<10} {_fmt_signed_inr(best.get('actual_pnl_inr'))}"))
        if worst:
            perf_lines.append(_box_row(f"💥 Worst: {worst['ticker']:<10} {_fmt_signed_inr(worst.get('actual_pnl_inr'))}"))
    elif perf.get("awaiting_prices"):
        perf_lines.append(_box_row("P&L:              awaiting prices"))
    else:
        perf_lines.append(_box_row("P&L today:        —"))
        perf_lines.append(_box_row(
            f"Loss limit used:  ₹0/₹{int(perf.get('loss_limit') or 0):,}"
        ))

    perf_lines.append(_box_bot())
    lines.append("<pre>" + "\n".join(perf_lines) + "</pre>")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>⚠️ Manual execution only</i>")
    return "\n".join(lines)


def format_weekly_scorecard(summary: dict[str, Any]) -> str:
    return (
        "<b>📅 Weekly scorecard</b>\n\n"
        f"Period: {summary.get('start')} → {summary.get('end')}\n"
        f"Trades: {summary.get('trades', 0)}\n"
        f"Win rate: {summary.get('win_rate', 0):.1%}\n"
        f"Total P&L: {_fmt_price(summary.get('total_pnl', 0))}\n"
        f"Avg alpha vs Nifty: {(summary.get('avg_alpha') or 0):+.2f}%\n"
        f"Best: {summary.get('best_trade', '—')}\n"
        f"Worst: {summary.get('worst_trade', '—')}\n"
    )
