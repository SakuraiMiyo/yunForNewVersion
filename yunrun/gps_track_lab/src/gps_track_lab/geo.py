from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True)
class LocalFrame:
    """Small-area equirectangular frame centered on a reference coordinate."""

    lon0: float
    lat0: float

    def to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        lat0_rad = math.radians(self.lat0)
        x = math.radians(lon - self.lon0) * EARTH_RADIUS_M * math.cos(lat0_rad)
        y = math.radians(lat - self.lat0) * EARTH_RADIUS_M
        return x, y

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        lat0_rad = math.radians(self.lat0)
        lon = self.lon0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(lat0_rad)))
        lat = self.lat0 + math.degrees(y / EARTH_RADIUS_M)
        return lon, lat


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    h = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def percentile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile of an empty list")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lower = int(math.floor(pos))
    upper = int(math.ceil(pos))
    if lower == upper:
        return ordered[lower]
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

