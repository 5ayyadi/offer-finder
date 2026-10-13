"""Conversation handlers driven with small fake Update/Context objects; the
allowlist wiring is checked against real telegram.Update objects."""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from telegram import Chat, Message, Update, User
from telegram.ext import ConversationHandler

from offer_finder.bot.conversation import (
    COLLECTING,
    CONFIRMING,
    build_conversation,
    on_button,
    on_text,
    start,
)
from offer_finder.config import Settings
from tests.helpers.fakes import FakeProvider, spec_response

ALLOWED_ID = 42


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


def _update(text: str = "", query: FakeQuery | None = None, user_id: int = ALLOWED_ID):
    return SimpleNamespace(
        message=FakeMessage(text) if query is None else None,
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
    )


class FakeContext:
    def __init__(self, *llm_responses):
        self.searches: list[tuple[int, object]] = []

        async def start_search(chat_id, spec):
            self.searches.append((chat_id, spec))

        self.provider = FakeProvider(*llm_responses)
        self.chat_data: dict = {}
        self.bot_data = {
            "settings": Settings(_env_file=None, telegram_allowlist=[ALLOWED_ID], home_plz="10115"),
            "llm": self.provider,
            "start_search": start_search,
        }


def run(coro):
    return asyncio.run(coro)


def test_start_greets_and_resets_the_conversation():
    context = FakeContext()
    context.chat_data["history"] = [("user", "old")]
    update = _update()
    update.message = FakeMessage("/start")
    assert run(start(update, context)) == COLLECTING
    assert context.chat_data == {}
    assert update.message.replies


def test_vague_message_gets_a_follow_up_question():
    context = FakeContext(spec_response(ready=False, question="Welches Budget?", query=None))
    update = _update("Ich will einen Fernseher")
    assert run(on_text(update, context)) == COLLECTING
    assert update.message.replies[0][0] == "Welches Budget?"
    assert context.chat_data["history"] == [("user", "Ich will einen Fernseher"), ("assistant", "Welches Budget?")]


def test_complete_request_shows_spec_with_confirm_buttons():
    context = FakeContext(spec_response())
    update = _update("good 65+ OLED TV around Mainz under 700")
    assert run(on_text(update, context)) == CONFIRMING
    text, markup = update.message.replies[0]
    assert "OLED 65 Zoll" in text and "55116" in text and "up to €700" in text
    buttons = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert buttons == ["search", "change", "cancel"]


def test_confirming_starts_the_search_and_ends_the_conversation():
    context = FakeContext(spec_response())
    run(on_text(_update("OLED TV"), context))
    query = FakeQuery("search")
    assert run(on_button(_update(query=query), context)) == ConversationHandler.END
    assert len(context.searches) == 1
    chat_id, spec = context.searches[0]
    assert chat_id == ALLOWED_ID and spec.query == "OLED 65 Zoll"
    assert query.markup_removed and query.answered
    assert context.chat_data == {}


def test_change_asks_what_to_change_and_keeps_the_proposal_in_context():
    context = FakeContext(spec_response(), spec_response(query="OLED 55 Zoll"))
    run(on_text(_update("OLED TV"), context))
    query = FakeQuery("change")
    assert run(on_button(_update(query=query), context)) == COLLECTING
    assert "change" in query.message.replies[0][0].lower()

    assert run(on_text(_update("55 inch is fine too"), context)) == CONFIRMING
    second_prompt = context.provider.prompts[1]
    assert "OLED 65 Zoll" in second_prompt and "55 inch is fine too" in second_prompt


def test_typing_instead_of_pressing_a_button_counts_as_a_change():
    context = FakeContext(spec_response(), spec_response(query="OLED 55 Zoll"))
    run(on_text(_update("OLED TV"), context))
    update = _update("actually make it 55 inch")
    assert run(on_text(update, context)) == CONFIRMING
    assert "OLED 55 Zoll" in update.message.replies[0][0]
    assert "OLED 65 Zoll" in context.provider.prompts[1]


def test_cancel_button_ends_without_searching():
    context = FakeContext(spec_response())
    run(on_text(_update("OLED TV"), context))
    assert run(on_button(_update(query=FakeQuery("cancel")), context)) == ConversationHandler.END
    assert context.searches == []
    assert context.chat_data == {}


