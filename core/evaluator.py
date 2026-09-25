"""Layer 3 of evaluation: the LLM's judgment, grounded in layers 1 and 2.

The model sees the listing text, the regex facts (null = not stated) and
the deterministic checks. It must back each highlight/concern with a
verbatim quote or a `fact:<name>` citation; verify_evidence() checks that
in Python, so unsupported claims never reach the user. Everything the
model saw is stored with the result so it can be replayed against a
hand-labelled set later.
"""

from __future__ import annotations

import json
import re
from typing import Any

from models import (
    Claim,
    DeterministicChecks,
    Evaluation,
    Judgment,
    Listing,
    ListingFacts,
    SearchSpec,
)
from services.llm import LLMProvider, complete_validated

EVALUATOR_PROMPT_VERSION = "2026-09-25.1"

SYSTEM_PROMPT = """You evaluate one Kleinanzeigen.de listing (German classifieds) for a buyer. Judge two things separately:

1. product_assessment: the product itself, ignoring the price. Is it a good product of its kind, and does it match the buyer's requirements (requirements_match)? A product that clearly lacks a stated requirement is "mismatch"; if the listing doesn't say, use "unclear".
2. deal_assessment: this listing as a purchase, ignoring how good the product is in general. Consider the price against the buyer's budget, condition, defects, completeness (accessories, cables, remote, ...), missing information, seller or listing red flags (shipping only, unusual payment methods, vague or copied text, too good to be true), and distance.

A great product at a bad price is product "good" / deal "poor". A mediocre product at a great price can be product "ok" / deal "good".

Grounding rules:
- Use only the listing text, facts and checks given to you. You know nothing else about this listing or seller.
- General product knowledge (what a model series is known for) is allowed in product_assessment.summary, phrased as general knowledge, never as something the listing says.
- There is no market price data. Never claim a price is below or above "market value", "typical price" or similar. Judge the price only against the buyer's budget and what the listing itself states (condition, included items, a stated Neupreis).
- Never turn missing information into a fact. Anything not stated goes into unknowns. Do not guess age, condition or included items.
- Every highlight and every concern needs evidence: either a short exact quote copied verbatim from the title or description, or "fact:<name>" citing one of the provided facts or checks by its key (for example "fact:over_budget_eur").
- Trust the deterministic checks. Do not redo distance or price arithmetic.
- Red-flag hints are keyword matches and can be false positives. Read the text yourself.

score (0-100): how worthwhile it is for this buyer to contact the seller. 80+ strong candidate, 60-79 worth a look, below 60 not worth it. A clear requirements mismatch or a serious red flag belongs below 40.
confidence: how sure you can be, given how much the listing says.
Write all summaries and claims in short English."""

_VERDICT = {"type": "string", "enum": ["good", "ok", "poor", "unclear"]}
_CLAIM_PROPERTIES = {"claim": {"type": "string"}, "evidence": {"type": "string"}}

JUDGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "product_assessment": {
            "type": "object",
            "properties": {
                "verdict": _VERDICT,
                "requirements_match": {"type": "string", "enum": ["match", "partial", "mismatch", "unclear"]},
                "summary": {"type": "string"},
            },
            "required": ["verdict", "requirements_match", "summary"],
            "additionalProperties": False,
        },
        "deal_assessment": {
            "type": "object",
            "properties": {"verdict": _VERDICT, "summary": {"type": "string"}},
            "required": ["verdict", "summary"],
            "additionalProperties": False,
        },
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _CLAIM_PROPERTIES,
                "required": ["claim", "evidence"],
                "additionalProperties": False,
            },
        },
        "concerns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {**_CLAIM_PROPERTIES, "severity": {"type": "string", "enum": ["low", "medium", "high"]}},
                "required": ["claim", "evidence", "severity"],
                "additionalProperties": False,
            },
        },
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": [
        "product_assessment", "deal_assessment", "highlights", "concerns", "unknowns", "score", "confidence",
    ],
    "additionalProperties": False,
}

_QUOTE_CHARS = "\"'„“”‚‘’«»"
_ELLIPSIS_RE = re.compile(r"\.\.\.|…")
_MIN_QUOTE_LENGTH = 3


