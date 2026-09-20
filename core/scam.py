"""Deliberately simple scam/plausibility checks: a small list of obvious
red flags and one summed-and-capped penalty. Not a fraud-detection system —
just the loud, obvious signals from the original brief.
"""

from __future__ import annotations

import re

SHIPPING_ONLY_WEIGHT = 15.0
NACHNAHME_WEIGHT = 20.0
FRIENDS_AND_FAMILY_WEIGHT = 25.0
DEFECT_KEYWORD_WEIGHT = 15.0
PENALTY_CAP = 100.0

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


def penalty_for(
    shipping_only: bool,
    payment_method_mentions: list[str],
    defect_keywords_found: list[str],
) -> tuple[float, list[str]]:
    penalty = 0.0
    triggered: list[str] = []

    if shipping_only:
        penalty += SHIPPING_ONLY_WEIGHT
        triggered.append("shipping-only")

    for mention in payment_method_mentions:
        if mention == _NACHNAHME_LABEL:
            penalty += NACHNAHME_WEIGHT
            triggered.append(mention)
        elif mention == _FRIENDS_AND_FAMILY_LABEL:
            penalty += FRIENDS_AND_FAMILY_WEIGHT
            triggered.append(mention)

    for keyword in defect_keywords_found:
        penalty += DEFECT_KEYWORD_WEIGHT
        triggered.append(keyword)

    return min(penalty, PENALTY_CAP), triggered
