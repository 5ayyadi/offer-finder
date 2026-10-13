"""Transport for any OpenAI-compatible chat-completions endpoint (Gemini's
compat layer by default; Groq/OpenRouter/Ollama by changing three .env
values). Prompts and schemas live with their callers
(core/search_spec.py, core/evaluator.py).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

ResponseFormat = Literal["json_schema", "json_object"]

M = TypeVar("M", bound=BaseModel)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class LLMProvider(Protocol):
    def complete_json(self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any]) -> dict[str, Any]: ...


class LLMResponseError(Exception):
    """The model twice returned something that isn't valid for the schema."""


class OpenAICompatProvider:
    """response_format="json_object" is the fallback for endpoints that reject
    strict JSON schemas; the schema then goes into the system prompt and the
    caller's pydantic validation does the enforcing."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
        response_format: ResponseFormat = "json_schema",
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self._client = client or httpx.Client(timeout=60.0)
        self._response_format = response_format

    def complete_json(self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any]) -> dict[str, Any]:
        response_format: dict[str, Any]
        if self._response_format == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            }
        else:
            response_format = {"type": "json_object"}
            system_prompt = f"{system_prompt}\n\nReply with one JSON object matching this JSON schema:\n{json.dumps(json_schema)}"

        started = time.monotonic()
        response = self._client.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": response_format,
            },
        )
        logger.debug("LLM %s answered %d in %.1fs", self.model, response.status_code, time.monotonic() - started)
        if response.is_error:  # keep the endpoint's explanation (bad key, rejected schema, rate limit)
            raise httpx.HTTPStatusError(
                f"{response.status_code} from LLM endpoint: {response.text[:500]}",
                request=response.request,
                response=response,
            )
        content = response.json()["choices"][0]["message"]["content"]
        result: dict[str, Any] = json.loads(_FENCE_RE.sub("", content))
        return result


def complete_validated(
    provider: LLMProvider,
    system_prompt: str,
    user_prompt: str,
    json_schema: dict[str, Any],
    model_cls: type[M],
) -> tuple[M, dict[str, Any]]:
    """One call plus one retry if the response is malformed or fails
    validation. Transport errors (HTTP, network) propagate unchanged."""
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            raw = provider.complete_json(system_prompt, user_prompt, json_schema)
            return model_cls.model_validate(raw), raw
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("LLM response was invalid (%s), %s", type(exc).__name__, "retrying" if _attempt == 0 else "giving up")
            last_error = exc
    raise LLMResponseError(f"invalid LLM response after retry: {last_error}") from last_error
