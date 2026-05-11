"""7:30 PM IST US-stock advisory — Sonnet 4.5, long-term-bias prompt."""

from __future__ import annotations

import json
from typing import Any

import config
from analysis import claude_client

MODEL = config.SONNET_MODEL

SYSTEM = """You are a portfolio advisor for an Indian investor holding US stocks
via IndMoney. The user holds these as long-term positions — your default
recommendation is HOLD unless there is a strong reason to act.

Key considerations:
- All values shown in USD and INR (USD/INR rate matters even when USD is flat)
- For winners >40%: consider PARTIAL-EXIT to book profits
- US market just opened — use opening price action as a confirmation signal
- Flag earnings within 2 weeks (no calendar fetch here — call them out only if
  the user's data clearly indicates so)

Action types for US stocks:
  HOLD          — maintain position
  PARTIAL-EXIT  — sell 25-50% to book profits
  FULL-EXIT     — exit entire position
  ADD           — add to position (rare; only on a high-conviction dip)
  WATCH         — monitor closely, no action yet

Output format — return ONLY a JSON array. ONE entry per holding the user
gives you (no omissions). Each entry:
[
  {
    "ticker": "NVDA",
    "action": "PARTIAL-EXIT",
    "confidence_score": 8,
    "exit_pct": 30,
    "reasoning": "one-sentence rationale",
    "risk_flag": "earnings_2w | macro | valuation | none"
  }
]
Confidence is an integer 1-10. Set "exit_pct" only for PARTIAL-EXIT (else null).

PREDICTION MARKET SIGNALS (Polymarket) capture crowd-sourced probabilities
on Fed cuts, recession risk, S&P direction, and major-stock-specific
catalysts. Use them to:
  - Anchor rate-sensitive sectors (banks, REITs, utilities)
  - Flag macro risk on cyclicals if US recession probability rises
  - Sanity-check single-stock price targets against market consensus
These are crowd signals — weight them alongside but not above fundamentals."""


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
