"""LLM usage for V1 is scoped to exactly one job: turn a natural-language
Telegram message into a StructuredQuery. It never judges deals or invents
prices — that's the deterministic core's job (core/scorer.py).

Any OpenAI-compatible chat-completions endpoint works here (Gemini's
compat layer by default; Groq/OpenRouter/Ollama via the same class by
just changing the three .env values).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from models import StructuredQuery

SYSTEM_PROMPT = (
    "You convert a user's natural-language request for TV deals on "
    "Kleinanzeigen (German classifieds) into a structured JSON search "
    "query. Extract only what the user actually said or clearly implied "
    "— never invent numeric values you were not given. Use null for "
    "anything unspecified. Output strict JSON matching the given schema, "
    "no prose, no markdown fences."
)

QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "max_price_eur": {"type": ["integer", "null"]},
        "min_price_eur": {"type": ["integer", "null"]},
        "min_screen_size_inch": {"type": ["number", "null"]},
        "preferred_brands": {"type": "array", "items": {"type": "string"}},
        "plz": {"type": ["string", "null"]},
        "location_text_raw": {"type": ["string", "null"]},
        "max_distance_km": {"type": ["number", "null"]},
    },
    "required": ["preferred_brands"],
    "additionalProperties": False,
}


class QueryParseError(Exception):
    """Raised when the LLM response can't be turned into a StructuredQuery.

    Callers (bot.py) should catch this and ask the user to rephrase rather
    than silently guessing a query.
    """


class LLMProvider(Protocol):
    def complete_json(self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any]) -> dict[str, Any]: ...


class OpenAICompatProvider:
    def __init__(self, base_url: str, api_key: str, model: str, client: httpx.Client | None = None):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.Client(timeout=30.0)

    def complete_json(self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "structured_query",
                        "schema": json_schema,
                        "strict": True,
                    },
                },
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result: dict[str, Any] = json.loads(content)
        return result


def _build_user_prompt(raw_message: str, today: date) -> str:
    return (
        f'User message: "{raw_message}"\n'
        f"Today's date: {today.isoformat()}\n\n"
        f"Extract into this schema:\n{json.dumps(QUERY_SCHEMA)}\n\n"
        "Rules:\n"
        "- price values are integers in EUR, no currency symbols\n"
        "- plz is a 5-digit German postal code string if the user gave one; "
        "if the user named a place instead, put it in location_text_raw and "
        "leave plz null\n"
        "- max_distance_km is null unless the user stated a radius\n"
        "- preferred_brands defaults to []"
    )


def parse_query(
    raw_message: str,
    home_plz_default: str,
    provider: LLMProvider,
    today: date | None = None,
) -> StructuredQuery:
    today = today or datetime.now().astimezone().date()
    user_prompt = _build_user_prompt(raw_message, today)

    try:
        response = provider.complete_json(SYSTEM_PROMPT, user_prompt, QUERY_SCHEMA)
    except Exception as exc:
        raise QueryParseError(f"LLM query parsing failed: {exc}") from exc

    plz = response.get("plz") or home_plz_default

    try:
        return StructuredQuery(
            max_price_eur=response.get("max_price_eur"),
            min_price_eur=response.get("min_price_eur"),
            min_screen_size_inch=response.get("min_screen_size_inch"),
            preferred_brands=response.get("preferred_brands", []),
            home_plz=plz,
            max_distance_km=response.get("max_distance_km"),
            raw_user_message=raw_message,
        )
    except ValidationError as exc:
        raise QueryParseError(f"LLM response didn't match the expected schema: {exc}") from exc
