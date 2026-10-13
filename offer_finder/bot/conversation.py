"""Telegram dialogue: collect a wish -> propose a SearchSpec -> the user
confirms, changes or cancels. State lives in memory (chat_data); a restart
just means starting over. Every handler starts with access.authorize().

bot_data must provide "settings", "llm" (an LLMProvider) and
"start_search" (async (chat_id, spec, run_id) -> None that returns immediately).
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from typing import Any

import httpx
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from offer_finder import runner
from offer_finder.bot.access import GREETING, authorize
from offer_finder.bot.preferences import home_plz, radius_km
from offer_finder.config import Settings
from offer_finder.core.models import SearchSpec, User
from offer_finder.core.search_spec import SpecError, describe, log_line, next_turn
from offer_finder.storage import db

logger = logging.getLogger(__name__)

COLLECTING, CONFIRMING = range(2)

CONFIRM_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("Search", callback_data="search"),
            InlineKeyboardButton("Change", callback_data="change"),
            InlineKeyboardButton("Cancel", callback_data="cancel"),
        ]
    ]
)

_CONFIRM_BUTTONS = "^(search|change|cancel)$"  # the pick list's offer buttons are handled in offers.py

async def _reply(query: CallbackQuery, text: str) -> None:
    await query.message.reply_text(text)  # type: ignore[union-attr]


def _proposal(spec: SearchSpec) -> str:
    return f"Proposed search:\n{describe(spec)}"


def _spec_failure_text(exc: SpecError) -> str:
    """Only ask to rephrase when the message was the problem, not the AI service."""
    cause = exc.__cause__
    if isinstance(cause, httpx.HTTPStatusError):
        return (
            f"The AI service returned an error (HTTP {cause.response.status_code}), so I couldn't "
            "build the search. The details are in the bot's terminal."
        )
    if isinstance(cause, httpx.TransportError):
        return "I couldn't reach the AI service. Please try again in a moment."
    return "Sorry, I couldn't turn that into a search. Could you rephrase?"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await authorize(update, context) is None:
        return ConversationHandler.END
    context.chat_data.clear()  # type: ignore[union-attr]
    if update.message:
        await update.message.reply_text(GREETING)
    return COLLECTING


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    user = await authorize(update, context)
    if user is None:
        return ConversationHandler.END
    chat_data: dict[str, Any] = context.chat_data  # type: ignore[assignment]
    history = chat_data.setdefault("history", [])
    if "spec" in chat_data:  # typed instead of pressing a button: treat it as a change request
        history.append(("assistant", _proposal(chat_data.pop("spec"))))
    text = update.message.text or ""
    history.append(("user", text))
    chat_id = update.effective_chat.id if update.effective_chat else 0
    logger.info('chat %d: message "%s"', chat_id, text[:100])

    settings: Settings = context.bot_data["settings"]
    try:
        turn = await asyncio.to_thread(
            next_turn,
            list(history),
            context.bot_data["llm"],
            home_plz(user, settings),
            default_radius_km=radius_km(user, settings),
        )
    except SpecError as exc:
        history.pop()
        logger.warning("chat %d: %s", chat_id, exc)
        await update.message.reply_text(_spec_failure_text(exc))
        return COLLECTING

    if turn.question:
        history.append(("assistant", turn.question))
        logger.info('chat %d: asked "%s"', chat_id, turn.question)
        await update.message.reply_text(turn.question)
        return COLLECTING

    assert turn.spec is not None
    logger.info("chat %d: proposed search %s", chat_id, log_line(turn.spec))
    chat_data["spec"] = turn.spec
    await update.message.reply_text(
        f"{describe(turn.spec)}\n\nSearch Kleinanzeigen for this?", reply_markup=CONFIRM_KEYBOARD
    )
    return CONFIRMING


def _reserve(settings: Settings, user: User) -> int | str:
    """A run id, or why this search can't start now."""
    limit = None if user.is_admin else settings.user_daily_searches
    conn = runner.open_db(settings)
    try:
        reserved = db.reserve_search_run(conn, user.telegram_id, limit)
    finally:
        conn.close()
    if reserved == "busy":
        return "Your previous search is already running. Press Search again once it has finished."
    if reserved == "limit":
        return f"You've used your {limit} searches for the last 24 hours. Please try again later."
    return reserved


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and query.message is not None
    user = await authorize(update, context)
    if user is None:
        return ConversationHandler.END  # silently ignore — no allowlist confirmation leak

    chat_data: dict[str, Any] = context.chat_data  # type: ignore[assignment]
    spec: SearchSpec | None = chat_data.pop("spec", None)  # taken first: a second tap finds nothing
    chat_id = user.telegram_id
    await query.answer()
    if spec is None:
        logger.info("chat %d: button pressed for an expired search", chat_id)
        await _reply(query, "That search has expired. Tell me again what you're looking for.")
        return ConversationHandler.END

    if query.data == "search":
        reserved = _reserve(context.bot_data["settings"], user)
        if isinstance(reserved, str):
            chat_data["spec"] = spec  # keep the proposal and its buttons for later
            logger.info("chat %d: search refused: %s", chat_id, reserved)
            await _reply(query, reserved)
            return CONFIRMING
        await query.edit_message_reply_markup(reply_markup=None)
        chat_data.clear()
        logger.info("chat %d: search confirmed", chat_id)
        await context.bot_data["start_search"](chat_id, spec, reserved)  # it posts a live status message
        return ConversationHandler.END

    await query.edit_message_reply_markup(reply_markup=None)
    if query.data == "change":
        chat_data.setdefault("history", []).append(("assistant", _proposal(spec)))
        logger.info("chat %d: change requested", chat_id)
        await _reply(query, "What should I change?")
        return COLLECTING

    chat_data.clear()
    logger.info("chat %d: cancelled", chat_id)
    await _reply(query, "Cancelled.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await authorize(update, context) is None:
        return ConversationHandler.END
    context.chat_data.clear()  # type: ignore[union-attr]
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_conversation() -> ConversationHandler[Any]:
    allowed = filters.ChatType.PRIVATE  # a first pass only; each handler calls authorize()
    text = filters.TEXT & ~filters.COMMAND & allowed
    with warnings.catch_warnings():
        # per_message=False is intended: the buttons belong to the chat's single open proposal
        warnings.simplefilter("ignore", PTBUserWarning)
        return ConversationHandler(
            entry_points=[
                CommandHandler("start", start, filters=allowed),
                MessageHandler(text, on_text),
                CallbackQueryHandler(on_button, pattern=_CONFIRM_BUTTONS),  # stale buttons from an ended conversation
            ],
            states={
                COLLECTING: [MessageHandler(text, on_text)],
                CONFIRMING: [CallbackQueryHandler(on_button, pattern=_CONFIRM_BUTTONS), MessageHandler(text, on_text)],
            },
            fallbacks=[CommandHandler("cancel", cancel, filters=allowed), CommandHandler("start", start, filters=allowed)],
            allow_reentry=True,
        )
