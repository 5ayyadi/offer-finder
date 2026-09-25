"""Telegram notifications. Reads recommended-but-unsent listings from the DB
and records each one only after Telegram accepted it, so a failed send is
retried next time and a successful one is never repeated.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from html import escape
from typing import Any, Protocol

from telegram import InputMediaPhoto
from telegram.error import BadRequest, TelegramError

from offer_finder.core.models import Evaluation, Listing
from offer_finder.core.pipeline import RunReport
from offer_finder.storage import db

logger = logging.getLogger(__name__)

MAX_IMAGES = 3
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
_MAX_CLAIMS = 3
_TITLE_MAX = 42
_CLAIM_MAX = 80
_UNKNOWN_MAX = 40
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_VERDICT_WORDS = {"good": "good", "ok": "OK", "poor": "poor", "unclear": "unclear"}
_MATCH_NOTES = {"partial": " · partly matches", "unclear": " · match unclear", "mismatch": " · doesn't match"}


class Sender(Protocol):
    """The part of telegram.Bot used here (a fake in tests)."""

    async def send_message(self, *args: Any, **kwargs: Any) -> Any: ...
    async def send_photo(self, *args: Any, **kwargs: Any) -> Any: ...
    async def send_media_group(self, *args: Any, **kwargs: Any) -> Any: ...
    async def edit_message_text(self, *args: Any, **kwargs: Any) -> Any: ...


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _town(location_text: str | None) -> str:
    """'67819 Rheinland-Pfalz - Kriegsfeld' -> 'Kriegsfeld', '55118 Mainz' -> 'Mainz'."""
    if not location_text:
        return "location unknown"
    town = re.sub(r"^\d{5}\s+", "", location_text.rsplit(" - ", 1)[-1]).strip()
    return town or location_text


def format_notification(
    listing: Listing, evaluation: Evaluation, limit: int = MESSAGE_LIMIT, html: bool = True
) -> str:
    """Telegram HTML (parse_mode="HTML"), or plain text for the terminal.
    Only verified claims are shown; the long summaries stay in the DB."""
    judgment, checks = evaluation.judgment, evaluation.checks
    product, deal = judgment.product_assessment, judgment.deal_assessment

    def text(value: str) -> str:
        return escape(value) if html else value

    def bold(value: str) -> str:
        return f"<b>{value}</b>" if html else value

    price = listing.price_raw or "price unknown"
    header = bold(text(f"{judgment.score}/100 — {_short(listing.title, _TITLE_MAX)} — {price}"))
    place = _town(listing.location_text)
    if checks.distance_km is not None:
        place += f" · {checks.distance_km:g} km"
    highlights = [c.claim for c in judgment.highlights if c.verified]
    concerns = [
        c.claim
        for c in sorted(judgment.concerns, key=lambda c: _SEVERITY_ORDER[c.severity])
        if c.verified
    ]
    unknowns = ", ".join(_short(u, _UNKNOWN_MAX) for u in judgment.unknowns[:_MAX_CLAIMS])
    verdict = (
        f"{bold('Verdict:')} {_VERDICT_WORDS[deal.verdict]} deal · {_VERDICT_WORDS[product.verdict]} product"
        f"{_MATCH_NOTES.get(product.requirements_match, '')}"
    )
    link = f'<a href="{escape(listing.url)}">View listing</a>' if html else listing.url

    def build(claims: int, with_unknowns: bool) -> str:
        blocks = [[header, text(place)]]
        for title, items in (("Matches", highlights), ("Concerns", concerns)):
            if items[:claims]:
                blocks.append([bold(title)] + [f"• {text(_short(item, _CLAIM_MAX))}" for item in items[:claims]])
        if with_unknowns and unknowns:
            line = text(f"Unknown: {unknowns}")
            blocks.append([f"<i>{line}</i>" if html else line])
        blocks.append([verdict, link])
        return "\n\n".join("\n".join(block) for block in blocks)

    # Drop detail (never cut through markup) until it fits the Telegram limit.
    for claims, with_unknowns in ((_MAX_CLAIMS, True), (_MAX_CLAIMS, False), (2, False), (1, False)):
        message = build(claims, with_unknowns)
        if len(message) <= limit:
            return message
    return build(0, False)


async def _send_one(bot: Sender, chat_id: int, listing: Listing, evaluation: Evaluation) -> None:
    images = listing.image_urls[:MAX_IMAGES]
    if images:
        caption = format_notification(listing, evaluation, limit=CAPTION_LIMIT)
        try:
            if len(images) == 1:
                await bot.send_photo(chat_id, images[0], caption=caption, parse_mode="HTML")
            else:
                first = InputMediaPhoto(images[0], caption=caption, parse_mode="HTML")
                await bot.send_media_group(chat_id, [first] + [InputMediaPhoto(url) for url in images[1:]])
            return
        except BadRequest as exc:  # Telegram couldn't fetch or accept the image URLs
            logger.info("sending images for %s failed (%s), falling back to text", listing.listing_id, exc)
    try:
        await bot.send_message(chat_id, format_notification(listing, evaluation), parse_mode="HTML")
    except BadRequest as exc:  # should not happen (everything is escaped), but never lose a find over markup
        logger.warning("HTML for %s was rejected (%s), sending plain text", listing.listing_id, exc)
        await bot.send_message(chat_id, format_notification(listing, evaluation, html=False))


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
        logger.info("chat %d: sent %s (%d/100)", chat_id, listing.listing_id, evaluation.judgment.score)
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
