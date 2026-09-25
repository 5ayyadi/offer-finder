from datetime import date

import pytest

from core.parse import (
    extract_condition_text,
    extract_facts,
    extract_plz,
    extract_posted_days_ago,
    extract_price_eur,
    parse_detail_page,
    parse_search_results,
)
from tests.conftest import load_fixture, make_listing

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


def test_parse_detail_page_extracts_unique_large_image_urls():
    page = parse_detail_page(load_fixture("tv_listing_normal.html"))
    assert page.image_urls == [
        "https://img.kleinanzeigen.de/api/v1/prod-ads/images/82/82aa6eb0-07e7-40a7-8d7c-f622eceab17e?rule=$_59.JPG",
        "https://img.kleinanzeigen.de/api/v1/prod-ads/images/fc/fca574a4-6432-4709-bf72-97268328826d?rule=$_59.JPG",
    ]


def test_parse_search_results_empty_results_page_returns_no_listings():
    html = '<div id="srchrslt-content"><p>Leider keine Ergebnisse</p></div>'
    assert parse_search_results(html) == []


def test_parse_search_results_unrecognised_page_still_raises():
    with pytest.raises(ValueError):
        parse_search_results("<html><body>something else entirely</body></html>")


# --- facts (layer 1 of evaluation) --------------------------------------------


def test_extract_facts_from_listing():
    listing = make_listing(
        price_raw="650 € VB",
        location_text="55118 Mainz - Neustadt",
        posted_at_raw="20.09.2026",
        description="Sehr guter Zustand. Nur Versand.",
    )
    facts = extract_facts(listing, today=date(2026, 9, 25))
    assert facts.price_eur.value == 650
    assert facts.price_raw == "650 € VB"
    assert facts.plz.value == "55118"
    assert facts.condition.value == "sehr guter Zustand"
    assert facts.posted_days_ago.value == 5
    assert facts.image_count == 2
    assert facts.redflag_hints == ["shipping only"]


def test_extract_facts_keeps_missing_values_unknown():
    listing = make_listing(price_raw="VB", location_text=None, posted_at_raw=None, description=None)
    facts = extract_facts(listing, today=date(2026, 9, 25))
    assert facts.price_eur.value is None and facts.price_eur.source == "none"
    assert facts.plz.value is None
    assert facts.condition.value is None
    assert facts.posted_days_ago.value is None
    assert facts.description is None


def test_extract_facts_never_takes_price_from_description():
    # "Neupreis" in the text is not the asking price — asking price is unknown here
    listing = make_listing(price_raw=None, description="Neupreis 1.500 €, kaum benutzt")
    assert extract_facts(listing, today=date(2026, 9, 25)).price_eur.value is None


def test_parse_detail_page_extracts_structured_attributes_and_shipping():
    page = parse_detail_page(load_fixture("tv_listing_normal.html"))
    assert page.attributes == {"Art": "Fernseher", "Zustand": "Sehr Gut", "Versand": "+ Versand ab 0,99 €"}


def test_extract_facts_prefers_the_structured_condition_field():
    listing = make_listing(attributes={"Zustand": "Sehr Gut"}, description="Gebrauchsspuren vorhanden")
    facts = extract_facts(listing, today=date(2026, 9, 25))
    assert facts.condition.value == "Sehr Gut"
    assert facts.condition.confidence > 0.9
    assert facts.attributes == {"Zustand": "Sehr Gut"}


def test_extract_facts_falls_back_to_condition_phrases_in_description():
    listing = make_listing(attributes={}, description="Gebrauchsspuren vorhanden")
    assert extract_facts(listing, today=date(2026, 9, 25)).condition.value == "gebrauchsspuren"


def test_shipping_only_hint_also_comes_from_the_structured_shipping_field():
    listing = make_listing(attributes={"Versand": "Nur Versand"}, description="Top Zustand")
    assert extract_facts(listing, today=date(2026, 9, 25)).redflag_hints == ["shipping only"]
