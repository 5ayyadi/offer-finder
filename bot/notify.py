"""Telegram notifications. Reads recommended-but-unsent listings from the DB
and records each one only after Telegram accepted it, so a failed send is
retried next time and a successful one is never repeated.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Protocol

from telegram import InputMediaPhoto
from telegram.error import BadRequest, TelegramError

import db
from core.pipeline import RunReport
from models import Evaluation, Listing

logger = logging.getLogger(__name__)

MAX_IMAGES = 3
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
_MAX_CLAIMS = 3


class Sender(Protocol):
    """The part of telegram.Bot used here (a fake in tests)."""

    async def send_message(self, *args: Any, **kwargs: Any) -> Any: ...
    async def send_photo(self, *args: Any, **kwargs: Any) -> Any: ...
    async def send_media_group(self, *args: Any, **kwargs: Any) -> Any: ...


def format_notification(listing: Listing, evaluation: Evaluation, limit: int = MESSAGE_LIMIT) -> str:
    judgment = evaluation.judgment
    distance = evaluation.checks.distance_km
    place = listing.location_text or "location unknown"
    if distance is not None:
        place += f" ({distance:g} km)"
    product, deal = judgment.product_assessment, judgment.deal_assessment

    lines = [
        f"{judgment.score}/100 · {listing.price_raw or 'price unknown'} · {place}",
        listing.title,
        f"Product: {product.verdict} ({product.requirements_match}) — {product.summary}",
        f"Deal: {deal.verdict} — {deal.summary}",
    ]
    lines += [f"+ {c.claim}" for c in judgment.highlights if c.verified][:_MAX_CLAIMS]
    lines += [f"! {c.claim}" for c in judgment.concerns if c.verified][:_MAX_CLAIMS]
    if judgment.unknowns:
        lines.append(f"? Unknown: {', '.join(judgment.unknowns)}")

    body = "\n".join(lines)
    room = limit - len(listing.url) - 1
    if len(body) > room:
        body = body[: room - 1] + "…"
    return f"{body}\n{listing.url}"


async def _send_one(bot: Sender, chat_id: int, listing: Listing, evaluation: Evaluation) -> None:
    images = listing.image_urls[:MAX_IMAGES]
    if images:
        caption = format_notification(listing, evaluation, limit=CAPTION_LIMIT)
        try:
            if len(images) == 1:
                await bot.send_photo(chat_id, images[0], caption=caption)
            else:
                media = [InputMediaPhoto(images[0], caption=caption)] + [InputMediaPhoto(url) for url in images[1:]]
                await bot.send_media_group(chat_id, media)
            return
        except BadRequest as exc:  # Telegram couldn't fetch or accept the image URLs
            logger.info("sending images for %s failed (%s), falling back to text", listing.listing_id, exc)
    await bot.send_message(chat_id, format_notification(listing, evaluation))


async def send_pending(
    bot: Sender, conn: sqlite3.Connection, chat_id: int, search_spec_id: int | None = None
) -> int:
    sent = 0
    for listing, evaluation, evaluation_id in db.pending_notifications(conn, chat_id, search_spec_id):
        try:
            await _send_one(bot, chat_id, listing, evaluation)
        except TelegramError as exc:
            logger.warning("notification for %s failed, will retry: %s", listing.listing_id, exc)
            continue
        db.record_notification(conn, listing.listing_id, chat_id, evaluation_id)
        sent += 1
    return sent


def format_summary(report: RunReport, sent: int, threshold: int) -> str:
    if report.found == 0:
        lines = ["No listings found for this search."]
    else:
        lines = [f"Checked {report.found} listings: {report.evaluated} evaluated, {report.recommended} recommended."]
    if report.recommended == 0 and report.best_score is not None:
        lines.append(f"Nothing reached {threshold}/100 — best was {report.best_score}/100.")
    if report.recommended > sent:
        lines.append(f"{report.recommended - sent} of them were already sent before.")
    if report.out_of_range:
        lines.append(f"{report.out_of_range} were outside your price range or radius.")
    if report.fetch_failed:
        lines.append(f"{report.fetch_failed} detail page(s) could not be loaded.")
    if report.evaluation_failed:
        lines.append(f"{report.evaluation_failed} could not be evaluated (will retry next time).")
    if report.stopped_reason:
        lines.append(f"Stopped early: {report.stopped_reason}")
    return "\n".join(lines)
