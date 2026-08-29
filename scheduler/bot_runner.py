"""Long-running Telegram bot — polling mode under systemd.

Only messages from TELEGRAM_CHAT_ID are processed; everything else is
silently ignored (the private bot is single-user by design).

The cron-driven scheduler (premarket/midday/eod/outcome_logger) is unaffected
— it uses delivery.telegram_bot to *send* alerts on its own schedule. This
process only *receives* messages and replies via the same bot token.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from bot import command_handler

log = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    if chat_id != str(config.TELEGRAM_CHAT_ID):
        log.debug("Ignoring message from unauthorised chat_id=%s", chat_id)
        return
    text = update.message.text.strip()
    log.info("CMD from %s: %s", chat_id, text[:120])
    try:
        replies = await command_handler.process(text)
    except Exception as exc:
        log.exception("command_handler crashed: %s", exc)
        replies = ["❌ Internal error. Check bot logs."]
    # A long PORTFOLIO comes back as two messages; everything else is one.
    for reply in replies:
        await update.message.reply_text(reply, parse_mode="HTML")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing — cannot start bot")
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Portfolio bot starting in polling mode")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
