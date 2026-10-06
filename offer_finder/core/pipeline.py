"""Both ingestion paths (interactive search, Gmail alerts) converge here:
store every listing -> fetch details -> evaluate -> store the evaluation.
Search cards clearly outside the price range or radius are stored as
'out_of_range' without a detail fetch.
Sending notifications is separate (bot/notify.py) and reads from the DB.

Synchronous on purpose: the bot runs it in a worker thread.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from offer_finder.core.checks import compute_checks, nearest_location
from offer_finder.core.distance import PlzTable
from offer_finder.core.evaluator import EvaluationError, evaluate
from offer_finder.core.models import Evaluation, Listing, ListingRef, ListingSummary, SearchSpec
from offer_finder.core.parse import (
    extract_facts,
    extract_plz,
    extract_price_eur,
    parse_detail_page,
    parse_search_results,
)
from offer_finder.services.fetch import BudgetExhausted, CacheMiss, FetchFailed, PageType
from offer_finder.services.llm import LLMProvider
from offer_finder.services.search_url import build_search_urls
from offer_finder.storage import db

logger = logging.getLogger(__name__)


class Fetcher(Protocol):
    def __call__(self, url: str, page_type: PageType) -> str: ...


@dataclass
class RunReport:
    found: int = 0
    new: int = 0
    out_of_range: int = 0
    fetched: int = 0
    fetch_failed: int = 0
    evaluated: int = 0
    evaluation_failed: int = 0
    recommended: int = 0
    best_score: int | None = None
    stopped_reason: str | None = None


@dataclass(frozen=True)
class Progress:
    """Where a running search is, for the live Telegram status message."""

    stage: Literal["results", "fetching", "evaluating"]
    done: int
    total: int
    report: RunReport  # a snapshot of the counters so far


def _in_price_range(price_eur: int | None, spec: SearchSpec) -> bool:
    if price_eur is None:
        return True  # unknown from the card; the detail page decides
    if spec.min_price_eur is not None and price_eur < spec.min_price_eur:
        return False
    return spec.max_price_eur is None or price_eur <= spec.max_price_eur


def _in_radius(plz: str | None, spec: SearchSpec, plz_table: PlzTable) -> bool:
    """Kleinanzeigen appends results from outside the radius; skip those
    before spending a detail fetch and an LLM call on them."""
    if plz is None or spec.radius_km is None:
        return True
    nearest = nearest_location(plz, spec.locations, plz_table)
    return nearest is None or nearest[1] <= spec.radius_km


def _title_from_url(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return parts[-2].replace("-", " ") if len(parts) >= 2 else url


@dataclass
class Pipeline:
    conn: sqlite3.Connection
    fetcher: Fetcher
    provider: LLMProvider
    model_name: str
    plz_table: PlzTable
    home_plz: str
    threshold: int
    llm_min_interval_s: float = 0.0
    sleep_fn: Callable[[float], None] = time.sleep
    on_progress: Callable[[Progress], None] | None = None  # called from the worker thread

    def _progress(
        self, stage: Literal["results", "fetching", "evaluating"], done: int, total: int, report: RunReport
    ) -> None:
        if self.on_progress is not None:
            self.on_progress(Progress(stage, done, total, replace(report)))

    def _search_cards(self, spec: SearchSpec, report: RunReport) -> list[ListingSummary]:
        """One results page per location; a card found around two cities counts once."""
        urls = build_search_urls(spec)
        cards: dict[str, ListingSummary] = {}
        for done, (_location, url) in enumerate(urls):
            self._progress("results", done, len(urls), report)
            for card in parse_search_results(self.fetcher(url, "search")):
                cards.setdefault(card.listing_id, card)
        return list(cards.values())

    def run_search(self, search_spec_id: int, spec: SearchSpec) -> RunReport:
        report = RunReport()
        cards = self._search_cards(spec, report)
        report.found = len(cards)

        for card in cards:
            price = extract_price_eur(card.price_raw or "").value
            plz = extract_plz(card.location_text or "").value
            in_range = _in_price_range(price, spec) and _in_radius(plz, spec, self.plz_table)
            listing = Listing(
                listing_id=card.listing_id,
                url=str(card.url),
                title=card.title,
                source="search",
                status="discovered" if in_range else "out_of_range",
                search_spec_id=search_spec_id,
                price_raw=card.price_raw,
                price_eur=price,
                location_text=card.location_text,
                plz=plz,
                posted_at_raw=card.posted_at_raw,
            )
            if db.insert_discovered(self.conn, listing):
                report.new += 1
            elif in_range and _status(self.conn, card.listing_id) == "out_of_range":
                db.set_listing_status(self.conn, card.listing_id, "discovered")  # a wider spec wants it
            if not in_range:
                report.out_of_range += 1

        label = f"search {search_spec_id}"
        logger.info(
            "%s: %d results page(s) have %d listings (%d new, %d outside price/radius)",
            label, len(spec.locations), report.found, report.new, report.out_of_range,
        )
        ids = [card.listing_id for card in cards]
        self._fetch_details(ids, report, label)
        self.evaluate_pending(search_spec_id, spec, ids, report, label)
        return report

    def process_email(
        self,
        refs: list[ListingRef],
        search_spec_id: int | None,
        spec: SearchSpec | None,
        label: str = "email",
    ) -> RunReport:
        report = RunReport(found=len(refs))
        for ref in refs:
            listing = Listing(
                listing_id=ref.listing_id,
                url=ref.url,
                title=_title_from_url(ref.url),
                source="gmail",
                status="discovered",
                search_spec_id=search_spec_id,
            )
            if db.insert_discovered(self.conn, listing):
                report.new += 1

        ids = [ref.listing_id for ref in refs]
        self._fetch_details(ids, report, label)
        self.evaluate_pending(search_spec_id, spec, ids, report, label)
        return report

    def _fetch_details(self, listing_ids: list[str], report: RunReport, label: str) -> None:
        """Fetch detail pages for listings still 'discovered'. Stops early (but
        keeps what it has) when the request budget runs out; TooManyFailures
        and KillSwitchActive propagate and abort the run."""
        listings = [db.get_listing(self.conn, listing_id) for listing_id in listing_ids]
        to_fetch = [listing for listing in listings if listing is not None and listing.status == "discovered"]
        if to_fetch:
            logger.info("%s: fetching %d detail pages", label, len(to_fetch))
        self._progress("fetching", 0, len(to_fetch), report)
        for done, listing in enumerate(to_fetch):
            if done > 0:
                self._progress("fetching", done, len(to_fetch), report)
            listing_id = listing.listing_id
            try:
                html = self.fetcher(listing.url, "detail")
                page = parse_detail_page(html)
            except BudgetExhausted as exc:
                report.stopped_reason = f"request budget reached ({exc}); the rest will be fetched next run"
                logger.warning(
                    "%s: request budget reached after %d detail pages; %d wait for the next run",
                    label, done, len(to_fetch) - done,
                )
                return
            except CacheMiss:
                continue  # DRY_RUN without a cached copy: leave it for a live run
            except (FetchFailed, ValueError) as exc:
                logger.warning("%s: detail fetch for %s failed: %s", label, listing_id, exc)
                db.set_listing_status(self.conn, listing_id, "fetch_failed")
                report.fetch_failed += 1
                continue

            price_raw = page.price_raw or listing.price_raw
            location_text = page.location_text or listing.location_text
            db.update_listing(
                self.conn,
                listing.model_copy(
                    update={
                        "title": page.title,
                        "description": page.description,
                        "price_raw": price_raw,
                        "price_eur": extract_price_eur(price_raw or "").value,
                        "location_text": location_text,
                        "plz": extract_plz(location_text or "").value,
                        "posted_at_raw": page.posted_raw or listing.posted_at_raw,
                        "image_urls": page.image_urls,
                        "attributes": page.attributes,
                        "status": "fetched",
                    }
                ),
            )
            report.fetched += 1
        if to_fetch:
            self._progress("fetching", len(to_fetch), len(to_fetch), report)

    def evaluate_pending(
        self,
        search_spec_id: int | None,
        spec: SearchSpec | None,
        listing_ids: list[str],
        report: RunReport,
        label: str = "evaluate",
    ) -> None:
        pending = db.listings_pending_evaluation(self.conn, search_spec_id, listing_ids)
        if pending:
            logger.info("%s: evaluating %d listings with %s", label, len(pending), self.model_name)
        self._progress("evaluating", 0, len(pending), report)
        for index, listing in enumerate(pending):
            if index > 0:
                self._progress("evaluating", index, len(pending), report)
                if self.llm_min_interval_s > 0:
                    self.sleep_fn(self.llm_min_interval_s)
            facts = extract_facts(listing)
            checks = compute_checks(facts, spec, self.plz_table, self.home_plz)
            try:
                evaluation = evaluate(
                    listing, spec, search_spec_id, facts, checks, self.provider, self.model_name, self.threshold
                )
            except EvaluationError as exc:
                logger.warning("%s: %s", label, exc)
                report.evaluation_failed += 1
                continue

            db.save_evaluation(self.conn, evaluation)
            report.evaluated += 1
            report.recommended += int(evaluation.recommended)
            score = evaluation.judgment.score
            report.best_score = score if report.best_score is None else max(report.best_score, score)
            logger.info(
                '%s: %d/100 %s "%s" %s → %s',
                label, score, listing.listing_id, listing.title[:50], listing.price_raw or "no price",
                _outcome(evaluation, self.threshold),
            )
        if pending:
            self._progress("evaluating", len(pending), len(pending), report)


def _outcome(evaluation: Evaluation, threshold: int) -> str:
    """Why a listing was or wasn't recommended, mirroring is_recommended()."""
    if evaluation.recommended:
        return "recommended"
    if evaluation.checks.hard_fail_reasons:
        return "skipped: " + "; ".join(evaluation.checks.hard_fail_reasons)
    if evaluation.judgment.product_assessment.requirements_match == "mismatch":
        return "skipped: doesn't match the requirements"
    return f"skipped: below {threshold}"


def _status(conn: sqlite3.Connection, listing_id: str) -> str | None:
    listing = db.get_listing(conn, listing_id)
    return listing.status if listing else None
