"""Offline end-to-end of the search flow behind the Telegram bot:
confirmed spec -> real fetch layer (DRY_RUN cache) -> DB -> evaluation ->
Telegram messages (fake bot)."""

import asyncio
import logging
import re

from telegram.ext import ConversationHandler

from offer_finder.bot.app import build_application, search_and_notify
from offer_finder.bot.offers import show_offer
from tests.helpers.fakes import FakeBot, ScriptedLLM, alert_gmail
from tests.helpers.offline import (
    cache_email_details,
    cache_search_fixtures,
    offline_settings,
    offline_spec,
)


def _run_search(settings, bot, llm=None):
    asyncio.run(search_and_notify(bot, settings, llm or ScriptedLLM(), asyncio.Lock(), 42, offline_spec()))


def _messages(bot):
    """New messages only; edits of the status message are left out."""
    return [(kind, payload) for kind, payload in bot.sent if kind != "edit"]


def _final_status(bot) -> str:
    edits = [payload for kind, payload in bot.sent if kind == "edit"]
    assert edits and {edit["message_id"] for edit in edits} == {1}  # always the first message
    return edits[-1]["text"]


def test_confirmed_search_sends_a_pick_list_then_a_summary(tmp_path):
    settings = offline_settings(tmp_path)
    cache_search_fixtures(settings)
    bot = FakeBot()
    _run_search(settings, bot)

    messages = _messages(bot)
    assert [kind for kind, _ in messages] == ["message", "message", "message"]
    assert messages[0][1]["text"].startswith("🔎 Searching Kleinanzeigen")  # the live status message
    pick_list = messages[1][1]
    assert pick_list["text"].startswith('🔎 1 pick for "Fernseher"')
    assert "85/100" in pick_list["text"] and "Grundig" in pick_list["text"]
    assert [b.text for row in pick_list["reply_markup"].inline_keyboard for b in row] == ["1"]
    summary = messages[2][1]["text"]
    assert "Checked 2 listings: 2 evaluated, 1 recommended." in summary


def test_status_message_ends_as_a_finished_checklist(tmp_path):
    settings = offline_settings(tmp_path)
    cache_search_fixtures(settings)
    bot = FakeBot()
    _run_search(settings, bot)
    final = _final_status(bot)
    assert "✅ Results page: 2 listings" in final
    assert "✅ Read 2 listing pages" in final
    assert "✅ Evaluated 2 · 1 recommended" in final
    assert "Done in" in final


def test_search_start_and_end_are_logged(tmp_path, caplog):
    settings = offline_settings(tmp_path)
    cache_search_fixtures(settings)
    with caplog.at_level(logging.INFO):
        _run_search(settings, FakeBot())
    assert 'search 1 (chat 42): "Fernseher"' in caplog.text
    assert "chat 42: sent a list of 1 pick" in caplog.text
    assert re.search(r"search 1 done in \d+s: 2 found, 2 evaluated, 1 recommended, 1 listed", caplog.text)


def test_repeating_the_search_lists_the_pick_again_as_seen(tmp_path):
    settings = offline_settings(tmp_path)
    cache_search_fixtures(settings)
    _run_search(settings, FakeBot())

    bot = FakeBot()
    _run_search(settings, bot)
    messages = _messages(bot)
    assert [kind for kind, _ in messages] == ["message", "message", "message"]  # status + list + summary
    assert "seen before" in messages[1][1]["text"]


def test_search_problem_is_reported_to_the_user(tmp_path):
    bot = FakeBot()
    _run_search(offline_settings(tmp_path), bot)  # nothing cached, DRY_RUN on
    assert len(_messages(bot)) == 1  # only the status message, which now says why it stopped
    assert "❌ Stopped: DRY_RUN is on" in _final_status(bot)


def test_unexpected_crash_is_reported_instead_of_silence(tmp_path, monkeypatch):
    settings = offline_settings(tmp_path)
    cache_search_fixtures(settings)

    async def crash(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("offer_finder.bot.app.send_search_results", crash)
    bot = FakeBot()
    _run_search(settings, bot)
    assert "went wrong" in bot.sent[-1][1]["text"]


def test_build_application_wires_conversation_and_dependencies(tmp_path):
    settings = offline_settings(tmp_path, telegram_bot_token="123:TEST", telegram_allowlist=[42])
    application = build_application(settings)
    handlers = [h for group in application.handlers.values() for h in group]
    assert any(isinstance(h, ConversationHandler) for h in handlers)
    assert any(getattr(h, "callback", None) is show_offer for h in handlers)
    assert {"settings", "llm", "start_search"} <= set(application.bot_data)
    assert application.job_queue is not None
    assert application.job_queue.jobs() == ()  # Gmail polling is off by default


# --- Gmail flow: alert email -> DB -> evaluation -> Telegram -------------------

from offer_finder.bot.app import gmail_poll_and_notify


def _poll(settings, bot):
    asyncio.run(gmail_poll_and_notify(bot, settings, ScriptedLLM(), asyncio.Lock(), alert_gmail()))


def test_alert_email_listing_is_listed_on_telegram_exactly_once(tmp_path):
    settings = offline_settings(tmp_path, telegram_allowlist=[42])
    cache_email_details(settings)
    bot = FakeBot()
    _poll(settings, bot)

    assert [kind for kind, _ in bot.sent] == ["message"]
    assert bot.sent[0][1]["chat_id"] == 42
    assert bot.sent[0][1]["text"].startswith("📬 1 new pick")
    assert "Grundig" in bot.sent[0][1]["text"]

    _poll(settings, bot)
    assert len(bot.sent) == 1


def test_gmail_poll_without_a_chat_to_notify_only_stores(tmp_path):
    settings = offline_settings(tmp_path)  # no allowlist, no TELEGRAM_NOTIFY_CHAT_ID
    cache_email_details(settings)
    bot = FakeBot()
    _poll(settings, bot)
    assert bot.sent == []


def test_gmail_job_is_scheduled_when_enabled(tmp_path):
    settings = offline_settings(
        tmp_path, telegram_bot_token="123:TEST", telegram_allowlist=[42], gmail_enabled=True,
        gmail_poll_interval_s=300,
    )
    application = build_application(settings, gmail_client=alert_gmail())
    assert application.job_queue is not None
    jobs = application.job_queue.get_jobs_by_name("gmail")
    assert len(jobs) == 1


def test_summary_offers_the_search_link_for_saving_on_kleinanzeigen(tmp_path):
    from offer_finder.services.search_url import build_search_url

    settings = offline_settings(tmp_path)
    cache_search_fixtures(settings)
    bot = FakeBot()
    _run_search(settings, bot)
    summary = bot.sent[-1][1]["text"]
    assert "save this search" in summary.lower()
    assert build_search_url(offline_spec()) in summary


def test_summary_offers_one_saved_search_link_per_place(tmp_path, monkeypatch):
    from offer_finder.bot.app import save_hint
    from offer_finder.core.models import Location
    from offer_finder.services.search_url import build_search_url
    from tests.helpers.factories import make_spec

    spec = make_spec(locations=[Location(name="Mainz", plz="55116"), Location(name="Mannheim", plz="68159")])
    hint = save_hint(spec)
    assert "save these searches" in hint
    assert f"Mainz: {build_search_url(spec, spec.locations[0])}" in hint
    assert f"Mannheim: {build_search_url(spec, spec.locations[1])}" in hint
