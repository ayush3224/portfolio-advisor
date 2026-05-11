"""Telethon-based Telegram channel scraper.

Reads recent messages from each configured analyst channel, weights
mentions by channel weight, and returns a per-ticker bullish/bearish
signal score for the tickers the user actually holds.

Uses a synchronous wrapper around the asyncio Telethon client so it can be
called from non-async code in scheduler modules.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import config

log = logging.getLogger(__name__)

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except Exception:  # pragma: no cover
    TelegramClient = None  # type: ignore[assignment]
    StringSession = None  # type: ignore[assignment]


_BULLISH_WORDS = {"buy", "long", "bullish", "target", "breakout", "upside", "accumulate", "add"}
_BEARISH_WORDS = {"sell", "short", "bearish", "exit", "stop loss", "sl hit", "downside", "book", "profit booking"}


def _classify(text: str) -> str:
    low = text.lower()
    bull = sum(1 for w in _BULLISH_WORDS if w in low)
    bear = sum(1 for w in _BEARISH_WORDS if w in low)
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return "neutral"


async def _fetch_channel(client: Any, handle: str, since: datetime, limit: int) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    try:
        async for m in client.iter_messages(handle, limit=limit):
            if m.date and m.date < since:
                break
            if not m.message:
                continue
            msgs.append({"channel": handle, "date": m.date, "text": m.message})
    except Exception as exc:
        log.warning("Telethon read %s failed: %s", handle, exc)
    return msgs


def _build_client() -> Any | None:
    """Construct a TelegramClient using string session if available, else file session.

    Preference order (per CLAUDE.md ops requirement — VPS cron must be headless):
      1. TELETHON_SESSION_STRING env var → StringSession
      2. TELETHON_SESSION_FILE path     → file-based session (default reuses
         /root/stocksage/stocksage_session)
    """
    if TelegramClient is None or not all([config.TELETHON_API_ID, config.TELETHON_API_HASH]):
        log.warning("Telethon not configured — skipping channel scrape")
        return None
    api_id = int(config.TELETHON_API_ID)
    api_hash = config.TELETHON_API_HASH

    if config.TELETHON_SESSION_STRING:
        log.info("Telethon: using TELETHON_SESSION_STRING")
        return TelegramClient(StringSession(config.TELETHON_SESSION_STRING), api_id, api_hash)

    log.info("Telethon: falling back to file session %s", config.TELETHON_SESSION_FILE)
    return TelegramClient(config.TELETHON_SESSION_FILE, api_id, api_hash)


async def _gather_messages(channels: dict[str, int], since: datetime, limit: int) -> list[dict[str, Any]]:
    client = _build_client()
    if client is None:
        return []
    # `start` will only prompt for phone code if the session is missing/invalid.
    # On VPS this should never trigger because we always load a pre-built session.
    await client.start(phone=config.TELETHON_PHONE)
    try:
        all_msgs: list[dict[str, Any]] = []
        for handle in channels:
            all_msgs.extend(await _fetch_channel(client, handle, since, limit))
        return all_msgs
    finally:
        await client.disconnect()


async def _join_channel(client: Any, handle: str) -> bool:
    """Resolve + join a channel/supergroup by username. Returns True on success."""
    from telethon.tl.functions.channels import JoinChannelRequest
    try:
        entity = await client.get_entity(handle)
        try:
            await client(JoinChannelRequest(entity))
            log.info("Joined channel %s (%s)", handle, getattr(entity, "title", "?"))
        except Exception as join_exc:
            # Already a member, or it's a public broadcast channel that doesn't
            # need explicit joining — still accessible for iter_messages.
            log.info("Channel %s reachable, join skipped: %s", handle, join_exc)
        return True
    except Exception as exc:
        log.warning("Channel %s unreachable: %s", handle, exc)
        return False


def join_channels(handles: list[str]) -> dict[str, bool]:
    """Sync wrapper to ensure a set of channels is joined / reachable."""
    async def _go() -> dict[str, bool]:
        client = _build_client()
        if client is None:
            return {h: False for h in handles}
        await client.start(phone=config.TELETHON_PHONE)
        try:
            return {h: await _join_channel(client, h) for h in handles}
        finally:
            await client.disconnect()
    try:
        return asyncio.run(_go())
    except Exception as exc:
        log.exception("join_channels failed: %s", exc)
        return {h: False for h in handles}


def scrape_all_channels(
    *,
    hours_back: int = 168,
    per_channel_limit: int = 30,
) -> list[dict[str, Any]]:
    """Return raw posts from all configured channels (no per-ticker filtering)."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    try:
        return asyncio.run(_gather_messages(config.TELEGRAM_CHANNELS, since, per_channel_limit))
    except Exception as exc:
        log.exception("scrape_all_channels failed: %s", exc)
        return []


def signals_for_holdings(
    tickers: list[str],
    *,
    hours_back: int = 168,
    per_channel_limit: int = 30,
) -> dict[str, dict[str, Any]]:
    """Return {ticker: {bullish, bearish, score, mentions: [...]}}.

    `score` = weighted (bullish - bearish) where weight is the channel weight.
    """
    if not tickers:
        return {}
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    try:
        msgs = asyncio.run(_gather_messages(config.TELEGRAM_CHANNELS, since, per_channel_limit))
    except Exception as exc:
        log.exception("Telethon gather failed: %s", exc)
        return {t: {"bullish": 0, "bearish": 0, "score": 0, "mentions": []} for t in tickers}

    out: dict[str, dict[str, Any]] = {
        t: {"bullish": 0, "bearish": 0, "score": 0.0, "mentions": []} for t in tickers
    }
    for m in msgs:
        weight = config.TELEGRAM_CHANNELS.get(m["channel"], 1)
        for t in tickers:
            try:
                if not re.search(rf"\b{re.escape(t)}\b", m["text"], flags=re.IGNORECASE):
                    continue
                cls = _classify(m["text"])
                bucket = out[t]
                if cls == "bullish":
                    bucket["bullish"] += weight
                    bucket["score"] += weight
                elif cls == "bearish":
                    bucket["bearish"] += weight
                    bucket["score"] -= weight
                bucket["mentions"].append(
                    {"channel": m["channel"], "weight": weight, "class": cls,
                     "snippet": m["text"][:240]}
                )
            except Exception as exc:
                log.warning("classify failed for %s: %s", t, exc)
    return out
