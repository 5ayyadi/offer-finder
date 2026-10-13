"""Telegram results. A search (or a batch of alert emails) ends in one ranked
text list of the picks, with numbered buttons; a button sends that offer's
full card with photos (send_offer). Picks are recorded as notified only after
Telegram accepted the list, so a failed send is retried next time.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from html import escape
from typing import Any, Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import BadRequest, TelegramError

from offer_finder.core.models import Evaluation, Listing
from offer_finder.core.pipeline import RunReport
from offer_finder.core.search_spec import places_text
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
_TIER_WORDS = {"top": "top tier", "upper_mid": "upper mid", "mid": "mid tier", "budget": "budget", "unknown": None}
_LIST_TITLE_MAX = 50
_SUMMARY_MAX = 160
_BUTTONS_PER_ROW = 5
OFFER_CALLBACK_PREFIX = "offer:"


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


async def send_offer(bot: Sender, conn: sqlite3.Connection, chat_id: int, evaluation_id: int) -> bool:
    """The full card behind a list button. False if the evaluation is gone."""
    found = db.get_evaluation(conn, evaluation_id)
    if found is None:
        return False
    await _send_one(bot, chat_id, *found)
    return True


# --- the pick list -----------------------------------------------------------------


@dataclass
class Pick:
    listing: Listing
    evaluation: Evaluation
    evaluation_id: int
    seen_before: bool = False


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _list_entry(number: int, pick: Pick, html: bool) -> str:
    listing, evaluation = pick.listing, pick.evaluation
    judgment, checks = evaluation.judgment, evaluation.checks

    def text(value: str) -> str:
        return escape(value) if html else value

    heading = text(f"{number}. {judgment.score}/100 · {_short(listing.title, _LIST_TITLE_MAX)} — "
                   f"{listing.price_raw or 'price unknown'}")
    place = _town(listing.location_text)
    if checks.distance_km is not None:
        place += f" · {checks.distance_km:g} km"
        if checks.nearest_location:
            place += f" from {checks.nearest_location}"
    details = [place]
    tier = _TIER_WORDS[judgment.product_assessment.tier]
    if tier:
        details.append(tier)
    if pick.seen_before:
        details.append("seen before")
    lines = [
        f"<b>{heading}</b>" if html else heading,
        text(" · ".join(details)),
        text(_short(judgment.product_assessment.summary, _SUMMARY_MAX)),
        text(_short(judgment.deal_assessment.summary, _SUMMARY_MAX)),
    ]
    return "\n".join(lines)


def format_pick_list(
    picks: list[Pick], header: str, limit: int = MESSAGE_LIMIT, html: bool = True
) -> list[tuple[str, list[tuple[int, int]]]]:
    """The ranked list as messages that each fit Telegram's limit, with the
    (number, evaluation id) pairs whose buttons go under each message."""
    chunks: list[tuple[str, list[tuple[int, int]]]] = []
    blocks: list[str] = [escape(header, quote=False) if html else header]
    buttons: list[tuple[int, int]] = []
    for number, pick in enumerate(picks, start=1):
        entry = _list_entry(number, pick, html)
        if buttons and len("\n\n".join([*blocks, entry])) > limit:
            chunks.append(("\n\n".join(blocks), buttons))
            blocks, buttons = [], []
        blocks.append(entry)
        buttons.append((number, pick.evaluation_id))
    chunks.append(("\n\n".join(blocks), buttons))
    return chunks


def offer_keyboard(buttons: list[tuple[int, int]]) -> InlineKeyboardMarkup:
    keys = [InlineKeyboardButton(str(n), callback_data=f"{OFFER_CALLBACK_PREFIX}{eid}") for n, eid in buttons]
    return InlineKeyboardMarkup([keys[i : i + _BUTTONS_PER_ROW] for i in range(0, len(keys), _BUTTONS_PER_ROW)])


async def _send_pick_list(bot: Sender, conn: sqlite3.Connection, chat_id: int, picks: list[Pick], header: str) -> int:
    """Send the list; record each part's picks once Telegram accepted that part.
    Returns how many picks were listed."""
    listed = 0
    html_chunks = format_pick_list(picks, header)
    plain_chunks = format_pick_list(picks, header, html=False)
    for (html_text, buttons), (plain_text, _) in zip(html_chunks, plain_chunks, strict=True):
        markup = offer_keyboard(buttons)
        try:
            try:
                await bot.send_message(chat_id, html_text, parse_mode="HTML", reply_markup=markup)
            except BadRequest as exc:  # should not happen (everything is escaped), but never lose a list
                logger.warning("chat %d: HTML list was rejected (%s), sending plain text", chat_id, exc)
                await bot.send_message(chat_id, plain_text, reply_markup=markup)
        except TelegramError as exc:
            logger.warning("chat %d: sending the pick list failed, will retry: %s", chat_id, exc)
            break
        chunk_picks = picks[listed : listed + len(buttons)]
        for pick in chunk_picks:
            db.record_notification(conn, pick.listing.listing_id, chat_id, pick.evaluation_id)
        listed += len(chunk_picks)
    if listed:
        logger.info("chat %d: sent a list of %s", chat_id, _plural(listed, "pick"))
    return listed


async def send_search_results(bot: Sender, conn: sqlite3.Connection, chat_id: int, search_spec_id: int) -> int:
    """All picks of one search, including ones listed before (marked as seen)."""
    picks = [Pick(*row) for row in db.picks_for_spec(conn, chat_id, search_spec_id)]
    spec = db.get_search_spec(conn, search_spec_id)
    if not picks or spec is None:
        return 0
    header = (f'🔎 {_plural(len(picks), "pick")} for "{spec.query}" · {places_text(spec)}\n'
              "Tap a number for photos and the link.")
    return await _send_pick_list(bot, conn, chat_id, picks, header)


async def send_new_picks(bot: Sender, conn: sqlite3.Connection, chat_id: int) -> int:
    """Picks from alert emails this chat hasn't been sent yet."""
    picks = [Pick(*row) for row in db.pending_notifications(conn, chat_id)]
    if not picks:
        return 0
    header = (f"📬 {_plural(len(picks), 'new pick')} from your Kleinanzeigen alerts\n"
              "Tap a number for photos and the link.")
    return await _send_pick_list(bot, conn, chat_id, picks, header)


def format_summary(report: RunReport, threshold: int) -> str:
    if report.found == 0:
        lines = ["No listings found for this search."]
    else:
        lines = [f"Checked {report.found} listings: {report.evaluated} evaluated, {report.recommended} recommended."]
    if report.recommended == 0 and report.best_score is not None:
        lines.append(f"Nothing reached {threshold}/100 — best was {report.best_score}/100.")
    if report.out_of_range:
        lines.append(f"{report.out_of_range} were outside your price range or radius.")
    if report.fetch_failed:
        lines.append(f"{report.fetch_failed} detail page(s) could not be loaded.")
    if report.evaluation_failed:
        lines.append(f"{report.evaluation_failed} could not be evaluated (will retry next time).")
    if report.stopped_reason:
        lines.append(f"Stopped early: {report.stopped_reason}")
    return "\n".join(lines)
