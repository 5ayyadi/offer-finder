"""Real wiring (Settings -> fetch_page/RequestGate -> pipeline -> SQLite),
kept offline by DRY_RUN serving fixture HTML from a temporary cache."""

from typing import Any

import db
import runner
from config import Settings
from services.fetch import _cache_write
from services.search_url import build_search_url
from tests.conftest import load_fixture, make_spec
from tests.test_pipeline import GRUNDIG_ID, SAMSUNG_ID, ScriptedLLM

_DETAIL_URLS = {
    GRUNDIG_ID: "https://www.kleinanzeigen.de/s-anzeige/grundig-43-gus-fernseher-43-zoll-4k-uhd-led-smart-tv/3518580533-175-1027",
    SAMSUNG_ID: "https://www.kleinanzeigen.de/s-anzeige/samsung-qn90b-65-zoll-tv-bastler/9999999901-175-1234",
}


def _settings(tmp_path, **overrides) -> Settings:
    values: dict[str, Any] = {
        "db_path": str(tmp_path / "test.db"),
        "cache_dir": str(tmp_path / "cache"),
        "dry_run": True,
        "llm_min_interval_s": 0.0,
        "home_plz": "57074",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _spec():
    return make_spec(query="Fernseher", plz="57074", radius_km=None, max_price_eur=200)


def _cache_fixtures(settings: Settings) -> None:
    _cache_write(settings.cache_dir, build_search_url(_spec()), load_fixture("e2e_search_results.html"))
    _cache_write(settings.cache_dir, _DETAIL_URLS[GRUNDIG_ID], load_fixture("tv_listing_normal.html"))
    _cache_write(settings.cache_dir, _DETAIL_URLS[SAMSUNG_ID], load_fixture("tv_listing_scam.html"))


def test_run_search_once_goes_through_real_fetch_layer_into_the_db(tmp_path):
    settings = _settings(tmp_path)
    _cache_fixtures(settings)

    outcome = runner.run_search_once(settings, ScriptedLLM(), chat_id=42, spec=_spec())

    assert outcome.error is None
    assert (outcome.report.found, outcome.report.evaluated, outcome.report.recommended) == (2, 2, 1)
    conn = db.connect(settings.db_path)
    assert db.get_search_spec(conn, outcome.search_spec_id) == _spec()
    assert [listing.listing_id for listing, _, _ in db.pending_notifications(conn, 42)] == [GRUNDIG_ID]


def test_uncached_search_in_dry_run_explains_how_to_go_live(tmp_path):
    outcome = runner.run_search_once(_settings(tmp_path), ScriptedLLM(), chat_id=42, spec=_spec())
    assert outcome.error is not None and "DRY_RUN" in outcome.error


def test_kill_switch_stops_the_search(tmp_path):
    settings = _settings(tmp_path, kill_switch=True)
    _cache_fixtures(settings)
    outcome = runner.run_search_once(settings, ScriptedLLM(), chat_id=42, spec=_spec())
    assert outcome.error is not None and "KILL_SWITCH" in outcome.error


def test_unreadable_search_page_is_reported_as_layout_change(tmp_path):
    settings = _settings(tmp_path)
    _cache_write(settings.cache_dir, build_search_url(_spec()), "<html>" + "x" * 2000 + "</html>")
    outcome = runner.run_search_once(settings, ScriptedLLM(), chat_id=42, spec=_spec())
    assert outcome.error is not None and "layout" in outcome.error


def test_make_provider_uses_configured_model_and_format(tmp_path):
    provider = runner.make_provider(_settings(tmp_path, llm_model="m-1", llm_response_format="json_object"))
    assert provider.model == "m-1"
