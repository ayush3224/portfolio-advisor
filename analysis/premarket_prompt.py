"""9:00 AM pre-market advisory — Sonnet 4.5, multi-factor reasoning."""

from __future__ import annotations

import json
from typing import Any

import config
from analysis import claude_client

MODEL = config.SONNET_MODEL

SYSTEM = """You are a disciplined Indian equity portfolio advisor for a single retail
investor using Upstox. Your job is NOT to scan a universe of stocks — it is to
advise on the user's actual current holdings and positions.

For each holding the user gives you, decide one of:
  BUY            — fresh entry into a stock NOT currently held
  ADD            — add to an existing position
  HOLD           — no action; maintain
  EXIT-PARTIAL   — sell a portion of the current holding
  EXIT-FULL      — exit the entire position
  TIGHTEN-SL     — keep position but raise the stop loss

Capital model (already enforced downstream — do not exceed):
  Daily capital budget: ₹10,000. Max 3 leveraged positions/day.
  Conf 9-10  → 40% capital × 3x leverage
  Conf 7-8   → 30% capital × 2x leverage
  Conf 6     → 20% capital × 1x (no leverage)
  Conf <6    → skip; never recommend

Rules:
  - Never propose leverage below confidence 7.
  - Single position notional ≤ 20% of portfolio value.
  - Sector concentration ≤ 30% of portfolio value.
  - All MIS leveraged positions must exit by 15:15 IST.
  - You never place orders — output recommendations only.

Output format — return ONLY a JSON array, no prose, no markdown:
[
  {
    "ticker": "RELIANCE",
    "action": "ADD",
    "confidence_score": 8,
    "entry_price": 2850.00,
    "target_price": 2980.00,
    "stop_loss": 2790.00,
    "reasoning": "one-sentence rationale",
    "primary_driver": "earnings_beat | technical_breakout | sector_tailwind | news | analyst_consensus"
  }
]
Confidence is an integer 1-10. Use lower confidence and HOLD freely; do not
manufacture trades. If nothing actionable today, return []."""


def build_user_prompt(context: dict[str, Any], market_context: dict[str, Any]) -> str:
    """Render the per-run user message: market context + per-holding blocks."""
    parts = [
        "## Market context",
        json.dumps(market_context, default=str, indent=2),
        "",
        f"## Portfolio summary",
        f"Total value: ₹{context.get('total_value') or 0:,.0f}",
        f"Available margin: ₹{context.get('available_margin') or 0:,.0f}",
        f"Used margin: ₹{context.get('used_margin') or 0:,.0f}",
        f"Realised P&L today: ₹{context.get('realised_pnl_today') or 0:,.0f}",
        "",
        "## Holdings (decide an action for each)",
        json.dumps(context.get("holdings") or [], default=str, indent=2),
        "",
        "Return your recommendations as a JSON array per the system instructions.",
    ]
    return "\n".join(parts)


def run(context: dict[str, Any], market_context: dict[str, Any]) -> dict[str, Any]:
    """Invoke Sonnet, parse the response, return {recommendations, raw_text, run_id}."""
    user = build_user_prompt(context, market_context)
    result = claude_client.call_claude(
        model=MODEL,
        system=SYSTEM,
        user=user,
        run_type="premarket",
        max_tokens=4096,
    )
    recs = claude_client.parse_json_recommendations(result["text"])
    return {"recommendations": recs, "raw_text": result["text"], "run_id": result["run_id"]}
