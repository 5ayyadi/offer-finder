import json

import httpx
import pytest
from pydantic import BaseModel

from offer_finder.services.llm import LLMResponseError, OpenAICompatProvider, complete_validated

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


def _provider_returning(contents: list[str], captured: list[dict] | None = None, **kwargs):
    responses = iter(contents)

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": next(responses)}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider("https://example.invalid/v1", "k", "m", client=client, **kwargs)


def test_provider_sends_strict_json_schema_by_default():
    captured: list[dict] = []
    _provider_returning(["{}"], captured).complete_json("sys", "user", {"type": "object"})
    assert captured[0]["response_format"]["type"] == "json_schema"
    assert captured[0]["response_format"]["json_schema"]["schema"] == {"type": "object"}


def test_provider_json_object_mode_puts_schema_in_system_prompt():
    captured: list[dict] = []
    provider = _provider_returning(["{}"], captured, response_format="json_object")
    provider.complete_json("sys", "user", {"type": "object", "title": "marker"})
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert "marker" in captured[0]["messages"][0]["content"]


def test_provider_tolerates_markdown_fenced_json():
    provider = _provider_returning(['```json\n{"a": 1}\n```'])
    assert provider.complete_json("sys", "user", {}) == {"a": 1}


# --- complete_validated ------------------------------------------------------------


class _Answer(BaseModel):
    answer: int


class _ScriptedProvider:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0

    def complete_json(self, system_prompt, user_prompt, json_schema):
        self.calls += 1
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_complete_validated_returns_model_and_raw_response():
    parsed, raw = complete_validated(_ScriptedProvider([{"answer": 42}]), "s", "u", {}, _Answer)
    assert parsed == _Answer(answer=42)
    assert raw == {"answer": 42}


def test_complete_validated_retries_once_on_invalid_response():
    provider = _ScriptedProvider([{"answer": "not a number"}, {"answer": 7}])
    parsed, _ = complete_validated(provider, "s", "u", {}, _Answer)
    assert parsed.answer == 7
    assert provider.calls == 2


def test_complete_validated_retries_once_on_malformed_json():
    provider = _ScriptedProvider([json.JSONDecodeError("bad", "doc", 0), {"answer": 7}])
    parsed, _ = complete_validated(provider, "s", "u", {}, _Answer)
    assert parsed.answer == 7


def test_complete_validated_gives_up_after_second_invalid_response():
    provider = _ScriptedProvider([{"answer": "x"}, {"answer": "y"}])
    with pytest.raises(LLMResponseError):
        complete_validated(provider, "s", "u", {}, _Answer)


def test_provider_without_api_key_sends_no_auth_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    OpenAICompatProvider("https://example.invalid/v1", "", "m", client=client).complete_json("s", "u", {})
    assert seen["auth"] is None


def test_http_error_includes_the_endpoints_explanation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Invalid JSON payload: unknown field additionalProperties"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatProvider("https://example.invalid/v1", "k", "m", client=client)
    with pytest.raises(httpx.HTTPStatusError, match="additionalProperties"):
        provider.complete_json("s", "u", {})
