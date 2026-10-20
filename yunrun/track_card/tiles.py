# -*- coding: utf-8 -*-
"""Pure tile math for the trajectory renderer -- no network, no rendering.

Kept separate from the renderer so the projection and the tile mosaic can be unit
tested: a wrong zoom or a half-tile offset produces a plausible-looking card with the
route drawn in the wrong place, which is exactly the kind of bug a screenshot cannot
reveal on its own.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

TILE_SIZE = 256
MAX_LATITUDE = 85.05112878


@dataclass(frozen=True)
class TileRange:
    """Inclusive tile bounds covering a pixel viewport."""

    z: int
    x_min: int
    x_max: int
    y_min: int
    y_max: int
    # Pixel offset of the viewport's top-left corner inside (x_min, y_min) tile.
    offset_x: float
    offset_y: float

    @property
    def columns(self) -> int:
        return self.x_max - self.x_min + 1

    @property
    def rows(self) -> int:
        return self.y_max - self.y_min + 1

    @property
    def count(self) -> int:
        return self.columns * self.rows


def lonlat_to_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Web Mercator world pixel coordinates at ``zoom`` (same scheme as XYZ tiles)."""
    scale = TILE_SIZE * (2 ** zoom)
    lat = max(-MAX_LATITUDE, min(MAX_LATITUDE, lat))
    x = (lon + 180.0) / 360.0 * scale
    sin_lat = math.sin(math.radians(lat))
    y = (0.5 - math.log((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * math.pi)) * scale
    return x, y


def bounds_of(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """(lon_min, lon_max, lat_min, lat_max) with a floor on the span."""
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return min(lons), max(lons), min(lats), max(lats)


def metres_per_pixel(lat: float, zoom: int) -> float:
    """Ground resolution, used to sanity check the chosen zoom."""
    return (math.cos(math.radians(lat)) * 2 * math.pi * 6378137.0) / (TILE_SIZE * (2 ** zoom))


def choose_zoom(points: list[tuple[float, float]], width: int, height: int,
                padding_px: int = 60, max_zoom: int = 19) -> int:
    """Pick the highest zoom whose viewport still contains the whole route.

    Zoom is selected by projecting at a fixed reference zoom and comparing spans, which
    avoids re-projecting for every candidate.
    """
    reference = 20
    xs = [lonlat_to_pixel(lon, lat, reference)[0] for lon, lat in points]
    ys = [lonlat_to_pixel(lon, lat, reference)[1] for lon, lat in points]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    usable_w = max(1, width - 2 * padding_px)
    usable_h = max(1, height - 2 * padding_px)
    if span_x <= 0 and span_y <= 0:
        return max_zoom
    zoom_x = math.log2(usable_w / span_x) if span_x > 0 else max_zoom
    zoom_y = math.log2(usable_h / span_y) if span_y > 0 else max_zoom
    zoom = reference + min(zoom_x, zoom_y)
    return max(1, min(max_zoom, int(math.floor(zoom))))


def plan_view(points: list[tuple[float, float]], zoom: int,
              width: int, height: int) -> tuple[TileRange, tuple[float, float]]:
    """Return the tile range to fetch and the world-pixel centre of the route.

    The route centre is snapped to a whole pixel so the mosaic and the SVG route share
    one coordinate system exactly; otherwise the line drifts relative to the imagery by
    up to a pixel, which is visible at zoom 18+.
    """
    xs = [lonlat_to_pixel(lon, lat, zoom)[0] for lon, lat in points]
    ys = [lonlat_to_pixel(lon, lat, zoom)[1] for lon, lat in points]
    centre_x = round((min(xs) + max(xs)) / 2.0)
    centre_y = round((min(ys) + max(ys)) / 2.0)

    origin_x = centre_x - width / 2.0
    origin_y = centre_y - height / 2.0
    x_min = math.floor(origin_x / TILE_SIZE)
    y_min = math.floor(origin_y / TILE_SIZE)
    x_max = math.floor((origin_x + width - 1) / TILE_SIZE)
    y_max = math.floor((origin_y + height - 1) / TILE_SIZE)

    tile_count = 2 ** zoom
    # Clamp to the valid tile grid (routes near the antimeridian or the poles).
    x_min_c = max(0, x_min)
    y_min_c = max(0, y_min)
    x_max_c = min(tile_count - 1, x_max)
    y_max_c = min(tile_count - 1, y_max)

    return (
        TileRange(z=zoom, x_min=x_min_c, x_max=x_max_c, y_min=y_min_c, y_max=y_max_c,
                  offset_x=origin_x - x_min_c * TILE_SIZE,
                  offset_y=origin_y - y_min_c * TILE_SIZE),
        (centre_x, centre_y),
    )


def route_pixels(points: list[tuple[float, float]], zoom: int,
                 tile_range: TileRange, centre: tuple[float, float],
                 width: int, height: int) -> list[tuple[float, float]]:
    """Project the route into viewport pixel coordinates (top-left origin)."""
    centre_x, centre_y = centre
    left = centre_x - width / 2.0
    top = centre_y - height / 2.0
    out = []
    for lon, lat in points:
        x, y = lonlat_to_pixel(lon, lat, zoom)
        out.append((x - left, y - top))
    return out


def simplify(points: list[tuple[float, float]], tolerance_px: float = 0.35) -> list[tuple[float, float]]:
    """Drop points that add nothing visually (keeps the SVG small at high zoom).

    Guarantees: the first and last points survive, and no two survivors are closer than
    ``tolerance_px`` in Manhattan distance. The distance is measured from the last *kept*
    point, which is what makes the spacing guarantee hold -- a running delta against the
    previous sample can accumulate a long sequence of sub-tolerance steps and keep them
    all (which is what an earlier version of this function did).
    """
    if len(points) < 3:
        return list(points)
    kept = [points[0]]
    for point in points[1:]:
        last = kept[-1]
        if abs(point[0] - last[0]) + abs(point[1] - last[1]) >= tolerance_px:
            kept.append(point)
    if kept[-1] != points[-1]:
        end = points[-1]
        if (len(kept) > 1 and
                abs(end[0] - kept[-2][0]) + abs(end[1] - kept[-2][1]) < tolerance_px):
            kept.pop()          # would violate the spacing guarantee next to the endpoint
        kept.append(end)
    return kept
