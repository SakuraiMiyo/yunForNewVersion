from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

from .calibration import discover_point_files
from .geo import haversine_m, percentile


def _summary(values: list[float]) -> dict[str, float] | None:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return None
    return {
        "count": len(finite),
        "min": round(min(finite), 4),
        "p05": round(percentile(finite, 0.05), 4),
        "median": round(median(finite), 4),
        "mean": round(mean(finite), 4),
        "p95": round(percentile(finite, 0.95), 4),
        "max": round(max(finite), 4),
        "stddev": round(pstdev(finite), 4),
    }


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def analyze_task_exports(
    inputs: list[str | Path], rule_min_km: float = 3.0, rule_max_minutes: float = 30.0
) -> dict[str, Any]:
    files = discover_point_files(inputs)
    file_reports = []
    positive_time_gaps: list[float] = []
    coordinate_steps: list[float] = []
    mileage_steps: list[float] = []
    observed_speeds: list[float] = []
    batch_sizes: list[float] = []
    approximate_periods: list[float] = []
    zero_time_gaps = 0
    all_time_gaps = 0

    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        data = document.get("data", {})
        points = data.get("pointsList", [])
        km = _number(data.get("recordMileage")) or 0.0
        duration = _number(data.get("duration")) or 0.0
        polyline_m = 0.0
        file_jumps = 0
        if len(points) > 1 and duration > 0:
            approximate_periods.append(duration / (len(points) - 1))

        current_ts = None
        current_batch = 0
        for index, point in enumerate(points):
            ts = point.get("ts")
            if ts == current_ts:
                current_batch += 1
            else:
                if current_batch:
                    batch_sizes.append(float(current_batch))
                current_ts = ts
                current_batch = 1
            if index == 0:
                continue
            previous = points[index - 1]
            try:
                lon1, lat1 = (float(part) for part in str(previous["point"]).split(",", 1))
                lon2, lat2 = (float(part) for part in str(point["point"]).split(",", 1))
            except (KeyError, TypeError, ValueError):
                continue
            step = haversine_m((lon1, lat1), (lon2, lat2))
            coordinate_steps.append(step)
            polyline_m += step
            if step > 25.0:
                file_jumps += 1

            first_time = _number(previous.get("runTime"))
            second_time = _number(point.get("runTime"))
            if first_time is not None and second_time is not None:
                dt = second_time - first_time
                all_time_gaps += 1
                if dt == 0:
                    zero_time_gaps += 1
                elif 0 < dt <= 60:
                    positive_time_gaps.append(dt)
                    observed_speeds.append(step / dt)

            first_mileage = _number(previous.get("runMileage"))
            second_mileage = _number(point.get("runMileage"))
            if first_mileage is not None and second_mileage is not None:
                delta = second_mileage - first_mileage
                if 0 < delta < 100:
                    mileage_steps.append(delta)
        if current_batch:
            batch_sizes.append(float(current_batch))

        file_reports.append({
            "file": str(path.resolve()),
            "record_mileage_km": km,
            "duration_seconds": duration,
            "computed_pace_min_per_km": round(duration / 60.0 / km, 3) if km else None,
            "reported_pace_min_per_km": _number(data.get("recodePace")),
            "cadence": _number(data.get("recodeCadence")),
            "points": len(points),
            "qualified_field": data.get("isQualified"),
            "stated_rule_pass": km > rule_min_km and 0 < duration <= rule_max_minutes * 60.0,
            "polyline_distance_m": round(polyline_m, 2),
            "polyline_to_recorded_ratio": round(polyline_m / (km * 1000.0), 4) if km else None,
            "coordinate_jumps_over_25m": file_jumps,
        })

    pass_count = sum(bool(report["stated_rule_pass"]) for report in file_reports)
    qualified_count = sum(str(report["qualified_field"]) == "1" for report in file_reports)
    return {
        "schema_version": 1,
        "source": "local server-exported task files",
        "rule": {"distance_strictly_greater_than_km": rule_min_km, "maximum_minutes": rule_max_minutes},
        "files": len(file_reports),
        "stated_rule_pass_count": pass_count,
        "qualified_field_count": qualified_count,
        "record_mileage_km": _summary([report["record_mileage_km"] for report in file_reports]),
        "duration_seconds": _summary([report["duration_seconds"] for report in file_reports]),
        "computed_pace_min_per_km": _summary([
            report["computed_pace_min_per_km"] for report in file_reports
            if report["computed_pace_min_per_km"] is not None
        ]),
        "approximate_sample_period_seconds": _summary(approximate_periods),
        "positive_run_time_gap_seconds": _summary(positive_time_gaps),
        "zero_run_time_gap_fraction": round(zero_time_gaps / all_time_gaps, 4) if all_time_gaps else None,
        "coordinate_step_m": _summary(coordinate_steps),
        "recorded_mileage_step_m": _summary(mileage_steps),
        "coordinate_speed_mps_when_time_advances": _summary(observed_speeds),
        "same_timestamp_batch_size": _summary(batch_sizes),
        "file_reports": file_reports,
    }


def save_analysis(report: dict[str, Any], path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

