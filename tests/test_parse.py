from datetime import date

from core.parse import (
    extract_condition_text,
    extract_plz,
    extract_posted_days_ago,
    extract_price_eur,
    extract_screen_size_inch,
    parse_detail_page,
    parse_search_results,
)
from tests.conftest import load_fixture

# --- price ---------------------------------------------------------------


def test_extract_price_simple():
    fv = extract_price_eur("Preis: 99 €")
    assert fv.value == 99
    assert fv.source == "regex"
    assert fv.confidence > 0.5


def test_extract_price_with_vb():
    fv = extract_price_eur("650 € VB")
    assert fv.value == 650


def test_extract_price_with_thousands_separator():
    fv = extract_price_eur("1.299 €")
    assert fv.value == 1299


def test_extract_price_with_festpreis():
    fv = extract_price_eur("Festpreis 500 €, keine Verhandlung")
    assert fv.value == 500


def test_extract_price_in_description_sentence():
    fv = extract_price_eur("Verkaufe meinen Fernseher, Preis 250 € VB, Abholung bevorzugt")
    assert fv.value == 250


def test_extract_price_missing():
    fv = extract_price_eur("VB")
    assert fv.value is None
    assert fv.source == "none"
    assert fv.confidence == 0.0


# --- plz -------------------------------------------------------------------


def test_extract_plz_from_location_text():
    fv = extract_plz("57074 Siegen")
    assert fv.value == "57074"


def test_extract_plz_missing():
    fv = extract_plz("Irgendwo in Deutschland")
    assert fv.value is None
    assert fv.source == "none"


# --- screen size -------------------------------------------------------------


def test_extract_screen_size_zoll():
    fv = extract_screen_size_inch("LG OLED 65 Zoll Fernseher")
    assert fv.value == 65.0


def test_extract_screen_size_double_quote():
    fv = extract_screen_size_inch('Samsung 55" TV wie neu')
    assert fv.value == 55.0


def test_extract_screen_size_prefers_zoll_over_cm():
    fv = extract_screen_size_inch("43 Zoll / 108 cm Bildschirmdiagonale")
    assert fv.value == 43.0


def test_extract_screen_size_falls_back_to_cm():
    fv = extract_screen_size_inch("Bildschirmdiagonale 108 cm, kein Zoll angegeben")
    assert fv.value is not None
    assert abs(fv.value - 42.5) < 0.1  # 108 / 2.54


def test_extract_screen_size_missing():
    fv = extract_screen_size_inch("Fernseher zu verkaufen, guter Zustand")
    assert fv.value is None
    assert fv.source == "none"


# --- condition text -------------------------------------------------------


def test_extract_condition_text_found():
    fv = extract_condition_text("Zustand: sehr guter Zustand, kaum benutzt")
    assert fv.value == "sehr guter Zustand"


def test_extract_condition_text_missing():
    fv = extract_condition_text("Kein Hinweis auf den Zustand hier")
    assert fv.value is None
    assert fv.source == "none"


# --- posted days ago --------------------------------------------------------


def test_extract_posted_days_ago_heute():
    fv = extract_posted_days_ago("Heute, 17:56", today=date(2026, 9, 20))
    assert fv.value == 0


def test_extract_posted_days_ago_gestern():
    fv = extract_posted_days_ago("Gestern, 09:10", today=date(2026, 9, 20))
    assert fv.value == 1


def test_extract_posted_days_ago_relative():
    fv = extract_posted_days_ago("vor 3 Tagen", today=date(2026, 9, 20))
    assert fv.value == 3


def test_extract_posted_days_ago_absolute_date():
    fv = extract_posted_days_ago("20.09.2026", today=date(2026, 9, 25))
    assert fv.value == 5


def test_extract_posted_days_ago_unparseable():
    fv = extract_posted_days_ago("irgendwann", today=date(2026, 9, 20))
    assert fv.value is None
    assert fv.source == "none"


# --- HTML parsing against real saved fixtures -------------------------------


def test_parse_search_results_finds_grundig_listing():
    html = load_fixture("tv_search_results.html")
    listings = parse_search_results(html)
    assert len(listings) > 10

    grundig = next(l for l in listings if l.listing_id == "3518580533")
    assert "Grundig" in grundig.title
    assert grundig.price_raw is not None and "99" in grundig.price_raw
    assert grundig.location_text is not None and "Siegen" in grundig.location_text
    assert grundig.posted_at_raw is not None
    assert str(grundig.url).endswith("/s-anzeige/grundig-43-gus-fernseher-43-zoll-4k-uhd-led-smart-tv/3518580533-175-1027")


def test_parse_detail_page_extracts_title_and_description():
    html = load_fixture("tv_listing_normal.html")
    page = parse_detail_page(html)
    assert "Grundig" in page.title
    assert "43 Zoll" in page.description
    assert "4K Ultra HD" in page.description
    assert page.location_text is not None and "Siegen" in page.location_text
    assert page.price_raw is not None and "99" in page.price_raw
    assert page.posted_raw is not None and "20.09.2026" in page.posted_raw
