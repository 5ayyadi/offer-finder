"""The live status message of a running search: one Telegram message, edited
in place as the search moves through its steps, so the bot isn't a black box.

The worker thread only calls set_progress(); edits happen on the event loop,
at most every interval_s and only when the text changed. A failed edit is
logged and ignored: status updates must never break a search.
"""

from __future__ import annotations

import asyncio
import logging

from telegram.error import TelegramError

from offer_finder.bot.notify import Sender
from offer_finder.core.models import SearchSpec
from offer_finder.core.pipeline import Progress, RunReport
from offer_finder.core.search_spec import log_line

logger = logging.getLogger(__name__)

_STEPS = ("results", "fetching", "evaluating")
_PENDING = {"results": "▫️ Results page", "fetching": "▫️ Reading listing pages", "evaluating": "▫️ Evaluating"}


def _duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _done_line(step: str, report: RunReport) -> str:
    if step == "results":
        outside = f" ({report.out_of_range} outside your price/radius)" if report.out_of_range else ""
        return f"✅ Results page: {_count(report.found, 'listing')}{outside}"
    if step == "fetching":
        if report.fetched == 0 and report.fetch_failed == 0:
            return "✅ No new listing pages to read"
        failed = f" ({report.fetch_failed} failed)" if report.fetch_failed else ""
        return f"✅ Read {_count(report.fetched, 'listing page')}{failed}"
    if report.evaluated == 0 and report.evaluation_failed == 0:
        return "✅ Nothing new to evaluate"
    failed = f" ({report.evaluation_failed} failed, retried next run)" if report.evaluation_failed else ""
    return f"✅ Evaluated {report.evaluated} · {report.recommended} recommended{failed}"


def _active_line(step: str, progress: Progress | None) -> str:
    if progress is None or step == "results":
        return "⏳ Loading the results page…"
    if step == "fetching":
        return f"⏳ Reading listing pages: {progress.done}/{progress.total}"
    return f"⏳ Evaluating: {progress.done}/{progress.total} · {progress.report.recommended} good so far"


def format_status(
    spec: SearchSpec,
    progress: Progress | None,
    *,
    waiting: bool = False,
    finished_s: float | None = None,
    error: str | None = None,
) -> str:
    lines = [f"🔎 Searching Kleinanzeigen: {log_line(spec)}", ""]
    if waiting:
        lines.append("⏳ Waiting for the running search or Gmail poll to finish…")
        current = -1
    else:
        current = 0 if progress is None else _STEPS.index(progress.stage)
    report = progress.report if progress else RunReport()

    for index, step in enumerate(_STEPS):
        if index < current or (finished_s is not None and index >= current >= 0):
            lines.append(_done_line(step, report))
        elif index == current:
            lines.append(f"❌ Stopped: {error}" if error else _active_line(step, progress))
        elif error is None:
            lines.append(_PENDING[step])

    if finished_s is not None:
        lines += ["", f"Done in {_duration(finished_s)}."]
    return "\n".join(lines)


class StatusMessage:
    def __init__(self, bot: Sender, chat_id: int, spec: SearchSpec, interval_s: float = 3.0):
        self._bot = bot
        self._chat_id = chat_id
        self._spec = spec
        self._interval_s = interval_s
        self._message_id: int | None = None
        self._shown = ""
        self.waiting = False
        self.progress: Progress | None = None

    def set_progress(self, progress: Progress) -> None:
        """Called from the worker thread; the next refresh() shows it."""
        self.progress = progress

    async def send(self, waiting: bool = False) -> None:
        self.waiting = waiting
        text = format_status(self._spec, None, waiting=waiting)
        try:
            message = await self._bot.send_message(self._chat_id, text)
        except TelegramError as exc:
            logger.warning("chat %d: couldn't send the status message: %s", self._chat_id, exc)
            return
        self._message_id, self._shown = message.message_id, text

    async def refresh(self, finished_s: float | None = None, error: str | None = None) -> None:
        text = format_status(
            self._spec, self.progress, waiting=self.waiting, finished_s=finished_s, error=error
        )
        if self._message_id is None or text == self._shown:
            return
        try:
            await self._bot.edit_message_text(text=text, chat_id=self._chat_id, message_id=self._message_id)
        except TelegramError as exc:
            logger.info("chat %d: status update skipped: %s", self._chat_id, exc)
            return
        self._shown = text

    async def keep_updated(self) -> None:
        """Run as a task while the search runs; cancel it when the search is done."""
        while True:
            await asyncio.sleep(self._interval_s)
            await self.refresh()
