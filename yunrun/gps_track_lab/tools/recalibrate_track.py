# -*- coding: utf-8 -*-
"""Recalibrate the track model from a directory of server exports.

Why this exists (beyond `python -m gps_track_lab calibrate`):

1. **Shape vetting.** Exports are not all the same venue. Feeding a directory that
   mixes a 400 m oval, a wandering campus run and a second playground into one
   pooled fit produces a stadium that existed nowhere (observed: a 63 m-radius
   circle with a 14.8 m residual). ``filter_stadium_runs`` fits every export on its
   own and drops the ones that are not a lap of plausible length.

2. **Robust aggregation.** Pooling the point clouds of many runs also inflates the
   extent, because each run's median centre differs by a few metres. ``--robust``
   fits each export alone and takes the per-parameter median instead.

3. **Perimeter normalisation.** The fitted envelope sits a few metres outside the
   centreline (runner's lateral offset + GPS bias), and `half_straight = semi_major
   - radius` inherits that error, so the local track fits ~425 m rather than
   the 400 m a standard oval actually is. ``--lap-length 400`` rescales both
   parameters by one factor, keeping the measured shape ratio.

4. **Orientation check.** ``--compare`` projects the fitted stadium the same way the
   generator does and reports its world-frame long axis next to the data's, so an
   orientation regression is visible instead of silent.

Note on rotation: ``calibration._rotate`` is a reflection, not a rotation, but
paired with the generator's proper rotation it round-trips correctly (verified
against synthetic ground truth at 0/30/45/89.8/120 deg). ``fit_rotation`` uses a
proper rotation with a matching convention; both reproduce the true heading.

Usage:
    python tools/recalibrate_track.py --input <dir-with-tasklists> --output config/track.json
    python tools/recalibrate_track.py --input <dir> --output config/track.json \
        --robust --lap-length 400 --cluster-radius-m 250 --compare
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

HERE = Path(__file__).resolve().parent
LAB_ROOT = HERE.parent
sys.path.insert(0, str(LAB_ROOT / "src"))

from gps_track_lab.calibration import (  # noqa: E402
    TrackCalibration,
    _distance_to_stadium,
    load_points,
    save_calibration,
)
from gps_track_lab.geo import LocalFrame, percentile  # noqa: E402


def rotate_proper(x: float, y: float, angle: float) -> tuple[float, float]:
    """Proper CCW rotation (the fix)."""
    c, s = math.cos(angle), math.sin(angle)
    return c * x - s * y, s * x + c * y


def pca_heading(centered: list[tuple[float, float]]) -> float:
    var_x = sum(x * x for x, _ in centered) / len(centered)
    var_y = sum(y * y for _, y in centered) / len(centered)
    cov_xy = sum(x * y for x, y in centered) / len(centered)
    return 0.5 * math.atan2(2.0 * cov_xy, var_x - var_y)


def fit_rotation(
    points: list[tuple[float, float]],
    *,
    lap_length: float | None = None,
    source_files: list[str] | None = None,
    coordinate_system: str = "inherited-from-input",
) -> tuple[TrackCalibration, dict[str, Any]]:
    """Fit a stadium model using a proper rotation. Pure function, no side effects."""
    lon0 = median(p[0] for p in points)
    lat0 = median(p[1] for p in points)
    frame = LocalFrame(lon0, lat0)
    xy = [frame.to_xy(*p) for p in points]
    mean_x = sum(x for x, _ in xy) / len(xy)
    mean_y = sum(y for _, y in xy) / len(xy)
    centered = [(x - mean_x, y - mean_y) for x, y in xy]

    heading = pca_heading(centered)
    rotated = [rotate_proper(x, y, -heading) for x, y in centered]

    x_low, x_high = percentile([x for x, _ in rotated], 0.02), percentile([x for x, _ in rotated], 0.98)
    y_low, y_high = percentile([y for _, y in rotated], 0.02), percentile([y for _, y in rotated], 0.98)
    shift_x, shift_y = (x_low + x_high) / 2.0, (y_low + y_high) / 2.0
    semi_major = (x_high - x_low) / 2.0
    radius = max(8.0, (y_high - y_low) / 2.0)
    half_straight = max(0.0, semi_major - radius)

    # World center: rotate the in-frame shift back by +heading (inverse of -heading).
    dx, dy = rotate_proper(shift_x, shift_y, heading)
    center_lon, center_lat = frame.to_lonlat(mean_x + dx, mean_y + dy)
    adjusted = [(x - shift_x, y - shift_y) for x, y in rotated]
    residuals = [_distance_to_stadium(x, y, half_straight, radius) for x, y in adjusted]
    raw_lap = 4.0 * half_straight + 2.0 * math.pi * radius

    scale = 1.0
    if lap_length:
        scale = lap_length / raw_lap
        half_straight *= scale
        radius *= scale
        residuals = [r * scale for r in residuals]

    calibration = TrackCalibration(
        schema_version=1,
        model="stadium-centerline",
        center_lon=center_lon,
        center_lat=center_lat,
        heading_degrees=math.degrees(heading),
        straight_half_length_m=half_straight,
        curve_radius_m=radius,
        lap_length_m=lap_length or raw_lap,
        fit_residual_m=median(residuals),
        source_points=len(points),
        coordinate_system=coordinate_system,
        source_files=source_files,
    )
    diagnostics = {
        "raw_lap_length_m": round(raw_lap, 3),
        "scale_applied": round(scale, 6),
        "residual_m": round(median(residuals), 4),
        "heading_degrees": round(math.degrees(heading), 4),
    }
    return calibration, diagnostics


def fit_robust(
    paths: list[Path],
    *,
    lap_length: float | None = None,
    coordinate_system: str = "inherited-from-input",
) -> tuple[TrackCalibration, dict[str, Any]]:
    """Fit every export separately, then take the per-parameter median.

    Pooling the point clouds of many runs inflates the apparent extent, because the
    median centre of each run differs by a few metres. Fitting each run alone and
    taking the median of heading / straight / radius is insensitive to that.
    """
    fits = []
    for path in paths:
        points = load_points(path)
        single, _ = fit_rotation(points)
        fits.append((single, fit_residual_for(points, single)))
    if not fits:
        raise ValueError("no usable exports")

    # Circular median for the heading (angles wrap around).
    angles = [math.radians(f[0].heading_degrees) for f in fits]
    heading = math.atan2(median([math.sin(a) for a in angles]), median([math.cos(a) for a in angles]))

    align = [math.radians(f[0].heading_degrees) for f in fits]
    # Bring every sample within +/-90 deg of the median heading so the (r, h) medians
    # are not polluted by the 180-deg ambiguity of a principal axis.
    for index, angle in enumerate(align):
        delta = (angle - heading + math.pi / 2) % math.pi - math.pi / 2
        align[index] = heading + delta

    # Re-express each run's stadium in the common frame: swap h/r when the axis flipped.
    halves, radii, residuals = [], [], []
    for (single, residual), angle in zip(fits, align):
        flipped = abs(angle - math.radians(single.heading_degrees)) > math.pi / 2
        halves.append(single.curve_radius_m if flipped else single.straight_half_length_m)
        radii.append(single.straight_half_length_m if flipped else single.curve_radius_m)
        residuals.append(residual)

    half_straight = median(halves)
    radius = median(radii)
    raw_lap = 4.0 * half_straight + 2.0 * math.pi * radius
    scale = (lap_length / raw_lap) if lap_length else 1.0
    half_straight *= scale
    radius *= scale

    centers = [f[0] for f in fits]
    center_lon = median(c.center_lon for c in centers)
    center_lat = median(c.center_lat for c in centers)

    # Principal axis is defined modulo 180 deg; pick the representative in (0, 180]
    # so the reported heading does not flip sign between statistically equal fits.
    heading_degrees = math.degrees(heading) % 180.0

    calibration = TrackCalibration(
        schema_version=1,
        model="stadium-centerline",
        center_lon=center_lon,
        center_lat=center_lat,
        heading_degrees=heading_degrees,
        straight_half_length_m=half_straight,
        curve_radius_m=radius,
        lap_length_m=lap_length or raw_lap,
        fit_residual_m=median(residuals),
        source_points=sum(c.source_points for c in centers),
        coordinate_system=coordinate_system,
        source_files=[str(p.resolve()) for p in paths],
    )
    diagnostics = {
        "method": "per-file median",
        "files_fitted": len(fits),
        "raw_lap_length_m": round(raw_lap, 3),
        "scale_applied": round(scale, 6),
        "residual_m": round(median(residuals), 4),
        "heading_degrees": round(heading_degrees, 4),
        "straight_half_spread_m": [round(min(halves), 2), round(max(halves), 2)],
        "radius_spread_m": [round(min(radii), 2), round(max(radii), 2)],
    }
    return calibration, diagnostics


def model_extent_m(calibration: TrackCalibration) -> dict[str, float]:
    """Extent of the stadium inside its own un-rotated model frame (straight runs along x).

    For orientation questions use ``world_extent_m`` / ``principal_axis_deg`` instead:
    these raw model-frame numbers are trivially "swapped" relative to world axes and
    comparing them against world data is misleading.
    """
    from gps_track_lab.generator import _stadium_point

    xs, ys = [], []
    step = calibration.lap_length_m / 2000.0
    for i in range(2000):
        x, y, _, _ = _stadium_point(calibration, i * step)
        xs.append(x)
        ys.append(y)
    return {
        "model_frame_straight_m": round(max(xs) - min(xs), 2),
        "model_frame_across_m": round(max(ys) - min(ys), 2),
    }


def world_extent_m(calibration: TrackCalibration) -> dict[str, float]:
    """Bounding box of the stadium as the *generator* projects it into the world."""
    from gps_track_lab.generator import _stadium_point

    heading = math.radians(calibration.heading_degrees)
    xs, ys = [], []
    for i in range(2000):
        x, y, _, _ = _stadium_point(calibration, i * calibration.lap_length_m / 2000.0)
        wx, wy = rotate_proper(x, y, heading)
        xs.append(wx)
        ys.append(wy)
    return {
        "world_east_m": round(max(xs) - min(xs), 2),
        "world_north_m": round(max(ys) - min(ys), 2),
    }


def _world_extent_points(points: list[tuple[float, float]]) -> dict[str, float]:
    """Bounding box of a lon/lat point cloud, in metres (local equirectangular frame)."""
    lon0 = median(p[0] for p in points)
    lat0 = median(p[1] for p in points)
    frame = LocalFrame(lon0, lat0)
    xy = [frame.to_xy(*p) for p in points]
    return {
        "world_east_m": round(max(x for x, _ in xy) - min(x for x, _ in xy), 2),
        "world_north_m": round(max(y for _, y in xy) - min(y for _, y in xy), 2),
    }


def principal_axis_deg(points: list[tuple[float, float]]) -> float:
    """Bearing (deg, in (0,180]) of the point cloud's long axis, measured on the ground."""
    lon0 = median(p[0] for p in points)
    lat0 = median(p[1] for p in points)
    frame = LocalFrame(lon0, lat0)
    xy = [frame.to_xy(*p) for p in points]
    mean_x = sum(x for x, _ in xy) / len(xy)
    mean_y = sum(y for _, y in xy) / len(xy)
    centered = [(x - mean_x, y - mean_y) for x, y in xy]
    return math.degrees(pca_heading(centered)) % 180.0


