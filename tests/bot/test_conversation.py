"""Conversation handlers driven with small fake Update/Context objects; the
allowlist wiring is checked against real telegram.Update objects."""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ConversationHandler

from offer_finder import runner
from offer_finder.bot.conversation import (
    COLLECTING,
    CONFIRMING,
    build_conversation,
    on_button,
    on_text,
    start,
)
from offer_finder.storage import db
from tests.helpers.fakes import FakeBot, FakeProvider, spec_response
from tests.helpers.offline import offline_settings

ALLOWED_ID = 42  # the admin (TELEGRAM_ALLOWLIST)
FRIEND = 7


class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.replies: list[tuple[str, object]] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


class FakeQuery:
    def __init__(self, data: str, user_id: int = ALLOWED_ID):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage()
        self.answered = False
        self.markup_removed = False

    async def answer(self, *args, **kwargs):
        self.answered = True

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.markup_removed = reply_markup is None


def _update(text: str = "", query: FakeQuery | None = None, user_id: int = ALLOWED_ID, chat_type: str = "private"):
    return SimpleNamespace(
        message=FakeMessage(text) if query is None else None,
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id, full_name="Sam", username=None),
        effective_chat=SimpleNamespace(id=user_id, type=chat_type),
    )


class FakeContext:
    def __init__(self, tmp_path, *llm_responses, **settings_overrides):
        self.searches: list[tuple[int, object, int]] = []

        async def start_search(chat_id, spec, run_id):
            self.searches.append((chat_id, spec, run_id))

        self.provider = FakeProvider(*llm_responses)
        self.chat_data: dict = {}
        self.bot = FakeBot()
        self.settings = offline_settings(
            tmp_path, telegram_allowlist=[ALLOWED_ID], home_plz="10115", **settings_overrides
        )
        self.bot_data = {"settings": self.settings, "llm": self.provider, "start_search": start_search}

    def add_friend(self, status: str = "approved", **prefs) -> None:
        conn = runner.open_db(self.settings)
        db.request_access(conn, FRIEND, "Sam")
        db.set_user_status(conn, FRIEND, status)  # type: ignore[arg-type]
        if "home_plz" in prefs:
            db.set_user_home(conn, FRIEND, prefs["home_plz"])
        if "radius_km" in prefs:
            db.set_user_radius(conn, FRIEND, prefs["radius_km"])
        conn.close()


@pytest.fixture
def make_context(tmp_path):
    return lambda *responses, **overrides: FakeContext(tmp_path, *responses, **overrides)


def run(coro):
    return asyncio.run(coro)


def test_start_greets_and_resets_the_conversation(make_context):
    context = make_context()
    context.chat_data["history"] = [("user", "old")]
    update = _update()
    update.message = FakeMessage("/start")
    assert run(start(update, context)) == COLLECTING
    assert context.chat_data == {}
    assert update.message.replies


def test_vague_message_gets_a_follow_up_question(make_context):
    context = make_context(spec_response(ready=False, question="Welches Budget?", query=None))
    update = _update("Ich will einen Fernseher")
    assert run(on_text(update, context)) == COLLECTING
    assert update.message.replies[0][0] == "Welches Budget?"
    assert context.chat_data["history"] == [("user", "Ich will einen Fernseher"), ("assistant", "Welches Budget?")]


def test_complete_request_shows_spec_with_confirm_buttons(make_context):
    context = make_context(spec_response())
    update = _update("good 65+ OLED TV around Mainz under 700")
    assert run(on_text(update, context)) == CONFIRMING
    text, markup = update.message.replies[0]
    assert "OLED 65 Zoll" in text and "55116" in text and "up to €700" in text
    buttons = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert buttons == ["search", "change", "cancel"]


def test_confirming_starts_the_search_and_ends_the_conversation(make_context):
    context = make_context(spec_response())
    run(on_text(_update("OLED TV"), context))
    query = FakeQuery("search")
    assert run(on_button(_update(query=query), context)) == ConversationHandler.END
    assert len(context.searches) == 1
    chat_id, spec, run_id = context.searches[0]
    assert chat_id == ALLOWED_ID and spec.query == "OLED 65 Zoll" and isinstance(run_id, int)
    assert query.markup_removed and query.answered
    assert context.chat_data == {}


def test_change_asks_what_to_change_and_keeps_the_proposal_in_context(make_context):
    context = make_context(spec_response(), spec_response(query="OLED 55 Zoll"))
    run(on_text(_update("OLED TV"), context))
    query = FakeQuery("change")
    assert run(on_button(_update(query=query), context)) == COLLECTING
    assert "change" in query.message.replies[0][0].lower()

    assert run(on_text(_update("55 inch is fine too"), context)) == CONFIRMING
    second_prompt = context.provider.prompts[1]
    assert "OLED 65 Zoll" in second_prompt and "55 inch is fine too" in second_prompt


