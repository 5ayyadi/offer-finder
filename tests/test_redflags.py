from core.redflags import (
    detect_defect_keywords,
    detect_payment_red_flags,
    detect_shipping_only,
    hints,
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


def test_hints_combines_all_detectors_into_labels():
    text = "Nur Versand. Zahlung per Nachnahme. Displaybruch, für Bastler."
    assert hints(text) == ["shipping only", "Nachnahme", "Displaybruch", "Bastler"]


def test_hints_empty_for_clean_text():
    assert hints("Abholung in Mainz, Barzahlung, funktioniert einwandfrei") == []
