"""Telegram bot process: access control, the conversation handler,
background searches, and (when GMAIL_ENABLED) the Gmail polling job.
Scraping runs in a worker thread under one lock, so a search and a Gmail
poll never hit Kleinanzeigen at the same time; several users' searches queue.
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot
from telegram.ext import Application

from offer_finder import runner
from offer_finder.bot.access import build_access_handlers
from offer_finder.bot.conversation import build_conversation
from offer_finder.bot.notify import Sender, format_summary, send_new_picks, send_search_results
from offer_finder.bot.offers import build_offer_handler
from offer_finder.bot.preferences import build_preference_handlers
from offer_finder.bot.status import StatusMessage
from offer_finder.config import Settings
from offer_finder.core.models import SearchSpec
from offer_finder.services.gmail import GmailClient, GoogleGmailClient
from offer_finder.services.llm import LLMProvider
from offer_finder.services.search_url import build_search_urls
from offer_finder.storage import db

logger = logging.getLogger(__name__)


def save_hint(spec: SearchSpec) -> str:
    urls = build_search_urls(spec)
    if len(urls) == 1:
        return f"To get new matches by email, save this search on Kleinanzeigen:\n{urls[0][1]}"
    links = "\n".join(f"{location.label}: {url}" for location, url in urls)
    return f"To get new matches by email, save these searches on Kleinanzeigen:\n{links}"


def _update_run(settings: Settings, run_id: int | None, status: str, search_spec_id: int | None = None) -> None:
    """Track a reserved search run (see db.reserve_search_run); None = untracked."""
    if run_id is None:
        return
    conn = runner.open_db(settings)
    try:
        if status == "running":
            db.start_search_run(conn, run_id)
        else:
            db.finish_search_run(conn, run_id, "done" if status == "done" else "failed", search_spec_id)
    finally:
        conn.close()


async def search_and_notify(
    bot: Sender,
    settings: Settings,
    llm: LLMProvider,
    lock: asyncio.Lock,
    chat_id: int,
    spec: SearchSpec,
    run_id: int | None = None,
) -> None:
    started = time.monotonic()
    status = StatusMessage(bot, chat_id, spec)
    outcome = None
    try:
        if lock.locked():
            logger.info("chat %d: waiting for the running search or Gmail poll to finish", chat_id)
        await status.send(waiting=lock.locked())
        updater = asyncio.create_task(status.keep_updated())
        try:
            async with lock:
                status.waiting = False
                _update_run(settings, run_id, "running")
                outcome = await asyncio.to_thread(
                    runner.run_search_once, settings, llm, chat_id, spec, on_progress=status.set_progress
                )
        finally:
            updater.cancel()
            await asyncio.gather(updater, return_exceptions=True)
        if outcome.error:
            _update_run(settings, run_id, "failed", outcome.search_spec_id)
            await status.refresh(error=outcome.error)
            return
        _update_run(settings, run_id, "done", outcome.search_spec_id)
        await status.refresh(finished_s=time.monotonic() - started)
        conn = runner.open_db(settings)
        try:
            listed = await send_search_results(bot, conn, chat_id, outcome.search_spec_id)
        finally:
            conn.close()
        report = outcome.report
        logger.info(
            "search %d done in %ds: %d found, %d evaluated, %d recommended, %d listed",
            outcome.search_spec_id, time.monotonic() - started, report.found, report.evaluated,
            report.recommended, listed,
        )
        summary = format_summary(outcome.report, settings.recommendation_threshold)
        await bot.send_message(chat_id, f"{summary}\n\n{save_hint(spec)}")
    except Exception as exc:
        logger.exception("search failed")
        if outcome is None:  # crashed before the run finished; afterwards it already counts as done
            _update_run(settings, run_id, "failed")
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
            listed = await send_new_picks(bot, conn, chat_id)
        finally:
            conn.close()
        if outcome.messages or listed:
            logger.info("gmail poll: %d new emails, %d new picks listed", outcome.messages, listed)
    except Exception:
        logger.exception("gmail poll failed")


def build_application(settings: Settings, gmail_client: GmailClient | None = None) -> Application:  # type: ignore[type-arg]
    application = Application.builder().token(settings.telegram_bot_token).build()
    llm = runner.make_provider(settings)
    lock = asyncio.Lock()
    bot: Bot = application.bot

    async def start_search(chat_id: int, spec: SearchSpec, run_id: int) -> None:
        application.create_task(search_and_notify(bot, settings, llm, lock, chat_id, spec, run_id))

    conn = runner.open_db(settings)
    try:
        interrupted = db.interrupt_unfinished_runs(conn)
    finally:
        conn.close()
    if interrupted:
        logger.info("%d search(es) were cut off by the last restart", interrupted)

    application.bot_data.update(settings=settings, llm=llm, start_search=start_search)
    for handler in [*build_access_handlers(), *build_preference_handlers(), build_offer_handler()]:
        application.add_handler(handler)
    application.add_handler(build_conversation())

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
