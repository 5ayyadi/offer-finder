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


# --- Gmail poll ----------------------------------------------------------------

from services.fetch import BudgetExhausted
from services.gmail import EmailMessage
from tests.test_pipeline import _status

_EMAIL_DETAIL_URLS = {
    GRUNDIG_ID: "https://www.kleinanzeigen.de/s-anzeige/lg-oled65c27la-65-zoll/3518580533-175-1027",
    SAMSUNG_ID: "https://www.kleinanzeigen.de/s-anzeige/sony-bravia-a80j-65/9999999901-175-4321",
}


class FakeGmail:
    def __init__(self, messages: dict[str, EmailMessage]):
        self._messages = messages
        self.fetched: list[str] = []
        self.queries: list[str] = []

    def list_message_ids(self, query):
        self.queries.append(query)
        return list(self._messages)

    def get_message(self, message_id):
        self.fetched.append(message_id)
        return self._messages[message_id]


def _alert_gmail() -> FakeGmail:
    subject = "Neue Anzeigen für deine Suche „oled 65“"
    return FakeGmail({"m1": EmailMessage("m1", subject, load_fixture("gmail_alert_email.html"))})


def _cache_email_details(settings: Settings) -> None:
    _cache_write(settings.cache_dir, _EMAIL_DETAIL_URLS[GRUNDIG_ID], load_fixture("tv_listing_normal.html"))
    _cache_write(settings.cache_dir, _EMAIL_DETAIL_URLS[SAMSUNG_ID], load_fixture("tv_listing_scam.html"))


def test_gmail_poll_stores_and_evaluates_email_listings(tmp_path):
    settings = _settings(tmp_path)
    _cache_email_details(settings)
    gmail = _alert_gmail()

    outcome = runner.poll_gmail_once(settings, ScriptedLLM(), gmail)

    assert outcome.error is None
    assert outcome.messages == 1
    assert (outcome.report.found, outcome.report.evaluated, outcome.report.recommended) == (2, 2, 1)
    assert gmail.queries == [settings.gmail_query]
    conn = db.connect(settings.db_path)
    listing = db.get_listing(conn, GRUNDIG_ID)
    assert listing is not None and (listing.source, listing.status) == ("gmail", "fetched")
    assert db.gmail_message_seen(conn, "m1")


def test_gmail_poll_uses_the_matching_spec(tmp_path):
    settings = _settings(tmp_path)
    _cache_email_details(settings)
    conn = runner.open_db(settings)
    spec_id = db.insert_search_spec(conn, 42, make_spec(query="OLED 65", plz="57074", radius_km=None))
    conn.close()

    runner.poll_gmail_once(settings, ScriptedLLM(), _alert_gmail())

    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT search_spec_id FROM evaluations WHERE listing_id = ?", (GRUNDIG_ID,)).fetchone()
    assert row["search_spec_id"] == spec_id


def test_processed_email_is_not_read_again(tmp_path):
    settings = _settings(tmp_path)
    _cache_email_details(settings)
    runner.poll_gmail_once(settings, ScriptedLLM(), _alert_gmail())

    gmail, llm = _alert_gmail(), ScriptedLLM()
    outcome = runner.poll_gmail_once(settings, llm, gmail)
    assert outcome.messages == 0
    assert gmail.fetched == [] and llm.calls == 0


def test_email_cut_short_by_the_budget_is_resumed_next_poll(tmp_path):
    settings = _settings(tmp_path)
    budget_left = {"pages": 1}

    def fetcher(url, page_type):
        if budget_left["pages"] == 0:
            raise BudgetExhausted("cap")
        budget_left["pages"] -= 1
        return load_fixture("tv_listing_normal.html" if GRUNDIG_ID in url else "tv_listing_scam.html")

    first = runner.poll_gmail_once(settings, ScriptedLLM(), _alert_gmail(), fetcher=fetcher)
    assert first.report.stopped_reason is not None
    conn = db.connect(settings.db_path)
    assert not db.gmail_message_seen(conn, "m1")
    assert _status(conn, SAMSUNG_ID) == "discovered"

    budget_left["pages"] = 5
    second = runner.poll_gmail_once(settings, ScriptedLLM(), _alert_gmail(), fetcher=fetcher)
    assert second.report.stopped_reason is None
    assert _status(conn, SAMSUNG_ID) == "fetched"
    assert db.gmail_message_seen(conn, "m1")


def test_email_without_listing_links_is_counted_so_format_changes_are_visible(tmp_path):
    gmail = FakeGmail({"m9": EmailMessage("m9", "Deine Anzeige läuft ab", "<p>no links</p>")})
    outcome = runner.poll_gmail_once(_settings(tmp_path), ScriptedLLM(), gmail)
    assert outcome.emails_without_listings == 1
