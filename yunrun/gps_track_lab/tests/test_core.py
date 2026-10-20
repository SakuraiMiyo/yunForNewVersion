from __future__ import annotations

import json
import math
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from gps_track_lab.calibration import TrackCalibration
from gps_track_lab.generator import (
    SimulationSettings,
    _stadium_point,
    assess_physics,
    generate_best_candidate,
    simulate,
)
from gps_track_lab.web import generate_run, load_run


class TrackLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calibration = TrackCalibration(
            schema_version=1,
            model="stadium-centerline",
            center_lon=117.0,
            center_lat=31.0,
            heading_degrees=12.0,
            straight_half_length_m=36.5,
            curve_radius_m=36.5,
            lap_length_m=4 * 36.5 + 2 * math.pi * 36.5,
            fit_residual_m=1.2,
            source_points=500,
        )
        self.settings = SimulationSettings(dropout_probability=0.0)

    def test_stadium_closes(self) -> None:
        start = _stadium_point(self.calibration, 0.0)
        end = _stadium_point(self.calibration, self.calibration.lap_length_m)
        self.assertAlmostEqual(start[0], end[0], places=7)
        self.assertAlmostEqual(start[1], end[1], places=7)

    def test_same_seed_is_reproducible(self) -> None:
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first, _ = simulate(self.calibration, self.settings, 800.0, 6.0, seed=7, start_time=started)
        second, _ = simulate(self.calibration, self.settings, 800.0, 6.0, seed=7, start_time=started)
        self.assertEqual(first, second)

    def test_different_seed_changes_track(self) -> None:
        first, _ = simulate(self.calibration, self.settings, 800.0, 6.0, seed=7)
        second, _ = simulate(self.calibration, self.settings, 800.0, 6.0, seed=8)
        self.assertNotEqual(first[0].longitude, second[0].longitude)
        self.assertAlmostEqual(first[-1].modeled_distance_m, 800.0)
        self.assertAlmostEqual(second[-1].modeled_distance_m, 800.0)

    def test_reporting_order_and_physics(self) -> None:
        points, _ = simulate(
            self.calibration,
            self.settings,
            1200.0,
            6.0,
            seed=11,
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        arrivals = [datetime.fromisoformat(p.available_timestamp.replace("Z", "+00:00")) for p in points]
        self.assertTrue(all(b > a for a, b in zip(arrivals, arrivals[1:])))
        self.assertTrue(assess_physics(points, self.settings, 1200.0)["valid"])

    def test_candidate_batch_reports_selection_counts(self) -> None:
        points, seed, quality = generate_best_candidate(
            self.calibration,
            self.settings,
            distance_m=800.0,
            pace_min_per_km=6.0,
            candidates=4,
            seed=42,
        )
        self.assertEqual(quality["candidate_count"], 4)
        self.assertGreaterEqual(quality["valid_candidate_count"], 1)
        self.assertTrue(points)
        self.assertIsInstance(seed, int)

    def test_local_web_generation_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            track_path = root / "track.json"
            settings_path = root / "settings.json"
            runs_path = root / "runs"
            track_path.write_text(json.dumps(self.calibration.to_dict()), encoding="utf-8")
            settings_path.write_text(json.dumps(asdict(self.settings)), encoding="utf-8")
            with (
                patch("gps_track_lab.web.TRACK_PATH", track_path),
                patch("gps_track_lab.web.SETTINGS_PATH", settings_path),
                patch("gps_track_lab.web.RUNS_PATH", runs_path),
            ):
                result = generate_run({
                    "distance_km": 0.8,
                    "pace_min_per_km": 6.0,
                    "candidates": 3,
                    "seed": "77",
                })
                reloaded = load_run(result["run_id"])

            self.assertTrue(result["synthetic"])
            self.assertEqual(result["run_id"], reloaded["run_id"])
            self.assertEqual(result["point_count"], len(result["series"]["coordinates"]))
            self.assertTrue((runs_path / result["run_id"] / "track.geojson").is_file())


if __name__ == "__main__":
    unittest.main()
