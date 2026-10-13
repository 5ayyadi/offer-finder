"""Real wiring (Settings -> fetch_page/RequestGate -> pipeline -> SQLite),
kept offline by DRY_RUN serving fixture HTML from a temporary cache."""

import logging

from offer_finder import runner
from offer_finder.services.fetch import BudgetExhausted, _cache_write
from offer_finder.services.gmail import EmailMessage
from offer_finder.services.search_url import build_search_url
from offer_finder.storage import db
from tests.helpers.factories import load_fixture, make_spec
from tests.helpers.fakes import GRUNDIG_ID, SAMSUNG_ID, FakeGmail, ScriptedLLM, alert_gmail
from tests.helpers.offline import (
    cache_email_details,
    cache_search_fixtures,
    offline_settings,
    offline_spec,
)


def test_run_search_once_goes_through_real_fetch_layer_into_the_db(tmp_path):
    settings = offline_settings(tmp_path)
    cache_search_fixtures(settings)

    outcome = runner.run_search_once(settings, ScriptedLLM(), chat_id=42, spec=offline_spec())

    assert outcome.error is None
    assert (outcome.report.found, outcome.report.evaluated, outcome.report.recommended) == (2, 2, 1)
    conn = db.connect(settings.db_path)
    assert db.get_search_spec(conn, outcome.search_spec_id) == offline_spec()
    assert [listing.listing_id for listing, _, _ in db.pending_notifications(conn, 42, is_admin=True)] == [GRUNDIG_ID]


def test_uncached_search_in_dry_run_explains_how_to_go_live(tmp_path):
    outcome = runner.run_search_once(offline_settings(tmp_path), ScriptedLLM(), chat_id=42, spec=offline_spec())
    assert outcome.error is not None and "DRY_RUN" in outcome.error


def test_kill_switch_stops_the_search(tmp_path):
    settings = offline_settings(tmp_path, kill_switch=True)
    cache_search_fixtures(settings)
    outcome = runner.run_search_once(settings, ScriptedLLM(), chat_id=42, spec=offline_spec())
    assert outcome.error is not None and "KILL_SWITCH" in outcome.error


def test_unreadable_search_page_is_reported_as_layout_change(tmp_path):
    settings = offline_settings(tmp_path)
    _cache_write(settings.cache_dir, build_search_url(offline_spec()), "<html>" + "x" * 2000 + "</html>")
    outcome = runner.run_search_once(settings, ScriptedLLM(), chat_id=42, spec=offline_spec())
    assert outcome.error is not None and "layout" in outcome.error


def test_make_provider_uses_configured_model_and_format(tmp_path):
    provider = runner.make_provider(offline_settings(tmp_path, llm_model="m-1", llm_response_format="json_object"))
    assert provider.model == "m-1"


# --- Gmail poll ----------------------------------------------------------------


def test_gmail_poll_stores_and_evaluates_email_listings(tmp_path):
    settings = offline_settings(tmp_path)
    cache_email_details(settings)
    gmail = alert_gmail()

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
    settings = offline_settings(tmp_path, telegram_allowlist=[42])
    cache_email_details(settings)
    conn = runner.open_db(settings)
    spec_id = db.insert_search_spec(conn, 42, make_spec(query="OLED 65", plz="57074", radius_km=None))
    conn.close()

    runner.poll_gmail_once(settings, ScriptedLLM(), alert_gmail())

    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT search_spec_id FROM evaluations WHERE listing_id = ?", (GRUNDIG_ID,)).fetchone()
    assert row["search_spec_id"] == spec_id


def test_alert_email_is_never_matched_to_a_friends_search(tmp_path):
    """Alert emails come from the admin's inbox: a friend's search with the
    same words must not receive them."""
    settings = offline_settings(tmp_path, telegram_allowlist=[42])
    cache_email_details(settings)
    conn = runner.open_db(settings)
    friend_spec = db.insert_search_spec(conn, 7, make_spec(query="OLED 65", plz="57074", radius_km=None))
    conn.close()

    runner.poll_gmail_once(settings, ScriptedLLM(), alert_gmail())

    conn = db.connect(settings.db_path)
    rows = conn.execute("SELECT search_spec_id FROM evaluations").fetchall()
    assert rows and all(row["search_spec_id"] is None for row in rows)
    assert db.evaluations_for_spec(conn, friend_spec) == []