def fit_residual_for(points: list[tuple[float, float]], calibration: TrackCalibration) -> float:
    """Median point-to-stadium distance for an already-fitted model."""
    frame = LocalFrame(calibration.center_lon, calibration.center_lat)
    heading = math.radians(calibration.heading_degrees)
    residuals = []
    for lon, lat in points:
        x, y = frame.to_xy(lon, lat)
        dx, dy = rotate_proper(x, y, -heading)
        residuals.append(_distance_to_stadium(dx, dy, calibration.straight_half_length_m,
                                              calibration.curve_radius_m))
    return median(residuals) if residuals else float("inf")


def filter_stadium_runs(
    paths: list[Path],
    *,
    lap_min: float,
    lap_max: float,
    residual_max: float,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Keep only exports whose own point cloud fits a stadium of a plausible lap length.

    An export of a run that wandered off the track (or crossed campus) is rejected
    here instead of being allowed to stretch the shared fit. Vetting each file on
    its own means the filter does not depend on the cluster it lands in.
    """
    kept: list[Path] = []
    rejected: list[tuple[Path, str]] = []
    for path in paths:
        try:
            points = load_points(path)
        except Exception as exc:  # noqa: BLE001
            rejected.append((path, f"unreadable: {exc}"))
            continue
        single, _ = fit_rotation(points)  # no lap normalisation: judge the raw shape
        lap = single.lap_length_m
        residual = fit_residual_for(points, single)
        if not lap_min <= lap <= lap_max:
            rejected.append((path, f"lap {lap:.1f} m outside [{lap_min:.0f},{lap_max:.0f}]"))
        elif residual > residual_max:
            rejected.append((path, f"residual {residual:.2f} m > {residual_max:.2f} m"))
        else:
            kept.append(path)
    return kept, rejected


def cluster_files(paths: list[Path], radius_m: float = 400.0) -> list[dict[str, Any]]:
    from gps_track_lab.geo import haversine_m

    clusters: list[dict[str, Any]] = []
    for path in paths:
        try:
            points = load_points(path)
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] {path.name}: {exc}")
            continue
        center = (median(p[0] for p in points), median(p[1] for p in points))
        for cluster in clusters:
            if haversine_m(center, cluster["center"]) <= radius_m:
                cluster["paths"].append(path)
                cluster["points"].extend(points)
                cluster["centers"].append(center)
                cluster["center"] = (
                    median(c[0] for c in cluster["centers"]),
                    median(c[1] for c in cluster["centers"]),
                )
                break
        else:
            clusters.append({"center": center, "centers": [center], "paths": [path], "points": list(points)})
    clusters.sort(key=lambda c: -len(c["points"]))
    return clusters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, help="tasklist file or directory")
    parser.add_argument("--output", required=True, help="destination calibration JSON")
    parser.add_argument("--coordinate-system", default="inherited-from-task-exports")
    parser.add_argument("--cluster-radius-m", type=float, default=400.0)
    parser.add_argument("--cluster-index", type=int, default=0, help="cluster to fit (default: largest)")
    parser.add_argument("--lap-length", type=float, default=None,
                        help="rescale the fit to this perimeter, e.g. 400 for a standard track")
    parser.add_argument("--compare", action="store_true", help="also report the model extent for orientation check")
    parser.add_argument("--robust", action="store_true",
                        help="fit each export separately and take the per-parameter median")
    parser.add_argument("--no-shape-filter", dest="shape_filter", action="store_false",
                        help="fit every file as-is (default: drop exports that do not look like a lap)")
    parser.add_argument("--shape-lap-min", type=float, default=360.0,
                        help="accept an export whose own fitted lap length is at least this")
    parser.add_argument("--shape-lap-max", type=float, default=470.0,
                        help="accept an export whose own fitted lap length is at most this")
    parser.add_argument("--shape-residual-max", type=float, default=3.0,
                        help="accept an export whose median distance to its own fit is at most this")
    args = parser.parse_args()

    source = Path(args.input)
    paths = sorted(p for p in (source.iterdir() if source.is_dir() else [source])
                   if p.suffix.lower() in {".json", ".geojson", ".csv"})
    if not paths:
        print(f"no supported point files in {source}", file=sys.stderr)
        return 1

    print(f"loading {len(paths)} file(s) from {source}")
    if args.shape_filter:
        paths, rejected = filter_stadium_runs(
            paths,
            lap_min=args.shape_lap_min,
            lap_max=args.shape_lap_max,
            residual_max=args.shape_residual_max,
        )
        print(f"shape filter: kept {len(paths)}, rejected {len(rejected)}")
        for path, reason in rejected:
            print(f"  [reject] {path.name}: {reason}")
        if not paths:
            print("every export was rejected; nothing to fit", file=sys.stderr)
            return 1

    clusters = cluster_files(paths, args.cluster_radius_m)
    print(f"detected {len(clusters)} location cluster(s):")
    for index, cluster in enumerate(clusters):
        mark = " [selected]" if index == args.cluster_index else ""
        print(f"  cluster {index}: {len(cluster['paths'])} files / {len(cluster['points'])} points / "
              f"center {cluster['center'][0]:.6f},{cluster['center'][1]:.6f}{mark}")
    if not 0 <= args.cluster_index < len(clusters):
        print(f"cluster-index must be between 0 and {len(clusters) - 1}", file=sys.stderr)
        return 1

    selected = clusters[args.cluster_index]
    if args.robust:
        calibration, diagnostics = fit_robust(
            selected["paths"],
            lap_length=args.lap_length,
            coordinate_system=args.coordinate_system,
        )
    else:
        calibration, diagnostics = fit_rotation(
            selected["points"],
            lap_length=args.lap_length,
            source_files=[str(p.resolve()) for p in selected["paths"]],
            coordinate_system=args.coordinate_system,
        )

    payload = calibration.to_dict()
    payload["generated_by"] = "tools/recalibrate_track.py"
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["fit"] = {
        "rotation": "proper rotation (same convention as generator)",
        "lap_length_normalized_to": args.lap_length,
        **diagnostics,
    }
    # Written directly (not via save_calibration) so the fit diagnostics survive.
    with Path(args.output).open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print()
    print(json.dumps({k: payload[k] for k in (
        "center_lon", "center_lat", "heading_degrees", "straight_half_length_m",
        "curve_radius_m", "lap_length_m", "fit_residual_m", "source_points")}, ensure_ascii=False, indent=2))
    print(f"saved: {Path(args.output).resolve()}")

    if args.compare:
        # Orientation check: the model's world-frame long axis must match the data's.
        from gps_track_lab.generator import _stadium_point

        real_axis = principal_axis_deg(selected["points"])
        frame = LocalFrame(calibration.center_lon, calibration.center_lat)
        heading = math.radians(calibration.heading_degrees)
        projected = []
        for i in range(2000):
            x, y, _, _ = _stadium_point(calibration, i * calibration.lap_length_m / 2000.0)
            projected.append(frame.to_lonlat(*rotate_proper(x, y, heading)))
        model_axis = principal_axis_deg(projected)

        def axis_delta(a: float, b: float) -> float:
            return min(abs(a - b), 180.0 - abs(a - b))

        print()
        print("orientation check (world frame):")
        print(f"  real data long axis : {real_axis:.2f} deg")
        print(f"  generated track axis: {model_axis:.2f} deg")
        print(f"  difference          : {axis_delta(real_axis, model_axis):.2f} deg")
        print("  extent (world frame, metres):")
        print(f"    real data        : {json.dumps(_world_extent_points(selected['points']))}")
        print(f"    generated stadium: {json.dumps(world_extent_m(calibration))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
