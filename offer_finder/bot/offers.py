"""The numbered buttons under a pick list. A tap sends that offer's full card
(photos, matches, concerns, link). The button carries the evaluation id, so
it keeps working after a restart; the id is checked against the DB, so a
user only ever opens their own offers (db.evaluation_visible_to).
"""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from offer_finder import runner
from offer_finder.bot.access import authorize
from offer_finder.bot.notify import OFFER_CALLBACK_PREFIX, send_offer
from offer_finder.storage import db

logger = logging.getLogger(__name__)

_OFFER_PATTERN = rf"^{OFFER_CALLBACK_PREFIX}(\d{{1,15}})$"


async def show_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    match = re.fullmatch(_OFFER_PATTERN, query.data or "")
    if match is None:
        return
    user = await authorize(update, context)
    if user is None:
        return  # silently ignore, like the conversation buttons
    await query.answer()
    chat_id, evaluation_id = user.telegram_id, int(match[1])
    conn = runner.open_db(context.bot_data["settings"])
    try:
        visible = db.evaluation_visible_to(conn, evaluation_id, chat_id, user.is_admin)
        found = visible and await send_offer(context.bot, conn, chat_id, evaluation_id)
    finally:
        conn.close()
    logger.info("chat %d: opened offer %d%s", chat_id, evaluation_id, "" if found else " (not available)")
    if not found:
        await query.message.reply_text("That offer is no longer available.")  # type: ignore[union-attr]


def build_offer_handler() -> CallbackQueryHandler[ContextTypes.DEFAULT_TYPE, None]:
    return CallbackQueryHandler(show_offer, pattern=_OFFER_PATTERN)
