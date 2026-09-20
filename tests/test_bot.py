from pydantic import HttpUrl

from bot.app import format_results, is_allowed
from config import Settings
from models import (
    CatalogMatch,
    ExtractedListing,
    FieldValue,
    ListingSummary,
    ScoreBreakdown,
    ScoredListing,
)


def _settings(**overrides) -> Settings:
    return Settings(telegram_allowlist=[111, 222], **overrides)


def test_is_allowed_true_for_allowlisted_user():
    assert is_allowed(111, _settings()) is True


def test_is_allowed_false_for_unknown_user():
    assert is_allowed(999, _settings()) is False


def _scored_listing(*, model_name="LG OLED C2 65", price=650, total_score=72.3) -> ScoredListing:
    def fv(value):
        return FieldValue(value=value, source="regex", confidence=0.9)

    return ScoredListing(
        summary=ListingSummary(
            listing_id="1",
            url=HttpUrl("https://www.kleinanzeigen.de/s-anzeige/example/1"),
            title="Example listing",
        ),
        fields=ExtractedListing(
            listing_id="1",
            price_eur=fv(price),
            plz=fv("55131"),
            screen_size_inch=fv(65.0),
            condition_text=fv("guter Zustand"),
            defect_keywords_found=fv([]),
            shipping_only=fv(False),
            payment_method_mentions=fv([]),
            posted_days_ago=fv(0),
        ),
        catalog_match=CatalogMatch(model_name=model_name, fair_used_price_eur=900, quality="great"),
        score=ScoreBreakdown(
            value_gap_score=27.8,
            distance_score=90.0,
            freshness_score=100.0,
            missing_data_penalty=0.0,
            scam_penalty=0.0,
            scam_signals_triggered=[],
            total_score=total_score,
            explanation=["€650 vs. a personal reference of €900 for LG OLED C2 65"],
        ),
    )


def test_format_results_empty_list_is_a_friendly_message():
    text = format_results([])
    assert "no" in text.lower()
    assert "deal" in text.lower()


def test_format_results_includes_model_price_score_and_link():
    text = format_results([_scored_listing()])
    assert "LG OLED C2 65" in text
    assert "650" in text
    assert "72" in text
    assert "https://www.kleinanzeigen.de/s-anzeige/example/1" in text


def test_format_results_limits_to_top_five():
    listings = [_scored_listing(model_name=f"Model {i}", total_score=100 - i) for i in range(8)]
    text = format_results(listings)
    for i in range(5):
        assert f"Model {i}" in text
    for i in range(5, 8):
        assert f"Model {i}" not in text
