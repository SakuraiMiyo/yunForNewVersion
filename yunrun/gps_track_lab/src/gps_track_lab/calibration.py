from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .geo import LocalFrame, haversine_m, percentile


@dataclass
class TrackCalibration:
    schema_version: int
    model: str
    center_lon: float
    center_lat: float
    heading_degrees: float
    straight_half_length_m: float
    curve_radius_m: float
    lap_length_m: float
    fit_residual_m: float
    source_points: int
    coordinate_system: str = "inherited-from-input"
    source_files: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackCalibration":
        # Ignore unknown keys (e.g. the fit diagnostics written by
        # tools/recalibrate_track.py) instead of crashing the loader.
        # Mirrors SimulationSettings.from_dict.
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})


def _walk_json(value: Any) -> Iterable[tuple[float, float]]:
    if isinstance(value, dict):
        point = value.get("point")
        if isinstance(point, str) and "," in point:
            try:
                lon, lat = (float(part.strip()) for part in point.split(",", 1))
                if -180 <= lon <= 180 and -90 <= lat <= 90:
                    yield lon, lat
            except ValueError:
                pass
        if "longitude" in value and "latitude" in value:
            try:
                yield float(value["longitude"]), float(value["latitude"])
            except (TypeError, ValueError):
                pass
        geometry = value.get("geometry")
        if isinstance(geometry, dict) and geometry.get("type") == "LineString":
            for coordinate in geometry.get("coordinates", []):
                if isinstance(coordinate, list) and len(coordinate) >= 2:
                    yield float(coordinate[0]), float(coordinate[1])
            return
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def load_points(path: str | Path) -> list[tuple[float, float]]:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix in {".json", ".geojson"}:
        with source.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        # Known task exports contain checkpoint metadata beside the actual sampled
        # path.  Only pointsList is a trajectory; including manageList would bias
        # the geometry fit toward checkpoint locations.
        task_points = document.get("data", {}).get("pointsList") if isinstance(document, dict) else None
        points = list(_walk_json(task_points if isinstance(task_points, list) else document))
    elif suffix == ".csv":
        points = []
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                lon = row.get("longitude") or row.get("lon") or row.get("lng")
                lat = row.get("latitude") or row.get("lat")
                if lon is not None and lat is not None:
                    points.append((float(lon), float(lat)))
    else:
        raise ValueError(f"unsupported input format: {suffix}")
    if len(points) < 20:
        raise ValueError(f"at least 20 coordinate points are required, found {len(points)}")
    return _deduplicate_and_filter(points)


def discover_point_files(inputs: list[str | Path]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in {".json", ".geojson", ".csv"}))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(path)
    if not files:
        raise ValueError("no supported point files found")
    return files


def cluster_point_files(
    inputs: list[str | Path], cluster_radius_m: float = 250.0
) -> list[tuple[list[Path], list[tuple[float, float]]]]:
    """Group separate exports by location so two playgrounds are never mixed."""
    if cluster_radius_m <= 0:
        raise ValueError("cluster_radius_m must be positive")
    groups: list[dict[str, Any]] = []
    for path in discover_point_files(inputs):
        points = load_points(path)
        center = (median(p[0] for p in points), median(p[1] for p in points))
        best_index = None
        best_distance = float("inf")
        for index, group in enumerate(groups):
            distance = haversine_m(center, group["center"])
            if distance <= cluster_radius_m and distance < best_distance:
                best_index, best_distance = index, distance
        if best_index is None:
            groups.append({"files": [path], "point_sets": [points], "centers": [center], "center": center})
        else:
            group = groups[best_index]
            group["files"].append(path)
            group["point_sets"].append(points)
            group["centers"].append(center)
            group["center"] = (
                median(p[0] for p in group["centers"]),
                median(p[1] for p in group["centers"]),
            )
    result = []
    for group in groups:
        combined = [point for point_set in group["point_sets"] for point in point_set]
        result.append((group["files"], combined))
    return sorted(result, key=lambda item: (-len(item[1]), str(item[0][0])))


def _deduplicate_and_filter(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    cleaned = [points[0]]
    for point in points[1:]:
        step = haversine_m(cleaned[-1], point)
        if step < 0.05:
            continue
        if step > 80.0:
            continue
        cleaned.append(point)
    if len(cleaned) < 20:
        raise ValueError("too few points remain after duplicate/outlier filtering")
    return cleaned


def _rotate(x: float, y: float, angle: float) -> tuple[float, float]:
    c = math.cos(angle)
    s = math.sin(angle)
    return c * x + s * y, -s * x + c * y


def _distance_to_stadium(x: float, y: float, half_straight: float, radius: float) -> float:
    if abs(x) <= half_straight:
        return abs(abs(y) - radius)
    cap_x = half_straight if x > 0 else -half_straight
    return abs(math.hypot(x - cap_x, y) - radius)


def calibrate(
    points: list[tuple[float, float]],
    coordinate_system: str = "inherited-from-input",
    source_files: list[str] | None = None,
) -> TrackCalibration:
    lon0 = median(point[0] for point in points)
    lat0 = median(point[1] for point in points)
    frame = LocalFrame(lon0, lat0)
    xy = [frame.to_xy(*point) for point in points]

    mean_x = sum(x for x, _ in xy) / len(xy)
    mean_y = sum(y for _, y in xy) / len(xy)
    centered = [(x - mean_x, y - mean_y) for x, y in xy]
    var_x = sum(x * x for x, _ in centered) / len(centered)
    var_y = sum(y * y for _, y in centered) / len(centered)
    cov_xy = sum(x * y for x, y in centered) / len(centered)
    heading = 0.5 * math.atan2(2.0 * cov_xy, var_x - var_y)
    rotated = [_rotate(x, y, heading) for x, y in centered]

    x_low, x_high = percentile([x for x, _ in rotated], 0.02), percentile([x for x, _ in rotated], 0.98)
    y_low, y_high = percentile([y for _, y in rotated], 0.02), percentile([y for _, y in rotated], 0.98)
    shift_x = (x_low + x_high) / 2.0
    shift_y = (y_low + y_high) / 2.0
    semi_major = (x_high - x_low) / 2.0
    radius = max(8.0, (y_high - y_low) / 2.0)
    half_straight = max(0.0, semi_major - radius)

    c = math.cos(heading)
    s = math.sin(heading)
    center_x = mean_x + c * shift_x - s * shift_y
    center_y = mean_y + s * shift_x + c * shift_y
    center_lon, center_lat = frame.to_lonlat(center_x, center_y)
    adjusted = [(x - shift_x, y - shift_y) for x, y in rotated]
    residuals = [_distance_to_stadium(x, y, half_straight, radius) for x, y in adjusted]
    lap_length = 4.0 * half_straight + 2.0 * math.pi * radius

    return TrackCalibration(
        schema_version=1,
        model="stadium-centerline",
        center_lon=center_lon,
        center_lat=center_lat,
        heading_degrees=math.degrees(heading),
        straight_half_length_m=half_straight,
        curve_radius_m=radius,
        lap_length_m=lap_length,
        fit_residual_m=median(residuals),
        source_points=len(points),
        coordinate_system=coordinate_system,
        source_files=source_files,
    )


def save_calibration(calibration: TrackCalibration, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(calibration.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def load_calibration(path: str | Path) -> TrackCalibration:
    with Path(path).open("r", encoding="utf-8") as handle:
        return TrackCalibration.from_dict(json.load(handle))
