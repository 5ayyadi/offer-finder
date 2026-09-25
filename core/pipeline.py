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
from dataclasses import dataclass
from typing import Protocol

import db
from core.checks import compute_checks
from core.distance import PlzTable, plz_distance_km
from core.evaluator import EvaluationError, evaluate
from core.parse import (
    extract_facts,
    extract_plz,
    extract_price_eur,
    parse_detail_page,
    parse_search_results,
)
from models import Listing, ListingRef, SearchSpec
from services.fetch import BudgetExhausted, CacheMiss, FetchFailed, PageType
from services.llm import LLMProvider
from services.search_url import build_search_url

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
    distance_km = plz_distance_km(plz, spec.plz, plz_table)
    return distance_km is None or distance_km <= spec.radius_km


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

    def run_search(self, search_spec_id: int, spec: SearchSpec) -> RunReport:
        report = RunReport()
        cards = parse_search_results(self.fetcher(build_search_url(spec), "search"))
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

        ids = [card.listing_id for card in cards]
        self._fetch_details(ids, report)
        self.evaluate_pending(search_spec_id, spec, ids, report)
        return report

    def process_email(
        self, refs: list[ListingRef], search_spec_id: int | None, spec: SearchSpec | None
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
        self._fetch_details(ids, report)
        self.evaluate_pending(search_spec_id, spec, ids, report)
        return report

    def _fetch_details(self, listing_ids: list[str], report: RunReport) -> None:
        """Fetch detail pages for listings still 'discovered'. Stops early (but
        keeps what it has) when the request budget runs out; TooManyFailures
        and KillSwitchActive propagate and abort the run."""
        for listing_id in listing_ids:
            listing = db.get_listing(self.conn, listing_id)
            if listing is None or listing.status != "discovered":
                continue
            try:
                html = self.fetcher(listing.url, "detail")
                page = parse_detail_page(html)
            except BudgetExhausted as exc:
                report.stopped_reason = f"request budget reached ({exc}); the rest will be fetched next run"
                return
            except CacheMiss:
                continue  # DRY_RUN without a cached copy: leave it for a live run
            except (FetchFailed, ValueError) as exc:
                logger.warning("detail fetch for %s failed: %s", listing_id, exc)
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

    def evaluate_pending(
        self,
        search_spec_id: int | None,
        spec: SearchSpec | None,
        listing_ids: list[str],
        report: RunReport,
    ) -> None:
        pending = db.listings_pending_evaluation(self.conn, search_spec_id, listing_ids)
        for index, listing in enumerate(pending):
            if index > 0 and self.llm_min_interval_s > 0:
                self.sleep_fn(self.llm_min_interval_s)
            facts = extract_facts(listing)
            checks = compute_checks(facts, spec, self.plz_table, self.home_plz)
            try:
                evaluation = evaluate(
                    listing, spec, search_spec_id, facts, checks, self.provider, self.model_name, self.threshold
                )
            except EvaluationError as exc:
                logger.warning("%s", exc)
                report.evaluation_failed += 1
                continue

            db.save_evaluation(self.conn, evaluation)
            report.evaluated += 1
            report.recommended += int(evaluation.recommended)
            score = evaluation.judgment.score
            report.best_score = score if report.best_score is None else max(report.best_score, score)


def _status(conn: sqlite3.Connection, listing_id: str) -> str | None:
    listing = db.get_listing(conn, listing_id)
    return listing.status if listing else None