def test_request_trail_is_logged_from_message_to_confirmed_search(caplog):
    context = FakeContext(spec_response())
    with caplog.at_level(logging.INFO):
        run(on_text(_update("good 65+ OLED TV around Mainz under 700"), context))
        run(on_button(_update(query=FakeQuery("search")), context))
    log = caplog.text
    assert f'chat {ALLOWED_ID}: message "good 65+ OLED TV around Mainz under 700"' in log
    assert f'chat {ALLOWED_ID}: proposed search "OLED 65 Zoll"' in log
    assert f"chat {ALLOWED_ID}: search confirmed" in log


def test_unusable_message_asks_to_rephrase_and_forgets_it():
    context = FakeContext(spec_response(query=None))  # neither a question nor a search
    update = _update("???")
    assert run(on_text(update, context)) == COLLECTING
    assert "rephrase" in update.message.replies[0][0]
    assert context.chat_data["history"] == []


def _llm_http_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://llm.example/chat/completions")
    response = httpx.Response(status, request=request, text=body)
    return httpx.HTTPStatusError(f"{status} from LLM endpoint: {body}", request=request, response=response)


def test_llm_service_error_is_reported_and_logged_instead_of_asking_to_rephrase(caplog):
    context = FakeContext(_llm_http_error(404, "model gemini-x is no longer available"))
    update = _update("good 65+ OLED TV around Mainz under 700")
    with caplog.at_level(logging.WARNING):
        assert run(on_text(update, context)) == COLLECTING
    reply = update.message.replies[0][0]
    assert "HTTP 404" in reply and "rephrase" not in reply
    assert "model gemini-x is no longer available" in caplog.text
    assert context.chat_data["history"] == []


def test_unreachable_llm_service_is_reported(caplog):
    context = FakeContext(httpx.ConnectError("name resolution failed"))
    update = _update("good 65+ OLED TV around Mainz under 700")
    with caplog.at_level(logging.WARNING):
        run(on_text(update, context))
    assert "couldn't reach the AI service" in update.message.replies[0][0]
    assert "name resolution failed" in caplog.text


def test_button_press_from_stranger_is_ignored():
    context = FakeContext(spec_response())
    run(on_text(_update("OLED TV"), context))
    query = FakeQuery("search", user_id=999)
    assert run(on_button(_update(query=query, user_id=999), context)) == ConversationHandler.END
    assert context.searches == []
    assert query.message.replies == []


def test_stale_button_after_restart_explains_itself():
    context = FakeContext()
    query = FakeQuery("search")
    assert run(on_button(_update(query=query), context)) == ConversationHandler.END
    assert context.searches == []
    assert "expired" in query.message.replies[0][0]


# --- wiring: allowlist is enforced by the handler filters --------------------------


def _real_update(user_id: int, text: str) -> Update:
    user = User(id=user_id, first_name="x", is_bot=False)
    chat = Chat(id=user_id, type="private")
    message = Message(message_id=1, date=datetime.now(UTC), chat=chat, from_user=user, text=text)
    return Update(update_id=1, message=message)


def test_conversation_accepts_allowlisted_user():
    handler = build_conversation([ALLOWED_ID])
    assert handler.check_update(_real_update(ALLOWED_ID, "OLED TV"))


def test_conversation_ignores_everyone_else():
    handler = build_conversation([ALLOWED_ID])
    assert not handler.check_update(_real_update(999, "OLED TV"))
    assert not handler.check_update(_real_update(999, "/start"))


def test_empty_allowlist_lets_nobody_in():
    handler = build_conversation([])
    assert not handler.check_update(_real_update(ALLOWED_ID, "OLED TV"))


def _real_button_press(user_id: int, data: str) -> Update:
    from telegram import CallbackQuery

    user = User(id=user_id, first_name="x", is_bot=False)
    chat = Chat(id=user_id, type="private")
    message = Message(message_id=5, date=datetime.now(UTC), chat=chat, from_user=user, text="spec")
    return Update(update_id=2, callback_query=CallbackQuery(id="q", from_user=user, chat_instance="c", message=message, data=data))


def test_stale_button_outside_a_conversation_is_still_answered():
    handler = build_conversation([ALLOWED_ID])
    assert handler.check_update(_real_button_press(ALLOWED_ID, "search"))
