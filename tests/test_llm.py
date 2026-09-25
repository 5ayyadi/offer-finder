import json

import httpx
import pytest

from services.llm import OpenAICompatProvider

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
