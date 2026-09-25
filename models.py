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
