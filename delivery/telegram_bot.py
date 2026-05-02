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
