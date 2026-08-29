"""Sunday 8 PM IST weekly backtest scorecard (cron: 30 14 * * 0).

Scores the trailing 7 days on the T+5 verdict — five sessions is the shortest
window where a swing call has actually resolved, so it is the first honest read
on whether the advisor is adding alpha.

Rows from failed or credit-gapped runs are excluded from every metric and
reported as a separate count, so a week of API outages reads as "3 calls, 8
excluded" rather than a fake 0% win rate.

backtest_results is shared with the sibling StockSage app; every query here is
scoped to project='portfolio-advisor'.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

import config
from analysis import claude_client
from delivery import telegram_bot
from storage import supabase_client

log = logging.getLogger(__name__)

MODEL = config.HAIKU_MODEL
PROJECT = config.PROJECT_NAME
_RULE = "━━━━━━━━━━━━━━━━━━━━━━"

SYSTEM = (
    "You are a trading system analyst. "
    "Given backtest metrics as JSON, respond "
    "with exactly 3 lines:\n"
    "LINE1: 💡 [what is working best]\n"
    "LINE2: ⚠️ [what to watch or is failing]\n"
    "LINE3: 🔧 [one specific prompt tuning suggestion]\n"
    "Be specific. Max 15 words per line. Only comment on what the JSON "
    "contains — a market or action absent from it was not traded, so never "
    "describe it as failing."
)

_ACTIONS = [
    "ADD", "HOLD", "EXIT-FULL", "EXIT-PARTIAL",
    "TIGHTEN-SL", "BUY-MOMENTUM", "BUY-EVENT",
]


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def win_rate(subset: list[dict[str, Any]]) -> tuple[int, int]:
    """(wins, n) for a subset. (0, 0) when empty — callers must guard the ratio."""
    if not subset:
        return 0, 0
    wins = sum(1 for r in subset if r.get("t5_outcome") == "win")
    return wins, len(subset)


def avg_alpha(subset: list[dict[str, Any]]) -> float:
    alphas = [float(r["t5_alpha"]) for r in subset if r.get("t5_alpha") is not None]
    return round(sum(alphas) / len(alphas), 2) if alphas else 0.0



_EXIT_ACTIONS = {"EXIT-FULL", "EXIT-PARTIAL", "BOOK-PROFIT",
                 "FULL-EXIT", "PARTIAL-EXIT", "SELL"}


def effective_alpha(row: dict[str, Any], horizon: str = "t5") -> float | None:
    """Alpha signed in the direction the call wanted the stock to move.

    An EXIT that is followed by a -6% slide is the best call of the week, but
    its raw alpha is -6 — sorting on raw alpha would file it under the worst.
    Flipping the sign for exits lets one ranking serve every action type.
    """
    alpha = row.get(f"{horizon}_alpha")
    if alpha is None:
        return None
    alpha = float(alpha)
    return -alpha if (row.get("action") or "").upper() in _EXIT_ACTIONS else alpha


def _pct(wins: int, n: int) -> float:
    return wins / n * 100 if n else 0.0


def emo(wr: float) -> str:
    return "✅" if wr >= 55 else "⚠️"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _fetch(week_ago: str) -> tuple[list[dict[str, Any]], int]:
    client = supabase_client.get_client()
    if client is None:
        return [], 0
    try:
        rows = (
            client.table("backtest_results")
            .select("*")
            .eq("project", PROJECT)
            .eq("excluded", False)
            .gte("run_date", week_ago)
            .not_.is_("t5_outcome", None)
            .neq("t5_outcome", "not_scored")
            .execute()
        ).data or []
    except Exception as exc:
        log.error("scorecard row fetch failed: %s", exc)
        rows = []
    try:
        excluded_count = (
            client.table("backtest_results")
            .select("id", count="exact")
            .eq("project", PROJECT)
            .eq("excluded", True)
            .gte("run_date", week_ago)
            .execute()
        ).count or 0
    except Exception as exc:
        log.warning("excluded count failed: %s", exc)
        excluded_count = 0
    return rows, excluded_count


def _ask_claude(metrics: dict[str, Any]) -> str:
    """Three-line Haiku insight. Empty string on failure — never fatal."""
    try:
        result = claude_client.call_claude(
            model=MODEL,
            system=SYSTEM,
            user=f"Metrics: {json.dumps(metrics, default=str)}",
            run_type="weekly_scorecard",
            max_tokens=300,
        )
        return (result.get("text") or "").strip()
    except Exception as exc:
        log.warning("Claude insight failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_weekly_scorecard() -> dict[str, Any]:
    today = date.today()
    week_ago = today - timedelta(days=7)
    rows, excluded_count = _fetch(week_ago.isoformat())

    if not rows:
        msg = (
            "📊 Weekly Backtest\n"
            "No T+5 verdicts yet this week.\n"
            f"Excluded rows: {excluded_count}\n"
            "First verdicts arrive 5 trading days after first recommendations."
        )
        _send(msg)
        return {"total": 0, "excluded": excluded_count, "message": msg}

    total_n = len(rows)
    total_wins, _ = win_rate(rows)
    overall_wr = _pct(total_wins, total_n)
    overall_alpha = avg_alpha(rows)

    ind = [r for r in rows if r.get("market") == "IND"]
    us = [r for r in rows if r.get("market") == "US"]
    ind_wins, ind_n = win_rate(ind)
    us_wins, us_n = win_rate(us)

    actions: dict[str, tuple[int, int, float]] = {}
    for action in _ACTIONS:
        subset = [r for r in rows if (r.get("action") or "").upper() == action]
        if subset:
            w, n = win_rate(subset)
            actions[action] = (w, n, avg_alpha(subset))

    high = [r for r in rows if (r.get("confidence_score") or 0) >= 8]
    mid = [r for r in rows if 6 <= (r.get("confidence_score") or 0) < 8]
    high_wins, high_n = win_rate(high)
    mid_wins, mid_n = win_rate(mid)

    executed = [r for r in rows if r.get("user_executed")]
    skipped = [r for r in rows if not r.get("user_executed")]
    exec_wins, exec_n = win_rate(executed)
    skip_wins, skip_n = win_rate(skipped)

    ranked = sorted(
        [r for r in rows if r.get("t5_alpha") is not None],
        key=lambda x: effective_alpha(x) or 0.0,
    )
    worst = ranked[0] if ranked else None
    best = ranked[-1] if ranked else None

    metrics: dict[str, Any] = {
        "overall_win_rate": round(overall_wr, 1),
        "avg_alpha": overall_alpha,
        "sample_size": total_n,
        "by_action": {
            k: {"win_rate": round(_pct(v[0], v[1]), 1), "n": v[1]}
            for k, v in actions.items() if v[1]
        },
    }
    if ind_n:
        metrics["ind"] = {"win_rate": round(_pct(ind_wins, ind_n), 1), "n": ind_n}
    if us_n:
        metrics["us"] = {"win_rate": round(_pct(us_wins, us_n), 1), "n": us_n}
    if high_n:
        metrics["high_conf"] = {"win_rate": round(_pct(high_wins, high_n), 1), "n": high_n}
    if mid_n:
        metrics["mid_conf"] = {"win_rate": round(_pct(mid_wins, mid_n), 1), "n": mid_n}
    if exec_n:
        metrics["executed"] = {"win_rate": round(_pct(exec_wins, exec_n), 1), "n": exec_n}
    if skip_n:
        metrics["skipped"] = {"win_rate": round(_pct(skip_wins, skip_n), 1), "n": skip_n}

    haiku = _ask_claude(metrics)

    msg = (
        f"📊 Weekly Backtest — {today.strftime('%b %d, %Y')}\n"
        f"{_RULE}\n"
        f"📈 OVERALL (excl {excluded_count} bad rows)\n"
        f"Calls: {total_n} | Win: {overall_wr:.0f}% {emo(overall_wr)}\n"
        f"Avg alpha: {overall_alpha:+.1f}%\n"
        f"Execution: {exec_n}/{total_n}\n\n"
    )

    if ind_n:
        wr = _pct(ind_wins, ind_n)
        msg += f"🇮🇳 INDIAN: {wr:.0f}% win {emo(wr)} | {avg_alpha(ind):+.1f}% alpha\n"
    if us_n:
        wr = _pct(us_wins, us_n)
        msg += f"🌐 US: {wr:.0f}% win {emo(wr)} | {avg_alpha(us):+.1f}% alpha\n"

    if actions:
        msg += "\n🎯 BY ACTION\n"
        for action, (w, n, alpha) in actions.items():
            wr = _pct(w, n)
            msg += f"{action}: {wr:.0f}% {emo(wr)} | {alpha:+.1f}%\n"

    # Each bucket is printed only when it has rows — a week with no
    # conf-8+ calls must not divide by zero here.
    if high_n or mid_n:
        msg += "\n📊 CONFIDENCE\n"
        if high_n:
            wr = _pct(high_wins, high_n)
            msg += f"Conf 8-10: {wr:.0f}% {emo(wr)} ({high_n})\n"
        if mid_n:
            wr = _pct(mid_wins, mid_n)
            msg += f"Conf 6-7:  {wr:.0f}% {emo(wr)} ({mid_n})\n"

    if exec_n or skip_n:
        msg += "\n💼 YOUR EXECUTION\n"
        if exec_n:
            msg += f"Followed: {_pct(exec_wins, exec_n):.0f}% win ({exec_n} trades)\n"
        if skip_n:
            msg += f"Skipped:  {_pct(skip_wins, skip_n):.0f}% win ({skip_n} calls)\n"
        if exec_n and skip_n:
            delta = _pct(exec_wins, exec_n) - _pct(skip_wins, skip_n)
            msg += f"Delta: {delta:+.0f}% following Claude\n"

    if best:
        msg += (f"\n🏆 Best: {best['ticker']} {best.get('action')} "
                f"{effective_alpha(best):+.1f}% in-favour alpha\n")
    if worst:
        msg += (f"💥 Worst: {worst['ticker']} {worst.get('action')} "
                f"{effective_alpha(worst):+.1f}% in-favour alpha\n")

    msg += f"❌ Excluded: {excluded_count} bad runs / bad prices\n"
    if haiku:
        msg += f"\n{haiku}\n"
    msg += _RULE

    _send(msg)
    return {"total": total_n, "excluded": excluded_count, "metrics": metrics, "message": msg}


def _send(body: str) -> None:
    if config.PAPER_TRADING:
        body = telegram_bot.PAPER_TRADING_BANNER + "\n\n" + body
    if config.DRY_RUN:
        log.info("[DRY_RUN] weekly scorecard not sent:\n%s", body)
        print(body)
        return
    telegram_bot.send_alert(body, parse_mode=None)
    print(body)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run_weekly_scorecard()
