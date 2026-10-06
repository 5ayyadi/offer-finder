"""Stand-ins for the external systems: Kleinanzeigen, the LLM, Telegram, Gmail."""

from types import SimpleNamespace

from telegram.error import BadRequest, NetworkError

from offer_finder.services.gmail import EmailMessage
from tests.helpers.factories import load_fixture

GRUNDIG_ID = "3518580533"  # 99 €, clean listing (tests/fixtures/tv_listing_normal.html)
SAMSUNG_ID = "9999999901"  # 120 €, scam signals (tests/fixtures/tv_listing_scam.html)


class FakeFetcher:
    def __init__(self, fail: dict[str, Exception] | None = None):
        self.calls: list[tuple[str, str]] = []
        self._fail = fail or {}

    def __call__(self, url: str, page_type: str) -> str:
        self.calls.append((url, page_type))
        for listing_id, exc in self._fail.items():
            if listing_id in url:
                raise exc
        if page_type == "search":
            return load_fixture("e2e_search_results.html")
        if GRUNDIG_ID in url:
            return load_fixture("tv_listing_normal.html")
        if SAMSUNG_ID in url:
            return load_fixture("tv_listing_scam.html")
        raise AssertionError(f"unexpected url {url}")

    def detail_calls(self) -> list[str]:
        return [url for url, page_type in self.calls if page_type == "detail"]


def judgment_response(score: int) -> dict:
    """A raw evaluator LLM response."""
    return {
        "product_assessment": {"verdict": "good", "requirements_match": "match", "summary": "a TV"},
        "deal_assessment": {"verdict": "good" if score > 50 else "poor", "summary": "deal"},
        "highlights": [{"claim": "4K", "evidence": "4K Ultra HD"}],
        "concerns": [],
        "unknowns": [],
        "score": score,
        "confidence": "high",
    }


class ScriptedLLM:
    """Scores the clean Grundig listing high and the scam one low."""

    def __init__(self, fail_times: int = 0):
        self.calls = 0
        self._fail_times = fail_times

    def complete_json(self, system_prompt, user_prompt, json_schema):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("429 Too Many Requests")
        return judgment_response(85 if "Grundig" in user_prompt else 20)


def spec_response(**overrides):
    """A raw spec-building LLM response."""
    values = {
        "ready": True,
        "question": None,
        "query": "OLED 65 Zoll",
        "product_summary": "65+ inch OLED TV",
        "requirements": ["OLED", "65 inch or larger"],
        "nice_to_have": [],
        "min_price_eur": None,
        "max_price_eur": 700,
        "locations": [{"name": "Mainz", "plz": "55116"}],
        "radius_km": 30,
        "quality_guide": "Top tier: LG C/G-series and Sony A80+ OLEDs. Avoid: edge-lit budget LCDs.",
    }
    values.update(overrides)
    return values


class FakeProvider:
    """Returns the given responses in order (an Exception is raised instead)."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete_json(self, system_prompt, user_prompt, json_schema):
        self.prompts.append(user_prompt)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeBot:
    def __init__(
        self, fail_photos: bool = False, fail_all: bool = False, fail_edits: bool = False, fail_html: bool = False
    ):
        self.sent: list[tuple[str, dict]] = []
        self._fail_photos = fail_photos
        self._fail_all = fail_all
        self._fail_edits = fail_edits
        self._fail_html = fail_html  # Telegram rejecting the HTML markup
        self._last_message_id = 0

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        if self._fail_all:
            raise NetworkError("down")
        if self._fail_html and parse_mode == "HTML":
            raise BadRequest("Can't parse entities: unsupported start tag")
        self.sent.append(("message", {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}))
        self._last_message_id += 1
        return SimpleNamespace(message_id=self._last_message_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        if self._fail_edits or self._fail_all:
            raise BadRequest("Message is not modified")
        self.sent.append(("edit", {"chat_id": chat_id, "message_id": message_id, "text": text}))

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        if self._fail_photos or self._fail_all:
            raise BadRequest("Wrong type of the web page content")
        self.sent.append(("photo", {"chat_id": chat_id, "photo": photo, "caption": caption}))

    async def send_media_group(self, chat_id, media, **kwargs):
        if self._fail_photos or self._fail_all:
            raise BadRequest("Failed to get http url content")
        self.sent.append(("media_group", {"chat_id": chat_id, "media": media}))


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


def alert_gmail() -> FakeGmail:
    """An inbox holding one saved-search alert for the two fixture listings."""
    subject = "Neue Anzeigen für deine Suche „oled 65“"
    return FakeGmail({"m1": EmailMessage("m1", subject, load_fixture("gmail_alert_email.html"))})
