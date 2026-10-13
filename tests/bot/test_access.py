"""Who may use the bot: authorize() on every update, the access request of a
stranger, and the admin's approve/block buttons and commands. The user row
is read from SQLite each time, so forged or stale button data never helps."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from telegram import CallbackQuery, Chat, Message, Update, User

from offer_finder import runner
from offer_finder.bot.access import (
    authorize,
    build_access_handlers,
    on_block_command,
    on_user_button,
    on_users_command,
)
from offer_finder.storage import db
from tests.helpers.fakes import FakeBot
from tests.helpers.offline import offline_settings

ADMIN = 42
FRIEND = 7


class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data: str, user_id: int):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage()
        self.answered = False
        self.edited: list[str] = []

    async def answer(self, *args, **kwargs):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        self.edited.append(text)


def _user(user_id: int, name: str = "Sam"):
    return SimpleNamespace(id=user_id, full_name=name, username=name.lower())


def _message(user_id: int, text: str = "hello", chat_type: str = "private", chat_id: int | None = None):
    return SimpleNamespace(
        message=FakeMessage(text),
        callback_query=None,
        effective_user=_user(user_id),
        effective_chat=SimpleNamespace(id=user_id if chat_id is None else chat_id, type=chat_type),
    )


def _press(user_id: int, data: str):
    return SimpleNamespace(
        message=None,
        callback_query=FakeQuery(data, user_id),
        effective_user=_user(user_id),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
    )


def _context(settings, args: list[str] | None = None):
    return SimpleNamespace(bot=FakeBot(), bot_data={"settings": settings}, chat_data={}, args=args or [])


def _setup(tmp_path, friend_status: str | None = None):
    settings = offline_settings(tmp_path, telegram_allowlist=[ADMIN])
    conn = runner.open_db(settings)
    if friend_status:
        db.request_access(conn, FRIEND, "Sam")
        db.set_user_status(conn, FRIEND, friend_status)  # type: ignore[arg-type]
    conn.close()
    return settings


def _user_row(settings, user_id):
    conn = runner.open_db(settings)
    try:
        return db.get_user(conn, user_id)
    finally:
        conn.close()


def run(coro):
    return asyncio.run(coro)


# --- authorize --------------------------------------------------------------------


def test_admin_and_approved_users_are_let_in(tmp_path):
    settings = _setup(tmp_path, friend_status="approved")
    admin = run(authorize(_message(ADMIN), _context(settings)))
    friend = run(authorize(_message(FRIEND), _context(settings)))
    assert admin is not None and admin.is_admin
    assert friend is not None and not friend.is_admin


def test_stranger_is_stored_as_pending_and_admins_are_asked_once(tmp_path):
    settings = _setup(tmp_path)
    context = _context(settings)
    update = _message(FRIEND)
    assert run(authorize(update, context)) is None
    assert _user_row(settings, FRIEND).status == "pending"
    assert "approve" in update.message.replies[0].lower()

    [(kind, sent)] = context.bot.sent
    assert kind == "message" and sent["chat_id"] == ADMIN
    assert str(FRIEND) in sent["text"] and "Sam" in sent["text"]
    buttons = [b.callback_data for row in sent["reply_markup"].inline_keyboard for b in row]
    assert buttons == [f"user:approve:{FRIEND}", f"user:block:{FRIEND}"]

    again = _message(FRIEND, "anyone there?")
    assert run(authorize(again, context)) is None
    assert len(context.bot.sent) == 1  # no second ping
    assert "waiting" in again.message.replies[0].lower()


def test_blocked_user_gets_silence(tmp_path):
    settings = _setup(tmp_path, friend_status="blocked")
    context = _context(settings)
    update = _message(FRIEND)
    assert run(authorize(update, context)) is None
    assert update.message.replies == [] and context.bot.sent == []


def test_group_chats_are_ignored_even_for_admins(tmp_path):
    settings = _setup(tmp_path)
    context = _context(settings)
    update = _message(ADMIN, chat_type="group", chat_id=-100)
    assert run(authorize(update, context)) is None
    assert update.message.replies == [] and context.bot.sent == []


def test_stranger_in_a_group_is_not_stored(tmp_path):
    settings = _setup(tmp_path)
    run(authorize(_message(FRIEND, chat_type="group", chat_id=-100), _context(settings)))
    assert _user_row(settings, FRIEND) is None


def test_admin_only_check_refuses_approved_friends(tmp_path):
    settings = _setup(tmp_path, friend_status="approved")
    assert run(authorize(_message(FRIEND), _context(settings), admin=True)) is None
    assert run(authorize(_message(ADMIN), _context(settings), admin=True)) is not None


def test_stranger_pressing_a_button_is_not_stored_or_answered(tmp_path):
    settings = _setup(tmp_path)
    context = _context(settings)
    update = _press(FRIEND, "search")
    assert run(authorize(update, context)) is None
    assert _user_row(settings, FRIEND) is None
    assert context.bot.sent == [] and not update.callback_query.answered


# --- approve / block buttons ------------------------------------------------------


def test_admin_approves_a_pending_user_who_then_gets_a_greeting(tmp_path):
    settings = _setup(tmp_path, friend_status="pending")
    context = _context(settings)
    update = _press(ADMIN, f"user:approve:{FRIEND}")
    run(on_user_button(update, context))
    assert _user_row(settings, FRIEND).status == "approved"
    assert update.callback_query.answered and "approved" in update.callback_query.edited[0].lower()
    assert [sent["chat_id"] for _, sent in context.bot.sent] == [FRIEND]


def test_pressing_approve_again_changes_nothing_and_sends_no_second_greeting(tmp_path):
    settings = _setup(tmp_path, friend_status="approved")
    context = _context(settings)
    run(on_user_button(_press(ADMIN, f"user:approve:{FRIEND}"), context))
    assert context.bot.sent == []


def test_admin_blocks_a_user(tmp_path):
    settings = _setup(tmp_path, friend_status="approved")
    run(on_user_button(_press(ADMIN, f"user:block:{FRIEND}"), _context(settings)))
    assert _user_row(settings, FRIEND).status == "blocked"
    assert run(authorize(_message(FRIEND), _context(settings))) is None


def test_friend_cannot_approve_or_block_with_forged_button_data(tmp_path):
    settings = _setup(tmp_path, friend_status="approved")
    conn = runner.open_db(settings)
    db.request_access(conn, 8, "Other")
    conn.close()
    for data in (f"user:approve:{FRIEND}", "user:approve:8", f"user:block:{ADMIN}", "user:block:8"):
        update = _press(FRIEND, data)
        run(on_user_button(update, _context(settings)))
        assert not update.callback_query.answered
    assert _user_row(settings, 8).status == "pending"
    assert _user_row(settings, ADMIN).status == "approved"


def test_pending_user_cannot_approve_themselves(tmp_path):
    settings = _setup(tmp_path, friend_status="pending")
    run(on_user_button(_press(FRIEND, f"user:approve:{FRIEND}"), _context(settings)))
    assert _user_row(settings, FRIEND).status == "pending"


def test_malformed_button_data_is_ignored(tmp_path):
    settings = _setup(tmp_path, friend_status="pending")
    for data in ("user:approve:", "user:approve:abc", f"user:approve:{FRIEND};x", "user:delete:7", "", "x" * 300):
        update = _press(ADMIN, data)
        run(on_user_button(update, _context(settings)))
        assert update.callback_query.edited == []
    assert _user_row(settings, FRIEND).status == "pending"


def test_approving_an_unknown_id_creates_nobody(tmp_path):
    settings = _setup(tmp_path)
    update = _press(ADMIN, "user:approve:12345")
    run(on_user_button(update, _context(settings)))
    assert _user_row(settings, 12345) is None
    assert "unknown" in update.callback_query.edited[0].lower()


def test_admins_cannot_be_blocked_from_telegram(tmp_path):
    settings = _setup(tmp_path)
    run(on_user_button(_press(ADMIN, f"user:block:{ADMIN}"), _context(settings)))
    assert _user_row(settings, ADMIN).status == "approved"


# --- admin commands ----------------------------------------------------------------


def test_users_command_lists_everyone_for_admins_only(tmp_path):
    settings = _setup(tmp_path, friend_status="pending")
    admin_update = _message(ADMIN, "/users")
    run(on_users_command(admin_update, _context(settings)))
    assert str(FRIEND) in admin_update.message.replies[0] and "pending" in admin_update.message.replies[0]

    conn = runner.open_db(settings)
    db.set_user_status(conn, FRIEND, "approved")
    conn.close()
    friend_update = _message(FRIEND, "/users")
    run(on_users_command(friend_update, _context(settings)))
    assert friend_update.message.replies == []


def test_block_command(tmp_path):
    settings = _setup(tmp_path, friend_status="approved")
    update = _message(ADMIN, f"/block {FRIEND}")
    run(on_block_command(update, _context(settings, args=[str(FRIEND)])))
    assert _user_row(settings, FRIEND).status == "blocked"
    assert "blocked" in update.message.replies[0].lower()


def test_block_command_from_a_friend_does_nothing(tmp_path):
    settings = _setup(tmp_path, friend_status="approved")
    conn = runner.open_db(settings)
    db.request_access(conn, 8, "Other")
    db.set_user_status(conn, 8, "approved")
    conn.close()
    run(on_block_command(_message(FRIEND, "/block 8"), _context(settings, args=["8"])))
    assert _user_row(settings, 8).status == "approved"


def test_block_command_needs_a_numeric_id(tmp_path):
    settings = _setup(tmp_path)
    update = _message(ADMIN, "/block sam")
    run(on_block_command(update, _context(settings, args=["sam"])))
    assert "usage" in update.message.replies[0].lower()


# --- wiring -----------------------------------------------------------------------


def _real_press(user_id: int, data: str) -> Update:
    user = User(id=user_id, first_name="x", is_bot=False)
    chat = Chat(id=user_id, type="private")
    message = Message(message_id=5, date=datetime.now(UTC), chat=chat, from_user=user, text="request")
    return Update(update_id=2, callback_query=CallbackQuery(id="q", from_user=user, chat_instance="c", message=message, data=data))


def test_user_buttons_only_match_the_strict_pattern():
    handlers = build_access_handlers()
    def matches(data: str) -> bool:
        return any(handler.check_update(_real_press(ADMIN, data)) for handler in handlers)
    assert matches("user:approve:7") and matches("user:block:123456789")
    assert not matches("user:approve:7x") and not matches("user:approve:") and not matches("offer:7")
