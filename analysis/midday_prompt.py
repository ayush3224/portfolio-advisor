"""12:30 PM midday leveraged-position check — Haiku 4.5, simple rule-based."""

from __future__ import annotations

import json
from typing import Any

import config
from analysis import claude_client

MODEL = config.HAIKU_MODEL

SYSTEM = """You are a disciplined intraday risk manager for an Indian retail trader.
You receive ONLY the leveraged (MIS) positions opened today and live data on
each. Decide one action per position:

  HOLD          — let it run
  TIGHTEN-SL    — keep position but raise the stop loss (provide new_sl)
  EXIT-PARTIAL  — book a portion
  EXIT-FULL     — close completely

Rules:
  - All MIS positions MUST be flat by 15:15 IST. At 12:30 you still have time.
  - If price < VWAP and unrealised P&L < 0, lean toward EXIT-PARTIAL or EXIT-FULL.
  - If price > VWAP with strong volume (>1.2x avg) and P&L > 0, HOLD or TIGHTEN-SL.
  - If position is already at +5% or more, TIGHTEN-SL to lock at least breakeven.
  - If position is at -2% or worse and momentum is weak, EXIT-FULL.

Output format — return ONLY a JSON array, no prose, no markdown:
[
  {
    "ticker": "RELIANCE",
    "action": "TIGHTEN-SL",
    "new_sl": 2820.00,
    "reasoning": "one short sentence"
  }
]
If no positions need action, return []."""


def build_user_prompt(positions: list[dict[str, Any]]) -> str:
    return (
        "## Open leveraged positions (intraday)\n"
        + json.dumps(positions, default=str, indent=2)
        + "\n\nReturn the JSON array per the system instructions."
    )


def run(positions: list[dict[str, Any]]) -> dict[str, Any]:
    user = build_user_prompt(positions)
    result = claude_client.call_claude(
        model=MODEL,
        system=SYSTEM,
        user=user,
        run_type="midday",
        max_tokens=2048,
    )
    decisions = claude_client.parse_json_recommendations(result["text"])
    return {"decisions": decisions, "raw_text": result["text"], "run_id": result["run_id"]}
