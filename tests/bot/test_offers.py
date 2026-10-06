"""The numbered buttons under a pick list: tap -> that offer's full card."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from telegram import CallbackQuery, Chat, Message, Update, User

from offer_finder.bot.conversation import build_conversation
from offer_finder.bot.offers import build_offer_handler, show_offer
from offer_finder.storage import db
from tests.helpers.factories import make_evaluation, make_listing
from tests.helpers.fakes import FakeBot
from tests.helpers.offline import offline_settings

ALLOWED_ID = 42


class FakeQuery:
    def __init__(self, data: str, user_id: int = ALLOWED_ID):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.replies: list[str] = []
        self.answered = False
        self.message = SimpleNamespace(reply_text=self._reply)

    async def _reply(self, text, **kwargs):
        self.replies.append(text)

    async def answer(self, *args, **kwargs):
        self.answered = True


def _press(settings, data, user_id=ALLOWED_ID):
    query = FakeQuery(data, user_id)
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=user_id))
    bot = FakeBot()
    context = SimpleNamespace(bot=bot, bot_data={"settings": settings})
    asyncio.run(show_offer(update, context))
    return query, bot


def _settings_with_offer(tmp_path):
    settings = offline_settings(tmp_path, telegram_allowlist=[ALLOWED_ID])
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    db.insert_discovered(conn, make_listing("111", image_urls=[]))
    evaluation_id = db.save_evaluation(conn, make_evaluation("111", search_spec_id=None))
    conn.close()
    return settings, evaluation_id


def test_tapping_a_number_sends_that_offer(tmp_path):
    settings, evaluation_id = _settings_with_offer(tmp_path)
    query, bot = _press(settings, f"offer:{evaluation_id}")
    assert query.answered
    assert [kind for kind, _ in bot.sent] == ["message"]
    assert "80/100" in bot.sent[0][1]["text"] and bot.sent[0][1]["chat_id"] == ALLOWED_ID


def test_offer_that_no_longer_exists_says_so(tmp_path):
    settings, evaluation_id = _settings_with_offer(tmp_path)
    query, bot = _press(settings, f"offer:{evaluation_id + 5}")
    assert bot.sent == []
    assert query.replies and "no longer" in query.replies[0]


def test_strangers_get_nothing(tmp_path):
    settings, evaluation_id = _settings_with_offer(tmp_path)
    query, bot = _press(settings, f"offer:{evaluation_id}", user_id=999)
    assert bot.sent == [] and not query.answered


def _real_press(data: str) -> Update:
    user = User(id=ALLOWED_ID, first_name="x", is_bot=False)
    chat = Chat(id=ALLOWED_ID, type="private")
    message = Message(message_id=5, date=datetime.now(UTC), chat=chat, from_user=user, text="list")
    return Update(update_id=3, callback_query=CallbackQuery(id="q", from_user=user, chat_instance="c", message=message, data=data))


def test_offer_buttons_go_to_the_offer_handler_not_the_conversation():
    assert build_offer_handler().check_update(_real_press("offer:7"))
    assert not build_offer_handler().check_update(_real_press("search"))
    assert not build_conversation([ALLOWED_ID]).check_update(_real_press("offer:7"))
