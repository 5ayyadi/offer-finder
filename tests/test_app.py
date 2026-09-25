"""Offline end-to-end of the search flow behind the Telegram bot:
confirmed spec -> real fetch layer (DRY_RUN cache) -> DB -> evaluation ->
Telegram messages (fake bot)."""

import asyncio

from telegram.ext import ConversationHandler

from offer_finder.bot.app import build_application, search_and_notify
from tests.test_notify import FakeBot
from tests.test_pipeline import ScriptedLLM
from tests.test_runner import _cache_fixtures, _settings, _spec


def _run_search(settings, bot, llm=None):
    asyncio.run(search_and_notify(bot, settings, llm or ScriptedLLM(), asyncio.Lock(), 42, _spec()))


def test_confirmed_search_sends_the_good_listing_then_a_summary(tmp_path):
    settings = _settings(tmp_path)
    _cache_fixtures(settings)
    bot = FakeBot()
    _run_search(settings, bot)

    kinds = [kind for kind, _ in bot.sent]
    assert kinds == ["media_group", "message"]
    caption = bot.sent[0][1]["media"][0].caption
    assert "85/100" in caption and "Grundig" in caption
    summary = bot.sent[1][1]["text"]
    assert "Checked 2 listings: 2 evaluated, 1 recommended." in summary


def test_repeating_the_search_does_not_resend_the_listing(tmp_path):
    settings = _settings(tmp_path)
    _cache_fixtures(settings)
    _run_search(settings, FakeBot())

    bot = FakeBot()
    _run_search(settings, bot)
    assert [kind for kind, _ in bot.sent] == ["message"]
    assert "already sent" in bot.sent[0][1]["text"]


def test_search_problem_is_reported_to_the_user(tmp_path):
    bot = FakeBot()
    _run_search(_settings(tmp_path), bot)  # nothing cached, DRY_RUN on
    assert len(bot.sent) == 1
    assert "DRY_RUN" in bot.sent[0][1]["text"]


def test_unexpected_crash_is_reported_instead_of_silence(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _cache_fixtures(settings)

    async def crash(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("offer_finder.bot.app.send_pending", crash)
    bot = FakeBot()
    _run_search(settings, bot)
    assert "went wrong" in bot.sent[-1][1]["text"]


def test_build_application_wires_conversation_and_dependencies(tmp_path):
    settings = _settings(tmp_path, telegram_bot_token="123:TEST", telegram_allowlist=[42])
    application = build_application(settings)
    handlers = [h for group in application.handlers.values() for h in group]
    assert any(isinstance(h, ConversationHandler) for h in handlers)
    assert {"settings", "llm", "start_search"} <= set(application.bot_data)
    assert application.job_queue is not None
    assert application.job_queue.jobs() == ()  # Gmail polling is off by default


# --- Gmail flow: alert email -> DB -> evaluation -> Telegram -------------------

from offer_finder.bot.app import gmail_poll_and_notify
from tests.test_runner import _alert_gmail, _cache_email_details


def _poll(settings, bot):
    asyncio.run(gmail_poll_and_notify(bot, settings, ScriptedLLM(), asyncio.Lock(), _alert_gmail()))


def test_alert_email_listing_is_sent_to_telegram_exactly_once(tmp_path):
    settings = _settings(tmp_path, telegram_allowlist=[42])
    _cache_email_details(settings)
    bot = FakeBot()
    _poll(settings, bot)

    assert [kind for kind, _ in bot.sent] == ["media_group"]
    assert bot.sent[0][1]["chat_id"] == 42
    assert "Grundig" in bot.sent[0][1]["media"][0].caption

    _poll(settings, bot)
    assert len(bot.sent) == 1


def test_gmail_poll_without_a_chat_to_notify_only_stores(tmp_path):
    settings = _settings(tmp_path)  # no allowlist, no TELEGRAM_NOTIFY_CHAT_ID
    _cache_email_details(settings)
    bot = FakeBot()
    _poll(settings, bot)
    assert bot.sent == []


def test_gmail_job_is_scheduled_when_enabled(tmp_path):
    settings = _settings(
        tmp_path, telegram_bot_token="123:TEST", telegram_allowlist=[42], gmail_enabled=True,
        gmail_poll_interval_s=300,
    )
    application = build_application(settings, gmail_client=_alert_gmail())
    assert application.job_queue is not None
    jobs = application.job_queue.get_jobs_by_name("gmail")
    assert len(jobs) == 1


def test_summary_offers_the_search_link_for_saving_on_kleinanzeigen(tmp_path):
    from offer_finder.services.search_url import build_search_url

    settings = _settings(tmp_path)
    _cache_fixtures(settings)
    bot = FakeBot()
    _run_search(settings, bot)
    summary = bot.sent[-1][1]["text"]
    assert "save this search" in summary.lower()
    assert build_search_url(_spec()) in summary
