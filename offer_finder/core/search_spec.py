"""Turn a short conversation into a SearchSpec. The user confirms the spec
before anything is searched, which is also where a wrong PLZ gets caught
(the PLZ table has no place names, so the LLM proposes one per place).
The spec also carries the LLM's quality guide for the category, so every
listing of the run is judged against the same idea of "the best".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from offer_finder.core.models import Location, SearchSpec
from offer_finder.services.llm import LLMProvider, complete_validated

Role = Literal["user", "assistant"]

SYSTEM_PROMPT = """You help a buyer turn a wish into a search on Kleinanzeigen.de (German classifieds). Any product category.

The buyer is always after the best product they can get for their budget: buying second-hand makes good products affordable (e.g. a used LG C1 OLED for the price of a new budget TV). So the buyer usually names only a category ("a router", "a 65 inch TV"). Never ask which brand or model they want, and don't turn brands into requirements unless the buyer named one.

From the conversation, fill in a search spec:
- query: 1-4 German search keywords for the category, as a seller would write them in a listing title (e.g. "WLAN Router", "OLED 65 Zoll", "Rennrad 56"). No brands or models unless the buyer named one. No filler words.
- product_summary: one short sentence describing what the buyer wants.
- requirements: hard requirements the buyer stated or clearly implied.
- nice_to_have: preferences the buyer mentioned that are not hard requirements.
- min_price_eur / max_price_eur: only if the buyer gave a budget. "under 700" or "around 700" means max_price_eur 700.
- locations: every place the buyer named, each with name (as written) and plz (a 5-digit German postal code for that place, the buyer will confirm it). [] if no place was named.
- radius_km: only if the buyer gave a distance, otherwise null. One radius applies around every place.
- quality_guide: 2-3 short sentences from your general product knowledge on what to look for in this category at this budget on the second-hand market: which model families or specs are top tier, which are solid mid-range, and which to avoid (outdated, budget, known problems). Name examples, not a complete list. Write it in English.

Never invent requirements, budgets or places the buyer did not state or clearly imply. Use null or [] for anything unknown.
If something essential is missing (what product it is, or the search would be far too broad to be useful), set ready=false and ask ONE short question. Write the question in the language the buyer writes in (English if they write English), even though the search keywords are German. Otherwise set ready=true and question=null. Prefer ready=true; do not ask about things that are merely nice to know."""

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
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": _NULLABLE_STRING, "plz": _NULLABLE_STRING},
                "required": ["name", "plz"],
                "additionalProperties": False,
            },
        },
        "radius_km": _NULLABLE_INT,
        "quality_guide": _NULLABLE_STRING,
    },
    "required": [
        "ready", "question", "query", "product_summary", "requirements", "nice_to_have",
        "min_price_eur", "max_price_eur", "locations", "radius_km", "quality_guide",
    ],
    "additionalProperties": False,
}

_PLZ_RE = re.compile(r"\d{5}")


class SpecError(Exception):
    """The conversation couldn't be turned into a spec — ask the user to rephrase."""


class _PlaceResponse(BaseModel):
    name: str | None = None
    plz: str | None = None


class _SpecResponse(BaseModel):
    ready: bool
    question: str | None = None
    query: str | None = None
    product_summary: str | None = None
    requirements: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    min_price_eur: int | None = None
    max_price_eur: int | None = None
    locations: list[_PlaceResponse] = Field(default_factory=list)
    radius_km: int | None = None
    quality_guide: str | None = None


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


def _locations(places: list[_PlaceResponse], home_plz: str) -> list[Location]:
    """Places with a valid PLZ (the buyer confirms them); home if none is left."""
    valid = [Location(name=p.name, plz=p.plz) for p in places if p.plz and _PLZ_RE.fullmatch(p.plz)]
    return valid or [Location(name=None, plz=home_plz)]


def next_turn(
    history: list[tuple[Role, str]],
    provider: LLMProvider,
    home_plz: str,
    max_questions: int = 3,
    default_radius_km: int | None = 20,
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

    return SpecTurn(
        spec=SearchSpec(
            query=parsed.query,
            product_summary=parsed.product_summary or parsed.query,
            requirements=parsed.requirements,
            nice_to_have=parsed.nice_to_have,
            min_price_eur=parsed.min_price_eur,
            max_price_eur=parsed.max_price_eur,
            locations=_locations(parsed.locations, home_plz),
            radius_km=parsed.radius_km if parsed.radius_km is not None else default_radius_km,
            quality_guide=parsed.quality_guide,
        )
    )


def _price_text(spec: SearchSpec) -> str:
    if spec.min_price_eur is not None and spec.max_price_eur is not None:
        return f"€{spec.min_price_eur}–€{spec.max_price_eur}"
    if spec.max_price_eur is not None:
        return f"up to €{spec.max_price_eur}"
    if spec.min_price_eur is not None:
        return f"from €{spec.min_price_eur}"
    return "any price"


def _distance_text(spec: SearchSpec) -> str:
    return f"within {spec.radius_km} km" if spec.radius_km is not None else "any distance"


def places_text(spec: SearchSpec) -> str:
    """'Wiesbaden, Mainz within 20 km' — short, for logs and the result list."""
    return f"{', '.join(loc.label for loc in spec.locations)} {_distance_text(spec)}"


def log_line(spec: SearchSpec) -> str:
    """The spec on one line, for the logs."""
    return f'"{spec.query}", {_price_text(spec)}, {places_text(spec)}'


def describe(spec: SearchSpec) -> str:
    price = _price_text(spec)
    place = ", ".join(f"{loc.name} (PLZ {loc.plz})" if loc.name else f"PLZ {loc.plz}" for loc in spec.locations)
    distance = _distance_text(spec) + (" of each" if len(spec.locations) > 1 and spec.radius_km is not None else "")

    lines = [
        f'Search: "{spec.query}"',
        f"Looking for: {spec.product_summary}",
        f"Must have: {', '.join(spec.requirements) or '-'}",
        f"Nice to have: {', '.join(spec.nice_to_have) or '-'}",
        f"Price: {price}",
        f"Location: {place}, {distance}",
    ]
    if spec.quality_guide:
        lines.append(f"Best looks like: {spec.quality_guide}")
    return "\n".join(lines)
