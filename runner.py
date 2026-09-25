"""Builds the real dependencies from Settings and runs one unit of work
(a search, or a Gmail poll). Synchronous; shared by cli.py and the bot,
which calls it from a worker thread.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import db
from config import Settings
from core import distance
from core.pipeline import Fetcher, Pipeline, RunReport
from models import SearchSpec
from services.fetch import (
    BudgetExhausted,
    CacheMiss,
    FetchFailed,
    KillSwitchActive,
    PageType,
    RequestGate,
    TooManyFailures,
    fetch_page,
)
from services.llm import LLMProvider, OpenAICompatProvider


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
