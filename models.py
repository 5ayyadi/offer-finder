"""Core data models shared across the pipeline.

Every extracted field carries where it came from (regex vs. LLM) and how
confident that extraction is, so missing or shaky data can pull a score
toward neutral instead of being silently defaulted.
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


class StructuredQuery(BaseModel):
    """Output of LLM query parsing, merged with config defaults."""

    max_price_eur: int | None = None
    min_price_eur: int | None = None
    min_screen_size_inch: float | None = None
    preferred_brands: list[str] = Field(default_factory=list)
    home_plz: str
    max_distance_km: float | None = None
    raw_user_message: str


class ListingSummary(BaseModel):
    """Parsed from a Kleinanzeigen search-results page."""

    listing_id: str
    url: HttpUrl
    title: str
    price_raw: str | None = None
    location_text: str | None = None
    posted_at_raw: str | None = None


class CatalogMatch(BaseModel):
    """A hit against the hand-curated catalog. The price is a personal
    reference judgment, not a market fact — see catalog_tv.yaml."""

    model_name: str
    fair_used_price_eur: float
    quality: str | None = None


class ExtractedListing(BaseModel):
    """Regex-extracted fields from a listing's detail page."""

    listing_id: str
    price_eur: FieldValue[int]
    plz: FieldValue[str]
    screen_size_inch: FieldValue[float]
    condition_text: FieldValue[str]
    defect_keywords_found: FieldValue[list[str]]
    shipping_only: FieldValue[bool]
    payment_method_mentions: FieldValue[list[str]]
    posted_days_ago: FieldValue[int]


class ScoreBreakdown(BaseModel):
    value_gap_score: float
    distance_score: float
    freshness_score: float
    missing_data_penalty: float
    scam_penalty: float
    scam_signals_triggered: list[str]
    total_score: float = Field(ge=0.0, le=100.0)
    explanation: list[str]


class ScoredListing(BaseModel):
    summary: ListingSummary
    fields: ExtractedListing
    catalog_match: CatalogMatch
    score: ScoreBreakdown
