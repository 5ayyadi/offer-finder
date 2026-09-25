"""Shared test helpers: fixture loading and small model factories."""

from pathlib import Path
from typing import Any

from models import (
    Claim,
    Concern,
    DealAssessment,
    DeterministicChecks,
    Evaluation,
    FieldValue,
    Judgment,
    Listing,
    ListingFacts,
    ProductAssessment,
    SearchSpec,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_spec(**overrides: Any) -> SearchSpec:
    values: dict[str, Any] = {
        "query": "OLED 65 Zoll",
        "product_summary": "65+ inch OLED TV",
        "requirements": ["OLED panel", "at least 65 inch"],
        "max_price_eur": 700,
        "location_text": "Mainz",
        "plz": "55116",
        "radius_km": 30,
    }
    values.update(overrides)
    return SearchSpec(**values)


def make_listing(listing_id: str = "111", **overrides: Any) -> Listing:
    values: dict[str, Any] = {
        "listing_id": listing_id,
        "url": f"https://www.kleinanzeigen.de/s-anzeige/tv/{listing_id}-175-1",
        "title": "LG OLED65C27LA 65 Zoll",
        "source": "search",
        "status": "fetched",
        "description": "LG OLED 65 Zoll, sehr guter Zustand, mit Fernbedienung.",
        "price_raw": "650 €",
        "price_eur": 650,
        "location_text": "55118 Mainz",
        "plz": "55118",
        "image_urls": ["https://img.kleinanzeigen.de/a.jpg", "https://img.kleinanzeigen.de/b.jpg"],
    }
    values.update(overrides)
    return Listing(**values)


def make_judgment(**overrides: Any) -> Judgment:
    values: dict[str, Any] = {
        "product_assessment": ProductAssessment(
            verdict="good", requirements_match="match", summary="65 inch OLED as requested"
        ),
        "deal_assessment": DealAssessment(verdict="ok", summary="€50 under budget"),
        "highlights": [Claim(claim="Is an OLED", evidence="LG OLED 65 Zoll", verified=True)],
        "concerns": [
            Concern(claim="Age not stated", evidence="not stated", severity="low", verified=False)
        ],
        "unknowns": ["purchase year"],
        "score": 80,
        "confidence": "medium",
    }
    values.update(overrides)
    return Judgment(**values)


def make_facts() -> ListingFacts:
    return ListingFacts(
        title="LG OLED65C27LA 65 Zoll",
        description="LG OLED 65 Zoll, sehr guter Zustand, mit Fernbedienung.",
        price_eur=FieldValue(value=650, source="regex", confidence=0.95),
        plz=FieldValue(value="55118", source="regex", confidence=0.9),
        location_text="55118 Mainz",
        condition=FieldValue(value="sehr guter Zustand", source="regex", confidence=0.7),
        posted_days_ago=FieldValue(value=None, source="none", confidence=0.0),
        image_count=2,
        redflag_hints=[],
    )


def make_checks(**overrides: Any) -> DeterministicChecks:
    values: dict[str, Any] = {
        "distance_km": 2.1,
        "within_radius": True,
        "price_within_budget": True,
        "over_budget_eur": None,
        "missing_fields": ["posted_days_ago"],
        "hard_fail_reasons": [],
    }
    values.update(overrides)
    return DeterministicChecks(**values)


def make_evaluation(
    listing_id: str = "111", search_spec_id: int | None = 1, recommended: bool = True, **judgment: Any
) -> Evaluation:
    return Evaluation(
        listing_id=listing_id,
        search_spec_id=search_spec_id,
        facts=make_facts(),
        checks=make_checks(),
        judgment=make_judgment(**judgment),
        recommended=recommended,
        model="test-model",
        prompt_version="test",
        user_prompt="evaluate this",
        raw_response={"score": 80},
    )
