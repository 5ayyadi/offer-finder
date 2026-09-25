"""Telegram bot process: the conversation handler, background searches, and
(when GMAIL_ENABLED) the Gmail polling job. Scraping runs in a worker thread
under one lock, so a search and a Gmail poll never hit Kleinanzeigen at
the same time.
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot
from telegram.ext import Application

from offer_finder import runner
from offer_finder.bot.conversation import build_conversation
from offer_finder.bot.notify import Sender, format_summary, send_pending
from offer_finder.config import Settings
from offer_finder.core.models import SearchSpec
from offer_finder.services.gmail import GmailClient, GoogleGmailClient
from offer_finder.services.llm import LLMProvider
from offer_finder.services.search_url import build_search_url

logger = logging.getLogger(__name__)


async def search_and_notify(
    bot: Sender, settings: Settings, llm: LLMProvider, lock: asyncio.Lock, chat_id: int, spec: SearchSpec
) -> None:
    started = time.monotonic()
    try:
        if lock.locked():
            logger.info("chat %d: waiting for the running search or Gmail poll to finish", chat_id)
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
        report = outcome.report
        logger.info(
            "search %d done in %ds: %d found, %d evaluated, %d recommended, %d sent",
            outcome.search_spec_id, time.monotonic() - started, report.found, report.evaluated,
            report.recommended, sent,
        )
        summary = format_summary(outcome.report, sent, settings.recommendation_threshold)
        save_hint = f"To get new matches by email, save this search on Kleinanzeigen:\n{build_search_url(spec)}"
        await bot.send_message(chat_id, f"{summary}\n\n{save_hint}")
    except Exception as exc:
        logger.exception("search failed")
        await bot.send_message(chat_id, f"Something went wrong during the search: {exc}")


async def gmail_poll_and_notify(
    bot: Sender, settings: Settings, llm: LLMProvider, lock: asyncio.Lock, client: GmailClient
) -> None:
    try:
        async with lock:
            outcome = await asyncio.to_thread(runner.poll_gmail_once, settings, llm, client)
        if outcome.error:
            logger.warning("gmail poll stopped early: %s", outcome.error)
        chat_id = settings.notify_chat_id
        if chat_id is None:
            logger.warning("no TELEGRAM_NOTIFY_CHAT_ID or allowlist entry; Gmail finds are only stored")
            return
        conn = runner.open_db(settings)
        try:
            sent = await send_pending(bot, conn, chat_id)
        finally:
            conn.close()
        if outcome.messages or sent:
            logger.info("gmail poll: %d new emails, %d notifications sent", outcome.messages, sent)
    except Exception:
        logger.exception("gmail poll failed")


def build_application(settings: Settings, gmail_client: GmailClient | None = None) -> Application:  # type: ignore[type-arg]
    application = Application.builder().token(settings.telegram_bot_token).build()
    llm = runner.make_provider(settings)
    lock = asyncio.Lock()
    bot: Bot = application.bot

    async def start_search(chat_id: int, spec: SearchSpec) -> None:
        application.create_task(search_and_notify(bot, settings, llm, lock, chat_id, spec))

    application.bot_data.update(settings=settings, llm=llm, start_search=start_search)
    application.add_handler(build_conversation(settings.telegram_allowlist))

    if settings.gmail_enabled:
        client = gmail_client or GoogleGmailClient(settings.gmail_token_path)

        async def gmail_job(_context: object) -> None:
            await gmail_poll_and_notify(bot, settings, llm, lock, client)

        assert application.job_queue is not None  # needs python-telegram-bot[job-queue]
        application.job_queue.run_repeating(gmail_job, interval=settings.gmail_poll_interval_s, first=10, name="gmail")
    return application


def main() -> None:
    settings = Settings()
    runner.setup_logging(settings)
    if not settings.telegram_bot_token or not settings.telegram_allowlist:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWLIST in .env first.")
    logger.info(
        "bot starting: model %s, threshold %d, dry_run %s, gmail %s, log file %s",
        settings.llm_model, settings.recommendation_threshold, settings.dry_run,
        "on" if settings.gmail_enabled else "off", settings.log_path,
    )
    build_application(settings).run_polling()


if __name__ == "__main__":
    main()
