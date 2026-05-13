"""Sunday 8 PM IST weekly backtest scorecard.

Pulls trailing 7 days of backtest_results, computes overall / market / action /
confidence metrics plus an execution-vs-skipped split, then asks Claude Haiku
for a 4-line insight (what worked, what to watch, one tuning suggestion). Posts
the whole thing to Telegram.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from analysis import claude_client
from delivery import telegram_bot
from storage import supabase_client

log = logging.getLogger(__name__)

MODEL = config.HAIKU_MODEL


SYSTEM = """You are reviewing the previous week's BUY/SELL/HOLD recommendations
for a retail investor. You will receive aggregated metrics as JSON.

Respond with EXACTLY four lines, each prefixed by an emoji marker and tag,
nothing else (no headers, no markdown, no preamble):

INSIGHT: <two-sentence observation about what is working>
WATCH: <one-sentence warning about the biggest weakness>
TUNE: <one-sentence concrete prompt or threshold change to try next week>

Total ≤ 90 words. Be specific — reference action types, confidence buckets, or
markets by name when the data supports it. If sample size is small, say so."""


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _win_rate(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    wins = sum(1 for r in rows if r.get("outcome") == "win")
    return wins / len(rows) * 100


def _avg_alpha(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(float(r.get("alpha_pct") or 0) for r in rows) / len(rows)


def _bucket_confidence(score: Any) -> str:
    """High-bucket vs low-bucket split per CNC swing model.

    8-10 = high conviction; 6-7 = moderate; <6 should not reach backtest_results
    because risk_guardrails skips them, but if it does we tag it explicitly.
    """
    try:
        s = int(score or 0)
    except (TypeError, ValueError):
        return "?"
    if s >= 8:
        return "8-10"
    if s >= 6:
        return "6-7"
    return "<6"


_ACTION_GROUPS = {
    "ADD/BUY":     {"ADD", "BUY", "BUY-MOMENTUM", "BUY-EVENT"},
    "HOLD":        {"HOLD", "TIGHTEN-SL"},
    "EXIT calls":  {"EXIT-FULL", "EXIT-PARTIAL", "PARTIAL-EXIT", "FULL-EXIT", "SELL"},
}


def _group_action(action: str | None) -> str:
    a = (action or "").upper()
    for group, members in _ACTION_GROUPS.items():
        if a in members:
            return group
    return "Other"


def compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"total": 0}

    ind_rows = [r for r in rows if r.get("market") == "IND"]
    us_rows = [r for r in rows if r.get("market") == "US"]
    executed = [r for r in rows if r.get("user_executed")]
    skipped = [r for r in rows if not r.get("user_executed")]

    # By action group (ADD/BUY vs HOLD vs EXIT calls)
    actions: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        actions.setdefault(_group_action(r.get("action")), []).append(r)
    action_stats = {
        a: {"n": len(rs), "win_rate": _win_rate(rs)}
        for a, rs in actions.items()
    }

    # By confidence bucket
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(_bucket_confidence(r.get("confidence_score")), []).append(r)
    conf_stats = {
        b: {"n": len(rs), "win_rate": _win_rate(rs)}
        for b, rs in buckets.items()
    }

    pnl_rows = [r for r in executed if r.get("actual_pnl_inr") is not None]
    weekly_pnl = sum(float(r.get("actual_pnl_inr") or 0) for r in pnl_rows)

    # Best / worst by alpha
    def _best_worst(rs: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not rs:
            return None, None
        best = max(rs, key=lambda r: float(r.get("alpha_pct") or -1e9))
        worst = min(rs, key=lambda r: float(r.get("alpha_pct") or 1e9))
        return best, worst

    best, worst = _best_worst(rows)
    ind_best, ind_worst = _best_worst(ind_rows)
    us_best, us_worst = _best_worst(us_rows)

    alpha_wins = sum(1 for r in rows if (float(r.get("alpha_pct") or 0)) > 0)

    return {
        "total": len(rows),
        "win_rate": _win_rate(rows),
        "alpha_win_rate": alpha_wins / len(rows) * 100,
        "avg_alpha": _avg_alpha(rows),
        "execution_rate": len(executed) / len(rows) * 100 if rows else 0,
        "ind": {
            "n": len(ind_rows), "win_rate": _win_rate(ind_rows),
            "avg_alpha": _avg_alpha(ind_rows),
            "best": ind_best, "worst": ind_worst,
        },
        "us": {
            "n": len(us_rows), "win_rate": _win_rate(us_rows),
            "avg_alpha": _avg_alpha(us_rows),
            "best": us_best, "worst": us_worst,
        },
        "actions": action_stats,
        "confidence": conf_stats,
        "execution": {
            "followed": {"n": len(executed), "win_rate": _win_rate(executed)},
            "skipped": {"n": len(skipped), "win_rate": _win_rate(skipped)},
            "pnl_inr": weekly_pnl,
        },
        "best": best,
        "worst": worst,
    }


# ---------------------------------------------------------------------------
# Claude insight
# ---------------------------------------------------------------------------

def _ask_claude(metrics: dict[str, Any]) -> dict[str, str]:
    """Returns dict with keys insight / watch / tune. Empty strings on failure."""
    result = claude_client.call_claude(
        model=MODEL,
        system=SYSTEM,
        user="## Weekly metrics\n" + json.dumps(metrics, default=str, indent=2),
        run_type="weekly_scorecard",
        max_tokens=400,
    )
    text = (result.get("text") or "").strip()
    out = {"insight": "", "watch": "", "tune": ""}
    # Claude tends to prefix each tag with an emoji + space, so anchor on the
    # tag itself rather than line start.
    for tag in ("INSIGHT", "WATCH", "TUNE"):
        m = re.search(rf"{tag}\s*:\s*(.+?)(?=\n\s*(?:\S+\s+)?(?:INSIGHT|WATCH|TUNE)\s*:|\Z)",
                      text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            out[tag.lower()] = " ".join(m.group(1).split())
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> dict[str, Any]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    rows = supabase_client.get_backtest_results_between(start.isoformat(), end.isoformat())
    metrics = compute_metrics(rows)
    metrics["start"] = start.isoformat()
    metrics["end"] = end.isoformat()

    insight = {"insight": "", "watch": "", "tune": ""}
    if rows:
        try:
            insight = _ask_claude(metrics)
        except Exception as exc:
            log.warning("Claude insight failed: %s", exc)

    body = telegram_bot.format_weekly_scorecard(metrics, insight)
    if config.PAPER_TRADING:
        body = telegram_bot.PAPER_TRADING_BANNER + "\n\n" + body
    telegram_bot.send_alert(body)
    return {"metrics": metrics, "insight": insight}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