def test_open_db_keeps_the_admins_in_sync_with_the_allowlist(tmp_path):
    conn = runner.open_db(offline_settings(tmp_path, telegram_allowlist=[42]))
    admin = db.get_user(conn, 42)
    assert admin is not None and admin.is_admin and admin.status == "approved"


def test_processed_email_is_not_read_again(tmp_path):
    settings = offline_settings(tmp_path)
    cache_email_details(settings)
    runner.poll_gmail_once(settings, ScriptedLLM(), alert_gmail())

    gmail, llm = alert_gmail(), ScriptedLLM()
    outcome = runner.poll_gmail_once(settings, llm, gmail)
    assert outcome.messages == 0
    assert gmail.fetched == [] and llm.calls == 0


def test_email_cut_short_by_the_budget_is_resumed_next_poll(tmp_path):
    settings = offline_settings(tmp_path)
    budget_left = {"pages": 1}

    def fetcher(url, page_type):
        if budget_left["pages"] == 0:
            raise BudgetExhausted("cap")
        budget_left["pages"] -= 1
        return load_fixture("tv_listing_normal.html" if GRUNDIG_ID in url else "tv_listing_scam.html")

    first = runner.poll_gmail_once(settings, ScriptedLLM(), alert_gmail(), fetcher=fetcher)
    assert first.report.stopped_reason is not None
    conn = db.connect(settings.db_path)
    assert not db.gmail_message_seen(conn, "m1")
    assert db.get_listing(conn, SAMSUNG_ID).status == "discovered"

    budget_left["pages"] = 5
    second = runner.poll_gmail_once(settings, ScriptedLLM(), alert_gmail(), fetcher=fetcher)
    assert second.report.stopped_reason is None
    assert db.get_listing(conn, SAMSUNG_ID).status == "fetched"
    assert db.gmail_message_seen(conn, "m1")


def test_each_new_email_is_logged_with_its_listings_and_spec(tmp_path, caplog):
    settings = offline_settings(tmp_path)
    cache_email_details(settings)
    with caplog.at_level(logging.INFO):
        runner.poll_gmail_once(settings, ScriptedLLM(), alert_gmail())
    assert "gmail: 1 new email(s)" in caplog.text
    assert "email m1: 2 listing(s), no matching spec" in caplog.text


# --- logging setup ----------------------------------------------------------------


def test_setup_logging_writes_a_log_file_without_the_bot_token(tmp_path):
    log_path = tmp_path / "logs" / "offer_finder.log"
    settings = offline_settings(tmp_path, log_path=str(log_path))
    runner.setup_logging(settings)
    runner.setup_logging(settings)  # calling it again must not duplicate lines

    logging.getLogger("offer_finder.core.pipeline").info("search 7: fetching 3 detail pages")
    logging.getLogger("httpx").info("POST https://api.telegram.org/bot123:SECRET/getUpdates")

    text = log_path.read_text(encoding="utf-8")
    assert text.count("search 7: fetching 3 detail pages") == 1
    assert "SECRET" not in text


def test_log_level_debug_shows_the_detail_lines(tmp_path):
    log_path = tmp_path / "offer_finder.log"
    runner.setup_logging(offline_settings(tmp_path, log_path=str(log_path), log_level="DEBUG"))
    logging.getLogger("offer_finder.services.fetch").debug("cache hit https://example")
    assert "cache hit https://example" in log_path.read_text(encoding="utf-8")


def test_email_without_listing_links_is_counted_so_format_changes_are_visible(tmp_path):
    gmail = FakeGmail({"m9": EmailMessage("m9", "Deine Anzeige läuft ab", "<p>no links</p>")})
    outcome = runner.poll_gmail_once(offline_settings(tmp_path), ScriptedLLM(), gmail)
    assert outcome.emails_without_listings == 1
