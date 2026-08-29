"""7:30 PM IST US-stock advisory — Haiku 4.5, long-term-bias prompt.

The prompt is deliberately terse: Haiku follows short, rule-shaped
instructions more reliably than prose, and the US book is a HOLD-by-default
long-term portfolio that does not need Sonnet-grade reasoning."""

from __future__ import annotations

import json
from typing import Any

import config
from analysis import claude_client

MODEL = config.HAIKU_MODEL  # tight rule-following task — Sonnet is not needed here

SYSTEM = """You advise an Indian investor holding US stocks via IndMoney. Positions are
long-term: default to HOLD unless there is a clear reason to act. Benchmark
relative strength against the S&P 500.

ACTIONS: HOLD | PARTIAL-EXIT (sell 25-50%) | FULL-EXIT | ADD (rare, high-conviction dip) | WATCH

POSITION RULES
- Values are USD and INR; USD/INR moves matter even when USD is flat.
- Winner up >40%: consider PARTIAL-EXIT.
- Flag earnings within 2 weeks only if the data given shows it.

TECHNICALS (per holding, in the TECHNICALS block)
- RSI >70: no ADD. <30: ADD only if EMA crossover or volume spike. 55-70: supports ADD/HOLD. 30-45: lean EXIT/WATCH.
- EMA20 >EMA50 supports ADD/HOLD; below, lean EXIT/WATCH. Fresh crossover (3d): strong, +1 confidence.
- Volume >1.5x avg: +1 confidence. <0.8x: -1 confidence.
- BB >0.85: no ADD, consider PARTIAL-EXIT. <0.15: bounce candidate if other signals confirm. Squeeze: breakout imminent — say so.
- MACD fresh crossover: strong signal in its direction, +1 confidence. Direction plus increasing momentum confirms it.
- S&R: near support + bullish signals = best ADD. Near resistance + RSI >70 = PARTIAL-EXIT. At resistance: never ADD.
- VWAP here is computed on daily candles, not intraday — read it as trend, not an intraday level. Above VWAP = bullish structure.
- Alignment: 5-6/6 highest conviction, 4 strong, 3 hold, 2 avoid new entry, 0-1 exit signal.
- Technicals confirm or contradict fundamentals. Bullish fundamentals + alignment <=1/6: downgrade to HOLD/WATCH. Bearish fundamentals + alignment >=5/6: still lean EXIT, note reversal risk.

PREDICTION MARKETS (Polymarket): anchor rate-sensitive sectors, flag recession
risk on cyclicals, sanity-check targets. Weight alongside, never above, fundamentals.

OUTPUT: a JSON array only — no preamble, no markdown fence. One entry per
holding given, no omissions. Keep "reasoning" to at most 2 lines.
[{"ticker":"NVDA","action":"PARTIAL-EXIT","confidence_score":8,"exit_pct":30,"reasoning":"one or two lines","risk_flag":"earnings_2w | macro | valuation | none"}]
confidence_score is an integer 1-10. Set exit_pct only for PARTIAL-EXIT, else null."""


def build_user_prompt(
    holdings: list[dict[str, Any]],
    macro: dict[str, Any],
    polymarket_text: str = "",
) -> str:
    parts = [
        "## US market context",
        json.dumps(macro, default=str, indent=2),
    ]
    if polymarket_text:
        parts += ["", "## " + polymarket_text]
    parts += [
        "",
        "## US Holdings (decide an action for each)",
        json.dumps(holdings, default=str, indent=2),
        "",
        "Return your recommendations as a JSON array per the system instructions.",
    ]
    return "\n".join(parts)


def run(
    holdings: list[dict[str, Any]],
    macro: dict[str, Any],
    polymarket_text: str = "",
) -> dict[str, Any]:
    user = build_user_prompt(holdings, macro, polymarket_text)
    result = claude_client.call_claude(
        model=MODEL,
        system=SYSTEM,
        user=user,
        run_type="us_advisory",
        max_tokens=4096,
    )
    recs = claude_client.parse_json_recommendations(result["text"])
    return {"recommendations": recs, "raw_text": result["text"], "run_id": result["run_id"]}
