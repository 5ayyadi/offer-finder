"""Layer 2 of evaluation: exact, deterministic checks of the listing's facts
against the buyer's spec. The LLM is handed these results and never redoes
the arithmetic or geography itself.
"""

from __future__ import annotations

from core.distance import PlzTable, plz_distance_km
from models import DeterministicChecks, ListingFacts, SearchSpec


def compute_checks(
    facts: ListingFacts, spec: SearchSpec | None, plz_table: PlzTable, home_plz: str
) -> DeterministicChecks:
    target_plz = spec.plz if spec else home_plz
    raw_distance = plz_distance_km(facts.plz.value, target_plz, plz_table) if facts.plz.value else None
    distance = round(raw_distance, 1) if raw_distance is not None else None
    radius = spec.radius_km if spec else None
    within_radius = None if distance is None or radius is None else distance <= radius

    price = facts.price_eur.value
    min_price = spec.min_price_eur if spec else None
    max_price = spec.max_price_eur if spec else None
    if price is None or (min_price is None and max_price is None):
        price_within_budget = None
    else:
        price_within_budget = (min_price is None or price >= min_price) and (
            max_price is None or price <= max_price
        )
    over_budget = price - max_price if price is not None and max_price is not None and price > max_price else None

    hard_fail_reasons = []
    if over_budget is not None:
        hard_fail_reasons.append(f"€{price} is €{over_budget} over the €{max_price} budget")
    if within_radius is False:
        hard_fail_reasons.append(f"{distance:g} km away, beyond the {radius} km radius")

    return DeterministicChecks(
        distance_km=distance,
        within_radius=within_radius,
        price_within_budget=price_within_budget,
        over_budget_eur=over_budget,
        missing_fields=_missing_fields(facts),
        hard_fail_reasons=hard_fail_reasons,
    )


def _missing_fields(facts: ListingFacts) -> list[str]:
    unknown = {
        "price": facts.price_eur.value is None,
        "location": facts.plz.value is None,
        "condition": facts.condition.value is None,
        "posting date": facts.posted_days_ago.value is None,
        "description": not facts.description,
        "photos": facts.image_count == 0,
    }
    return [name for name, is_unknown in unknown.items() if is_unknown]
