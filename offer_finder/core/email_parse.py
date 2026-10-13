"""Kleinanzeigen saved-search alert emails -> listing links.

Only the listing URLs are taken from the email; the detail page (fetched
through the normal choke point) supplies everything else. Tracking-wrapped
links are handled by URL-decoding before matching.
"""

from __future__ import annotations

import html as html_module
import re
from urllib.parse import unquote

from offer_finder.core.models import ListingRef, SearchSpec
from offer_finder.core.parse import clean_html_text

_LISTING_URL_RE = re.compile(
    r"https?://(?:www\.)?kleinanzeigen\.de/s-anzeige/([^/\s\"'<>?#&]+)/(\d+)-(\d+)-(\d+)"
)
_WORD_RE = re.compile(r"\w+")


def extract_listing_refs(email_html: str) -> list[ListingRef]:
    text = html_module.unescape(email_html)
    for _ in range(2):  # tracking links percent-encode the target, sometimes twice
        text = unquote(text)

    refs: dict[str, ListingRef] = {}
    for match in _LISTING_URL_RE.finditer(text):
        slug, listing_id, category, location = match.groups()
        refs.setdefault(
            listing_id,
            ListingRef(
                listing_id=listing_id,
                url=f"https://www.kleinanzeigen.de/s-anzeige/{slug}/{listing_id}-{category}-{location}",
            ),
        )
    return list(refs.values())


def html_to_text(email_html: str) -> str:
    return clean_html_text(email_html)


def match_spec(text: str, specs: list[tuple[int, SearchSpec]]) -> tuple[int, SearchSpec] | None:
    """First spec (callers pass newest first) whose query words all appear in
    the email's subject/text. No match means the email's listings are judged
    without a spec."""
    words = set(_WORD_RE.findall(text.casefold()))
    for spec_id, spec in specs:
        query_words = _WORD_RE.findall(spec.query.casefold())
        if query_words and all(word in words for word in query_words):
            return spec_id, spec
    return None
