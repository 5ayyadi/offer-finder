"""Real Settings and fetch layer, kept offline: DRY_RUN serves fixture HTML
from a temporary cache."""

from typing import Any

from offer_finder.config import Settings
from offer_finder.core.models import SearchSpec
from offer_finder.services.fetch import _cache_write
from offer_finder.services.search_url import build_search_url
from tests.helpers.factories import load_fixture, make_spec
from tests.helpers.fakes import GRUNDIG_ID, SAMSUNG_ID

_DETAIL_URLS = {
    GRUNDIG_ID: "https://www.kleinanzeigen.de/s-anzeige/grundig-43-gus-fernseher-43-zoll-4k-uhd-led-smart-tv/3518580533-175-1027",
    SAMSUNG_ID: "https://www.kleinanzeigen.de/s-anzeige/samsung-qn90b-65-zoll-tv-bastler/9999999901-175-1234",
}
# The alert email links the same two listings under different slugs.
_EMAIL_DETAIL_URLS = {
    GRUNDIG_ID: "https://www.kleinanzeigen.de/s-anzeige/lg-oled65c27la-65-zoll/3518580533-175-1027",
    SAMSUNG_ID: "https://www.kleinanzeigen.de/s-anzeige/sony-bravia-a80j-65/9999999901-175-4321",
}


def offline_settings(tmp_path, **overrides) -> Settings:
    values: dict[str, Any] = {
        "db_path": str(tmp_path / "test.db"),
        "cache_dir": str(tmp_path / "cache"),
        "dry_run": True,
        "llm_min_interval_s": 0.0,
        "home_plz": "57074",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def offline_spec() -> SearchSpec:
    """The spec whose search page is cached by cache_search_fixtures()."""
    return make_spec(query="Fernseher", location_text="Siegen", plz="57074", radius_km=None, max_price_eur=200)


def cache_search_fixtures(settings: Settings) -> None:
    _cache_write(settings.cache_dir, build_search_url(offline_spec()), load_fixture("e2e_search_results.html"))
    _cache_write(settings.cache_dir, _DETAIL_URLS[GRUNDIG_ID], load_fixture("tv_listing_normal.html"))
    _cache_write(settings.cache_dir, _DETAIL_URLS[SAMSUNG_ID], load_fixture("tv_listing_scam.html"))


def cache_email_details(settings: Settings) -> None:
    _cache_write(settings.cache_dir, _EMAIL_DETAIL_URLS[GRUNDIG_ID], load_fixture("tv_listing_normal.html"))
    _cache_write(settings.cache_dir, _EMAIL_DETAIL_URLS[SAMSUNG_ID], load_fixture("tv_listing_scam.html"))
