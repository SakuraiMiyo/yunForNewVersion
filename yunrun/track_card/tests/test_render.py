# -*- coding: utf-8 -*-
"""Unit tests for the card renderer's pure layers.

These cover the parts whose bugs are silent: a wrong zoom or projection offset draws a
plausible-looking card with the route in the wrong place, and a metric formatting slip
only shows up as an odd-looking number. Rendering itself is covered by pixel_check.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]   # track_card/tests -> track_card
PROJECT_ROOT = PROJECT_ROOT.parent                    # -> 仓库根
sys.path.insert(0, str(PROJECT_ROOT))

from track_card.basemap import Basemap, sniff_image_mime  # noqa: E402
from track_card.pixel_check import _is_accent, _variance  # noqa: E402
from track_card.render_card import (  # noqa: E402
    CardData,
    build_facts,
    build_metric_cells,
    compile_card,
    load_tasklist_points,
)
from track_card.tiles import (  # noqa: E402
    choose_zoom,
    lonlat_to_pixel,
    metres_per_pixel,
    plan_view,
    route_pixels,
    simplify,
)

# A stand-in centre, shifted off any real campus on purpose -- only the relative geometry is used.
CENTER_LON, CENTER_LAT = 117.2000015, 31.7700010


def oval(steps: int = 400, radius_deg: float = 0.0008) -> list[tuple[float, float]]:
    return [
        (CENTER_LON + radius_deg * math.cos(2 * math.pi * i / steps),
         CENTER_LAT + radius_deg * 0.5 * math.sin(2 * math.pi * i / steps))
        for i in range(steps + 1)
    ]


class FixedBasemap(Basemap):
    """A basemap that never touches the network: one tiny valid PNG tile."""

    def __init__(self) -> None:
        super().__init__(provider="amap_vector")
        self.requested: list[tuple[int, int, int]] = []

    def tile(self, z: int, x: int, y: int):  # type: ignore[override]
        from track_card.basemap import Tile

        self.requested.append((z, x, y))
        return Tile(z, x, y, None, False)


class TileMathTests(unittest.TestCase):
    def test_world_pixel_origin(self) -> None:
        x, y = lonlat_to_pixel(-180.0, 85.05112878, 0)
        self.assertAlmostEqual(x, 0.0, places=6)
        self.assertAlmostEqual(y, 0.0, places=3)

    def test_known_tile_coordinate(self) -> None:
        # At zoom 18 this latitude/longitude lands in a specific tile; a projection change
        # would move the imagery somewhere else entirely. The numbers are pinned to the
        # placeholder centre above (shifted off any real campus on purpose), so they move
        # with it -- what this guards is the projection itself, not the location.
        x, y = lonlat_to_pixel(CENTER_LON, CENTER_LAT, 18)
        self.assertEqual(int(y // 256), 106652)
        self.assertEqual(int(x // 256), 216414)
        self.assertLess(x % 256, 256)
        self.assertLess(y % 256, 256)

    def test_zoom_shrinks_as_the_route_grows(self) -> None:
        small = choose_zoom(oval(200, 0.0003), 1080, 760, padding_px=70)
        large = choose_zoom(oval(200, 0.0060), 1080, 760, padding_px=70)
        self.assertGreater(small, large)
        self.assertLessEqual(small, 19)

    def test_route_stays_inside_the_viewport(self) -> None:
        points = oval(300)
        for zoom in (16, 17, 18, 19):
            tile_range, centre = plan_view(points, zoom, 1080, 760)
            canvas = route_pixels(points, zoom, tile_range, centre, 1080, 760)
            xs = [p[0] for p in canvas]
            ys = [p[1] for p in canvas]
            self.assertGreaterEqual(min(xs), -1.0)
            self.assertLessEqual(max(xs), 1081.0)
            self.assertGreaterEqual(min(ys), -1.0)
            self.assertLessEqual(max(ys), 761.0)

    def test_centre_is_the_middle_of_the_route(self) -> None:
        points = oval(300)
        canvas = route_pixels(points, 18, *plan_view(points, 18, 1080, 760), 1080, 760)
        mid_x = (max(p[0] for p in canvas) + min(p[0] for p in canvas)) / 2
        mid_y = (max(p[1] for p in canvas) + min(p[1] for p in canvas)) / 2
        self.assertAlmostEqual(mid_x, 540.0, delta=2.0)
        self.assertAlmostEqual(mid_y, 380.0, delta=2.0)

    def test_metres_per_pixel_reasonable(self) -> None:
        # A zoom-18 tile at this latitude is ~0.5 m/px; guard against a swapped constant.
        self.assertAlmostEqual(metres_per_pixel(CENTER_LAT, 18), 0.503, delta=0.02)

    def test_simplify_keeps_endpoints_and_spacing(self) -> None:
        # Points a hundredth of a pixel apart are invisible: they must collapse, while the
        # endpoints survive and no two survivors end up closer than the tolerance.
        points = [(i * 0.01, 0.0) for i in range(50)] + [(0.49 + i, 0.0) for i in range(10)]
        kept = simplify(points, tolerance_px=0.3)
        self.assertEqual(kept[0], points[0])
        self.assertEqual(kept[-1], points[-1])
        self.assertLess(len(kept), len(points))
        for first, second in zip(kept, kept[1:]):
            self.assertGreaterEqual(
                abs(second[0] - first[0]) + abs(second[1] - first[1]), 0.3 - 1e-9)


class MimeTests(unittest.TestCase):
    def test_detects_png_and_jpeg(self) -> None:
        self.assertEqual(sniff_image_mime(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20), "image/png")
        self.assertEqual(sniff_image_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 20), "image/jpeg")

    def test_rejects_error_pages(self) -> None:
        # 高德 returns JPEG from URLs ending in .png; an HTML error body must not be
        # accepted as a tile or it gets cached and poisons later renders.
        self.assertIsNone(sniff_image_mime(b"<html><body>403</body></html>"))
        self.assertIsNone(sniff_image_mime(b""))


class MetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = CardData(
            points=oval(100),
            distance_km=3.03, duration_s=962.0, pace_min_per_km=5.29,
            cadence=159, stride_m=1.181, step_total=2565, qualified=True,
        )

    def cells(self) -> dict[str, str]:
        return {cell["key"]: cell["value"] for cell in build_metric_cells(self.data)}

    def test_duration_formatting(self) -> None:
        self.assertEqual(self.cells()["duration_text"], "16:02")
        self.assertEqual(build_metric_cells(CardData(points=oval(10), duration_s=3725.0))[0]["value"],
                         "1:02:05")

    def test_distance_and_cadence(self) -> None:
        cells = self.cells()
        self.assertEqual(cells["distance_text"], "3.03 km")
        self.assertEqual(cells["cadence_text"], "159 步/分")
        self.assertEqual(cells["speed_text"], "11.34 km/h")

    def test_missing_values_do_not_crash(self) -> None:
        empty = CardData(points=oval(10))
        for cell in build_metric_cells(empty):
            if cell["key"] != "points_text":
                self.assertEqual(cell["value"], "—", cell)
        self.assertIsNone(build_facts(empty) or None)

    def test_facts_include_laps_and_seed(self) -> None:
        data = CardData(points=oval(10), laps=7.58, lap_length_m=400.0, seed=42,
                        labels=["本地仿真"])
        facts = build_facts(data)
        self.assertIn("7.58 圈 × 400 m", facts)
        self.assertIn("seed 42", facts)
        self.assertIn("本地仿真", facts)


class CompileTests(unittest.TestCase):
    def test_card_is_self_contained(self) -> None:
        document, meta = compile_card(CardData(points=oval(200), distance_km=3.0, duration_s=1000.0),
                                      basemap=FixedBasemap())
        self.assertIn("<!doctype html>", document.lower())
        self.assertIn("<svg", document)
        self.assertIn('class="r-line"', document)
        self.assertIn("起点", document)
        self.assertIn("终点", document)
        self.assertIn("时长", document)
        # No external references: the card must render with the network unplugged.
        self.assertNotIn("http://", document.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", document)
        self.assertGreater(meta["tiles"], 0)

    def test_short_trajectory_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compile_card(CardData(points=[(CENTER_LON, CENTER_LAT)]), basemap=FixedBasemap())

    def test_missing_tiles_become_placeholders(self) -> None:
        document, meta = compile_card(CardData(points=oval(200)), basemap=FixedBasemap())
        self.assertEqual(meta["tiles_failed"], meta["tiles"])
        self.assertIn("tile-missing", document)


class TasklistTests(unittest.TestCase):
    def test_reads_a_real_export(self) -> None:
        # track_card/tests -> track_card -> project root
        sample = (PROJECT_ROOT / "history" / "server_gt3km"
                  / "tasklist__20251_07_3.02km.json")
        if not sample.is_file():
            self.skipTest(f"sample export missing: {sample}")
        points = load_tasklist_points(sample)
        self.assertGreater(len(points), 400)
        lon, lat = points[0]
        self.assertAlmostEqual(lon, 117.03, delta=0.05)
        self.assertAlmostEqual(lat, 33.55, delta=0.05)


class PixelHelperTests(unittest.TestCase):
    def test_variance_separates_flat_from_imagery(self) -> None:
        flat = [(20, 22, 30)] * 100
        rich = [(20 + i % 60, 30 + (i * 3) % 70, 40 + (i * 7) % 90) for i in range(100)]
        self.assertLess(_variance(flat), 1.0)
        # Threshold used by check_card to call a map "has imagery".
        self.assertGreater(_variance(rich), 100.0)
        # A real rendered card's map region measures ~1223.
        self.assertGreater(_variance(rich * 8), 100.0)

    def test_accent_detection(self) -> None:
        self.assertTrue(_is_accent((34, 211, 238)))
        self.assertFalse(_is_accent((90, 90, 95)))
        self.assertFalse(_is_accent((200, 60, 60)))


if __name__ == "__main__":
    unittest.main()
