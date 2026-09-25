"""Turn a short conversation into a SearchSpec. The user confirms the spec
before anything is searched, which is also where a wrong PLZ gets caught
(the PLZ table has no place names, so the LLM proposes one).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from offer_finder.core.models import SearchSpec
from offer_finder.services.llm import LLMProvider, complete_validated

Role = Literal["user", "assistant"]

SYSTEM_PROMPT = """You help a buyer turn a wish into a search on Kleinanzeigen.de (German classifieds). Any product category.

From the conversation, fill in a search spec:
- query: 1-4 German search keywords as a seller would write them in a listing title (e.g. "OLED 65 Zoll", "Rennrad 56"). No filler words.
- product_summary: one short sentence describing what the buyer wants.
- requirements: hard requirements the buyer stated or clearly implied.
- nice_to_have: preferences the buyer mentioned that are not hard requirements.
- min_price_eur / max_price_eur: only if the buyer gave a budget. "under 700" or "around 700" means max_price_eur 700.
- location_text: the place the buyer named, as written.
- plz: a 5-digit German postal code for that place (the buyer will confirm it), or null if no place was named.
- radius_km: only if the buyer gave a distance, otherwise null.

Never invent requirements, budgets or places the buyer did not state or clearly imply. Use null or [] for anything unknown.
If something essential is missing (what product it is, or the search would be far too broad to be useful), set ready=false and ask ONE short question in the buyer's language. Otherwise set ready=true and question=null. Prefer ready=true; do not ask about things that are merely nice to know."""

_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}
_STRING_LIST = {"type": "array", "items": {"type": "string"}}

SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ready": {"type": "boolean"},
        "question": _NULLABLE_STRING,
        "query": _NULLABLE_STRING,
        "product_summary": _NULLABLE_STRING,
        "requirements": _STRING_LIST,
        "nice_to_have": _STRING_LIST,
        "min_price_eur": _NULLABLE_INT,
        "max_price_eur": _NULLABLE_INT,
        "location_text": _NULLABLE_STRING,
        "plz": _NULLABLE_STRING,
        "radius_km": _NULLABLE_INT,
    },
    "required": [
        "ready", "question", "query", "product_summary", "requirements", "nice_to_have",
        "min_price_eur", "max_price_eur", "location_text", "plz", "radius_km",
    ],
    "additionalProperties": False,
}

_PLZ_RE = re.compile(r"\d{5}")


class SpecError(Exception):
    """The conversation couldn't be turned into a spec — ask the user to rephrase."""


class _SpecResponse(BaseModel):
    ready: bool
    question: str | None = None
    query: str | None = None
    product_summary: str | None = None
    requirements: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    min_price_eur: int | None = None
    max_price_eur: int | None = None
    location_text: str | None = None
    plz: str | None = None
    radius_km: int | None = None


@dataclass
class SpecTurn:
    question: str | None = None
    spec: SearchSpec | None = None


def _user_prompt(history: list[tuple[Role, str]], asked: int, max_questions: int) -> str:
    transcript = "\n".join(f"{role}: {text}" for role, text in history)
    prompt = f"Conversation so far:\n{transcript}\n\nQuestions asked so far: {asked} of at most {max_questions}."
    if asked >= max_questions:
        prompt += "\nDo not ask another question: set ready=true and fill in the spec with what is known."
    return prompt


def next_turn(
    history: list[tuple[Role, str]], provider: LLMProvider, home_plz: str, max_questions: int = 3
) -> SpecTurn:
    asked = sum(1 for role, _ in history if role == "assistant")
    at_limit = asked >= max_questions
    try:
        parsed, _raw = complete_validated(
            provider, SYSTEM_PROMPT, _user_prompt(history, asked, max_questions), SPEC_SCHEMA, _SpecResponse
        )
    except Exception as exc:
        raise SpecError(f"could not build a search spec: {exc}") from exc

    if not parsed.ready and parsed.question and not at_limit:
        return SpecTurn(question=parsed.question)
    if not parsed.query:
        raise SpecError("model returned neither a question nor a search query")

    plz = parsed.plz if parsed.plz and _PLZ_RE.fullmatch(parsed.plz) else home_plz
    return SpecTurn(
        spec=SearchSpec(
            query=parsed.query,
            product_summary=parsed.product_summary or parsed.query,
            requirements=parsed.requirements,
            nice_to_have=parsed.nice_to_have,
            min_price_eur=parsed.min_price_eur,
            max_price_eur=parsed.max_price_eur,
            location_text=parsed.location_text,
            plz=plz,
            radius_km=parsed.radius_km,
        )
    )


def describe(spec: SearchSpec) -> str:
    if spec.min_price_eur is not None and spec.max_price_eur is not None:
        price = f"€{spec.min_price_eur}–€{spec.max_price_eur}"
    elif spec.max_price_eur is not None:
        price = f"up to €{spec.max_price_eur}"
    elif spec.min_price_eur is not None:
        price = f"from €{spec.min_price_eur}"
    else:
        price = "any price"
    place = f"{spec.location_text} (PLZ {spec.plz})" if spec.location_text else f"PLZ {spec.plz}"
    distance = f"within {spec.radius_km} km" if spec.radius_km is not None else "any distance"

    lines = [
        f'Search: "{spec.query}"',
        f"Looking for: {spec.product_summary}",
        f"Must have: {', '.join(spec.requirements) or '-'}",
        f"Nice to have: {', '.join(spec.nice_to_have) or '-'}",
        f"Price: {price}",
        f"Location: {place}, {distance}",
    ]
    return "\n".join(lines)
