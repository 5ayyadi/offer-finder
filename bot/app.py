"""Telegram bot process: the conversation handler, background searches, and
(when enabled) the Gmail polling job. Scraping runs in a worker thread
under one lock, so a search and a Gmail poll never hit Kleinanzeigen at
the same time.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.ext import Application

import runner
from bot.conversation import build_conversation
from bot.notify import Sender, format_summary, send_pending
from config import Settings
from models import SearchSpec
from services.llm import LLMProvider

logger = logging.getLogger(__name__)


async def search_and_notify(
    bot: Sender, settings: Settings, llm: LLMProvider, lock: asyncio.Lock, chat_id: int, spec: SearchSpec
) -> None:
    try:
        async with lock:
            outcome = await asyncio.to_thread(runner.run_search_once, settings, llm, chat_id, spec)
        if outcome.error:
            await bot.send_message(chat_id, outcome.error)
            return
        conn = runner.open_db(settings)
        try:
            sent = await send_pending(bot, conn, chat_id, outcome.search_spec_id)
        finally:
            conn.close()
        await bot.send_message(chat_id, format_summary(outcome.report, sent, settings.recommendation_threshold))
    except Exception as exc:
        logger.exception("search failed")
        await bot.send_message(chat_id, f"Something went wrong during the search: {exc}")


def build_application(settings: Settings) -> Application:  # type: ignore[type-arg]
    application = Application.builder().token(settings.telegram_bot_token).build()
    llm = runner.make_provider(settings)
    lock = asyncio.Lock()
    bot: Bot = application.bot

    async def start_search(chat_id: int, spec: SearchSpec) -> None:
        application.create_task(search_and_notify(bot, settings, llm, lock, chat_id, spec))

    application.bot_data.update(settings=settings, llm=llm, start_search=start_search)
    application.add_handler(build_conversation(settings.telegram_allowlist))
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the bot token in URLs
    settings = Settings()
    if not settings.telegram_bot_token or not settings.telegram_allowlist:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWLIST in .env first.")
    build_application(settings).run_polling()


if __name__ == "__main__":
    main()