class EvaluationError(Exception):
    """The listing couldn't be evaluated this time; it stays pending and is
    retried on the next run."""


def _facts_for_prompt(facts: ListingFacts) -> dict[str, Any]:
    return {
        "price_eur": facts.price_eur.value,
        "price_field": facts.price_raw,
        "plz": facts.plz.value,
        "location": facts.location_text,
        "condition": facts.condition.value,
        "posted_days_ago": facts.posted_days_ago.value,
        "image_count": facts.image_count,
        "redflag_hints": facts.redflag_hints,
    }


def build_user_prompt(facts: ListingFacts, checks: DeterministicChecks, spec: SearchSpec | None) -> str:
    spec_text = (
        spec.model_dump_json(indent=2)
        if spec
        else "No specific search spec. Judge the product on its own merits and the deal on the listing alone."
    )
    listing_text = (
        f"Title: {facts.title}\n"
        f"Price field: {facts.price_raw or '(none)'}\n"
        f"Location: {facts.location_text or '(none)'}\n"
        f"Description:\n{facts.description or '(no description)'}"
    )
    facts_json = json.dumps(_facts_for_prompt(facts), ensure_ascii=False, indent=2)
    checks_json = json.dumps(checks.model_dump(), ensure_ascii=False, indent=2)
    return (
        f"BUYER'S SEARCH SPEC\n{spec_text}\n\n"
        f"LISTING TEXT\n{listing_text}\n\n"
        f"FACTS (regex-extracted from the listing, null = not stated)\n{facts_json}\n\n"
        f"DETERMINISTIC CHECKS (exact, trust these)\n{checks_json}"
    )


def _normalize(text: str) -> str:
    stripped = text.casefold().strip().strip(_QUOTE_CHARS).strip()
    return " ".join(stripped.split())


def _is_supported(evidence: str, listing_text: str, citeable: dict[str, Any]) -> bool:
    evidence = evidence.strip()
    if evidence.startswith("fact:"):
        value = citeable.get(evidence.removeprefix("fact:").strip())
        return value is not None and value != []
    fragments = [_normalize(part) for part in _ELLIPSIS_RE.split(evidence)]
    fragments = [part for part in fragments if part]
    return bool(fragments) and all(
        len(part) >= _MIN_QUOTE_LENGTH and part in listing_text for part in fragments
    )


def verify_evidence(judgment: Judgment, facts: ListingFacts, checks: DeterministicChecks) -> Judgment:
    listing_text = _normalize(f"{facts.title} {facts.description or ''}")
    citeable = {**_facts_for_prompt(facts), **checks.model_dump()}

    def check(claim: Claim) -> Any:
        return claim.model_copy(update={"verified": _is_supported(claim.evidence, listing_text, citeable)})

    return judgment.model_copy(
        update={
            "highlights": [check(c) for c in judgment.highlights],
            "concerns": [check(c) for c in judgment.concerns],
        }
    )


def is_recommended(judgment: Judgment, checks: DeterministicChecks, threshold: int) -> bool:
    return (
        judgment.score >= threshold
        and not checks.hard_fail_reasons
        and judgment.product_assessment.requirements_match != "mismatch"
    )


def evaluate(
    listing: Listing,
    spec: SearchSpec | None,
    search_spec_id: int | None,
    facts: ListingFacts,
    checks: DeterministicChecks,
    provider: LLMProvider,
    model_name: str,
    threshold: int,
) -> Evaluation:
    user_prompt = build_user_prompt(facts, checks, spec)
    try:
        judgment, raw = complete_validated(provider, SYSTEM_PROMPT, user_prompt, JUDGMENT_SCHEMA, Judgment)
    except Exception as exc:
        raise EvaluationError(f"evaluating {listing.listing_id} failed: {exc}") from exc

    judgment = verify_evidence(judgment, facts, checks)
    return Evaluation(
        listing_id=listing.listing_id,
        search_spec_id=search_spec_id,
        facts=facts,
        checks=checks,
        judgment=judgment,
        recommended=is_recommended(judgment, checks, threshold),
        model=model_name,
        prompt_version=EVALUATOR_PROMPT_VERSION,
        user_prompt=user_prompt,
        raw_response=raw,
    )
