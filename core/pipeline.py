"""Orchestration: search -> cheap filters -> catalog match -> skip
unmatched -> score -> rank. This is the seam both the CLI and the Telegram
bot call, and the one a future scheduler would call identically on a timer.
"""

from __future__ import annotations

from typing import Protocol

from core import catalog, parse, scam
from core.distance import PlzTable
from core.scorer import score
from models import CatalogMatch, ExtractedListing, FieldValue, ScoredListing, StructuredQuery
from services.fetch import PageType, SeenIds


class Fetcher(Protocol):
    def __call__(self, url: str, page_type: PageType) -> str: ...


def build_extracted_listing(listing_id: str, html: str) -> tuple[ExtractedListing, parse.DetailPageText]:
    page = parse.parse_detail_page(html)
    combined_text = f"{page.title} {page.description}"

    shipping_only = scam.detect_shipping_only(page.description)
    payment_flags = scam.detect_payment_red_flags(page.description)
    defect_keywords = scam.detect_defect_keywords(page.description)

    extracted = ExtractedListing(
        listing_id=listing_id,
        price_eur=parse.extract_price_eur(page.price_raw or combined_text),
        plz=parse.extract_plz(page.location_text or ""),
        screen_size_inch=parse.extract_screen_size_inch(combined_text),
        condition_text=parse.extract_condition_text(page.description),
        defect_keywords_found=FieldValue(
            value=defect_keywords,
            source="regex" if defect_keywords else "none",
            confidence=0.8 if defect_keywords else 0.0,
        ),
        shipping_only=FieldValue(value=shipping_only, source="regex", confidence=0.8),
        payment_method_mentions=FieldValue(
            value=payment_flags,
            source="regex" if payment_flags else "none",
            confidence=0.8 if payment_flags else 0.0,
        ),
        posted_days_ago=parse.extract_posted_days_ago(page.posted_raw or ""),
    )
    return extracted, page


def _passes_cheap_price_filter(summary_price_raw: str | None, query: StructuredQuery) -> bool:
    if query.min_price_eur is None and query.max_price_eur is None:
        return True
    price_field = parse.extract_price_eur(summary_price_raw or "")
    if price_field.value is None:
        return True  # can't tell yet from the search card — let the detail fetch decide
    if query.min_price_eur is not None and price_field.value < query.min_price_eur:
        return False
    return not (query.max_price_eur is not None and price_field.value > query.max_price_eur)


def run_search(
    query: StructuredQuery,
    search_url: str,
    tv_catalog: catalog.CatalogDict,
    plz_table: PlzTable,
    seen_ids: SeenIds,
    fetcher: Fetcher,
) -> list[ScoredListing]:
    search_html = fetcher(search_url, "search")
    summaries = parse.parse_search_results(search_html)

    results: list[ScoredListing] = []
    for summary in summaries:
        if seen_ids.has(summary.listing_id):
            continue
        if not _passes_cheap_price_filter(summary.price_raw, query):
            continue

        detail_html = fetcher(str(summary.url), "detail")
        extracted, page = build_extracted_listing(summary.listing_id, detail_html)
        seen_ids.add(summary.listing_id)

        catalog_match: CatalogMatch | None = catalog.match(page.title, page.description, tv_catalog)
        if catalog_match is None:
            continue  # unidentified model — correct outcome, not a failure

        breakdown = score(
            extracted,
            catalog_match,
            home_plz=query.home_plz,
            max_distance_km=query.max_distance_km or 50.0,
            plz_table=plz_table,
        )
        results.append(
            ScoredListing(summary=summary, fields=extracted, catalog_match=catalog_match, score=breakdown)
        )

    results.sort(key=lambda r: r.score.total_score, reverse=True)
    return results
