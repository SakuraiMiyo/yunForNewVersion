from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .calibration import TrackCalibration
from .geo import LocalFrame, haversine_m


@dataclass
class SimulationSettings:
    sample_interval_seconds: float = 2.0
    speed_sigma_mps: float = 0.18
    speed_correlation_seconds: float = 24.0
    max_acceleration_mps2: float = 0.32
    lateral_sigma_m: float = 0.45
    lateral_correlation_seconds: float = 35.0
    gps_sigma_m: float = 1.8
    gps_correlation_seconds: float = 16.0
    white_noise_sigma_m: float = 0.35
    dropout_probability: float = 0.005
    reporting_latency_mean_ms: float = 550.0
    reporting_latency_sigma_ms: float = 140.0
    reporting_latency_correlation_seconds: float = 12.0
    latency_spike_probability: float = 0.008
    latency_spike_mean_ms: float = 900.0
    coordinate_system: str = "inherited-from-calibration"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SimulationSettings":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def validate(self) -> None:
        if self.sample_interval_seconds <= 0:
            raise ValueError("sample_interval_seconds must be positive")
        if (
            self.speed_correlation_seconds <= 0
            or self.gps_correlation_seconds <= 0
            or self.lateral_correlation_seconds <= 0
            or self.reporting_latency_correlation_seconds <= 0
        ):
            raise ValueError("correlation times must be positive")
        if self.max_acceleration_mps2 <= 0:
            raise ValueError("max_acceleration_mps2 must be positive")
        if not 0.0 <= self.dropout_probability < 0.5:
            raise ValueError("dropout_probability must be in [0, 0.5)")
        if not 0.0 <= self.latency_spike_probability < 0.5:
            raise ValueError("latency_spike_probability must be in [0, 0.5)")
        if min(self.reporting_latency_mean_ms, self.reporting_latency_sigma_ms, self.latency_spike_mean_ms) < 0:
            raise ValueError("latency values cannot be negative")


@dataclass
class SimulatedPoint:
    timestamp: str
    available_timestamp: str
    reporting_latency_ms: float
    longitude: float
    latitude: float
    modeled_distance_m: float
    modeled_speed_mps: float
    observed_step_m: float
    lap: int
    synthetic: bool = True


def load_settings(path: str | Path | None) -> SimulationSettings:
    if path is None:
        settings = SimulationSettings()
    else:
        with Path(path).open("r", encoding="utf-8") as handle:
            settings = SimulationSettings.from_dict(json.load(handle))
    settings.validate()
    return settings


@dataclass
class SpeedProfile:
    """A piecewise target speed: alternating run and walk segments.

    A single constant target speed produces a pace chart that is one gently rippling line,
    which is exactly what a coach flagged as fake. Real campus runs -- measured on four
    captured exports -- are **not** one long run with a couple of breaks: they swing between
    a fast median of 5.0-5.7 min/km and slow stretches above 9 min/km that take 23-52% of the
    time, in 9-28 separate stretches per 3 km, most of them only 2-40 s long with the odd one
    running to 2-3 minutes. The full spread reaches 12-13.5 min/km.

    ``segments`` is that structure in simulation units: ``(end_distance_m, speed_mps)`` pairs
    in increasing distance order, and ``speed_at`` walks them in first-match order.
    """

    run_speed_mps: float
    # (end_distance_m, speed_mps, is_slow_stretch)
    segments: tuple[tuple[float, float, bool], ...] = ()
    end_distance_m: float = 0.0

    def _boundaries(self) -> list[tuple[float, float, float, bool]]:
        """`(start_m, end_m, speed, is_slow)` for every piece, runs implied in between.

        `is_slow` is carried explicitly rather than inferred from the speed: running
        stretches deliberately carry their own pace factors (0.90-1.24x), so a slow *run*
        segment and a *walk* segment can have the same speed, and anything comparing speeds
        ends up counting ordinary running as walking.
        """
        pieces: list[tuple[float, float, float, bool]] = []
        cursor = 0.0
        for end, speed, is_slow in self.segments:
            end = min(float(end), self.end_distance_m)
            if end <= cursor:
                continue
            pieces.append((cursor, end, float(speed), bool(is_slow)))
            cursor = end
        if cursor < self.end_distance_m:
            pieces.append((cursor, self.end_distance_m, self.run_speed_mps, False))
        return pieces

    def speed_at(self, traveled_m: float) -> float:
        for start, end, speed, _slow in self._boundaries():
            if start <= traveled_m < end:
                return speed
        return self.run_speed_mps

    def is_walking(self, traveled_m: float) -> bool:
        """True when the piece at this distance is one of the slow stretches."""
        for start, end, _speed, slow in self._boundaries():
            if start <= traveled_m < end:
                return slow
        return False

    def walk_distance_m(self) -> float:
        return sum(end - start for start, end, _speed, slow in self._boundaries() if slow)

    def duration_for(self, distance_m: float) -> float:
        """Seconds this profile needs to cover ``distance_m`` (walk segments included)."""
        total = 0.0
        for start, end, speed, _slow in self._boundaries():
            if start >= distance_m:
                break
            span = min(end, distance_m) - start
            if span > 0:
                total += span / speed
        return total