def test_typing_instead_of_pressing_a_button_counts_as_a_change(make_context):
    context = make_context(spec_response(), spec_response(query="OLED 55 Zoll"))
    run(on_text(_update("OLED TV"), context))
    update = _update("actually make it 55 inch")
    assert run(on_text(update, context)) == CONFIRMING
    assert "OLED 55 Zoll" in update.message.replies[0][0]
    assert "OLED 65 Zoll" in context.provider.prompts[1]


def test_cancel_button_ends_without_searching(make_context):
    context = make_context(spec_response())
    run(on_text(_update("OLED TV"), context))
    assert run(on_button(_update(query=FakeQuery("cancel")), context)) == ConversationHandler.END
    assert context.searches == []
    assert context.chat_data == {}


def test_request_trail_is_logged_from_message_to_confirmed_search(caplog, make_context):
    context = make_context(spec_response())
    with caplog.at_level(logging.INFO):
        run(on_text(_update("good 65+ OLED TV around Mainz under 700"), context))
        run(on_button(_update(query=FakeQuery("search")), context))
    log = caplog.text
    assert f'chat {ALLOWED_ID}: message "good 65+ OLED TV around Mainz under 700"' in log
    assert f'chat {ALLOWED_ID}: proposed search "OLED 65 Zoll"' in log
    assert f"chat {ALLOWED_ID}: search confirmed" in log


def test_unusable_message_asks_to_rephrase_and_forgets_it(make_context):
    context = make_context(spec_response(query=None))  # neither a question nor a search
    update = _update("???")
    assert run(on_text(update, context)) == COLLECTING
    assert "rephrase" in update.message.replies[0][0]
    assert context.chat_data["history"] == []


def _llm_http_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://llm.example/chat/completions")
    response = httpx.Response(status, request=request, text=body)
    return httpx.HTTPStatusError(f"{status} from LLM endpoint: {body}", request=request, response=response)


def test_llm_service_error_is_reported_and_logged_instead_of_asking_to_rephrase(caplog, make_context):
    context = make_context(_llm_http_error(404, "model gemini-x is no longer available"))
    update = _update("good 65+ OLED TV around Mainz under 700")
    with caplog.at_level(logging.WARNING):
        assert run(on_text(update, context)) == COLLECTING
    reply = update.message.replies[0][0]
    assert "HTTP 404" in reply and "rephrase" not in reply
    assert "model gemini-x is no longer available" in caplog.text
    assert context.chat_data["history"] == []


def test_unreachable_llm_service_is_reported(caplog, make_context):
    context = make_context(httpx.ConnectError("name resolution failed"))
    update = _update("good 65+ OLED TV around Mainz under 700")
    with caplog.at_level(logging.WARNING):
        run(on_text(update, context))
    assert "couldn't reach the AI service" in update.message.replies[0][0]
    assert "name resolution failed" in caplog.text


def test_button_press_from_stranger_is_ignored(make_context):
    context = make_context(spec_response())
    run(on_text(_update("OLED TV"), context))
    query = FakeQuery("search", user_id=999)
    assert run(on_button(_update(query=query, user_id=999), context)) == ConversationHandler.END
    assert context.searches == []
    assert query.message.replies == []


def test_stale_button_after_restart_explains_itself(make_context):
    context = make_context()
    query = FakeQuery("search")
    assert run(on_button(_update(query=query), context)) == ConversationHandler.END
    assert context.searches == []
    assert "expired" in query.message.replies[0][0]


# --- strangers, per-user defaults, search limit -------------------------------------


def test_stranger_message_becomes_an_access_request_without_calling_the_llm(make_context):
    context = make_context(spec_response())
    update = _update("OLED TV", user_id=FRIEND)
    assert run(on_text(update, context)) == ConversationHandler.END
    assert context.provider.prompts == []
    assert "approve" in update.message.replies[0][0].lower()
    assert [sent["chat_id"] for _, sent in context.bot.sent] == [ALLOWED_ID]


def test_group_chat_messages_are_ignored(make_context):
    context = make_context(spec_response())
    update = _update("OLED TV", chat_type="group")
    assert run(on_text(update, context)) == ConversationHandler.END
    assert context.provider.prompts == [] and update.message.replies == []


def test_blocked_users_old_buttons_do_nothing(make_context):
    context = make_context(spec_response())
    context.add_friend()
    run(on_text(_update("OLED TV", user_id=FRIEND), context))
    conn = runner.open_db(context.settings)
    db.set_user_status(conn, FRIEND, "blocked")
    conn.close()
    query = FakeQuery("search", user_id=FRIEND)
    assert run(on_button(_update(query=query, user_id=FRIEND), context)) == ConversationHandler.END
    assert context.searches == [] and not query.answered


