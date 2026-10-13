"""The numbered buttons under a pick list. A tap sends that offer's full card
(photos, matches, concerns, link). The button carries the evaluation id, so
it keeps working after a restart.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from offer_finder import runner
from offer_finder.bot.notify import OFFER_CALLBACK_PREFIX, send_offer
from offer_finder.config import Settings

logger = logging.getLogger(__name__)


async def show_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None and update.effective_chat is not None
    settings: Settings = context.bot_data["settings"]
    if query.from_user.id not in settings.telegram_allowlist:
        return  # silently ignore, like the conversation buttons
    await query.answer()
    chat_id = update.effective_chat.id
    evaluation_id = int(query.data.removeprefix(OFFER_CALLBACK_PREFIX))
    conn = runner.open_db(settings)
    try:
        found = await send_offer(context.bot, conn, chat_id, evaluation_id)
    finally:
        conn.close()
    logger.info("chat %d: opened offer %d%s", chat_id, evaluation_id, "" if found else " (gone)")
    if not found:
        await query.message.reply_text("That offer is no longer in the database.")  # type: ignore[union-attr]


def build_offer_handler() -> CallbackQueryHandler[ContextTypes.DEFAULT_TYPE, None]:
    return CallbackQueryHandler(show_offer, pattern=rf"^{OFFER_CALLBACK_PREFIX}\d+$")