def _ou_step(value: float, sigma: float, correlation_seconds: float, dt: float, rng: random.Random) -> float:
    retain = math.exp(-dt / correlation_seconds)
    innovation = sigma * math.sqrt(max(0.0, 1.0 - retain * retain))
    return retain * value + rng.gauss(0.0, innovation)


def _stadium_point(cal: TrackCalibration, station: float) -> tuple[float, float, float, float]:
    """Return local x/y and unit tangent for a clockwise stadium path."""
    h = cal.straight_half_length_m
    r = cal.curve_radius_m
    straight = 2.0 * h
    arc = math.pi * r
    perimeter = 2.0 * straight + 2.0 * arc
    s = station % perimeter
    if s < straight:
        x = -h + s
        y = r
        tx, ty = 1.0, 0.0
    elif s < straight + arc:
        angle = math.pi / 2.0 - (s - straight) / r
        x = h + r * math.cos(angle)
        y = r * math.sin(angle)
        tx, ty = math.sin(angle), -math.cos(angle)
    elif s < 2.0 * straight + arc:
        offset = s - straight - arc
        x = h - offset
        y = -r
        tx, ty = -1.0, 0.0
    else:
        angle = -math.pi / 2.0 - (s - 2.0 * straight - arc) / r
        x = -h + r * math.cos(angle)
        y = r * math.sin(angle)
        tx, ty = math.sin(angle), -math.cos(angle)
    return x, y, tx, ty


