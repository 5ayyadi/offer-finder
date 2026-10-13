"""/settings, /home and /radius: each user's own defaults for new searches.
A command only ever changes the caller's own row."""

import asyncio
from types import SimpleNamespace

from offer_finder import runner
from offer_finder.bot.preferences import on_home, on_radius, on_settings
from offer_finder.storage import db
from tests.helpers.fakes import FakeBot
from tests.helpers.offline import offline_settings

ADMIN = 42
FRIEND = 7


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def _call(handler, settings, user_id, *args):
    update = SimpleNamespace(
        message=FakeMessage(),
        callback_query=None,
        effective_user=SimpleNamespace(id=user_id, full_name="Sam", username=None),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
    )
    context = SimpleNamespace(bot=FakeBot(), bot_data={"settings": settings}, chat_data={}, args=list(args))
    asyncio.run(handler(update, context))
    return update.message.replies


def _setup(tmp_path, friend_status="approved"):
    settings = offline_settings(tmp_path, telegram_allowlist=[ADMIN], default_radius_km=20)
    conn = runner.open_db(settings)
    db.request_access(conn, FRIEND, "Sam")
    db.set_user_status(conn, FRIEND, friend_status)
    conn.close()
    return settings


def _row(settings, user_id):
    conn = runner.open_db(settings)
    try:
        return db.get_user(conn, user_id)
    finally:
        conn.close()


def test_settings_shows_the_defaults_until_changed(tmp_path):
    settings = _setup(tmp_path)
    [reply] = _call(on_settings, settings, FRIEND)
    assert "57074" in reply and "20 km" in reply


def test_home_sets_a_known_postcode(tmp_path):
    settings = _setup(tmp_path)
    [reply] = _call(on_home, settings, FRIEND, "55116")
    assert "55116" in reply
    assert _row(settings, FRIEND).home_plz == "55116"
    assert _row(settings, ADMIN).home_plz is None  # nobody else changes


def test_home_rejects_unknown_or_malformed_postcodes(tmp_path):
    settings = _setup(tmp_path)
    for args in (["00000"], ["abc"], ["551161"], [], ["55116", "55118"]):
        [reply] = _call(on_home, settings, FRIEND, *args)
        assert "usage" in reply.lower() or "don't know" in reply.lower()
    assert _row(settings, FRIEND).home_plz is None


def test_radius_takes_km_or_any(tmp_path):
    settings = _setup(tmp_path)
    _call(on_radius, settings, FRIEND, "50")
    assert _row(settings, FRIEND).radius_km == 50
    _call(on_radius, settings, FRIEND, "any")
    assert _row(settings, FRIEND).radius_km == 0
    [reply] = _call(on_settings, settings, FRIEND)
    assert "any distance" in reply


def test_radius_rejects_out_of_range_values(tmp_path):
    settings = _setup(tmp_path)
    for args in (["0"], ["201"], ["-5"], ["far"], []):
        [reply] = _call(on_radius, settings, FRIEND, *args)
        assert "usage" in reply.lower()
    assert _row(settings, FRIEND).radius_km is None


def test_pending_or_blocked_users_cannot_change_settings(tmp_path):
    for status in ("pending", "blocked"):
        settings = _setup(tmp_path / status, friend_status=status)
        _call(on_home, settings, FRIEND, "55116")
        _call(on_radius, settings, FRIEND, "50")
        row = _row(settings, FRIEND)
        assert row.home_plz is None and row.radius_km is None
