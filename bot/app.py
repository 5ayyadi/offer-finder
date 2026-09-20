"""Telegram adapter: polling, allowlist enforcement, and the /search
handler. Kept thin — all real work happens in core.pipeline and services.*.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings
from core import catalog, distance
from core.pipeline import run_search
from models import ScoredListing
from services.fetch import (
    KillSwitchActive,
    PageType,
    RequestGate,
    SeenIds,
    TooManyFailures,
    fetch_page,
)
from services.llm import OpenAICompatProvider, QueryParseError, parse_query

logger = logging.getLogger(__name__)

_TV_SEARCH_URL = "https://www.kleinanzeigen.de/s-fernseher/k0"


def is_allowed(user_id: int, settings: Settings) -> bool:
    return user_id in settings.telegram_allowlist


def format_results(results: list[ScoredListing]) -> str:
    if not results:
        return "No matching deals found right now. Try a broader search or check back later."

    lines = []
    for r in results[:5]:
        lines.append(
            f"*{r.catalog_match.model_name}* — €{r.fields.price_eur.value} "
            f"(score {r.score.total_score:.0f}/100)\n"
            f"{r.score.explanation[0]}\n"
            f"{r.summary.url}"
        )
    return "\n\n".join(lines)


async def start_handler(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Tell me what TV deal you're looking for, e.g. "
        '"Find me good TVs under €700 around 55128, 65 inch or bigger."'
    )


async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    settings: Settings = context.bot_data["settings"]
    if not is_allowed(update.effective_user.id, settings):
        return  # silently ignore — no allowlist confirmation leak

    raw_message = update.message.text or ""

    try:
        provider = OpenAICompatProvider(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
        query = parse_query(raw_message, settings.home_plz, provider)
    except QueryParseError:
        await update.message.reply_text("Sorry, I couldn't understand that — could you rephrase?")
        return

    tv_catalog = catalog.load_catalog(settings.catalog_path)
    plz_table = distance.load_plz_table(settings.plz_csv_path)
    seen_ids = SeenIds(f"{settings.cache_dir}/seen_ids.json")
    gate = RequestGate(
        max_requests_per_run=settings.max_requests_per_run,
        delay_floor_search_s=settings.fetch_delay_floor_search_s,
        delay_floor_detail_s=settings.fetch_delay_floor_detail_s,
        jitter_s=settings.fetch_delay_jitter_s,
        max_consecutive_failures=settings.max_consecutive_failures,
    )

    def fetcher(url: str, page_type: PageType) -> str:
        return fetch_page(
            url, page_type, gate,
            cache_dir=settings.cache_dir, dry_run=settings.dry_run, kill_switch=settings.kill_switch,
        )

    try:
        results = run_search(
            query, search_url=_TV_SEARCH_URL, tv_catalog=tv_catalog, plz_table=plz_table,
            seen_ids=seen_ids, fetcher=fetcher,
        )
    except (KillSwitchActive, TooManyFailures) as exc:
        logger.warning("stopping search: %s", exc)
        await update.message.reply_text(
            f"Kleinanzeigen looks like it's blocking me or my budget ran out — stopping. ({exc})"
        )
        return

    await update.message.reply_text(format_results(results), parse_mode="Markdown")


def build_application(settings: Settings) -> Application:
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["settings"] = settings
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_handler))
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    application = build_application(settings)
    application.run_polling()


if __name__ == "__main__":
    main()
