from offer_finder.core.email_parse import extract_listing_refs, html_to_text, match_spec
from tests.helpers.factories import load_fixture, make_spec


def test_extracts_each_listing_once_with_canonical_url():
    refs = extract_listing_refs(load_fixture("gmail_alert_email.html"))
    assert [(r.listing_id, r.url) for r in refs] == [
        ("3518580533", "https://www.kleinanzeigen.de/s-anzeige/lg-oled65c27la-65-zoll/3518580533-175-1027"),
        ("9999999901", "https://www.kleinanzeigen.de/s-anzeige/sony-bravia-a80j-65/9999999901-175-4321"),
    ]


def test_email_without_listing_links_yields_nothing():
    assert extract_listing_refs('<a href="https://www.kleinanzeigen.de/m-meine-suchen.html">x</a>') == []


def test_html_to_text_strips_tags_and_entities():
    assert html_to_text("<p>Neue Anzeigen f&uuml;r <b>oled 65</b></p>") == "Neue Anzeigen für oled 65"


def test_match_spec_picks_first_spec_whose_query_words_all_appear():
    specs = [(3, make_spec(query="Rennrad 56")), (2, make_spec(query="OLED 65")), (1, make_spec(query="oled"))]
    match = match_spec("Neue Anzeigen für deine Suche „oled 65“", specs)
    assert match is not None and match[0] == 2


def test_match_spec_returns_none_without_a_match():
    assert match_spec("Neue Anzeigen für deine Suche „kinderwagen“", [(1, make_spec(query="OLED 65"))]) is None
