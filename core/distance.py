"""Haversine distance over a local PLZ -> lat/lon table (no routing API)."""

from __future__ import annotations

import csv
import math
from pathlib import Path

PlzTable = dict[str, tuple[float, float]]

_EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def load_plz_table(path: str) -> PlzTable:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"PLZ table not found: {path}")
    table: PlzTable = {}
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            table[row["plz"]] = (float(row["lat"]), float(row["lon"]))
    return table


def plz_distance_km(plz_a: str, plz_b: str, table: PlzTable) -> float | None:
    if plz_a not in table or plz_b not in table:
        return None
    lat_a, lon_a = table[plz_a]
    lat_b, lon_b = table[plz_b]
    return haversine_km(lat_a, lon_a, lat_b, lon_b)
