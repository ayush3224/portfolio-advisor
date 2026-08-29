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


def send_alert(text: str, *, parse_mode: str | None = "HTML", disable_web_preview: bool = True) -> bool:
    """Send a Telegram message. Returns True on success, False on failure or DRY_RUN."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] telegram skip — message length=%d, body follows:\n%s",
                 len(text), text)
        return False
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram creds missing — message dropped")
        return False
    url = f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text[:4096],  # Telegram hard cap
        "disable_web_page_preview": disable_web_preview,
    }
    # Telegram rejects a null parse_mode outright ("unsupported parse_mode"),
    # so plain-text sends must omit the key rather than pass None.
    if parse_mode:
        payload["parse_mode"] = parse_mode
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


def format_premarket(
    recommendations: Iterable[dict[str, Any]],
    *,
    skipped: Iterable[dict[str, Any]] | None = None,
) -> str:
    """Pre-market advisory message.

    `skipped` — tickers Claude analysed but discarded (confidence < 6 or
    explicit skipped=true). Surfaced in a footer so the user can see what
    was considered and why.
    """
    recs = list(recommendations)
    skipped_list = list(skipped or [])
    if not recs:
        body = "<b>🌅 Pre-market 9:00 AM</b>\n\nNo actionable recommendations today. Hold all positions."
        return body + _skipped_footer(skipped_list)
    lines = ["<b>🌅 Pre-market 9:00 AM</b>", ""]
    for r in recs:
        action = (r.get("action") or "").upper()
        action_emoji = {
            "BUY": "🟢", "ADD": "🟢", "HOLD": "⚪",
            "EXIT-PARTIAL": "🟡", "EXIT-FULL": "🔴", "TIGHTEN-SL": "🟠",
        }.get(action, "•")
        lines.append(
            f"{action_emoji} <b>{action} {r['ticker']}</b> "
            f"(conf {r['confidence_score']}/10)"
        )
        if r.get("entry_price"):
            lines.append(f"   Entry: {_fmt_price(r['entry_price'])}  "
                         f"Target: {_fmt_price(r.get('target_price'))}  "
                         f"SL: {_fmt_price(r.get('stop_loss'))}")
        # HOLD/TIGHTEN-SL recommend keeping what you already own — show the
        # existing position, not new-entry sizing. New-entry actions (BUY/ADD)
        # show the proposed deploy. Leverage is gone — every trade is 1x CNC.
        if action in ("HOLD", "TIGHTEN-SL"):
            qty = r.get("held_qty")
            avg = r.get("held_avg_price")
            pnl = r.get("held_unrealised_pnl")
            pnl_pct = r.get("held_unrealised_pnl_pct")
            if qty is not None and avg is not None:
                pnl_part = ""
                if pnl is not None and pnl_pct is not None:
                    pnl_part = f" | Unrealised: {_fmt_signed_inr(pnl)} ({pnl_pct:+.2f}%)"
                lines.append(
                    f"   Holding: {int(qty) if float(qty).is_integer() else qty} shares | "
                    f"Avg: {_fmt_price(avg)}{pnl_part}"
                )
        elif r.get("shares_qty") or r.get("capital_deployed"):
            lines.append(
                f"   Size: {r.get('shares_qty', '—')} shares  "
                f"Capital: {_fmt_price(r.get('capital_deployed'))}"
            )
        if r.get("reasoning"):
            lines.append(f"   <i>{r['reasoning']}</i>")
        lines.append("")
    lines.append("<i>⚠️ Manual execution only — system never places orders.</i>")
    return "\n".join(lines) + _skipped_footer(skipped_list)


def _skipped_footer(skipped: list[dict[str, Any]]) -> str:
    if not skipped:
        return ""
    lines = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🔕 <b>SKIPPED (confidence below threshold)</b>",
    ]
    for s in skipped:
        ticker = s.get("ticker") or "—"
        conf = s.get("confidence_score")
        conf_str = f"{conf}/10" if conf is not None else "?/10"
        reason = (s.get("reasoning") or "").strip() or "no reasoning provided"
        lines.append(f"<b>{ticker}</b> — conf {conf_str} | Reason: <i>{reason}</i>")
    return "\n" + "\n".join(lines)


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


def _mark_for(outcome: str | None) -> str:
    if outcome == "win":
        return "✅"
    if outcome == "loss":
        return "❌"
    return "⚪"


def _bench_label(market: str) -> str:
    return "Nifty" if market == "IND" else "S&amp;P"


def _format_scorecard_section(title: str, rows: list[dict[str, Any]], bench_pct: float | None, market: str) -> list[str]:
    lines = [f"<b>{title}</b>"]
    if not rows:
        lines.append("<i>No picks today</i>")
        return lines
    bench_str = ""
    if bench_pct is not None:
        bench_str = f"  ({_bench_label(market)} {bench_pct:+.1f}%)"
    for r in rows:
        executed = bool(r.get("user_executed"))
        outcome = r.get("outcome")
        ticker = (r.get("ticker") or "—")[:10]
        action = (r.get("action") or "—")[:11]
        alpha = float(r.get("alpha_pct") or 0)
        if executed:
            outcome_mark = _mark_for(outcome)
            line = f"⚡ {ticker:<9} {action:<11} executed {outcome_mark}"
        else:
            mark = _mark_for(outcome)
            line = f"{mark} {ticker:<9} {action:<11} {alpha:+.1f}% alpha"
        # Only the first row gets the benchmark tag for readability.
        if r is rows[0]:
            line += bench_str
        lines.append(line)
    return lines


def format_daily_scorecard(date_str: str, scorecard: dict[str, Any], cost: dict[str, Any]) -> str:
    """4 PM EOD scorecard — per-pick win/loss with alpha vs Nifty / S&P 500."""
    rule = "━━━━━━━━━━━━━━━━━━━━━━"
    lines: list[str] = [f"📊 <b>Today's Scorecard — {date_str}</b>", rule]

    body_ind = _format_scorecard_section(
        "🇮🇳 INDIAN PICKS",
        scorecard.get("ind_rows") or [],
        scorecard.get("ind_benchmark"),
        "IND",
    )
    body_us = _format_scorecard_section(
        "🌐 US PICKS",
        scorecard.get("us_rows") or [],
        scorecard.get("us_benchmark"),
        "US",
    )
    lines.append("<pre>" + "\n".join(body_ind) + "</pre>")
    lines.append("<pre>" + "\n".join(body_us) + "</pre>")
    lines.append(rule)

    total = scorecard.get("total", 0)
    wins = scorecard.get("wins", 0)
    losses = scorecard.get("losses", 0)
    win_rate = (wins / total * 100) if total else 0
    executed = scorecard.get("executed", 0)
    avg_alpha = float(scorecard.get("avg_alpha") or 0)
    total_pnl = float(scorecard.get("total_pnl") or 0)
    lines.append(f"Today: <b>{wins} wins / {losses} losses</b> ({win_rate:.0f}%)")
    lines.append(f"Executed: {executed}/{total} recommendations followed")
    lines.append(f"Avg alpha: {avg_alpha:+.2f}%")
    if executed:
        lines.append(f"P&amp;L (executed): {_fmt_signed_inr(total_pnl)}")
    lines.append("")

    # ---- AI COST ----
    lines.append("🤖 <b>AI COST TODAY</b>")
    cost_lines: list[str] = []
    for row in cost.get("model_rows") or []:
        label = row.get("label") or row.get("model") or "?"
        model = row.get("model") or ""
        usd = float(row.get("cost_usd") or 0)
        cost_lines.append(f"{label} {model}:".ljust(20) + f"${usd:.3f}")
    if not cost_lines:
        cost_lines.append("No AI runs logged today")
    total_usd = float(cost.get("total_cost_usd") or 0)
    total_inr = float(cost.get("total_cost_inr") or 0)
    cost_lines.append(f"Total: ${total_usd:.3f} / ₹{total_inr:.2f}")
    lines.append("<pre>" + "\n".join(cost_lines) + "</pre>")

    lines.append(rule)
    lines.append("<i>⚠️ Manual execution only</i>")
    return "\n".join(lines)


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


def format_weekly_review(s: dict[str, Any]) -> str:
    """Sunday recap — recommendations + outcomes for the trailing 7 days."""
    by_action = s.get("by_action") or {}
    lines = [
        "<b>📅 Weekly Review</b>",
        f"<i>{s.get('start')} → {s.get('end')}</i>",
        "",
        "<b>Recommendations</b>",
        f"  Total: {s.get('recs_total', 0)} "
        f"(executed {s.get('executed', 0)} · paper {s.get('paper', 0)})",
    ]
    if by_action:
        action_summary = " · ".join(f"{a} {n}" for a, n in sorted(by_action.items()))
        lines.append(f"  Actions: {action_summary}")
    lines.append("")
    lines.append("<b>Outcomes</b>")
    if not s.get("trades"):
        lines.append("  No closed trades this week.")
    else:
        lines.append(
            f"  Trades: {s.get('trades', 0)} · "
            f"Wins: {s.get('wins', 0)} · Losses: {s.get('losses', 0)}"
        )
        lines.append(f"  Win rate: {s.get('win_rate', 0):.1%}")
        lines.append(f"  Net P&L: {_fmt_signed_inr(s.get('total_pnl'))}")
        if s.get("avg_alpha") is not None:
            lines.append(f"  Avg alpha vs Nifty: {s['avg_alpha']:+.2f}%")
        if s.get("best"):
            b = s["best"]
            lines.append(
                f"  🏆 Best: {b.get('ticker')} {_fmt_signed_inr(b.get('actual_pnl_inr'))}"
            )
        if s.get("worst"):
            w = s["worst"]
            lines.append(
                f"  💥 Worst: {w.get('ticker')} {_fmt_signed_inr(w.get('actual_pnl_inr'))}"
            )
    return "\n".join(lines)


def _wr_marker(pct: float, *, ok: float = 60.0, warn: float = 50.0) -> str:
    if pct >= ok:
        return "✅"
    if pct >= warn:
        return "⚪"
    return "⚠️"


def format_weekly_scorecard(metrics: dict[str, Any], insight: dict[str, str] | None = None) -> str:
    """Sunday weekly backtest recap — overall, per-market, per-action, per-confidence."""
    rule = "━━━━━━━━━━━━━━━━━━━━━━"
    insight = insight or {}
    start = metrics.get("start")
    end = metrics.get("end")
    total = metrics.get("total", 0)

    lines: list[str] = [f"📊 <b>Weekly Backtest — {start} → {end}</b>", rule]

    if not total:
        lines.append("<i>No recommendations recorded this week.</i>")
        return "\n".join(lines)

    # ---- OVERALL ----
    win_rate = float(metrics.get("win_rate") or 0)
    alpha_wr = float(metrics.get("alpha_win_rate") or 0)
    avg_alpha = float(metrics.get("avg_alpha") or 0)
    exec_rate = float(metrics.get("execution_rate") or 0)
    followed = metrics.get("execution", {}).get("followed", {}).get("n", 0)
    overall = [
        "📈 <b>OVERALL</b>",
        f"Win rate:    {win_rate:.0f}% ({metrics.get('total', 0)} calls)",
        f"Alpha win:   {alpha_wr:.0f}% beat benchmark",
        f"Avg alpha:   {avg_alpha:+.2f}%",
        f"Execution:   {followed}/{total} followed ({exec_rate:.0f}%)",
    ]
    lines.append("<pre>" + "\n".join(overall) + "</pre>")

    # ---- BY MARKET ----
    def _market_block(label: str, m: dict[str, Any]) -> list[str]:
        n = m.get("n", 0)
        if not n:
            return [f"<b>{label}</b>", "<i>No picks</i>"]
        out = [
            f"<b>{label}</b>",
            f"Win rate: {m.get('win_rate', 0):.0f}% | Avg alpha: {m.get('avg_alpha', 0):+.2f}%",
        ]
        if m.get("best"):
            b = m["best"]
            out.append(f"Best:  {b.get('ticker')} {float(b.get('alpha_pct') or 0):+.1f}% alpha")
        if m.get("worst"):
            w = m["worst"]
            out.append(f"Worst: {w.get('ticker')} {float(w.get('alpha_pct') or 0):+.1f}% alpha")
        return out

    lines.append("<pre>" + "\n".join(_market_block("🇮🇳 INDIAN PICKS", metrics.get("ind", {}))) + "</pre>")
    lines.append("<pre>" + "\n".join(_market_block("🌐 US PICKS", metrics.get("us", {}))) + "</pre>")

    # ---- BY ACTION GROUP ----
    action_lines = ["🎯 <b>BY ACTION</b>"]
    order = ["ADD/BUY", "HOLD", "EXIT calls", "Other"]
    actions_map = metrics.get("actions") or {}
    for action in order:
        stats = actions_map.get(action)
        if not stats:
            continue
        n = stats.get("n", 0)
        wr = stats.get("win_rate", 0)
        action_lines.append(f"{action:<11} {wr:>3.0f}%  ({n}) {_wr_marker(wr)}")
    lines.append("<pre>" + "\n".join(action_lines) + "</pre>")

    # ---- BY CONFIDENCE ----
    conf_lines = ["📊 <b>CONFIDENCE</b>"]
    order = ["8-10", "6-7", "<6"]
    for b in order:
        s = (metrics.get("confidence") or {}).get(b)
        if not s:
            continue
        wr = s.get("win_rate", 0)
        label = b.replace("<", "&lt;")
        conf_lines.append(f"Conf {label:<5} {wr:>3.0f}% win ({s.get('n', 0)}) {_wr_marker(wr)}")
    lines.append("<pre>" + "\n".join(conf_lines) + "</pre>")

    # ---- EXECUTION ----
    ex = metrics.get("execution") or {}
    fol = ex.get("followed", {})
    sk = ex.get("skipped", {})
    pnl = float(ex.get("pnl_inr") or 0)
    ex_lines = [
        "💼 <b>YOUR EXECUTION</b>",
        f"Followed: {fol.get('win_rate', 0):.0f}% win  ({fol.get('n', 0)})",
        f"Skipped:  {sk.get('win_rate', 0):.0f}% win  ({sk.get('n', 0)})",
    ]
    if fol.get("n") and sk.get("n"):
        diff = float(fol.get("win_rate", 0)) - float(sk.get("win_rate", 0))
        verdict = "Claude outperforming your intuition" if diff > 5 else (
            "Your skips were correct" if diff < -5 else "Roughly tied"
        )
        ex_lines.append(f"→ {verdict}")
    lines.append("<pre>" + "\n".join(ex_lines) + "</pre>")

    # ---- CLAUDE INSIGHT ----
    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if insight.get("insight") or insight.get("watch") or insight.get("tune"):
        lines.append("")
        if insight.get("insight"):
            lines.append(f"💡 <b>INSIGHT:</b> {_esc(insight['insight'])}")
        if insight.get("watch"):
            lines.append(f"⚠️ <b>WATCH:</b> {_esc(insight['watch'])}")
        if insight.get("tune"):
            lines.append(f"🔧 <b>TUNE:</b> {_esc(insight['tune'])}")

    lines.append(rule)
    lines.append(f"Weekly P&amp;L (executed trades): {_fmt_signed_inr(pnl)}")
    return "\n".join(lines)