def test_friend_without_a_named_place_searches_around_their_home(make_context):
    context = make_context(spec_response(locations=[], radius_km=None))
    context.add_friend(home_plz="20095", radius_km=50)
    update = _update("OLED TV", user_id=FRIEND)
    assert run(on_text(update, context)) == CONFIRMING
    spec = context.chat_data["spec"]
    assert [location.plz for location in spec.locations] == ["20095"] and spec.radius_km == 50


def test_friend_with_radius_any_gets_no_radius(make_context):
    context = make_context(spec_response(locations=[], radius_km=None), default_radius_km=20)
    context.add_friend(radius_km=0)
    run(on_text(_update("OLED TV", user_id=FRIEND), context))
    assert context.chat_data["spec"].radius_km is None


def test_user_without_preferences_gets_the_env_defaults(make_context):
    context = make_context(spec_response(locations=[], radius_km=None), default_radius_km=20)
    context.add_friend()
    run(on_text(_update("OLED TV", user_id=FRIEND), context))
    spec = context.chat_data["spec"]
    assert [location.plz for location in spec.locations] == ["10115"] and spec.radius_km == 20


def test_double_tap_on_search_starts_one_search(make_context):
    context = make_context(spec_response())
    run(on_text(_update("OLED TV"), context))
    run(on_button(_update(query=FakeQuery("search")), context))
    second = FakeQuery("search")
    run(on_button(_update(query=second), context))
    assert len(context.searches) == 1
    assert "expired" in second.message.replies[0][0]


def _confirm(context, user_id):
    run(on_text(_update("OLED TV", user_id=user_id), context))
    query = FakeQuery("search", user_id=user_id)
    state = run(on_button(_update(query=query, user_id=user_id), context))
    return state, query


def _finish_runs(context):
    conn = runner.open_db(context.settings)
    for _, _, run_id in context.searches:
        db.finish_search_run(conn, run_id, "done", search_spec_id=None)
    conn.close()


def test_second_search_while_one_is_running_is_refused_and_the_proposal_kept(make_context):
    context = make_context(spec_response(), spec_response())
    context.add_friend()
    _confirm(context, FRIEND)
    state, query = _confirm(context, FRIEND)
    assert state == CONFIRMING and len(context.searches) == 1
    assert "already running" in query.message.replies[0][0]
    assert not query.markup_removed and "spec" in context.chat_data


def test_friend_is_limited_to_the_daily_number_of_searches(make_context):
    context = make_context(*[spec_response()] * 3, user_daily_searches=2)
    context.add_friend()
    for _ in range(2):
        _confirm(context, FRIEND)
        _finish_runs(context)
    state, query = _confirm(context, FRIEND)
    assert state == CONFIRMING and len(context.searches) == 2
    assert "2 searches" in query.message.replies[0][0]


def test_admins_have_no_daily_limit(make_context):
    context = make_context(*[spec_response()] * 3, user_daily_searches=1)
    for _ in range(3):
        _confirm(context, ALLOWED_ID)
        _finish_runs(context)
    assert len(context.searches) == 3


# --- wiring ------------------------------------------------------------------------


def _real_update(user_id: int, text: str, chat_type: str = "private") -> Update:
    user = User(id=user_id, first_name="x", is_bot=False)
    chat = Chat(id=user_id if chat_type == "private" else -100, type=chat_type)
    message = Message(message_id=1, date=datetime.now(UTC), chat=chat, from_user=user, text=text)
    return Update(update_id=1, message=message)


def test_conversation_takes_private_messages_from_anyone_and_authorizes_inside():
    handler = build_conversation()
    assert handler.check_update(_real_update(ALLOWED_ID, "OLED TV"))
    assert handler.check_update(_real_update(999, "OLED TV"))  # becomes an access request
    assert handler.check_update(_real_update(999, "/start"))


def test_conversation_ignores_group_chats():
    handler = build_conversation()
    assert not handler.check_update(_real_update(ALLOWED_ID, "OLED TV", chat_type="group"))


def _real_button_press(user_id: int, data: str) -> Update:
    from telegram import CallbackQuery

    user = User(id=user_id, first_name="x", is_bot=False)
    chat = Chat(id=user_id, type="private")
    message = Message(message_id=5, date=datetime.now(UTC), chat=chat, from_user=user, text="spec")
    return Update(update_id=2, callback_query=CallbackQuery(id="q", from_user=user, chat_instance="c", message=message, data=data))


def test_stale_button_outside_a_conversation_is_still_answered():
    handler = build_conversation()
    assert handler.check_update(_real_button_press(ALLOWED_ID, "search"))
