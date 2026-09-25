"""Builds the real dependencies from Settings and runs one unit of work
(a search, or a Gmail poll). Synchronous; shared by cli.py and the Telegram bot,
which calls it from a worker thread.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from offer_finder.config import Settings
from offer_finder.core import distance
from offer_finder.core.email_parse import extract_listing_refs, html_to_text, match_spec
from offer_finder.core.models import SearchSpec
from offer_finder.core.pipeline import Fetcher, Pipeline, RunReport
from offer_finder.services.fetch import (
    BudgetExhausted,
    CacheMiss,
    FetchFailed,
    KillSwitchActive,
    PageType,
    RequestGate,
    TooManyFailures,
    fetch_page,
)
from offer_finder.services.gmail import GmailClient
from offer_finder.services.llm import LLMProvider, OpenAICompatProvider
from offer_finder.storage import db

logger = logging.getLogger(__name__)


def make_provider(settings: Settings) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model,
        response_format=settings.llm_response_format,
    )


def make_fetcher(settings: Settings) -> Fetcher:
    """A fresh RequestGate per unit of work: the request budget is per run."""
    gate = RequestGate(
        max_requests_per_run=settings.max_requests_per_run,
        delay_floor_search_s=settings.fetch_delay_floor_search_s,
        delay_floor_detail_s=settings.fetch_delay_floor_detail_s,
        jitter_s=settings.fetch_delay_jitter_s,
        max_consecutive_failures=settings.max_consecutive_failures,
    )

    def fetcher(url: str, page_type: PageType) -> str:
        return fetch_page(
            url,
            page_type,
            gate,
            cache_dir=settings.cache_dir,
            dry_run=settings.dry_run,
            kill_switch=settings.kill_switch,
            cache_ttl_s=settings.search_cache_ttl_s if page_type == "search" else None,
        )

    return fetcher


def open_db(settings: Settings) -> sqlite3.Connection:
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    return conn


def make_pipeline(
    settings: Settings, conn: sqlite3.Connection, provider: LLMProvider, fetcher: Fetcher | None = None
) -> Pipeline:
    return Pipeline(
        conn=conn,
        fetcher=fetcher or make_fetcher(settings),
        provider=provider,
        model_name=getattr(provider, "model", "unknown"),
        plz_table=distance.load_plz_table(settings.plz_csv_path),
        home_plz=settings.home_plz,
        threshold=settings.recommendation_threshold,
        llm_min_interval_s=settings.llm_min_interval_s,
    )


def explain_stop(exc: Exception) -> str:
    if isinstance(exc, KillSwitchActive):
        return "KILL_SWITCH is on, so I'm not fetching anything."
    if isinstance(exc, CacheMiss):
        return "DRY_RUN is on and this page isn't cached. Set DRY_RUN=false in .env to search live."
    if isinstance(exc, (TooManyFailures, FetchFailed)):
        return f"Kleinanzeigen looks like it's blocking me, so I stopped. ({exc})"
    if isinstance(exc, BudgetExhausted):
        return f"The request budget ran out. ({exc})"
    return f"Couldn't read the Kleinanzeigen page; the layout may have changed. ({exc})"


_STOP_ERRORS = (KillSwitchActive, CacheMiss, TooManyFailures, FetchFailed, BudgetExhausted, ValueError)


@dataclass
class SearchOutcome:
    search_spec_id: int
    report: RunReport
    error: str | None = None


def run_search_once(
    settings: Settings, provider: LLMProvider, chat_id: int, spec: SearchSpec, fetcher: Fetcher | None = None
) -> SearchOutcome:
    conn = open_db(settings)
    try:
        search_spec_id = db.insert_search_spec(conn, chat_id, spec)
        pipeline = make_pipeline(settings, conn, provider, fetcher)
        try:
            report = pipeline.run_search(search_spec_id, spec)
        except _STOP_ERRORS as exc:
            return SearchOutcome(search_spec_id, RunReport(), error=explain_stop(exc))
        return SearchOutcome(search_spec_id, report)
    finally:
        conn.close()


@dataclass
class GmailOutcome:
    messages: int = 0
    emails_without_listings: int = 0
    report: RunReport = field(default_factory=RunReport)
    error: str | None = None


_COUNTERS = ("found", "new", "out_of_range", "fetched", "fetch_failed", "evaluated", "evaluation_failed", "recommended")


def _add_report(total: RunReport, part: RunReport) -> None:
    for name in _COUNTERS:
        setattr(total, name, getattr(total, name) + getattr(part, name))
    if part.best_score is not None:
        total.best_score = part.best_score if total.best_score is None else max(total.best_score, part.best_score)
    total.stopped_reason = total.stopped_reason or part.stopped_reason


def poll_gmail_once(
    settings: Settings, provider: LLMProvider, client: GmailClient, fetcher: Fetcher | None = None
) -> GmailOutcome:
    """Process every not-yet-processed alert email. An email counts as
    processed only once all its listings were fetched and evaluated, so a
    poll cut short by the request budget or an LLM hiccup resumes later."""
    outcome = GmailOutcome()
    conn = open_db(settings)
    try:
        pipeline = make_pipeline(settings, conn, provider, fetcher)
        specs = db.active_search_specs(conn)
        for message_id in client.list_message_ids(settings.gmail_query):
            if db.gmail_message_seen(conn, message_id):
                continue
            message = client.get_message(message_id)
            refs = extract_listing_refs(message.html)
            if not refs:
                outcome.emails_without_listings += 1
                logger.warning(
                    "no listing links in email %r; if this was a saved-search alert, its format differs "
                    "from tests/fixtures/gmail_alert_email.html",
                    message.subject,
                )
            match = match_spec(f"{message.subject} {html_to_text(message.html)}", specs)
            search_spec_id, spec = match if match else (None, None)
            try:
                report = pipeline.process_email(refs, search_spec_id, spec)
            except _STOP_ERRORS as exc:
                outcome.error = explain_stop(exc)
                break
            _add_report(outcome.report, report)
            outcome.messages += 1
            if report.stopped_reason is None and report.evaluation_failed == 0:
                db.record_gmail_message(
                    conn, message_id, message.subject, [ref.listing_id for ref in refs], search_spec_id
                )
            if report.stopped_reason:
                break  # request budget is gone for this poll
    finally:
        conn.close()
    return outcome
