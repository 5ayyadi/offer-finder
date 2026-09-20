"""End-to-end: a raw Telegram-style message goes through LLM query parsing
(stubbed — no network), then the real pipeline against saved HTML fixtures
(no network), producing a deterministic final ranking and reply text.
"""

from bot.app import format_results
from core import catalog, distance
from core.pipeline import run_search
from services.fetch import SeenIds
from services.llm import parse_query
from tests.conftest import load_fixture

_SEARCH_URL = "https://www.kleinanzeigen.de/s-fernseher/k0"


class _StubLLMProvider:
    def complete_json(self, system_prompt, user_prompt, json_schema):
        return {
            "max_price_eur": None,
            "min_price_eur": None,
            "min_screen_size_inch": None,
            "preferred_brands": [],
            "plz": "55128",
            "location_text_raw": None,
            "max_distance_km": 50,
        }


def _fake_fetcher(url: str, _page_type: str) -> str:
    if url == _SEARCH_URL:
        return load_fixture("e2e_search_results.html")
    if "grundig" in url:
        return load_fixture("tv_listing_normal.html")
    if "samsung-qn90b" in url:
        return load_fixture("tv_listing_scam.html")
    raise AssertionError(f"unexpected URL fetched: {url}")


def test_full_pipeline_from_raw_message_to_reply_text(tmp_path):
    raw_message = "Find me good TVs around 55128"

    query = parse_query(raw_message, home_plz_default="10115", provider=_StubLLMProvider())
    assert query.home_plz == "55128"

    seen_ids = SeenIds(str(tmp_path / "seen.json"))
    results = run_search(
        query,
        search_url=_SEARCH_URL,
        tv_catalog=catalog.load_catalog("catalog_tv.yaml"),
        plz_table=distance.load_plz_table("data/plz_de.csv"),
        seen_ids=seen_ids,
        fetcher=_fake_fetcher,
    )

    # deterministic ranking: the clean, genuinely underpriced listing beats
    # the equally-cheap-looking one riddled with scam red flags
    assert [r.catalog_match.model_name for r in results] == [
        "Grundig 43 GUS 8768",
        "Samsung QN90B 65",
    ]

    reply = format_results(results)
    assert "Grundig 43 GUS 8768" in reply
    assert reply.index("Grundig 43 GUS 8768") < reply.index("Samsung QN90B 65")
