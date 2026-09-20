"""Load and match against the hand-curated TV catalog (catalog_tv.yaml).

The catalog is the one idea worth keeping from the elaborate architecture:
a small, human-reviewed list of models worth watching for, with a personal
reference price. Nothing here computes or invents that price — it's read
verbatim from the YAML file. See catalog_tv.yaml for the full rationale.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from models import CatalogMatch

CatalogDict = dict[str, dict[str, Any]]


def load_catalog(path: str) -> CatalogDict:
    catalog_path = Path(path)
    if not catalog_path.exists():
        raise FileNotFoundError(f"catalog file not found: {path}")
    with catalog_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def _normalize(text: str) -> str:
    text = text.lower()
    return re.sub(r"[^a-z0-9äöüß ]", " ", text)


def match(title: str, description: str, catalog: CatalogDict) -> CatalogMatch | None:
    normalized_title = _normalize(title)
    normalized_description = _normalize(description)

    for model_name, entry in catalog.items():
        aliases = [_normalize(alias) for alias in entry["aliases"]]
        if any(alias in normalized_title for alias in aliases):
            return CatalogMatch(
                model_name=model_name,
                fair_used_price_eur=entry["fair_used_price_eur"],
                quality=entry.get("quality"),
            )

    for model_name, entry in catalog.items():
        aliases = [_normalize(alias) for alias in entry["aliases"]]
        if any(alias in normalized_description for alias in aliases):
            return CatalogMatch(
                model_name=model_name,
                fair_used_price_eur=entry["fair_used_price_eur"],
                quality=entry.get("quality"),
            )

    return None
