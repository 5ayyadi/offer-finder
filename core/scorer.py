"""The pure scorer: a boring weighted sum. No I/O, no LLM, no network —
same inputs always produce the same ScoreBreakdown, so it's fully
replayable against stored listings with edited weights.
"""

from __future__ import annotations

from core.distance import PlzTable, plz_distance_km
from core.scam import penalty_for
from models import CatalogMatch, ExtractedListing, ScoreBreakdown

VALUE_GAP_WEIGHT = 0.60
DISTANCE_WEIGHT = 0.25
FRESHNESS_WEIGHT = 0.15

VALUE_GAP_MIN = -30.0
VALUE_GAP_MAX = 100.0

FRESHNESS_DECAY_PER_DAY = 10.0
NEUTRAL_SCORE = 50.0

MISSING_FIELD_PENALTY = 3.0
MISSING_FIELD_PENALTY_CAP = 20.0

_CRITICAL_FIELD_NAMES = ("price_eur", "plz", "screen_size_inch", "condition_text")


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _value_gap_score(price: int | None, fair_used_price_eur: float) -> float:
    if price is None:
        return 0.0
    gap_pct = ((fair_used_price_eur - price) / fair_used_price_eur) * 100
    return _clip(gap_pct, VALUE_GAP_MIN, VALUE_GAP_MAX)


def _distance_score(
    listing_plz: str | None, home_plz: str, max_distance_km: float, plz_table: PlzTable
) -> float:
    if listing_plz is None:
        return NEUTRAL_SCORE
    distance_km = plz_distance_km(home_plz, listing_plz, plz_table)
    if distance_km is None:
        return NEUTRAL_SCORE
    return _clip((max_distance_km - distance_km) / max_distance_km, 0.0, 1.0) * 100


def _freshness_score(posted_days_ago: int | None) -> float:
    if posted_days_ago is None:
        return NEUTRAL_SCORE
    return _clip(100 - posted_days_ago * FRESHNESS_DECAY_PER_DAY, 0.0, 100.0)


def _missing_data_penalty(extracted: ExtractedListing) -> float:
    missing_count = 0
    for name in _CRITICAL_FIELD_NAMES:
        field = getattr(extracted, name)
        if field.value is None or field.confidence < 0.5:
            missing_count += 1
    return min(missing_count * MISSING_FIELD_PENALTY, MISSING_FIELD_PENALTY_CAP)


def score(
    extracted: ExtractedListing,
    catalog_match: CatalogMatch,
    home_plz: str,
    max_distance_km: float,
    plz_table: PlzTable,
) -> ScoreBreakdown:
    value_gap = _value_gap_score(extracted.price_eur.value, catalog_match.fair_used_price_eur)
    distance = _distance_score(extracted.plz.value, home_plz, max_distance_km, plz_table)
    freshness = _freshness_score(extracted.posted_days_ago.value)
    missing_penalty = _missing_data_penalty(extracted)
    scam_penalty, triggered = penalty_for(
        shipping_only=extracted.shipping_only.value or False,
        payment_method_mentions=extracted.payment_method_mentions.value or [],
        defect_keywords_found=extracted.defect_keywords_found.value or [],
    )

    total = _clip(
        VALUE_GAP_WEIGHT * value_gap
        + DISTANCE_WEIGHT * distance
        + FRESHNESS_WEIGHT * freshness
        - missing_penalty
        - scam_penalty,
        0.0,
        100.0,
    )

    explanation = [
        f"€{extracted.price_eur.value} vs. a personal reference of "
        f"€{catalog_match.fair_used_price_eur:.0f} for {catalog_match.model_name}"
        if extracted.price_eur.value is not None
        else "price unknown — value gap treated as neutral",
        f"{extracted.posted_days_ago.value} day(s) ago"
        if extracted.posted_days_ago.value is not None
        else "posting date unknown",
        f"{len(triggered)} red flag(s): {', '.join(triggered)}" if triggered else "no red flags found",
    ]
    if missing_penalty > 0:
        explanation.append("some fields missing or uncertain — score pulled toward neutral")

    return ScoreBreakdown(
        value_gap_score=value_gap,
        distance_score=distance,
        freshness_score=freshness,
        missing_data_penalty=missing_penalty,
        scam_penalty=scam_penalty,
        scam_signals_triggered=triggered,
        total_score=total,
        explanation=explanation,
    )
