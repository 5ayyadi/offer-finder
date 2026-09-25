"""Offline end-to-end of the search flow behind the Telegram bot:
confirmed spec -> real fetch layer (DRY_RUN cache) -> DB -> evaluation ->
Telegram messages (fake bot)."""

import asyncio

from telegram.ext import ConversationHandler

from bot.app import build_application, search_and_notify
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

    monkeypatch.setattr("bot.app.send_pending", crash)
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
