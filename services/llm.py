"""Transport for any OpenAI-compatible chat-completions endpoint (Gemini's
compat layer by default; Groq/OpenRouter/Ollama by changing three .env
values). Prompts and schemas live with their callers
(core/search_spec.py, core/evaluator.py).
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx


class LLMProvider(Protocol):
    def complete_json(self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any]) -> dict[str, Any]: ...


class OpenAICompatProvider:
    def __init__(self, base_url: str, api_key: str, model: str, client: httpx.Client | None = None):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self._client = client or httpx.Client(timeout=60.0)

    def complete_json(self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "schema": json_schema, "strict": True},
                },
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result: dict[str, Any] = json.loads(content)
        return result
