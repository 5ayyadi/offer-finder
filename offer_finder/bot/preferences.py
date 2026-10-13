"""/settings, /home <PLZ>, /radius <km|any>: each user's defaults for new
searches (used when the conversation names no place or distance). The
commands only ever change the caller's own row; unset values fall back to
HOME_PLZ / DEFAULT_RADIUS_KM from .env.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

from telegram import Update
from telegram.ext import BaseHandler, CommandHandler, ContextTypes, filters

from offer_finder import runner
from offer_finder.bot.access import authorize
from offer_finder.config import Settings
from offer_finder.core import distance
from offer_finder.core.models import User
from offer_finder.storage import db

logger = logging.getLogger(__name__)

MAX_RADIUS_KM = 200


def home_plz(user: User, settings: Settings) -> str:
    return user.home_plz or settings.home_plz


def radius_km(user: User, settings: Settings) -> int | None:
    """None = any distance."""
    if user.radius_km is None:
        return settings.default_radius_km
    return user.radius_km or None


def _radius_text(radius: int | None) -> str:
    return "any distance" if radius is None else f"{radius} km"


@lru_cache(maxsize=1)
def _plz_table(path: str) -> distance.PlzTable:
    return distance.load_plz_table(path)


async def on_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await authorize(update, context)
    if user is None or update.message is None:
        return
    settings: Settings = context.bot_data["settings"]
    await update.message.reply_text(
        f"Home postcode: {home_plz(user, settings)}\n"
        f"Search radius: {_radius_text(radius_km(user, settings))}\n\n"
        "These are used when you don't name a place or distance.\n"
        "Change them with /home <postcode> or /radius <km|any>."
    )


async def on_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await authorize(update, context)
    if user is None or update.message is None:
        return
    settings: Settings = context.bot_data["settings"]
    args = context.args or []
    if len(args) != 1 or not re.fullmatch(r"\d{5}", args[0]):
        await update.message.reply_text("Usage: /home <5-digit postcode>, e.g. /home 55116")
        return
    if args[0] not in _plz_table(settings.plz_csv_path):
        await update.message.reply_text(f"I don't know the postcode {args[0]}.")
        return
    conn = runner.open_db(settings)
    try:
        db.set_user_home(conn, user.telegram_id, args[0])
    finally:
        conn.close()
    logger.info("chat %d: home postcode set", user.telegram_id)
    await update.message.reply_text(f"Home postcode set to {args[0]}.")


async def on_radius(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await authorize(update, context)
    if user is None or update.message is None:
        return
    args = context.args or []
    value = args[0].lower() if len(args) == 1 else ""
    if value == "any":
        radius = 0
    elif re.fullmatch(r"\d{1,3}", value) and 1 <= int(value) <= MAX_RADIUS_KM:
        radius = int(value)
    else:
        await update.message.reply_text(f"Usage: /radius <1-{MAX_RADIUS_KM}> or /radius any")
        return
    conn = runner.open_db(context.bot_data["settings"])
    try:
        db.set_user_radius(conn, user.telegram_id, radius)
    finally:
        conn.close()
    logger.info("chat %d: radius set to %s", user.telegram_id, value)
    await update.message.reply_text(f"Search radius set to {_radius_text(radius or None)}.")


def build_preference_handlers() -> list[BaseHandler[Any, Any, Any]]:
    private = filters.ChatType.PRIVATE
    return [
        CommandHandler("settings", on_settings, filters=private),
        CommandHandler("home", on_home, filters=private),
        CommandHandler("radius", on_radius, filters=private),
    ]
