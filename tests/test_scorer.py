from core.scorer import score
from models import CatalogMatch, ExtractedListing, FieldValue

_PLZ_TABLE = {
    "55128": (49.9783406, 8.239083),
    "55131": (49.9889241, 8.2716688),  # ~3km from 55128
    "57074": (50.8749296, 8.0633292),  # ~100+km from 55128
}

_CATALOG_MATCH = CatalogMatch(model_name="LG OLED C2 65", fair_used_price_eur=900, quality="great")


def _listing(
    *,
    price=650,
    plz="55131",
    screen_size=65.0,
    condition="sehr guter Zustand",
    defects=None,
    shipping_only=False,
    payment_flags=None,
    days_ago=0,
    confidence=0.9,
) -> ExtractedListing:
    def fv(value, conf=confidence):
        return FieldValue(value=value, source="regex" if value is not None else "none", confidence=conf if value is not None else 0.0)

    return ExtractedListing(
        listing_id="1",
        price_eur=fv(price),
        plz=fv(plz),
        screen_size_inch=fv(screen_size),
        condition_text=fv(condition),
        defect_keywords_found=fv(defects if defects is not None else []),
        shipping_only=fv(shipping_only),
        payment_method_mentions=fv(payment_flags if payment_flags is not None else []),
        posted_days_ago=fv(days_ago),
    )


def _score(listing, **kwargs):
    return score(
        listing,
        _CATALOG_MATCH,
        home_plz=kwargs.pop("home_plz", "55128"),
        max_distance_km=kwargs.pop("max_distance_km", 50.0),
        plz_table=_PLZ_TABLE,
    )


# --- golden cases ------------------------------------------------------------


def test_clean_great_deal_scores_high():
    result = _score(_listing(price=450))  # 450 vs. fair 900 (50% off), close, fresh, no red flags
    assert result.total_score > 60
    assert result.scam_penalty == 0
    assert result.missing_data_penalty == 0


def test_fair_priced_listing_scores_lower_than_great_deal():
    fair_priced = _score(_listing(price=890))
    great_deal = _score(_listing(price=650))
    assert great_deal.total_score > fair_priced.total_score


def test_overpriced_listing_scores_low():
    result = _score(_listing(price=1400))  # well above the 900 reference
    assert result.value_gap_score < 0


def test_total_score_is_clipped_to_0_100():
    great = _score(_listing(price=1))
    terrible = _score(
        _listing(price=1400, defects=["Displaybruch", "Bastler", "defekt", "Ersatzteil", "Riss"], shipping_only=True)
    )
    assert 0 <= great.total_score <= 100
    assert 0 <= terrible.total_score <= 100


def test_scam_signals_lower_the_score_even_at_the_same_price():
    clean = _score(_listing(price=650))
    scammy = _score(_listing(price=650, defects=["Displaybruch"], shipping_only=True))
    assert scammy.total_score < clean.total_score
    assert scammy.scam_penalty > 0
    assert "shipping-only" in scammy.scam_signals_triggered
    assert "Displaybruch" in scammy.scam_signals_triggered


def test_missing_fields_lower_the_score_via_missing_data_penalty():
    complete = _score(_listing())
    incomplete = _score(_listing(condition=None, screen_size=None))
    assert incomplete.missing_data_penalty > 0
    assert incomplete.total_score < complete.total_score


def test_closer_listing_scores_higher_than_farther_one_all_else_equal():
    close = _score(_listing(plz="55131"))  # ~3km from home
    far = _score(_listing(plz="57074"))  # ~100km+ from home
    assert close.distance_score > far.distance_score
    assert close.total_score > far.total_score


def test_unknown_plz_defaults_to_neutral_distance():
    result = _score(_listing(plz="00000"))
    assert result.distance_score == 50.0


def test_fresher_listing_scores_higher_than_older_one():
    fresh = _score(_listing(days_ago=0))
    old = _score(_listing(days_ago=8))
    assert fresh.freshness_score > old.freshness_score
    assert fresh.total_score > old.total_score


def test_unknown_posted_date_defaults_to_neutral_freshness():
    listing = _listing(days_ago=None)
    result = _score(listing)
    assert result.freshness_score == 50.0


def test_missing_price_yields_zero_value_gap_not_a_crash():
    result = _score(_listing(price=None))
    assert result.value_gap_score == 0.0


def test_explanation_is_a_nonempty_list_of_strings():
    result = _score(_listing())
    assert len(result.explanation) > 0
    assert all(isinstance(line, str) for line in result.explanation)
