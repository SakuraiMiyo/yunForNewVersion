# -*- coding: utf-8 -*-
"""Summarise a directory of tasklist exports into the statistics a generator must match.

Everything here is a *measurement* over recorded runs, so it can be computed identically
for real exports and for generated ones. That symmetry is the point: ``compare_generator``
runs the same function over both sets and diffs the two reports.

Metrics per record:
  mileage / duration / pace        -- what the server field says
  point_count / sample_step_s      -- how densely the track is sampled
  point_spacing_m                  -- straight-line distance between consecutive points
  offset_sigma_m / offset_p95_m    -- distance from the fitted stadium centreline
                                      (runner wander + GPS error combined)
  stride_m / cadence               -- runMileage/runStep and runStep*60/duration

Usage:
    python tools/track_stats.py --input <dir> --reference     # human-readable table
    python tools/track_stats.py --input <dir> --json out.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
LAB_ROOT = HERE.parent
sys.path.insert(0, str(LAB_ROOT / "src"))

from gps_track_lab.calibration import load_points, _distance_to_stadium  # noqa: E402
from gps_track_lab.geo import LocalFrame, haversine_m, percentile  # noqa: E402

from recalibrate_track import rotate_proper, fit_rotation  # noqa: E402


def _walk_points(document: dict[str, Any]) -> list[dict[str, Any]]:
    data = document.get("data", {})
    points = data.get("pointsList")
    return points if isinstance(points, list) else []


def record_metrics(path: Path) -> dict[str, Any] | None:
    """Measure one export file. Returns None when the file is not a usable tasklist."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return metrics_from_document(document, path.name)


