"""Kleinanzeigen search URLs for a confirmed SearchSpec, one per location.

Uses the site's own search-form endpoint so Kleinanzeigen resolves the PLZ
to its internal location id. Server-side filtering is never trusted on its
own: the pipeline re-checks price and distance client-side.
"""

from __future__ import annotations

from urllib.parse import urlencode

from offer_finder.core.models import Location, SearchSpec

_SEARCH_FORM_URL = "https://www.kleinanzeigen.de/s-suchanfrage.html"


def build_search_url(spec: SearchSpec, location: Location | None = None) -> str:
    """The search around one location (default: the spec's first)."""
    location = location or spec.locations[0]
    params: dict[str, str | int] = {"keywords": spec.query, "locationStr": location.plz}
    if spec.radius_km is not None:
        params["radius"] = spec.radius_km
    if spec.min_price_eur is not None:
        params["minPrice"] = spec.min_price_eur
    if spec.max_price_eur is not None:
        params["maxPrice"] = spec.max_price_eur
    return f"{_SEARCH_FORM_URL}?{urlencode(params)}"


def build_search_urls(spec: SearchSpec) -> list[tuple[Location, str]]:
    return [(location, build_search_url(spec, location)) for location in spec.locations]