def simulate(
    calibration: TrackCalibration,
    settings: SimulationSettings,
    distance_m: float,
    pace_min_per_km: float,
    seed: int | None = None,
    start_time: datetime | None = None,
    speed_profile: SpeedProfile | None = None,
) -> tuple[list[SimulatedPoint], int]:
    """Simulate one run.

    ``speed_profile`` overlays a run/walk segmentation on top of the mean pace: the target
    speed then depends on how far the runner has already gone, so the emitted per-point
    speeds show walking stretches instead of a single flat pace. When it is None the
    behaviour is unchanged (one target speed for the whole run).
    """
    if distance_m <= 0:
        raise ValueError("distance_m must be positive")
    if pace_min_per_km <= 0:
        raise ValueError("pace_min_per_km must be positive")
    settings.validate()
    actual_seed = seed if seed is not None else secrets.randbits(63)
    rng = random.Random(actual_seed)
    dt = settings.sample_interval_seconds
    target_speed = 1000.0 / (pace_min_per_km * 60.0)
    speed_delta = rng.gauss(0.0, settings.speed_sigma_mps * 0.35)
    lateral = rng.gauss(0.0, settings.lateral_sigma_m * 0.4)
    gps_x = rng.gauss(0.0, settings.gps_sigma_m * 0.4)
    gps_y = rng.gauss(0.0, settings.gps_sigma_m * 0.4)
    latency_delta = rng.gauss(0.0, settings.reporting_latency_sigma_ms * 0.4)
    station = rng.uniform(0.0, calibration.lap_length_m)
    traveled = 0.0
    started = start_time or datetime.now(timezone.utc)
    frame = LocalFrame(calibration.center_lon, calibration.center_lat)
    heading = math.radians(calibration.heading_degrees)
    cos_h, sin_h = math.cos(heading), math.sin(heading)
    points: list[SimulatedPoint] = []
    previous_lonlat: tuple[float, float] | None = None
    previous_available: datetime | None = None
    tick = 0

    while traveled < distance_m:
        tick += 1
        old_speed_delta = speed_delta
        # Walking is a different gait, not a slower version of running: the fluctuations
        # around it are smaller, so a walk segment reads as a stable slow stretch rather
        # than as noise dipping below the run pace.
        walking = speed_profile is not None and speed_profile.speed_at(traveled) < (
            speed_profile.run_speed_mps - 1e-9)
        sigma = settings.speed_sigma_mps * (0.45 if walking else 1.0)
        speed_delta = _ou_step(speed_delta, sigma, settings.speed_correlation_seconds, dt, rng)
        max_change = settings.max_acceleration_mps2 * dt
        speed_delta = max(old_speed_delta - max_change, min(old_speed_delta + max_change, speed_delta))
        base_speed = (speed_profile.speed_at(traveled) if speed_profile is not None
                      else target_speed)
        speed = max(0.8, base_speed + speed_delta)
        step = min(speed * dt, distance_m - traveled)
        traveled += step
        station += step

        lateral = _ou_step(lateral, settings.lateral_sigma_m, settings.lateral_correlation_seconds, dt, rng)
        gps_x = _ou_step(gps_x, settings.gps_sigma_m, settings.gps_correlation_seconds, dt, rng)
        gps_y = _ou_step(gps_y, settings.gps_sigma_m, settings.gps_correlation_seconds, dt, rng)
        latency_delta = _ou_step(
            latency_delta,
            settings.reporting_latency_sigma_ms,
            settings.reporting_latency_correlation_seconds,
            dt,
            rng,
        )
        latency_ms = max(0.0, settings.reporting_latency_mean_ms + latency_delta)
        if rng.random() < settings.latency_spike_probability:
            latency_ms += rng.expovariate(1.0 / max(1.0, settings.latency_spike_mean_ms))
        x, y, tx, ty = _stadium_point(calibration, station)
        nx, ny = -ty, tx
        x += nx * lateral
        y += ny * lateral
        world_x = cos_h * x - sin_h * y + gps_x + rng.gauss(0.0, settings.white_noise_sigma_m)
        world_y = sin_h * x + cos_h * y + gps_y + rng.gauss(0.0, settings.white_noise_sigma_m)
        lon, lat = frame.to_lonlat(world_x, world_y)
        lonlat = (lon, lat)
        observed_step = haversine_m(previous_lonlat, lonlat) if previous_lonlat else 0.0
        timestamp = started + timedelta(seconds=tick * dt)
        available_timestamp = timestamp + timedelta(milliseconds=latency_ms)
        # Model a FIFO sensor/reporting channel: a latency spike can delay later
        # samples, but cannot make an older sample arrive after a newer one.
        if previous_available is not None and available_timestamp <= previous_available:
            available_timestamp = previous_available + timedelta(milliseconds=1)
            latency_ms = (available_timestamp - timestamp).total_seconds() * 1000.0

        if rng.random() >= settings.dropout_probability or traveled >= distance_m:
            points.append(SimulatedPoint(
                timestamp=timestamp.isoformat().replace("+00:00", "Z"),
                available_timestamp=available_timestamp.isoformat().replace("+00:00", "Z"),
                reporting_latency_ms=round(latency_ms, 3),
                longitude=lon,
                latitude=lat,
                modeled_distance_m=traveled,
                modeled_speed_mps=speed,
                observed_step_m=observed_step,
                lap=int(traveled // calibration.lap_length_m) + 1,
            ))
            previous_lonlat = lonlat
            previous_available = available_timestamp

    return points, actual_seed


def assess_physics(
    points: list[SimulatedPoint], settings: SimulationSettings, target_distance_m: float
) -> dict[str, float | bool]:
    if len(points) < 2:
        return {"valid": False, "score": float("inf"), "reason": "too_few_points"}
    observed_distance = sum(point.observed_step_m for point in points[1:])
    observed_ratio = observed_distance / target_distance_m
    max_observed_speed = 0.0
    max_modeled_acceleration = 0.0
    maximum_latency_ms = max(point.reporting_latency_ms for point in points)
    mean_latency_ms = sum(point.reporting_latency_ms for point in points) / len(points)
    monotonic = True
    for previous, current in zip(points, points[1:]):
        previous_time = datetime.fromisoformat(previous.timestamp.replace("Z", "+00:00"))
        current_time = datetime.fromisoformat(current.timestamp.replace("Z", "+00:00"))
        elapsed = (current_time - previous_time).total_seconds()
        if elapsed <= 0 or current.modeled_distance_m <= previous.modeled_distance_m:
            monotonic = False
            continue
        max_observed_speed = max(max_observed_speed, current.observed_step_m / elapsed)
        acceleration = abs(current.modeled_speed_mps - previous.modeled_speed_mps) / elapsed
        max_modeled_acceleration = max(max_modeled_acceleration, acceleration)
    valid = (
        monotonic
        and 0.90 <= observed_ratio <= 1.25
        and max_observed_speed <= 8.0
        and max_modeled_acceleration <= settings.max_acceleration_mps2 + 1e-6
    )
    score = (
        abs(observed_ratio - 1.0) * 10.0
        + max(0.0, max_observed_speed - 6.0)
        + max_modeled_acceleration
        + (0.0 if monotonic else 100.0)
    )
    return {
        "valid": valid,
        "score": round(score, 6),
        "observed_distance_ratio": round(observed_ratio, 6),
        "max_observed_speed_mps": round(max_observed_speed, 6),
        "max_modeled_acceleration_mps2": round(max_modeled_acceleration, 6),
        "mean_reporting_latency_ms": round(mean_latency_ms, 3),
        "maximum_reporting_latency_ms": round(maximum_latency_ms, 3),
        "monotonic_time_and_distance": monotonic,
    }


def generate_best_candidate(
    calibration: TrackCalibration,
    settings: SimulationSettings,
    distance_m: float,
    pace_min_per_km: float,
    candidates: int = 8,
    seed: int | None = None,
    speed_profile: SpeedProfile | None = None,
) -> tuple[list[SimulatedPoint], int, dict[str, Any]]:
    """Generate a batch and return the best physically valid candidate."""
    if not 1 <= candidates <= 100:
        raise ValueError("candidates must be between 1 and 100")

    results = []
    seed_rng = random.Random(seed) if seed is not None else None
    for _ in range(candidates):
        candidate_seed = seed_rng.getrandbits(63) if seed_rng is not None else None
        points, actual_seed = simulate(
            calibration=calibration,
            settings=settings,
            distance_m=distance_m,
            pace_min_per_km=pace_min_per_km,
            seed=candidate_seed,
            speed_profile=speed_profile,
        )
        report = assess_physics(points, settings, distance_m)
        results.append((bool(report["valid"]), float(report["score"]), points, actual_seed, report))

    valid_results = [candidate for candidate in results if candidate[0]]
    pool = valid_results or results
    _, _, points, selected_seed, quality = min(pool, key=lambda candidate: candidate[1])
    quality = dict(quality)
    quality["candidate_count"] = len(results)
    quality["valid_candidate_count"] = len(valid_results)
    return points, selected_seed, quality


def write_outputs(
    points: list[SimulatedPoint],
    output_root: str | Path,
    seed: int,
    calibration: TrackCalibration,
    settings: SimulationSettings,
    quality: dict[str, Any] | None = None,
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = f"synthetic-{timestamp}-{seed & 0xFFFF:04x}"
    output_path = Path(output_root)
    output_path.mkdir(parents=True, exist_ok=True)
    for suffix in range(100):
        run_name = base_name if suffix == 0 else f"{base_name}-{suffix}"
        run_dir = output_path / run_name
        try:
            run_dir.mkdir(exist_ok=False)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"could not allocate a unique run directory for {base_name}")

    csv_path = run_dir / "track.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SimulatedPoint.__dataclass_fields__.keys())
        writer.writeheader()
        for point in points:
            writer.writerow(asdict(point))

    geojson = {
        "type": "FeatureCollection",
        "properties": {
            "synthetic": True,
            "purpose": "offline testing and visualization",
            "coordinate_system": settings.coordinate_system,
            "seed": seed,
        },
        "features": [{
            "type": "Feature",
            "properties": {"synthetic": True, "point_count": len(points)},
            "geometry": {
                "type": "LineString",
                "coordinates": [[point.longitude, point.latitude] for point in points],
            },
        }],
    }
    geojson_path = run_dir / "track.geojson"
    with geojson_path.open("w", encoding="utf-8") as handle:
        json.dump(geojson, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    started = datetime.fromisoformat(points[0].timestamp.replace("Z", "+00:00"))
    ended = datetime.fromisoformat(points[-1].timestamp.replace("Z", "+00:00"))
    manifest = {
        "schema_version": 1,
        "synthetic": True,
        "purpose": "offline testing and visualization",
        "seed": seed,
        "point_count": len(points),
        "duration_seconds": (ended - started).total_seconds(),
        "modeled_distance_m": points[-1].modeled_distance_m,
        "csv_sha256": digest,
        "calibration": calibration.to_dict(),
        "settings": asdict(settings),
        "quality": quality or {},
    }
    with (run_dir / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return run_dir