def metrics_from_document(document: dict[str, Any], name: str = "<memory>") -> dict[str, Any] | None:
    """Measure a tasklist document already in memory.

    Split out so a freshly generated batch can be scored without writing it to disk.
    """
    data = document.get("data")
    if not isinstance(data, dict):
        return None
    points = _walk_points(document)
    if len(points) < 20:
        return None

    try:
        mileage = float(data.get("recordMileage"))
        duration = float(data.get("duration"))
    except (TypeError, ValueError):
        return None

    lonlats: list[tuple[float, float]] = []
    run_times: list[float] = []
    for point in points:
        try:
            lonlats.append(tuple(float(x) for x in str(point["point"]).split(",", 1)))  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        try:
            run_times.append(float(point.get("runTime")))
        except (TypeError, ValueError):
            run_times.append(float("nan"))

    if len(lonlats) < 20:
        return None

    spacing = [haversine_m(a, b) for a, b in zip(lonlats, lonlats[1:])]
    sample_steps = [
        b_ - a_ for a_, b_ in zip(run_times, run_times[1:])
        if not (math.isnan(a_) or math.isnan(b_)) and 0 < b_ - a_ <= 60
    ]

    # The server's `speed` field is a per-point pace in min/km. Its spread is a
    # visible signature: a constant-pace generator is flat where real runs vary.
    point_paces = []
    for point in points:
        try:
            value = float(point.get("speed"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and 0 < value < 60:
            point_paces.append(value)

    # Per-sample distance as actually recorded by the app.
    mileage_steps = []
    for first, second in zip(points, points[1:]):
        try:
            delta = float(second.get("runMileage")) - float(first.get("runMileage"))
        except (TypeError, ValueError):
            continue
        if 0 < delta < 100:
            mileage_steps.append(delta)

    # Observed point-to-point speed from the coordinates on the runTime grid. This is
    # the honest measure of within-run variation and the calibration target for the
    # simulator's speed noise: real clean runs sit near median 3.34 m/s, sigma 1.41.
    observed_speeds = []
    for index in range(1, len(lonlats)):
        dt = run_times[index] - run_times[index - 1]
        if math.isnan(dt) or dt <= 0:
            continue
        observed_speeds.append(haversine_m(lonlats[index - 1], lonlats[index]) / dt)

    # Offset from this run's own fitted stadium centreline: runner wander + GPS error.
    calibration, _diag = fit_rotation(lonlats)
    frame = LocalFrame(calibration.center_lon, calibration.center_lat)
    heading = math.radians(calibration.heading_degrees)
    offsets = []
    for lon, lat in lonlats:
        x, y = frame.to_xy(lon, lat)
        dx, dy = rotate_proper(x, y, -heading)
        offsets.append(_distance_to_stadium(dx, dy, calibration.straight_half_length_m,
                                            calibration.curve_radius_m))

    stride = None
    cadence = None
    last = points[-1]
    try:
        last_mileage = float(last.get("runMileage"))
        last_step = float(last.get("runStep"))
        if last_step > 0:
            stride = last_mileage / last_step
    except (TypeError, ValueError):
        pass
    try:
        cadence = float(data.get("recodeCadence"))
    except (TypeError, ValueError):
        pass

    return {
        "file": name,
        "mileage_km": mileage,
        "duration_s": duration,
        "duration_min": duration / 60.0,
        "pace_min_per_km": duration / 60.0 / mileage if mileage else None,
        "point_count": len(lonlats),
        "sample_step_s": median(sample_steps) if sample_steps else None,
        "sample_step_min_s": min(sample_steps) if sample_steps else None,
        "sample_step_max_s": max(sample_steps) if sample_steps else None,
        "point_spacing_m": median(spacing) if spacing else None,
        "point_spacing_mean_m": mean(spacing) if spacing else None,
        "point_spacing_p95_m": percentile(spacing, 0.95) if spacing else None,
        "point_spacing_max_m": max(spacing) if spacing else None,
        "mileage_step_m": median(mileage_steps) if mileage_steps else None,
        # Record-level mean speed (recorded distance / duration). Distinct from the
        # per-point median below, which end-point jitter inflates by ~0.45 m/s.
        "mean_speed_mps": (mileage * 1000.0 / duration) if duration else None,
        "obs_speed_med_mps": median(observed_speeds) if observed_speeds else None,
        "obs_speed_sd_mps": pstdev(observed_speeds) if len(observed_speeds) > 1 else None,
        "obs_speed_p95_mps": percentile(observed_speeds, 0.95) if observed_speeds else None,
        "obs_speed_max_mps": max(observed_speeds) if observed_speeds else None,
        "point_pace_sd": pstdev(point_paces) if len(point_paces) > 1 else None,
        "offset_sigma_m": pstdev(offsets) if len(offsets) > 1 else None,
        "offset_p95_m": percentile(offsets, 0.95) if offsets else None,
        "stride_m": stride,
        "cadence": cadence,
        "fitted_lap_m": calibration.lap_length_m,
        "fit_residual_m": calibration.fit_residual_m,
    }


FIELD_ORDER = [
    "mileage_km", "duration_s", "duration_min", "pace_min_per_km", "point_count",
    "sample_step_s", "sample_step_min_s", "sample_step_max_s",
    "point_spacing_m", "point_spacing_mean_m", "point_spacing_p95_m", "point_spacing_max_m",
    "mileage_step_m", "mean_speed_mps", "obs_speed_med_mps", "obs_speed_sd_mps",
    "obs_speed_p95_mps",
    "obs_speed_max_mps", "point_pace_sd",
    "offset_sigma_m", "offset_p95_m", "stride_m", "cadence",
    "fitted_lap_m", "fit_residual_m",
]


def _summary(values: Iterable[float | None]) -> dict[str, float] | None:
    finite = sorted(v for v in values if v is not None and math.isfinite(v))
    if not finite:
        return None
    return {
        "count": len(finite),
        "min": round(finite[0], 4),
        "p25": round(percentile(finite, 0.25), 4),
        "median": round(median(finite), 4),
        "mean": round(mean(finite), 4),
        "p75": round(percentile(finite, 0.75), 4),
        "max": round(finite[-1], 4),
        "stddev": round(pstdev(finite), 4) if len(finite) > 1 else 0.0,
    }


def summarise(paths: list[Path]) -> dict[str, Any]:
    records = [m for m in (record_metrics(p) for p in paths) if m]
    return _report(records)


def summarise_documents(documents: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Same report, from in-memory documents, so nothing has to be written to disk."""
    records = [m for m in (metrics_from_document(d, n) for n, d in documents) if m]
    return _report(records)


def _report(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "files": len(records),
        "summary": {field: _summary([r[field] for r in records]) for field in FIELD_ORDER},
        "records": records,
    }


def collect_files(source: Path) -> list[Path]:
    if source.is_dir():
        return sorted(p for p in source.iterdir()
                      if p.suffix.lower() == ".json" and not p.name.startswith("_"))
    return [source]


def print_report(report: dict[str, Any], title: str) -> None:
    print(f"=== {title} ({report['files']} records) ===")
    print(f"  {'metric':<22} {'min':>9} {'p25':>9} {'median':>9} {'mean':>9} {'p75':>9} {'max':>9} {'sd':>8}")
    for field in FIELD_ORDER:
        s = report["summary"].get(field)
        if not s:
            continue
        print(f"  {field:<22} {s['min']:>9.3f} {s['p25']:>9.3f} {s['median']:>9.3f} "
              f"{s['mean']:>9.3f} {s['p75']:>9.3f} {s['max']:>9.3f} {s['stddev']:>8.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, help="tasklist file or directory")
    parser.add_argument("--json", default=None, help="write the full report as JSON")
    parser.add_argument("--reference", action="store_true", help="print the human-readable table")
    args = parser.parse_args()

    report = summarise(collect_files(Path(args.input)))
    if args.reference or not args.json:
        print_report(report, str(args.input))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
