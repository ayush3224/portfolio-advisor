"""Anthropic API wrapper with usage / cost logging.

Every call records input/output tokens and an estimated USD cost into the
run_log table via storage.supabase_client. Models are chosen by the caller
(SONNET_MODEL for premarket, HAIKU_MODEL for midday/eod) per CLAUDE.md.

Uses prompt caching on system prompts when they're large enough to benefit.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from anthropic import Anthropic

import config
from storage import supabase_client

log = logging.getLogger(__name__)


# USD per 1M tokens — keep in sync with public Anthropic pricing.
_PRICING = {
    config.SONNET_MODEL: {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    config.HAIKU_MODEL:  {"input": 1.00, "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
}


_client: Anthropic | None = None


def get_client() -> Anthropic | None:
    global _client
    if _client is not None:
        return _client
    if not config.ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY missing — Claude calls will fail")
        return None
    _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _estimate_cost(model: str, usage: Any) -> float:
    pricing = _PRICING.get(model, {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30})
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        in_tok * pricing["input"]
        + out_tok * pricing["output"]
        + cache_w * pricing["cache_write"]
        + cache_r * pricing["cache_read"]
    ) / 1_000_000


def call_claude(
    *,
    model: str,
    system: str,
    user: str,
    run_type: str,
    max_tokens: int = 4096,
    cache_system: bool = True,
) -> dict[str, Any]:
    """Call Claude and persist usage to run_log. Returns {text, raw, run_id}."""
    run_id = supabase_client.start_run(run_type)
    client = get_client()
    if client is None:
        supabase_client.finish_run(run_id, status="failed",
                                   error_message="Anthropic client unavailable")
        return {"text": "", "raw": None, "run_id": run_id}

    system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
    if cache_system and len(system) > 1024:
        system_blocks[0]["cache_control"] = {"type": "ephemeral"}

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        cost = _estimate_cost(model, resp.usage)
        supabase_client.finish_run(
            run_id,
            status="success",
            model_used=model,
            input_tokens=getattr(resp.usage, "input_tokens", 0),
            output_tokens=getattr(resp.usage, "output_tokens", 0),
            estimated_cost_usd=round(cost, 6),
        )
        return {"text": text, "raw": resp, "run_id": run_id}
    except Exception as exc:
        log.exception("Claude call failed: %s", exc)
        supabase_client.finish_run(run_id, status="failed", model_used=model,
                                   error_message=str(exc)[:500])
        return {"text": "", "raw": None, "run_id": run_id}


def parse_json_recommendations(text: str) -> list[dict[str, Any]]:
    """Extract a JSON array of recommendation objects from a model response.

    Models sometimes wrap the JSON in markdown fences or surrounding prose.
    We try strict json.loads first, then fall back to fenced extraction.
    """
    if not text:
        return []
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else parsed.get("recommendations", [])
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        try:
            parsed = json.loads(fence.group(1))
            return parsed if isinstance(parsed, list) else parsed.get("recommendations", [])
        except json.JSONDecodeError:
            pass
    bracket = re.search(r"\[\s*\{.*\}\s*\]", text, flags=re.DOTALL)
    if bracket:
        try:
            return json.loads(bracket.group(0))
        except json.JSONDecodeError:
            return []
    return []
