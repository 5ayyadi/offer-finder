import json

import httpx
import pytest

from services.llm import OpenAICompatProvider, QueryParseError, parse_query


class _FakeProvider:
    """Duck-typed stand-in for OpenAICompatProvider — no network involved."""

    def __init__(self, response: dict | None = None, raise_error: bool = False):
        self._response = response or {}
        self._raise_error = raise_error

    def complete_json(self, system_prompt: str, user_prompt: str, json_schema: dict) -> dict:
        if self._raise_error:
            raise RuntimeError("upstream boom")
        return self._response


# --- parse_query (mocked provider) -------------------------------------------


def test_parse_query_full_message_populates_all_fields():
    provider = _FakeProvider(
        {
            "max_price_eur": 700,
            "min_price_eur": None,
            "min_screen_size_inch": 65,
            "preferred_brands": ["LG", "Samsung"],
            "plz": "55128",
            "location_text_raw": None,
            "max_distance_km": 30,
        }
    )
    query = parse_query("Find me good TVs under 700 around 55128", "10115", provider=provider)
    assert query.max_price_eur == 700
    assert query.min_screen_size_inch == 65
    assert query.preferred_brands == ["LG", "Samsung"]
    assert query.home_plz == "55128"
    assert query.max_distance_km == 30
    assert query.raw_user_message == "Find me good TVs under 700 around 55128"


def test_parse_query_falls_back_to_home_plz_when_none_given():
    provider = _FakeProvider({"preferred_brands": []})
    query = parse_query("Find me a good TV deal", "10115", provider=provider)
    assert query.home_plz == "10115"


def test_parse_query_defaults_missing_optional_fields():
    provider = _FakeProvider({"preferred_brands": []})
    query = parse_query("any TV deal", "10115", provider=provider)
    assert query.max_price_eur is None
    assert query.min_screen_size_inch is None
    assert query.preferred_brands == []


def test_parse_query_raises_clear_error_on_provider_failure():
    provider = _FakeProvider(raise_error=True)
    with pytest.raises(QueryParseError):
        parse_query("Find me a TV", "10115", provider=provider)


def test_parse_query_raises_clear_error_on_schema_violation():
    # preferred_brands as a string instead of a list — invalid per StructuredQuery
    provider = _FakeProvider({"preferred_brands": "not-a-list"})
    with pytest.raises(QueryParseError):
        parse_query("Find me a TV", "10115", provider=provider)


# --- OpenAICompatProvider (real HTTP shape, mocked transport) ----------------


def test_provider_extracts_json_content_from_chat_completion_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"preferred_brands": ["Sony"]})}}
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatProvider(
        base_url="https://example.invalid/v1", api_key="test-key", model="test-model", client=client
    )
    result = provider.complete_json("system", "user", {"type": "object"})
    assert result == {"preferred_brands": ["Sony"]}


def test_provider_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatProvider(
        base_url="https://example.invalid/v1", api_key="test-key", model="test-model", client=client
    )
    with pytest.raises(httpx.HTTPStatusError):
        provider.complete_json("system", "user", {"type": "object"})
