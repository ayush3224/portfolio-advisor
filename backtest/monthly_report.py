"""1st-of-month 9 AM IST calibration report (cron: 30 3 1 * *).

The monthly sibling of weekly_scorecard.py, with three differences that earn it
a Sonnet call rather than Haiku:

  - 30-day window, so the sample is large enough to trust.
  - Both horizons. T+5 says whether the entry timing was right; T+15 says
    whether the thesis was. A call that wins at T+5 and loses at T+15 is a
    momentum read being mistaken for a swing thesis, and only the pair shows it.
  - Per-ticker alpha ranking, which is what actually drives a prompt change —
    "stop trusting the model on this name" is a concrete edit.

backtest_results is shared with the sibling StockSage app; every query here is
scoped to project='portfolio-advisor'.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import config
from analysis import claude_client
from delivery import telegram_bot
from storage import supabase_client

log = logging.getLogger(__name__)

MODEL = config.SONNET_MODEL
PROJECT = config.PROJECT_NAME
_RULE = "━━━━━━━━━━━━━━━━━━━━━━"

# Telegram's hard cap is 4096; split below it so a long month never truncates.
_SPLIT_AT = 3800

SYSTEM = (
    "You are a trading system analyst reviewing a month of BUY/SELL/HOLD "
    "recommendations for a retail swing investor (CNC delivery, no leverage). "
    "You are given aggregated backtest metrics as JSON, covering two horizons: "
    "T+5 (entry timing) and T+15 (thesis).\n\n"
    "Respond with exactly 5 lines, no preamble and no markdown:\n"
    "LINE1: 💡 [what is working best — cite the action type or market]\n"
    "LINE2: ⚠️ [the biggest weakness — cite the number that shows it]\n"
    "LINE3: 📉 [what T+5 vs T+15 divergence reveals about holding period]\n"
    "LINE4: 🎯 [which tickers or confidence bands to trust less]\n"
    "LINE5: 🔧 [one specific, concrete prompt or threshold change for next month]\n\n"
    "Be specific and quantitative. Max 20 words per line. If the sample is "
    "too small to support a claim, say so rather than inventing a pattern. "
    "Only comment on what the JSON contains — a market or action absent from "
    "it was not traded, so never describe it as failing."
)

_ACTIONS = [
    "ADD", "HOLD", "EXIT-FULL", "EXIT-PARTIAL",
    "TIGHTEN-SL", "BUY-MOMENTUM", "BUY-EVENT",
]


# ---------------------------------------------------------------------------
# Metric helpers — horizon-parameterised
# ---------------------------------------------------------------------------

def win_rate(subset: list[dict[str, Any]], horizon: str = "t5") -> tuple[int, int]:
    """(wins, n) counting only rows that have a real verdict for this horizon."""
    scored = [
        r for r in subset
        if r.get(f"{horizon}_outcome") not in (None, "not_scored")
    ]
    wins = sum(1 for r in scored if r.get(f"{horizon}_outcome") == "win")
    return wins, len(scored)


def avg_alpha(subset: list[dict[str, Any]], horizon: str = "t5") -> float:
    alphas = [
        float(r[f"{horizon}_alpha"]) for r in subset
        if r.get(f"{horizon}_alpha") is not None
    ]
    return round(sum(alphas) / len(alphas), 2) if alphas else 0.0



_EXIT_ACTIONS = {"EXIT-FULL", "EXIT-PARTIAL", "BOOK-PROFIT",
                 "FULL-EXIT", "PARTIAL-EXIT", "SELL"}


def effective_alpha(row: dict[str, Any], horizon: str = "t5") -> float | None:
    """Alpha signed in the direction the call wanted the stock to move, so one
    ranking can compare an EXIT against an ADD. See weekly_scorecard."""
    alpha = row.get(f"{horizon}_alpha")
    if alpha is None:
        return None
    alpha = float(alpha)
    return -alpha if (row.get("action") or "").upper() in _EXIT_ACTIONS else alpha


def _pct(wins: int, n: int) -> float:
    return wins / n * 100 if n else 0.0


def emo(wr: float) -> str:
    return "✅" if wr >= 55 else "⚠️"


def ticker_rankings(rows: list[dict[str, Any]], horizon: str = "t5", top: int = 5):
    """(best, worst) ticker lists as (ticker, avg_alpha, n), ranked by the
    alpha each call was actually aiming for (exits inverted)."""
    by_ticker: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        alpha = effective_alpha(r, horizon)
        if r.get("ticker") and alpha is not None:
            by_ticker[r["ticker"]].append(alpha)
    ranked = sorted(
        ((t, round(sum(a) / len(a), 2), len(a)) for t, a in by_ticker.items()),
        key=lambda x: x[1],
        reverse=True,
    )
    return ranked[:top], list(reversed(ranked[-top:]))


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _fetch(since: str) -> tuple[list[dict[str, Any]], int]:
    client = supabase_client.get_client()
    if client is None:
        return [], 0
    try:
        rows = (
            client.table("backtest_results")
            .select("*")
            .eq("project", PROJECT)
            .eq("excluded", False)
            .gte("run_date", since)
            .execute()
        ).data or []
        # Keep rows that resolved on at least one horizon.
        rows = [
            r for r in rows
            if r.get("t5_outcome") not in (None, "not_scored")
            or r.get("t15_outcome") not in (None, "not_scored")
        ]
    except Exception as exc:
        log.error("monthly row fetch failed: %s", exc)
        rows = []
    try:
        excluded_count = (
            client.table("backtest_results")
            .select("id", count="exact")
            .eq("project", PROJECT)
            .eq("excluded", True)
            .gte("run_date", since)
            .execute()
        ).count or 0
    except Exception as exc:
        log.warning("excluded count failed: %s", exc)
        excluded_count = 0
    return rows, excluded_count


def _ask_claude(metrics: dict[str, Any]) -> str:
    try:
        result = claude_client.call_claude(
            model=MODEL,
            system=SYSTEM,
            user=f"Metrics: {json.dumps(metrics, default=str)}",
            run_type="monthly_report",
            max_tokens=600,
        )
        return (result.get("text") or "").strip()
    except Exception as exc:
        log.warning("Claude monthly insight failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Delivery — split rather than truncate
# ---------------------------------------------------------------------------

def split_message(msg: str, limit: int = _SPLIT_AT) -> list[str]:
    """Split on line boundaries so no chunk exceeds `limit`."""
    if len(msg) <= limit:
        return [msg]
    chunks: list[str] = []
    current = ""
    for line in msg.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks


def _send(body: str) -> None:
    if config.PAPER_TRADING:
        body = telegram_bot.PAPER_TRADING_BANNER + "\n\n" + body
    parts = split_message(body)
    for i, part in enumerate(parts, 1):
        if len(parts) > 1:
            part = f"{part}\n\n(part {i}/{len(parts)})"
        if config.DRY_RUN:
            log.info("[DRY_RUN] monthly report part %d not sent", i)
        else:
            telegram_bot.send_alert(part, parse_mode=None)
        print(part)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_monthly_report() -> dict[str, Any]:
    today = date.today()
    since = today - timedelta(days=30)
    rows, excluded_count = _fetch(since.isoformat())

    if not rows:
        msg = (
            f"📊 Monthly Calibration — {today.strftime('%b %Y')}\n"
            "No resolved verdicts in the last 30 days.\n"
            f"Excluded rows: {excluded_count}"
        )
        _send(msg)
        return {"total": 0, "excluded": excluded_count, "message": msg}

    total_n = len(rows)
    t5_wins, t5_n = win_rate(rows, "t5")
    t15_wins, t15_n = win_rate(rows, "t15")
    t5_wr, t15_wr = _pct(t5_wins, t5_n), _pct(t15_wins, t15_n)
    t5_alpha, t15_alpha = avg_alpha(rows, "t5"), avg_alpha(rows, "t15")

    ind = [r for r in rows if r.get("market") == "IND"]
    us = [r for r in rows if r.get("market") == "US"]

    actions: dict[str, dict[str, Any]] = {}
    for action in _ACTIONS:
        subset = [r for r in rows if (r.get("action") or "").upper() == action]
        if not subset:
            continue
        w5, n5 = win_rate(subset, "t5")
        w15, n15 = win_rate(subset, "t15")
        actions[action] = {
            "t5_wr": round(_pct(w5, n5), 1), "t5_n": n5,
            "t15_wr": round(_pct(w15, n15), 1), "t15_n": n15,
            "t5_alpha": avg_alpha(subset, "t5"),
            "t15_alpha": avg_alpha(subset, "t15"),
        }

    high = [r for r in rows if (r.get("confidence_score") or 0) >= 8]
    mid = [r for r in rows if 6 <= (r.get("confidence_score") or 0) < 8]
    high_w, high_n = win_rate(high, "t5")
    mid_w, mid_n = win_rate(mid, "t5")

    executed = [r for r in rows if r.get("user_executed")]
    skipped = [r for r in rows if not r.get("user_executed")]
    exec_w, exec_n = win_rate(executed, "t5")
    skip_w, skip_n = win_rate(skipped, "t5")

    best_t5, worst_t5 = ticker_rankings(rows, "t5")
    best_t15, worst_t15 = ticker_rankings(rows, "t15")

    metrics: dict[str, Any] = {
        "window_days": 30,
        "sample_size": total_n,
        "t5": {"win_rate": round(t5_wr, 1), "n": t5_n, "avg_alpha": t5_alpha},
        "t15": {"win_rate": round(t15_wr, 1), "n": t15_n, "avg_alpha": t15_alpha},
        "by_action": actions,
        "confidence": {
            "high_8_10": {"t5_wr": round(_pct(high_w, high_n), 1), "n": high_n},
            "mid_6_7": {"t5_wr": round(_pct(mid_w, mid_n), 1), "n": mid_n},
        },
        "execution": {
            "followed": {"t5_wr": round(_pct(exec_w, exec_n), 1), "n": exec_n},
            "skipped": {"t5_wr": round(_pct(skip_w, skip_n), 1), "n": skip_n},
        },
        "best_tickers_t5": best_t5,
        "worst_tickers_t5": worst_t5,
        "excluded_bad_runs": excluded_count,
    }

    if ind:
        metrics["ind"] = {"n": len(ind), "t5_wr": round(_pct(*win_rate(ind, "t5")), 1),
                          "t5_alpha": avg_alpha(ind, "t5"), "t15_alpha": avg_alpha(ind, "t15")}
    if us:
        metrics["us"] = {"n": len(us), "t5_wr": round(_pct(*win_rate(us, "t5")), 1),
                         "t5_alpha": avg_alpha(us, "t5"), "t15_alpha": avg_alpha(us, "t15")}

    insight = _ask_claude(metrics)

    msg = (
        f"📊 Monthly Calibration — {today.strftime('%b %d, %Y')}\n"
        f"{_RULE}\n"
        f"📈 OVERALL (30d, excl {excluded_count} bad rows)\n"
        f"Calls: {total_n}\n"
        f"T+5:  {t5_wr:.0f}% win {emo(t5_wr)} ({t5_n}) | {t5_alpha:+.1f}% alpha\n"
        f"T+15: {t15_wr:.0f}% win {emo(t15_wr)} ({t15_n}) | {t15_alpha:+.1f}% alpha\n"
    )
    if t5_n and t15_n:
        drift = t15_wr - t5_wr
        verdict = "theses outlast entries" if drift >= 0 else "entries decay by T+15"
        msg += f"Drift: {drift:+.0f}pp — {verdict}\n"
    msg += f"Execution: {exec_n}/{total_n}\n\n"

    if ind:
        wr = _pct(*win_rate(ind, "t5"))
        msg += (f"🇮🇳 INDIAN: {wr:.0f}% win {emo(wr)} | T+5 {avg_alpha(ind,'t5'):+.1f}% "
                f"| T+15 {avg_alpha(ind,'t15'):+.1f}%\n")
    if us:
        wr = _pct(*win_rate(us, "t5"))
        msg += (f"🌐 US: {wr:.0f}% win {emo(wr)} | T+5 {avg_alpha(us,'t5'):+.1f}% "
                f"| T+15 {avg_alpha(us,'t15'):+.1f}%\n")

    if actions:
        msg += "\n🎯 BY ACTION (T+5 → T+15)\n"
        for action, s in actions.items():
            msg += (f"{action}: {s['t5_wr']:.0f}% {emo(s['t5_wr'])} → "
                    f"{s['t15_wr']:.0f}% {emo(s['t15_wr'])} | "
                    f"{s['t5_alpha']:+.1f}% → {s['t15_alpha']:+.1f}%\n")

    if high_n or mid_n:
        msg += "\n📊 CONFIDENCE (T+5)\n"
        if high_n:
            wr = _pct(high_w, high_n)
            msg += f"Conf 8-10: {wr:.0f}% {emo(wr)} ({high_n})\n"
        if mid_n:
            wr = _pct(mid_w, mid_n)
            msg += f"Conf 6-7:  {wr:.0f}% {emo(wr)} ({mid_n})\n"

    if best_t5:
        msg += "\n🏆 TOP 5 TICKERS (T+5 in-favour alpha)\n"
        for t, a, n in best_t5:
            msg += f"{t}: {a:+.1f}% ({n})\n"
    if worst_t5:
        msg += "\n💥 BOTTOM 5 TICKERS (T+5 in-favour alpha)\n"
        for t, a, n in worst_t5:
            msg += f"{t}: {a:+.1f}% ({n})\n"
    if best_t15:
        msg += "\n🔭 TOP 5 (T+15 in-favour alpha)\n"
        for t, a, n in best_t15:
            msg += f"{t}: {a:+.1f}% ({n})\n"

    if exec_n or skip_n:
        msg += "\n💼 YOUR EXECUTION (T+5)\n"
        if exec_n:
            msg += f"Followed: {_pct(exec_w, exec_n):.0f}% win ({exec_n} trades)\n"
        if skip_n:
            msg += f"Skipped:  {_pct(skip_w, skip_n):.0f}% win ({skip_n} calls)\n"
        if exec_n and skip_n:
            msg += f"Delta: {_pct(exec_w, exec_n) - _pct(skip_w, skip_n):+.0f}% following Claude\n"

    msg += f"\n❌ Excluded: {excluded_count} bad runs / bad prices\n"
    if insight:
        msg += f"\n{insight}\n"
    msg += _RULE

    _send(msg)
    return {"total": total_n, "excluded": excluded_count, "metrics": metrics, "message": msg}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run_monthly_report()
