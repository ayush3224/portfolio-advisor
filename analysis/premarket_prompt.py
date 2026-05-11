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
  Conf <6    → skipped downstream — still INCLUDE the ticker in output with "skipped": true

Rules:
  - Never propose leverage below confidence 7.
  - Single position notional ≤ 20% of portfolio value.
  - Sector concentration ≤ 30% of portfolio value.
  - All MIS leveraged positions must exit by 15:15 IST.
  - You never place orders — output recommendations only.

Output format — return ONLY a JSON array, no prose, no markdown.
Return ONE entry for EVERY holding the user gave you (no omissions). Mark
sub-threshold entries with "skipped": true so the user can see what you
considered but discarded:
[
  {
    "ticker": "RELIANCE",
    "action": "ADD",
    "confidence_score": 8,
    "entry_price": 2850.00,
    "target_price": 2980.00,
    "stop_loss": 2790.00,
    "reasoning": "one-sentence rationale",
    "primary_driver": "earnings_beat | technical_breakout | sector_tailwind | news | analyst_consensus",
    "skipped": false
  },
  {
    "ticker": "INFY",
    "action": "HOLD",
    "confidence_score": 4,
    "reasoning": "no clear edge today; flat tape, no catalyst",
    "skipped": true
  }
]
Confidence is an integer 1-10. Use lower confidence and HOLD freely; do not
manufacture trades. If a ticker has no edge, give it a low confidence and
"skipped": true — never omit it from the array.

PREDICTION MARKET SIGNALS show crowd-sourced probability of macro events
(RBI rate decisions, oil price ranges, geopolitical risk). Use these to:
  - Adjust conviction on rate-sensitive stocks (banks, NBFCs) based on RBI
    cut probability
  - Flag geopolitical risk on relevant sectors
  - Incorporate oil price probabilities for energy stocks and import-heavy
    sectors (oil PSUs, aviation, paints)
These are market consensus signals — weight them alongside but not above
technical signals."""


def build_user_prompt(context: dict[str, Any], market_context: dict[str, Any]) -> str:
    """Render the per-run user message: market context + per-holding blocks."""
    # Separate the Polymarket block from the JSON dump so the model sees it as
    # a clean text section, not buried inside a long JSON blob.
    poly_text = market_context.pop("polymarket_text", "") if isinstance(market_context, dict) else ""
    market_context.pop("polymarket", None)

    parts = [
        "## Market context",
        json.dumps(market_context, default=str, indent=2),
    ]
    if poly_text:
        parts += ["", "## " + poly_text]
    parts += [
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
