"""Dev runner: exercise the pipeline against saved fixtures, against the
live site, or end-to-end with a real natural-language query — no Telegram
needed for any of it.
"""

from __future__ import annotations

import sys
import time

from config import Settings
from core import catalog, distance, parse
from core.pipeline import build_extracted_listing, run_search
from core.scorer import score
from models import CatalogMatch, ExtractedListing, ScoreBreakdown, StructuredQuery
from services.fetch import PageType, RequestGate, SeenIds, fetch_page
from services.llm import OpenAICompatProvider, parse_query

# Detail fixtures we have saved HTML for. Used by `run-fixtures` to prove the
# offline core (parse -> catalog match -> score) works with zero network/LLM.
_DETAIL_FIXTURES = ["tv_listing_normal.html", "tv_listing_scam.html"]

_TV_SEARCH_URL = "https://www.kleinanzeigen.de/s-fernseher/k0"


def run_fixtures() -> None:
    settings = Settings()
    tv_catalog = catalog.load_catalog(settings.catalog_path)
    plz_table = distance.load_plz_table(settings.plz_csv_path)

    with open("fixtures/tv_search_results.html", encoding="utf-8") as f:
        search_html = f.read()
    listings = parse.parse_search_results(search_html)
    print(f"Parsed {len(listings)} listings from the search-results fixture.\n")

    results: list[tuple[str, ExtractedListing, CatalogMatch, ScoreBreakdown]] = []
    for fixture_name in _DETAIL_FIXTURES:
        with open(f"fixtures/{fixture_name}", encoding="utf-8") as f:
            html = f.read()
        extracted, page = build_extracted_listing(fixture_name, html)

        catalog_match = catalog.match(page.title, page.description, tv_catalog)
        if catalog_match is None:
            print(f"SKIPPED (no catalog match): {page.title}")
            continue

        breakdown = score(
            extracted,
            catalog_match,
            home_plz=settings.home_plz,
            max_distance_km=settings.default_max_distance_km,
            plz_table=plz_table,
        )
        results.append((page.title, extracted, catalog_match, breakdown))
        print(f"MATCHED: {page.title}")
        print(f"  catalog model: {catalog_match.model_name} (ref. €{catalog_match.fair_used_price_eur:.0f})")
        print(f"  price: €{extracted.price_eur.value}")
        print(f"  total score: {breakdown.total_score:.1f}")
        for line in breakdown.explanation:
            print(f"    - {line}")
        print()

    results.sort(key=lambda r: r[3].total_score, reverse=True)
    print("--- Ranked ---")
    for title, extracted, catalog_match, breakdown in results:
        print(f"{breakdown.total_score:5.1f}  {catalog_match.model_name}  (€{extracted.price_eur.value})  — {title}")


def run_live() -> None:
    """Phase 3 manual check: fetch a couple of real pages through the real
    choke point, with a tiny cap, and prove pacing/caching/budget work."""
    settings = Settings()
    gate = RequestGate(
        max_requests_per_run=settings.max_requests_per_run,
        delay_floor_search_s=settings.fetch_delay_floor_search_s,
        delay_floor_detail_s=settings.fetch_delay_floor_detail_s,
        jitter_s=settings.fetch_delay_jitter_s,
        max_consecutive_failures=settings.max_consecutive_failures,
    )

    urls: list[tuple[str, PageType]] = [
        (_TV_SEARCH_URL, "search"),
        (f"{_TV_SEARCH_URL}/seite:2", "search"),
        (f"{_TV_SEARCH_URL}/seite:3", "search"),
    ]
    for url, page_type in urls:
        start = time.monotonic()
        try:
            html = fetch_page(
                url, page_type, gate,
                cache_dir=settings.cache_dir,
                dry_run=settings.dry_run,
                kill_switch=settings.kill_switch,
            )
        except Exception as exc:  # noqa: BLE001 - this is a manual smoke-test script
            print(f"[{time.monotonic() - start:5.1f}s] {page_type:6} {url} -> {type(exc).__name__}: {exc}")
            continue
        elapsed = time.monotonic() - start
        print(f"[{elapsed:5.1f}s] {page_type:6} {url} -> {len(html)} bytes (requests this run: {gate.requests_this_run})")


def run_full_search(raw_message: str) -> None:
    """The full pipeline end-to-end: LLM query parsing -> live/cached search
    -> catalog match -> score -> rank. This is exactly what bot/app.py's
    /search handler does, minus the Telegram plumbing."""
    settings = Settings()
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

    provider = OpenAICompatProvider(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    query: StructuredQuery = parse_query(raw_message, settings.home_plz, provider)
    print(f"Parsed query: {query.model_dump_json(indent=2)}\n")

    def fetcher(url: str, page_type: PageType) -> str:
        return fetch_page(
            url, page_type, gate,
            cache_dir=settings.cache_dir, dry_run=settings.dry_run, kill_switch=settings.kill_switch,
        )

    results = run_search(
        query, search_url=_TV_SEARCH_URL, tv_catalog=tv_catalog, plz_table=plz_table,
        seen_ids=seen_ids, fetcher=fetcher,
    )

    if not results:
        print("No matching deals found.")
        return
    for r in results[:5]:
        print(f"{r.score.total_score:5.1f}  {r.catalog_match.model_name}  €{r.fields.price_eur.value}  {r.summary.url}")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "run-fixtures"
    if command == "run-fixtures":
        run_fixtures()
    elif command == "run-live":
        run_live()
    elif command == "run-search":
        run_full_search(" ".join(sys.argv[2:]))
    else:
        print(f"unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
