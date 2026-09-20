from core.scam import (
    detect_defect_keywords,
    detect_payment_red_flags,
    detect_shipping_only,
    penalty_for,
)
from tests.conftest import load_fixture

# --- detection ---------------------------------------------------------------


def test_detect_shipping_only_true():
    assert detect_shipping_only("Nur Versand, keine Abholung möglich") is True


def test_detect_shipping_only_false_when_pickup_mentioned():
    assert detect_shipping_only("Abholung bevorzugt, Versand nach Absprache") is False


def test_detect_payment_red_flags_nachnahme():
    flags = detect_payment_red_flags("Zahlung nur per Nachnahme")
    assert "Nachnahme" in flags


def test_detect_payment_red_flags_friends_and_family():
    flags = detect_payment_red_flags("Bitte PayPal Freunde und Familie senden")
    assert "PayPal Freunde und Familie" in flags


def test_detect_payment_red_flags_none_found():
    flags = detect_payment_red_flags("Zahlung bar bei Abholung")
    assert flags == []


def test_detect_defect_keywords_found():
    keywords = detect_defect_keywords("Achtung: Displaybruch, daher günstig. Nur für Bastler.")
    assert "Displaybruch" in keywords
    assert "Bastler" in keywords


def test_detect_defect_keywords_none_found():
    assert detect_defect_keywords("TV funktioniert einwandfrei, keine Mängel") == []


# --- penalty scoring -----------------------------------------------------------


def test_penalty_for_clean_listing_is_zero():
    penalty, triggered = penalty_for(
        shipping_only=False, payment_method_mentions=[], defect_keywords_found=[]
    )
    assert penalty == 0
    assert triggered == []


def test_penalty_for_shipping_only():
    penalty, triggered = penalty_for(
        shipping_only=True, payment_method_mentions=[], defect_keywords_found=[]
    )
    assert penalty == 15
    assert "shipping-only" in triggered


def test_penalty_for_nachnahme():
    penalty, _triggered = penalty_for(
        shipping_only=False, payment_method_mentions=["Nachnahme"], defect_keywords_found=[]
    )
    assert penalty == 20


def test_penalty_for_friends_and_family_is_worse_than_nachnahme():
    penalty_ff, _ = penalty_for(
        shipping_only=False,
        payment_method_mentions=["PayPal Freunde und Familie"],
        defect_keywords_found=[],
    )
    penalty_nn, _ = penalty_for(
        shipping_only=False, payment_method_mentions=["Nachnahme"], defect_keywords_found=[]
    )
    assert penalty_ff > penalty_nn


def test_penalty_for_defect_keywords_stack():
    penalty, triggered = penalty_for(
        shipping_only=False,
        payment_method_mentions=[],
        defect_keywords_found=["Displaybruch", "Bastler"],
    )
    assert penalty == 30
    assert "Displaybruch" in triggered
    assert "Bastler" in triggered


def test_penalty_for_is_capped_at_100():
    penalty, _ = penalty_for(
        shipping_only=True,
        payment_method_mentions=["Nachnahme", "PayPal Freunde und Familie"],
        defect_keywords_found=["Displaybruch", "Bastler", "defekt", "Ersatzteil", "Riss"],
    )
    assert penalty == 100


# --- integration against the scam fixture --------------------------------------


def test_scam_fixture_triggers_all_signal_types():
    from core.parse import parse_detail_page

    html = load_fixture("tv_listing_scam.html")
    description = parse_detail_page(html).description

    assert detect_shipping_only(description) is True
    assert "Nachnahme" in detect_payment_red_flags(description)
    assert "PayPal Freunde und Familie" in detect_payment_red_flags(description)
    assert "Displaybruch" in detect_defect_keywords(description)
    assert "Bastler" in detect_defect_keywords(description)
