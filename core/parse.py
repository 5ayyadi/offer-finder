"""Regex-first extraction of structured fields from Kleinanzeigen HTML/text.

Every extractor returns a FieldValue so a missing/low-confidence field is
explicit rather than silently defaulted (see models.FieldValue).
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from datetime import date, datetime

from pydantic import HttpUrl

from models import FieldValue, ListingSummary

_PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*)(?:,(\d{1,2}))?\s*€")
_PLZ_RE = re.compile(r"\b(\d{5})\b")
_ZOLL_RE = re.compile(r"(\d{2,3})\s*(?:\"|''|Zoll|zoll)")
_CM_RE = re.compile(r"(\d{2,3})\s*cm")

_CONDITION_PHRASES = [
    "neuwertig",
    "sehr guter Zustand",
    "guter Zustand",
    "gebrauchsspuren",
    "defekt",
    "gebraucht",
]

_RELATIVE_DAY_WORDS = {"heute": 0, "gestern": 1}
_RELATIVE_DAYS_RE = re.compile(r"vor\s+(\d+)\s+Tag")
_ABSOLUTE_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


def extract_price_eur(text: str) -> FieldValue[int]:
    match = _PRICE_RE.search(text)
    if not match:
        return FieldValue(value=None, source="none", confidence=0.0)
    euros = int(match.group(1).replace(".", ""))
    return FieldValue(value=euros, source="regex", confidence=0.95)


def extract_plz(text: str) -> FieldValue[str]:
    match = _PLZ_RE.search(text)
    if not match:
        return FieldValue(value=None, source="none", confidence=0.0)
    return FieldValue(value=match.group(1), source="regex", confidence=0.9)


def extract_screen_size_inch(text: str) -> FieldValue[float]:
    zoll_match = _ZOLL_RE.search(text)
    if zoll_match:
        return FieldValue(value=float(zoll_match.group(1)), source="regex", confidence=0.9)
    cm_match = _CM_RE.search(text)
    if cm_match:
        inches = round(float(cm_match.group(1)) / 2.54, 1)
        return FieldValue(value=inches, source="regex", confidence=0.6)
    return FieldValue(value=None, source="none", confidence=0.0)


def extract_condition_text(text: str) -> FieldValue[str]:
    lowered = text.lower()
    for phrase in _CONDITION_PHRASES:
        if phrase.lower() in lowered:
            return FieldValue(value=phrase, source="regex", confidence=0.7)
    return FieldValue(value=None, source="none", confidence=0.0)


def extract_posted_days_ago(text: str, today: date | None = None) -> FieldValue[int]:
    today = today or datetime.now().astimezone().date()
    lowered = text.lower()

    for word, days_ago in _RELATIVE_DAY_WORDS.items():
        if word in lowered:
            return FieldValue(value=days_ago, source="regex", confidence=0.9)

    relative_match = _RELATIVE_DAYS_RE.search(text)
    if relative_match:
        return FieldValue(value=int(relative_match.group(1)), source="regex", confidence=0.85)

    absolute_match = _ABSOLUTE_DATE_RE.search(text)
    if absolute_match:
        day, month, year = (int(g) for g in absolute_match.groups())
        posted = date(year, month, day)
        return FieldValue(value=(today - posted).days, source="regex", confidence=0.9)

    return FieldValue(value=None, source="none", confidence=0.0)


# --- HTML parsing ------------------------------------------------------------

_ARTICLE_RE = re.compile(r'data-adid="(\d+)"\s+data-href="(/s-anzeige/[^"]+)"')
_TITLE_JSON_RE = re.compile(r'"title":"((?:[^"\\]|\\.)*)"')
_LOCATION_RE = re.compile(r"locationOutline.*?</svg><span>([^<]+)</span>", re.DOTALL)
_DATE_RE = re.compile(r"calendarOutline.*?</svg><span>([^<]+)</span>", re.DOTALL)
_CARD_PRICE_RE = re.compile(r">([0-9][0-9.,]*\s*€[^<]*)<")

_DETAIL_TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
_DETAIL_DESCRIPTION_RE = re.compile(
    r'id="viewad-description-text"[^>]*>(.*?)</p>', re.DOTALL
)
_DETAIL_LOCALITY_RE = re.compile(r'id="viewad-locality"[^>]*>(.*?)<', re.DOTALL)
_DETAIL_PRICE_RE = re.compile(r'id="viewad-price"[^>]*>(.*?)<', re.DOTALL)
_DETAIL_POSTED_RE = re.compile(r'icon-calendar-gray-simple"[^>]*></i><span>([^<]+)</span>')


@dataclass
class DetailPageText:
    title: str
    description: str
    location_text: str | None
    price_raw: str | None
    posted_raw: str | None


def _clean_html_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_search_results(html: str) -> list[ListingSummary]:
    matches = list(_ARTICLE_RE.finditer(html))
    if not matches:
        raise ValueError("no listing cards found on search-results page — layout may have changed")

    listings: list[ListingSummary] = []
    for i, match in enumerate(matches):
        listing_id, path = match.group(1), match.group(2)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        chunk = html[match.start():end]

        title_match = _TITLE_JSON_RE.search(chunk)
        title = html_module.unescape(title_match.group(1)) if title_match else path.rsplit("/", 2)[-2]

        location_match = _LOCATION_RE.search(chunk)
        date_match = _DATE_RE.search(chunk)
        price_match = _CARD_PRICE_RE.search(chunk)

        listings.append(
            ListingSummary(
                listing_id=listing_id,
                url=HttpUrl(f"https://www.kleinanzeigen.de{path}"),
                title=title,
                price_raw=price_match.group(1).strip() if price_match else None,
                location_text=location_match.group(1).strip() if location_match else None,
                posted_at_raw=date_match.group(1).strip() if date_match else None,
            )
        )
    return listings


def parse_detail_page(html: str) -> DetailPageText:
    title_match = _DETAIL_TITLE_RE.search(html)
    description_match = _DETAIL_DESCRIPTION_RE.search(html)
    if not title_match or not description_match:
        raise ValueError("could not find title/description on detail page — layout may have changed")

    title = _clean_html_text(title_match.group(1))
    description = _clean_html_text(description_match.group(1))

    locality_match = _DETAIL_LOCALITY_RE.search(html)
    price_match = _DETAIL_PRICE_RE.search(html)
    posted_match = _DETAIL_POSTED_RE.search(html)

    return DetailPageText(
        title=title,
        description=description,
        location_text=_clean_html_text(locality_match.group(1)) if locality_match else None,
        price_raw=_clean_html_text(price_match.group(1)) if price_match else None,
        posted_raw=_clean_html_text(posted_match.group(1)) if posted_match else None,
    )
