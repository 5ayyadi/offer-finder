"""Telegram dialogue: collect a wish -> propose a SearchSpec -> the user
confirms, changes or cancels. State lives in memory (chat_data); a restart
just means starting over.

bot_data must provide "settings", "llm" (an LLMProvider) and
"start_search" (async (chat_id, spec) -> None that returns immediately).
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

from offer_finder.config import Settings
from offer_finder.core.models import SearchSpec
from offer_finder.core.search_spec import SpecError, describe, log_line, next_turn

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

GREETING = (
    "Tell me what you're looking for on Kleinanzeigen, for example:\n"
    '"a good 65+ inch OLED TV around Mainz under €700"'
)


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
    context.chat_data.clear()  # type: ignore[union-attr]
    if update.message:
        await update.message.reply_text(GREETING)
    return COLLECTING


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
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
            settings.home_plz,
            default_radius_km=settings.default_radius_km,
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


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and query.message is not None
    settings: Settings = context.bot_data["settings"]
    if query.from_user.id not in settings.telegram_allowlist:
        return ConversationHandler.END  # silently ignore — no allowlist confirmation leak

    await query.answer()
    chat_data: dict[str, Any] = context.chat_data  # type: ignore[assignment]
    spec: SearchSpec | None = chat_data.get("spec")
    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    if spec is None:
        logger.info("chat %d: button pressed for an expired search", chat_id)
        await _reply(query, "That search has expired. Tell me again what you're looking for.")
        return ConversationHandler.END

    await query.edit_message_reply_markup(reply_markup=None)
    if query.data == "search":
        chat_data.clear()
        logger.info("chat %d: search confirmed", chat_id)
        await context.bot_data["start_search"](chat_id, spec)  # it posts a live status message
        return ConversationHandler.END
    if query.data == "change":
        chat_data.setdefault("history", []).append(("assistant", _proposal(chat_data.pop("spec"))))
        logger.info("chat %d: change requested", chat_id)
        await _reply(query, "What should I change?")
        return COLLECTING

    chat_data.clear()
    logger.info("chat %d: cancelled", chat_id)
    await _reply(query, "Cancelled.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data.clear()  # type: ignore[union-attr]
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_conversation(allowlist: list[int]) -> ConversationHandler[Any]:
    allowed = filters.User(user_id=allowlist)
    text = filters.TEXT & ~filters.COMMAND & allowed
    with warnings.catch_warnings():
        # per_message=False is intended: the buttons belong to the chat's single open proposal
        warnings.simplefilter("ignore", PTBUserWarning)
        return ConversationHandler(
            entry_points=[
                CommandHandler("start", start, filters=allowed),
                MessageHandler(text, on_text),
                CallbackQueryHandler(on_button),  # stale buttons from an ended conversation
            ],
            states={
                COLLECTING: [MessageHandler(text, on_text)],
                CONFIRMING: [CallbackQueryHandler(on_button), MessageHandler(text, on_text)],
            },
            fallbacks=[CommandHandler("cancel", cancel, filters=allowed), CommandHandler("start", start, filters=allowed)],
            allow_reentry=True,
        )
