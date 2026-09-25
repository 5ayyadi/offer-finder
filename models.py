"""Core data models shared across the pipeline.

Every regex-extracted field carries where it came from and how confident
that extraction is, so a missing value stays explicitly unknown instead of
being silently defaulted.
"""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field, HttpUrl

T = TypeVar("T")

Source = Literal["regex", "llm", "none"]


class FieldValue(BaseModel, Generic[T]):
    value: T | None
    source: Source
    confidence: float = Field(ge=0.0, le=1.0)


class ListingSummary(BaseModel):
    """Parsed from a Kleinanzeigen search-results page."""

    listing_id: str
    url: HttpUrl
    title: str
    price_raw: str | None = None
    location_text: str | None = None
    posted_at_raw: str | None = None


class SearchSpec(BaseModel):
    """What the user wants, built in conversation and confirmed by them."""

    query: str
    product_summary: str
    requirements: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    min_price_eur: int | None = None
    max_price_eur: int | None = None
    location_text: str | None = None
    plz: str
    radius_km: int | None = None


class ListingRef(BaseModel):
    """A listing link found in a Kleinanzeigen alert email."""

    listing_id: str
    url: str


ListingSource = Literal["search", "gmail"]
ListingStatus = Literal["discovered", "fetched", "out_of_range", "fetch_failed"]


class Listing(BaseModel):
    """One row of the `listings` table: raw listing data as scraped."""

    listing_id: str
    url: str
    title: str
    source: ListingSource
    status: ListingStatus
    search_spec_id: int | None = None
    description: str | None = None
    price_raw: str | None = None
    price_eur: int | None = None
    location_text: str | None = None
    plz: str | None = None
    posted_at_raw: str | None = None
    image_urls: list[str] = Field(default_factory=list)


# --- Evaluation: facts -> deterministic checks -> LLM judgment ---------------


class ListingFacts(BaseModel):
    """Layer 1: what the listing itself says, regex-extracted. Unknown stays None."""

    title: str
    description: str | None
    price_raw: str | None
    price_eur: FieldValue[int]
    plz: FieldValue[str]
    location_text: str | None
    condition: FieldValue[str]
    posted_days_ago: FieldValue[int]
    image_count: int
    redflag_hints: list[str]


class DeterministicChecks(BaseModel):
    """Layer 2: computed in Python against the spec. The LLM never does this math."""

    distance_km: float | None
    within_radius: bool | None
    price_within_budget: bool | None
    over_budget_eur: int | None
    missing_fields: list[str]
    hard_fail_reasons: list[str]


Verdict = Literal["good", "ok", "poor", "unclear"]


class ProductAssessment(BaseModel):
    """Is the product itself good and what the buyer wants, regardless of price."""

    verdict: Verdict
    requirements_match: Literal["match", "partial", "mismatch", "unclear"]
    summary: str


class DealAssessment(BaseModel):
    """Is this listing a good deal, regardless of how good the product is."""

    verdict: Verdict
    summary: str


class Claim(BaseModel):
    claim: str
    evidence: str
    verified: bool = False  # set by verify_evidence(), never by the LLM


class Concern(Claim):
    severity: Literal["low", "medium", "high"]


class Judgment(BaseModel):
    """Layer 3: the LLM's opinion, with evidence for each claim."""

    product_assessment: ProductAssessment
    deal_assessment: DealAssessment
    highlights: list[Claim] = Field(default_factory=list)
    concerns: list[Concern] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    score: int = Field(ge=0, le=100)
    confidence: Literal["low", "medium", "high"]


class Evaluation(BaseModel):
    listing_id: str
    search_spec_id: int | None
    facts: ListingFacts
    checks: DeterministicChecks
    judgment: Judgment
    recommended: bool
    model: str
    prompt_version: str
    user_prompt: str  # exactly what the LLM saw, for replay against labelled listings
    raw_response: dict[str, object]
