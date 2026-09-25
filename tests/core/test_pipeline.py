"""Offline end-to-end: fixture HTML + scripted LLM + a real (temporary)
SQLite DB. Nothing here touches the network."""


import logging

from offer_finder.core.models import ListingRef
from offer_finder.core.pipeline import Pipeline
from offer_finder.services.fetch import BudgetExhausted, FetchFailed
from offer_finder.storage import db
from tests.helpers.factories import make_spec
from tests.helpers.fakes import GRUNDIG_ID, SAMSUNG_ID, FakeFetcher, ScriptedLLM

PLZ_TABLE = {"57074": (50.87, 8.02), "10115": (52.53, 13.38)}


def _pipeline(conn, fetcher=None, llm=None, sleeps=None, interval=0.0):
    return Pipeline(
        conn=conn,
        fetcher=fetcher or FakeFetcher(),
        provider=llm or ScriptedLLM(),
        model_name="scripted",
        plz_table=PLZ_TABLE,
        home_plz="57074",
        threshold=70,
        llm_min_interval_s=interval,
        sleep_fn=(sleeps.append if sleeps is not None else lambda _s: None),
    )


def _spec(conn, **overrides):
    values = {"query": "Fernseher", "plz": "57074", "radius_km": None, "max_price_eur": 200, **overrides}
    spec = make_spec(**values)
    return db.insert_search_spec(conn, 42, spec), spec


def _status(conn, listing_id):
    listing = db.get_listing(conn, listing_id)
    assert listing is not None
    return listing.status


# --- interactive search path ---------------------------------------------------


def test_search_logs_each_step_and_every_verdict(conn, caplog):
    spec_id, spec = _spec(conn)
    with caplog.at_level(logging.INFO):
        _pipeline(conn).run_search(spec_id, spec)
    log = caplog.text
    assert f"search {spec_id}: results page has 2 listings (2 new, 0 outside price/radius)" in log
    assert f"search {spec_id}: fetching 2 detail pages" in log
    assert f"search {spec_id}: evaluating 2 listings" in log
    verdicts = [r.getMessage() for r in caplog.records if "/100 " in r.getMessage()]
    assert verdicts[0].startswith(f'search {spec_id}: 85/100 {GRUNDIG_ID} "Grundig')
    assert verdicts[0].endswith("99 € → recommended")
    assert verdicts[1].startswith(f"search {spec_id}: 20/100 {SAMSUNG_ID}")
    assert verdicts[1].endswith("→ skipped: below 70")


def test_progress_counts_up_through_detail_pages_then_evaluations(conn):
    spec_id, spec = _spec(conn)
    events = []
    pipeline = _pipeline(conn)
    pipeline.on_progress = events.append
    pipeline.run_search(spec_id, spec)

    assert [(e.stage, e.done, e.total) for e in events] == [
        ("fetching", 0, 2), ("fetching", 1, 2), ("fetching", 2, 2),
        ("evaluating", 0, 2), ("evaluating", 1, 2), ("evaluating", 2, 2),
    ]
    assert events[-1].report.recommended == 1
    assert events[0].report.found == 2


def test_budget_stop_is_logged_as_a_warning(conn, caplog):
    spec_id, spec = _spec(conn)
    fetcher = FakeFetcher(fail={SAMSUNG_ID: BudgetExhausted("cap of 2 reached")})
    with caplog.at_level(logging.INFO):
        _pipeline(conn, fetcher=fetcher).run_search(spec_id, spec)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(f"search {spec_id}: request budget reached" in message for message in warnings)


def test_search_stores_fetches_and_evaluates_every_card(conn):
    spec_id, spec = _spec(conn)
    report = _pipeline(conn).run_search(spec_id, spec)

    assert (report.found, report.new, report.fetched, report.evaluated) == (2, 2, 2, 2)
    assert report.recommended == 1
    assert report.best_score == 85
    grundig = db.get_listing(conn, GRUNDIG_ID)
    assert grundig is not None
    assert grundig.status == "fetched"
    assert grundig.description is not None and "4K Ultra HD" in grundig.description
    assert len(grundig.image_urls) == 2
    assert grundig.source == "search" and grundig.search_spec_id == spec_id


def test_only_the_good_listing_becomes_a_pending_notification(conn):
    spec_id, spec = _spec(conn)
    _pipeline(conn).run_search(spec_id, spec)
    pending = db.pending_notifications(conn, chat_id=42)
    assert [listing.listing_id for listing, _, _ in pending] == [GRUNDIG_ID]


def test_card_outside_price_range_is_stored_but_never_fetched_or_evaluated(conn):
    spec_id, spec = _spec(conn, max_price_eur=110)  # Samsung costs 120
    fetcher, llm = FakeFetcher(), ScriptedLLM()
    report = _pipeline(conn, fetcher, llm).run_search(spec_id, spec)

    assert report.out_of_range == 1
    assert _status(conn, SAMSUNG_ID) == "out_of_range"
    assert all(SAMSUNG_ID not in url for url in fetcher.detail_calls())
    assert llm.calls == 1


