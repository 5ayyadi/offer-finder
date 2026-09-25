"""Kleinanzeigen search URL for a confirmed SearchSpec.

Uses the site's own search-form endpoint so Kleinanzeigen resolves the PLZ
to its internal location id. Server-side filtering is never trusted on its
own: the pipeline re-checks price and distance client-side.
"""

from __future__ import annotations

from urllib.parse import urlencode

from offer_finder.core.models import SearchSpec

_SEARCH_FORM_URL = "https://www.kleinanzeigen.de/s-suchanfrage.html"


def build_search_url(spec: SearchSpec) -> str:
    params: dict[str, str | int] = {"keywords": spec.query, "locationStr": spec.plz}
    if spec.radius_km is not None:
        params["radius"] = spec.radius_km
    if spec.min_price_eur is not None:
        params["minPrice"] = spec.min_price_eur
    if spec.max_price_eur is not None:
        params["maxPrice"] = spec.max_price_eur
    return f"{_SEARCH_FORM_URL}?{urlencode(params)}"
