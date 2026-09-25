from urllib.parse import parse_qs, urlsplit

from offer_finder.services.search_url import build_search_url
from tests.conftest import make_spec


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_search_url_uses_search_form_endpoint_with_all_filters():
    spec = make_spec(query="OLED 65 Zoll", plz="55116", radius_km=30, min_price_eur=200, max_price_eur=700)
    url = build_search_url(spec)
    assert url.startswith("https://www.kleinanzeigen.de/s-suchanfrage.html?")
    assert _query(url) == {
        "keywords": ["OLED 65 Zoll"],
        "locationStr": ["55116"],
        "radius": ["30"],
        "minPrice": ["200"],
        "maxPrice": ["700"],
    }


def test_search_url_omits_unset_filters():
    url = build_search_url(make_spec(radius_km=None, min_price_eur=None, max_price_eur=None))
    assert set(_query(url)) == {"keywords", "locationStr"}


def test_search_url_encodes_umlauts_and_spaces():
    url = build_search_url(make_spec(query="Kühlschrank groß"))
    assert "Kühlschrank" not in url
    assert _query(url)["keywords"] == ["Kühlschrank groß"]
