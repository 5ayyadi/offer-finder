from typing import Any

from core import catalog, distance
from core.pipeline import run_search
from models import StructuredQuery
from services.fetch import SeenIds
from tests.conftest import load_fixture

_SEARCH_URL = "https://www.kleinanzeigen.de/s-fernseher/k0"


def _fake_fetcher(url: str, _page_type: str) -> str:
    if url == _SEARCH_URL:
        return load_fixture("e2e_search_results.html")
    if "grundig" in url:
        return load_fixture("tv_listing_normal.html")
    if "samsung-qn90b" in url:
        return load_fixture("tv_listing_scam.html")
    raise AssertionError(f"unexpected URL fetched: {url}")


def _query(**overrides: Any) -> StructuredQuery:
    defaults: dict[str, Any] = {
        "max_price_eur": None,
        "min_price_eur": None,
        "min_screen_size_inch": None,
        "preferred_brands": [],
        "home_plz": "55128",
        "max_distance_km": 50.0,
        "raw_user_message": "any TV deal",
    }
    defaults.update(overrides)
    return StructuredQuery(**defaults)


def _catalog():
    return catalog.load_catalog("catalog_tv.yaml")


def _plz_table():
    return distance.load_plz_table("data/plz_de.csv")


def test_pipeline_ranks_clean_deal_above_scam_flagged_one(tmp_path):
    seen_ids = SeenIds(str(tmp_path / "seen.json"))
    results = run_search(
        _query(),
        search_url=_SEARCH_URL,
        tv_catalog=_catalog(),
        plz_table=_plz_table(),
        seen_ids=seen_ids,
        fetcher=_fake_fetcher,
    )

    assert [r.catalog_match.model_name for r in results] == [
        "Grundig 43 GUS 8768",
        "Samsung QN90B 65",
    ]
    assert results[0].score.total_score > results[1].score.total_score
    assert results[1].score.scam_penalty > 0


def test_pipeline_marks_fetched_listings_as_seen(tmp_path):
    seen_path = str(tmp_path / "seen.json")
    seen_ids = SeenIds(seen_path)
    run_search(
        _query(), search_url=_SEARCH_URL, tv_catalog=_catalog(), plz_table=_plz_table(),
        seen_ids=seen_ids, fetcher=_fake_fetcher,
    )

    reloaded = SeenIds(seen_path)
    assert reloaded.has("3518580533") is True
    assert reloaded.has("9999999901") is True


def test_pipeline_skips_already_seen_listings(tmp_path):
    seen_ids = SeenIds(str(tmp_path / "seen.json"))
    seen_ids.add("3518580533")

    fetched_urls = []

    def counting_fetcher(url: str, page_type: str) -> str:
        fetched_urls.append(url)
        return _fake_fetcher(url, page_type)

    results = run_search(
        _query(), search_url=_SEARCH_URL, tv_catalog=_catalog(), plz_table=_plz_table(),
        seen_ids=seen_ids, fetcher=counting_fetcher,
    )

    assert all("grundig" not in u for u in fetched_urls)
    assert [r.catalog_match.model_name for r in results] == ["Samsung QN90B 65"]


def test_pipeline_applies_cheap_price_filter_before_fetching_detail(tmp_path):
    seen_ids = SeenIds(str(tmp_path / "seen.json"))
    fetched_urls = []

    def counting_fetcher(url: str, page_type: str) -> str:
        fetched_urls.append(url)
        return _fake_fetcher(url, page_type)

    results = run_search(
        _query(min_price_eur=110),  # excludes the 99€ Grundig listing by its search-result price
        search_url=_SEARCH_URL,
        tv_catalog=_catalog(),
        plz_table=_plz_table(),
        seen_ids=seen_ids,
        fetcher=counting_fetcher,
    )

    assert all("grundig" not in u for u in fetched_urls)
    assert [r.catalog_match.model_name for r in results] == ["Samsung QN90B 65"]
