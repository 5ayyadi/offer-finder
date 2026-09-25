from core.checks import compute_checks
from models import FieldValue
from tests.conftest import make_facts, make_spec

# Mainz 55116 and Mainz 55118 are ~2 km apart; Wiesbaden 65183 ~10 km, Frankfurt 60311 ~35 km
PLZ_TABLE = {
    "55116": (49.9990, 8.2710),
    "55118": (50.0080, 8.2600),
    "65183": (50.0820, 8.2400),
    "60311": (50.1110, 8.6820),
}


def _facts(price=650, plz="55118", **overrides):
    facts = make_facts()
    facts.price_eur = FieldValue(value=price, source="regex" if price is not None else "none",
                                 confidence=0.95 if price is not None else 0.0)
    facts.plz = FieldValue(value=plz, source="regex" if plz else "none", confidence=0.9 if plz else 0.0)
    return facts.model_copy(update=overrides)


def test_listing_within_budget_and_radius_passes():
    checks = compute_checks(_facts(), make_spec(max_price_eur=700, radius_km=30), PLZ_TABLE, "10115")
    assert checks.distance_km is not None and 1 < checks.distance_km < 3
    assert checks.within_radius is True
    assert checks.price_within_budget is True
    assert checks.over_budget_eur is None
    assert checks.hard_fail_reasons == []


def test_over_budget_is_a_hard_fail_with_exact_amount():
    checks = compute_checks(_facts(price=750), make_spec(max_price_eur=700), PLZ_TABLE, "10115")
    assert checks.price_within_budget is False
    assert checks.over_budget_eur == 50
    assert checks.hard_fail_reasons == ["€750 is €50 over the €700 budget"]


def test_below_min_price_is_outside_budget_but_not_a_hard_fail():
    checks = compute_checks(_facts(price=50), make_spec(min_price_eur=100, max_price_eur=700), PLZ_TABLE, "10115")
    assert checks.price_within_budget is False
    assert checks.hard_fail_reasons == []


def test_outside_radius_is_a_hard_fail():
    checks = compute_checks(_facts(plz="60311"), make_spec(radius_km=20), PLZ_TABLE, "10115")
    assert checks.within_radius is False
    assert len(checks.hard_fail_reasons) == 1
    assert "beyond the 20 km radius" in checks.hard_fail_reasons[0]


def test_unknown_location_and_price_stay_unknown_not_failed():
    checks = compute_checks(_facts(price=None, plz=None), make_spec(), PLZ_TABLE, "10115")
    assert checks.distance_km is None
    assert checks.within_radius is None
    assert checks.price_within_budget is None
    assert checks.hard_fail_reasons == []
    assert {"price", "location"} <= set(checks.missing_fields)


def test_missing_fields_lists_every_unknown_fact():
    facts = _facts().model_copy(update={"description": None, "image_count": 0})
    facts.posted_days_ago = FieldValue(value=None, source="none", confidence=0.0)
    facts.condition = FieldValue(value=None, source="none", confidence=0.0)
    checks = compute_checks(facts, make_spec(), PLZ_TABLE, "10115")
    assert checks.missing_fields == ["condition", "posting date", "description", "photos"]


def test_without_spec_distance_is_from_home_and_no_budget_applies():
    checks = compute_checks(_facts(plz="65183"), None, PLZ_TABLE, home_plz="55116")
    assert checks.distance_km is not None and 8 < checks.distance_km < 12
    assert checks.within_radius is None
    assert checks.price_within_budget is None
    assert checks.hard_fail_reasons == []