def test_wider_spec_later_fetches_a_listing_that_was_out_of_range(conn):
    narrow_id, narrow = _spec(conn, max_price_eur=110)
    _pipeline(conn).run_search(narrow_id, narrow)
    wide_id, wide = _spec(conn, max_price_eur=500)
    fetcher = FakeFetcher()
    _pipeline(conn, fetcher).run_search(wide_id, wide)
    assert _status(conn, SAMSUNG_ID) == "fetched"
    assert fetcher.detail_calls() == [u for u in fetcher.detail_calls() if SAMSUNG_ID in u]


def test_rerun_never_refetches_details_or_reevaluates(conn):
    spec_id, spec = _spec(conn)
    fetcher, llm = FakeFetcher(), ScriptedLLM()
    pipeline = _pipeline(conn, fetcher, llm)
    pipeline.run_search(spec_id, spec)
    second = pipeline.run_search(spec_id, spec)

    assert len(fetcher.detail_calls()) == 2
    assert llm.calls == 2
    assert (second.new, second.evaluated) == (0, 0)


def test_failed_evaluation_stays_pending_and_is_retried_next_run(conn):
    spec_id, spec = _spec(conn)
    llm = ScriptedLLM(fail_times=2)  # first run: both evaluations hit a 429
    first = _pipeline(conn, llm=llm).run_search(spec_id, spec)
    assert (first.evaluated, first.evaluation_failed) == (0, 2)

    second = _pipeline(conn, llm=llm).run_search(spec_id, spec)
    assert second.evaluated == 2


def test_budget_exhaustion_keeps_remaining_listings_discovered_for_next_run(conn):
    spec_id, spec = _spec(conn)
    fetcher = FakeFetcher(fail={SAMSUNG_ID: BudgetExhausted("cap")})
    report = _pipeline(conn, fetcher).run_search(spec_id, spec)

    assert report.stopped_reason is not None and "cap" in report.stopped_reason
    assert _status(conn, SAMSUNG_ID) == "discovered"
    assert report.evaluated == 1  # what was fetched still gets evaluated

    _pipeline(conn).run_search(spec_id, spec)
    assert _status(conn, SAMSUNG_ID) == "fetched"


def test_failed_detail_fetch_is_recorded(conn):
    spec_id, spec = _spec(conn)
    report = _pipeline(conn, FakeFetcher(fail={SAMSUNG_ID: FetchFailed("blocked")})).run_search(spec_id, spec)
    assert report.fetch_failed == 1
    assert _status(conn, SAMSUNG_ID) == "fetch_failed"


def test_llm_calls_are_paced(conn):
    spec_id, spec = _spec(conn)
    sleeps: list[float] = []
    _pipeline(conn, sleeps=sleeps, interval=4.0).run_search(spec_id, spec)
    assert sleeps == [4.0]  # two evaluations, one pause between them


def test_card_outside_radius_is_stored_but_never_fetched_or_evaluated(conn):
    # Kleinanzeigen appends far-away results after the in-radius ones
    spec_id, spec = _spec(conn, radius_km=50)  # around Siegen; the Samsung card says Berlin
    fetcher, llm = FakeFetcher(), ScriptedLLM()
    report = _pipeline(conn, fetcher, llm).run_search(spec_id, spec)

    assert report.out_of_range == 1
    assert _status(conn, SAMSUNG_ID) == "out_of_range"
    assert all(SAMSUNG_ID not in url for url in fetcher.detail_calls())
    assert llm.calls == 1


def test_detail_page_contradicting_the_card_still_fails_the_hard_check(conn):
    # the Samsung card says Berlin, but its detail page says Siegen (far outside 10 km of Berlin)
    spec = make_spec(query="Fernseher", plz="10115", radius_km=10, max_price_eur=200)
    spec_id = db.insert_search_spec(conn, 42, spec)
    report = _pipeline(conn).run_search(spec_id, spec)
    assert (report.out_of_range, report.evaluated, report.recommended) == (1, 1, 0)


# --- gmail path --------------------------------------------------------------------


def _grundig_ref():
    return ListingRef(
        listing_id=GRUNDIG_ID,
        url=f"https://www.kleinanzeigen.de/s-anzeige/grundig-43-gus-fernseher/{GRUNDIG_ID}-175-1027",
    )


def test_email_listing_is_stored_fetched_and_evaluated_without_spec(conn):
    report = _pipeline(conn).process_email([_grundig_ref()], None, None)
    listing = db.get_listing(conn, GRUNDIG_ID)
    assert listing is not None
    assert (listing.source, listing.status) == ("gmail", "fetched")
    assert listing.title.startswith("Grundig 43 GUS")  # replaced by the detail page title
    assert report.evaluated == 1 and report.recommended == 1
    assert len(db.pending_notifications(conn, 42)) == 1


def test_email_listing_already_found_by_search_is_not_fetched_again(conn):
    spec_id, spec = _spec(conn)
    _pipeline(conn).run_search(spec_id, spec)
    fetcher, llm = FakeFetcher(), ScriptedLLM()
    report = _pipeline(conn, fetcher, llm).process_email([_grundig_ref()], spec_id, spec)
    assert fetcher.calls == []
    assert llm.calls == 0
    assert report.new == 0
