"""Who may use the bot. Every handler starts with authorize(): private chats
only, and the user's row is read from SQLite on each update, so approving or
blocking someone takes effect at once and forged button data gets nowhere.
Telegram handler filters are only a first pass.

A stranger who writes to the bot is stored as pending, and the admins
(TELEGRAM_ALLOWLIST) get one message with Approve/Block buttons.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import BaseHandler, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from offer_finder import runner
from offer_finder.config import Settings
from offer_finder.core.models import User
from offer_finder.storage import db

logger = logging.getLogger(__name__)

USER_BUTTON_PATTERN = r"^user:(approve|block):(\d{1,15})$"

GREETING = (
    "Tell me what you're looking for on Kleinanzeigen, for example:\n"
    '"a good 65+ inch OLED TV around Mainz under €700"\n\n'
    "/settings shows your home postcode and search radius."
)
_ASKED = "Hi! This bot is invite-only. I've asked the owner to let you in; you'll get a message once you're approved."
_WAITING = "You're still waiting for the owner to approve you."


async def authorize(update: Update, context: ContextTypes.DEFAULT_TYPE, *, admin: bool = False) -> User | None:
    """The approved user behind this update, or None (and the caller does
    nothing). A stranger's first message turns into an access request."""
    tg_user, chat = update.effective_user, update.effective_chat
    if tg_user is None or chat is None or chat.type != "private" or chat.id != tg_user.id:
        return None
    settings: Settings = context.bot_data["settings"]
    conn = runner.open_db(settings)
    try:
        user = db.get_user(conn, tg_user.id)
        if user is None and update.message is not None:
            user, created = db.request_access(conn, tg_user.id, tg_user.full_name or str(tg_user.id))
            if created:
                await _ask_admins(context, settings, user, tg_user.username)
                await update.message.reply_text(_ASKED)
                return None
    finally:
        conn.close()
    if user is None or user.status == "blocked":
        return None
    if user.status == "pending":
        if update.message is not None:
            await update.message.reply_text(_WAITING)
        return None
    if admin and not user.is_admin:
        logger.warning("chat %d: admin action refused", user.telegram_id)
        return None
    return user


async def _ask_admins(context: Any, settings: Settings, user: User, username: str | None) -> None:
    logger.info("chat %d: access requested by %s", user.telegram_id, user.name)
    handle = f" (@{username})" if username else ""
    text = f"{user.name}{handle}, ID {user.telegram_id}, asks to use the bot."
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"user:approve:{user.telegram_id}"),
                InlineKeyboardButton("Block", callback_data=f"user:block:{user.telegram_id}"),
            ]
        ]
    )
    for admin_id in settings.telegram_allowlist:
        try:
            await context.bot.send_message(admin_id, text, reply_markup=keyboard)
        except TelegramError as exc:
            logger.warning("chat %d: couldn't ask admin %d about access: %s", user.telegram_id, admin_id, exc)


async def _decide(context: Any, admin: User, target_id: int, approve: bool) -> str:
    """Approve or block a user. Returns the text for the admin."""
    settings: Settings = context.bot_data["settings"]
    conn = runner.open_db(settings)
    try:
        target = db.get_user(conn, target_id)
        if target is None:
            return f"Unknown user {target_id}."
        if target.is_admin:
            return "Admins are managed in TELEGRAM_ALLOWLIST."
        changed = db.set_user_status(conn, target_id, "approved" if approve else "blocked")
    finally:
        conn.close()
    verb = "approved" if approve else "blocked"
    if changed:
        logger.info("chat %d: %s by admin %d", target_id, verb, admin.telegram_id)
    if changed and approve:
        try:
            await context.bot.send_message(target_id, f"You're in!\n\n{GREETING}")
        except TelegramError as exc:
            logger.warning("chat %d: couldn't send the welcome message: %s", target_id, exc)
    return f"{target.name} (ID {target_id}): {verb}."


async def on_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    match = re.fullmatch(USER_BUTTON_PATTERN, query.data or "")
    if match is None:
        return
    admin = await authorize(update, context, admin=True)
    if admin is None:
        return
    await query.answer()
    await query.edit_message_text(await _decide(context, admin, int(match[2]), approve=match[1] == "approve"))


async def on_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await authorize(update, context, admin=True) is None or update.message is None:
        return
    conn = runner.open_db(context.bot_data["settings"])
    try:
        users = db.list_users(conn)
    finally:
        conn.close()
    lines = [
        f"{user.name} · {user.telegram_id} · {'admin' if user.is_admin else user.status}" for user in users
    ]
    await update.message.reply_text("\n".join(lines) or "No users yet.")


async def on_block_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _decide_command(update, context, approve=False)


async def on_approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _decide_command(update, context, approve=True)


async def _decide_command(update: Update, context: ContextTypes.DEFAULT_TYPE, approve: bool) -> None:
    admin = await authorize(update, context, admin=True)
    if admin is None or update.message is None:
        return
    args = context.args or []
    if len(args) != 1 or not re.fullmatch(r"\d{1,15}", args[0]):
        await update.message.reply_text(f"Usage: /{'approve' if approve else 'block'} <user ID> (see /users)")
        return
    await update.message.reply_text(await _decide(context, admin, int(args[0]), approve))


def build_access_handlers() -> list[BaseHandler[Any, Any, Any]]:
    private = filters.ChatType.PRIVATE
    return [
        CallbackQueryHandler(on_user_button, pattern=USER_BUTTON_PATTERN),
        CommandHandler("users", on_users_command, filters=private),
        CommandHandler("approve", on_approve_command, filters=private),
        CommandHandler("block", on_block_command, filters=private),
    ]
