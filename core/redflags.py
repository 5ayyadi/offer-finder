"""Cheap keyword-based red-flag detection. These are hints handed to the
LLM evaluator (labelled as heuristic), not a score or a verdict.
"""

from __future__ import annotations

import re

_FRIENDS_AND_FAMILY_LABEL = "PayPal Freunde und Familie"
_NACHNAHME_LABEL = "Nachnahme"

_DEFECT_KEYWORDS = ["Displaybruch", "Bastler", "defekt", "Ersatzteil", "Riss"]

_SHIPPING_ONLY_RE = re.compile(r"nur\s+versand", re.IGNORECASE)


def detect_shipping_only(text: str) -> bool:
    return _SHIPPING_ONLY_RE.search(text) is not None


def detect_payment_red_flags(text: str) -> list[str]:
    lowered = text.lower()
    flags = []
    if "nachnahme" in lowered:
        flags.append(_NACHNAHME_LABEL)
    if "freunde und familie" in lowered or "friends and family" in lowered:
        flags.append(_FRIENDS_AND_FAMILY_LABEL)
    return flags


def detect_defect_keywords(text: str) -> list[str]:
    lowered = text.lower()
    return [kw for kw in _DEFECT_KEYWORDS if kw.lower() in lowered]


def hints(text: str) -> list[str]:
    labels = ["shipping only"] if detect_shipping_only(text) else []
    return labels + detect_payment_red_flags(text) + detect_defect_keywords(text)
