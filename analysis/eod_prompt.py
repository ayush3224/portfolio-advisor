"""3:00 PM EOD exit/hold decisions — Haiku 4.5, simple rule-based.

By 3 PM all MIS positions must be exiting. For CNC (delivery) holdings the
question is overnight: hold or exit before close.
"""

from __future__ import annotations

import json
from typing import Any

import config
from analysis import claude_client

MODEL = config.HAIKU_MODEL

SYSTEM = """You are a disciplined end-of-day risk manager for an Indian retail trader.
You receive the user's open positions and delivery holdings 15 minutes before
market close. Decide one action per position:

  EXIT-FULL     — close completely before market close
  EXIT-PARTIAL  — book a portion, hold the rest overnight
  HOLD          — keep overnight (only for CNC / delivery, never for MIS)

Rules:
  - All MIS positions MUST be flat by 15:15. For any MIS row, action MUST be
    EXIT-FULL or EXIT-PARTIAL — never HOLD.
  - For delivery positions, HOLD overnight only if the technical and news
    setup remains intact and the position is not at risk of an event-driven gap.
  - Day before a known major event (RBI / budget / earnings): prefer EXIT-PARTIAL.

Output format — return ONLY a JSON array, no prose, no markdown:
[
  {
    "ticker": "RELIANCE",
    "action": "HOLD",
    "hold_overnight": true,
    "reasoning": "one short sentence"
  }
]
"""


def build_user_prompt(positions: list[dict[str, Any]]) -> str:
    return (
        "## Open positions and holdings at 3:00 PM\n"
        + json.dumps(positions, default=str, indent=2)
        + "\n\nReturn the JSON array per the system instructions."
    )


def run(positions: list[dict[str, Any]]) -> dict[str, Any]:
    user = build_user_prompt(positions)
    result = claude_client.call_claude(
        model=MODEL,
        system=SYSTEM,
        user=user,
        run_type="eod",
        max_tokens=2048,
    )
    decisions = claude_client.parse_json_recommendations(result["text"])
    return {"decisions": decisions, "raw_text": result["text"], "run_id": result["run_id"]}
