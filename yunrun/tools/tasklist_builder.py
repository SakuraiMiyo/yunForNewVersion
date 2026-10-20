# -*- coding: utf-8 -*-
"""Build a 云运动 tasklist-format record from a simulated trajectory.

Split out of ``sim_pipeline.py`` so the field-level business rules (when a run counts
as qualified, how step counts and cadence are derived, what timestamps the points
carry) can be tested and compared against real exports independently of the CLI.

Business rule implemented here (the local 健康跑 standard):

    合格 = 单次里程 > 3.0 km 且 用时 <= 30 min

The *generation* window is deliberately tighter than the pass window (default
16-25 min) so that a generated record never lands near the 30-minute boundary.

Everything random is derived from one seed, so the *shape* of a record is
reproducible: same seed, same distances, cadences and point offsets. The wall-clock
timestamps are not — they come from ``datetime.now()`` at call time, because a record
that claims to have been run at a fixed past instant is exactly what the server would
reject. Compare two runs by their point list, not by byte equality.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# --- local 健康跑 rules -----------------------------------------------------
MIN_QUALIFIED_KM = 3.0
MAX_QUALIFIED_MIN = 30.0

# --- generation window: deliberately tighter than the pass window ----------
GEN_DURATION_MIN_RANGE = (16.0, 25.0)
GEN_DISTANCE_KM_RANGE = (3.01, 3.20)

# Real clean exports: pace median 5.80 min/km, p25 5.2, p75 6.9, min 5.18, max 8.07.
# sigma is in log space, fitted to the observed p25..p75 span of log(pace).
PACE_MEDIAN = 5.80
PACE_LOG_SIGMA = 0.20

# Cadence is drawn per run and stride is *derived* from it. Two measured populations:
#   all 36 exports     -- min 95, median 137, max 157, sd 16.4 (includes slow walk-runs)
#   the 19 clean laps  -- 130 .. 157, median 147, sd 7.3
# This generator targets the clean-lap population; using the all-record spread would
# emit strides up to ~1.7 m, which no real record shows (observed max 1.261 m).
CADENCE_RANGE = (125.0, 160.0)
CADENCE_MEAN = 146.0
CADENCE_SIGMA = 8.0
# Observed coupling: 157 at 5.18 min/km down to 137 at 8.07 min/km => slope ~ -7.7.
PACE_CADENCE_PIVOT = 6.5
PACE_CADENCE_SLOPE = -7.7

# Guard rails: a derived stride outside the observed range means the (pace, cadence)
# pair is not something a real record shows. Observed: 0.846 .. 1.261 m, median 1.12.
# The band is deliberately a little wider than observed: the guard runs on the *planned*
# duration while the emitted duration is the simulated tick count, and the two differ by
# about 1% (measured range after 150 runs: 0.918 .. 1.304 m).
STRIDE_M_RANGE = (0.82, 1.32)
STRIDE_M_MID = 1.12

# Real exports sample on a constant 2 s grid (all 36 records: min = median = max = 2).
SAMPLE_INTERVAL_S = 2.0


def _find_lab_root() -> Path:
    """Locate ``gps_track_lab`` for this script's default config paths.

    This file lives at ``yunrun/tools/tasklist_builder.py``, while the lab sits at
    ``yunrun/gps_track_lab``. A hard-coded ``parents[N]`` broke once the file moved, so
    search upwards instead of assuming a depth.

    The markers have to include files that are actually **tracked**: ``config/track.json``
    only exists after a calibration and is git-ignored, so on a fresh clone the search
    found nothing and raised a bare "not found" long before the user could calibrate.
    """
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:5]:
        candidate = parent / "gps_track_lab"
        if not candidate.is_dir():
            continue
        if ((candidate / "config" / "track.json").is_file()
                or (candidate / "config.example.json").is_file()
                or (candidate / "pyproject.toml").is_file()):
            return candidate
    raise FileNotFoundError(f"找不到 gps_track_lab 目录（从 {here} 往上找了 5 层）")


def default_calibration_path() -> Path:
    return _find_lab_root() / "config" / "track.json"


def default_settings_path() -> Path:
    return _find_lab_root() / "config.example.json"

# The server field ``speed`` in pointsList is a pace in min/km, clamped by the app.
# The server's own rule band, stated in its isStandard checklist: "配速应在3.3min/km和
# 10.15min/km之间". The ceiling used to be 9.0, which flattened every slow stretch in
# the pace chart onto one line -- the walks are 8.5-12.5 before clamping.
PACE_CLAMP = (3.6, 10.1)


@dataclass
class RunPlan:
    """The randomised intent of one run, before the trajectory is simulated."""

    distance_m: float
    duration_s: float
    pace_min_per_km: float
    cadence: float
    seed: int
    start_epoch: float
    start_utc: str = field(default="")
    # Run/walk segmentation, when the generator is building a realistic campus run.
    # ``None`` keeps the plain single-speed behaviour (tests and callers that ask for it).
    speed_profile: Any = None
    walk_paces: tuple[float, ...] = ()
    run_pace_min_per_km: float | None = None
    # Exact step count once the profile has been integrated. ``step_total`` prefers it, so the
    # record summary and the last emitted row cannot disagree (they did: 2700 vs 2715).
    step_override: int | None = None
    # Emitted duration must land inside this window; ``build_tasklist_record`` enforces it.
    duration_window_s: tuple[float, float] | None = None

    @property
    def walk_distance_m(self) -> float:
        return self.speed_profile.walk_distance_m() if self.speed_profile is not None else 0.0

    @property
    def walk_share(self) -> float:
        return self.walk_distance_m / self.distance_m if self.distance_m > 0 else 0.0

    @property
    def stride_m(self) -> float:
        """Derived: cadence and speed together determine how long a step is."""
        steps = self.cadence * self.duration_s / 60.0
        return self.distance_m / steps if steps > 0 else 1.0

    @property
    def step_total(self) -> int:
        if self.step_override is not None:
            return int(self.step_override)
        return max(1, int(round(self.cadence * self.duration_s / 60.0)))

    @property
    def qualified(self) -> bool:
        return (self.distance_m / 1000.0 > MIN_QUALIFIED_KM
                and 0.0 < self.duration_s <= MAX_QUALIFIED_MIN * 60.0)


def plan_run(
    *,
    seed: int | None = None,
    distance_km: float | None = None,
    duration_min: float | None = None,
    pace_min_per_km: float | None = None,
    cadence: float | None = None,
    stride_m: float | None = None,
    start_time: datetime | None = None,
    duration_range: tuple[float, float] = GEN_DURATION_MIN_RANGE,
    distance_range: tuple[float, float] = GEN_DISTANCE_KM_RANGE,
    walk_segments: int | None = None,
) -> RunPlan:
    """Draw the run's distance / pace / cadence, and its run/walk shape.

    Distance and pace come from distributions measured on real exports (distance
    clusters just above 3 km; pace centres near 5.8 min/km), then duration follows as
    ``distance * pace`` and is clamped into the requested window. Cadence is drawn from
    the observed range and stride is *derived* from cadence and speed, so the emitted
    step count and pace can never contradict each other.

    ``walk_segments`` fixes how many walking breaks the run gets; ``None`` lets the
    distance decide (see :func:`plan_slow_stretches`). Passing ``0`` produces the old
    single-speed profile, which is what the tests that predate the walk model expect.
    """
    if seed is None:
        seed = random.SystemRandom().randrange(2**63)
    rng = random.Random(seed)

    if distance_km is None:
        # Real distribution: min 3.01, median 3.02, p75 3.03, max 3.25. A beta-shaped
        # draw keeps almost everything at 3.0x with a thin tail (3.01..3.20).
        unit = rng.betavariate(1.6, 6.0)
        distance_km = round(distance_range[0] + unit * (distance_range[1] - distance_range[0]), 2)
    if pace_min_per_km is None and duration_min is None:
        # Lognormal keeps pace strictly positive with a right tail; parameters are set
        # so the median lands on the observed 5.80 min/km and the spread reproduces the
        # observed p25/p75 (5.2 / 6.9). Do not round here: rounding shifts the median.
        pace_min_per_km = math.exp(rng.gauss(math.log(PACE_MEDIAN), PACE_LOG_SIGMA))
    if duration_min is None:
        if pace_min_per_km is not None:
            duration_min = distance_km * pace_min_per_km
        else:
            duration_min = rng.uniform(*duration_range)
    if pace_min_per_km is None:
        pace_min_per_km = duration_min / distance_km

    # A plausible *running* pace, then the walking breaks added on top of it. Drawing the
    # duration uniformly over 16-25 min instead produced records whose implied running pace
    # was 7-8.5 min/km -- slower than the walk segments themselves, i.e. a "run" slower than
    # its own walking. The real spread in duration comes from how much walking happened, not
    # from covering 3 km at a stroll.
    modelling_walks = walk_segments is None or walk_segments > 0
    if modelling_walks and duration_min is None:
        realistic_run_pace = min(RUN_PACE_SOLVE_RANGE[1],
                                 max(RUN_PACE_SOLVE_RANGE[0], pace_min_per_km))
        duration_min = distance_km * realistic_run_pace

    # The generation window is a hard constraint, and the emitted duration can only be a
    # whole number of sample intervals (the simulator ticks a fixed grid). Snap onto the
    # nearest achievable grid point inside the window, so the *emitted* duration -- not
    # merely the intended one -- is guaranteed to be in range.
    clipped = min(max(duration_min, duration_range[0]), duration_range[1])
    steps = max(1, int(round(clipped * 60.0 / SAMPLE_INTERVAL_S)))
    low = math.ceil(duration_range[0] * 60.0 / SAMPLE_INTERVAL_S)
    high = math.floor(duration_range[1] * 60.0 / SAMPLE_INTERVAL_S)
    steps = min(max(steps, low), high)
    duration_min = steps * SAMPLE_INTERVAL_S / 60.0
    pace_min_per_km = duration_min / distance_km

    if cadence is None:
        cadence = sample_cadence(rng, pace_min_per_km)
        # A run whose (pace, cadence) pair implies a stride no real record shows is
        # re-drawn rather than emitted: the derived stride must stay in the observed
        # range (0.846 .. 1.261 m).
        for _ in range(24):
            stride = distance_km * 1000.0 / (cadence * duration_min)
            if STRIDE_M_RANGE[0] <= stride <= STRIDE_M_RANGE[1]:
                break
            cadence = sample_cadence(rng, pace_min_per_km)
        else:
            # Fall back to the cadence that yields the midpoint stride for this run.
            cadence = min(CADENCE_RANGE[1], max(CADENCE_RANGE[0],
                           (distance_km * 1000.0 / duration_min) / STRIDE_M_MID))

    started = start_time or datetime.now(timezone.utc)
    plan = RunPlan(
        distance_m=distance_km * 1000.0,
        duration_s=duration_min * 60.0,
        pace_min_per_km=pace_min_per_km,
        cadence=cadence,
        seed=seed,
        start_epoch=started.timestamp(),
        start_utc=started.isoformat().replace("+00:00", "Z"),
    )

    # Pace shape. Anatomically this belongs with the plan (it decides what the pace looks
    # like), while the simulator only consumes it -- so every caller that builds a tasklist
    # gets a realistic trace without opting in.
    # ``None`` = let the random process decide; ``0`` = explicitly a single steady pace.
    if walk_segments is None or walk_segments > 0:
        walks = plan_slow_stretches(rng, plan.distance_m, plan.duration_s,
                                    walk_count=walk_segments)
        profile, run_pace, walk_paces = build_speed_profile(
            rng, plan.distance_m, plan.duration_s, walks)
        # The slow stretches must not push the record past the ceiling: drop trailing ones
        # until it fits. The solved running pace is recomputed each time, so this keeps the
        # window true without bending the running into a stroll.
        ceiling_s = duration_range[1] * 60.0
        while profile.segments and profile.duration_for(plan.distance_m) > ceiling_s:
            walks = walks[:-1]
            profile, run_pace, walk_paces = build_speed_profile(
                rng, plan.distance_m, plan.duration_s, walks)

        if profile.segments:
            total_s = profile.duration_for(plan.distance_m)
            grid = max(1, int(round(total_s / SAMPLE_INTERVAL_S)))
            lo = math.ceil(duration_range[0] * 60.0 / SAMPLE_INTERVAL_S)
            hi = math.floor(duration_range[1] * 60.0 / SAMPLE_INTERVAL_S)
            # Clamped on both ends: with only a ceiling, a plan whose slow stretches added up
            # to less than 16 minutes came out at 15.2 -- below the window the coach asked for.
            lo_s = lo * SAMPLE_INTERVAL_S
            hi_s = hi * SAMPLE_INTERVAL_S
            # Clamp *before* rounding to the grid, not after: rounding 957.3 s down to 958 s
            # and only then applying the floor still lands 2 s below the 16-minute bound.
            clamped = min(max(total_s, lo_s), hi_s)
            target_s = max(1, int(round(clamped / SAMPLE_INTERVAL_S))) * SAMPLE_INTERVAL_S
            if abs(target_s - total_s) > 1e-9 and total_s > 0:
                # Land exactly on the gridded duration. Comparing against the profile's own
                # length (rather than only when the floor pushed it up) matters: the grid
                # rounds 479.45 s to 479 s, which is 0.03 s below the 16-minute floor and was
                # slipping through.
                plan.duration_s = target_s
                # The window moved the run's length, so the running has to carry the
                # difference -- rescale the run stretches (walks keep their own pace) instead
                # of letting the record contradict the profile it was built from.
                from gps_track_lab.generator import SpeedProfile

                scale = target_s / total_s
                run_speed = profile.run_speed_mps
                profile = SpeedProfile(
                    run_speed_mps=run_speed * (1.0 / scale),
                    segments=tuple(
                        (end, speed * (1.0 / scale) if not slow else speed, slow)
                        for end, speed, slow in profile.segments),
                    end_distance_m=plan.distance_m,
                )
                run_pace = 1000.0 / (profile.run_speed_mps * 60.0)
        # Keep the fastest running from implying an inhuman stride. The record must satisfy
        # cadence == steps*60/duration with steps = distance/stride, so at a fixed cadence a
        # fast enough run forces a stride no real export shows (0.85-1.26 m observed).
        if profile.segments:
            strides = []
            for end, speed, _slow in profile.segments:
                pace = 1000.0 / (speed * 60.0)
                strides.append(plan.distance_m / (cadence_for_pace(pace) * plan.duration_s / 60.0))
            fastest_stride = min(strides)
            if fastest_stride > STRIDE_M_RANGE[1]:
                adjust = fastest_stride / STRIDE_M_RANGE[1]
                speed_cap = profile.run_speed_mps / adjust
                run_speed = profile.run_speed_mps
                profile = SpeedProfile(
                    run_speed_mps=run_speed,
                    segments=tuple((end, max(speed_cap, speed) if not slow else speed, slow)
                                   for end, speed, slow in profile.segments),
                    end_distance_m=plan.distance_m,
                )
        plan.duration_window_s = (duration_range[0] * 60.0, duration_range[1] * 60.0)
        plan.speed_profile = profile
        plan.walk_paces = tuple(walk_paces)
        plan.run_pace_min_per_km = run_pace
        plan.pace_min_per_km = plan.duration_s / 60.0 / (plan.distance_m / 1000.0)
        # The record's cadence is the run's average, and the pace/cadence coupling is defined
        # on the *average* pace, so a plan with slow stretches reports a cadence consistent
        # with its own numbers.
        plan.cadence = min(CADENCE_RANGE[1], max(CADENCE_RANGE[0],
                           cadence_for_pace(plan.pace_min_per_km)))
    return plan


def sample_cadence(rng: random.Random, pace_min_per_km: float | None = None) -> float:
    """Draw a cadence, optionally coupled to pace.

    Real records are not independent: over the 19 clean laps, cadence runs 157 at
    5.18 min/km down to 137 at 8.07 min/km (about -7.7 cadence per +1 min/km). Coupling
    the draw this way keeps the derived stride realistic at both ends -- without it, a
    fast short run could pair a high pace with a low cadence and imply a ~0.78 m stride,
    which no real record shows.
    """
    mean = CADENCE_MEAN
    if pace_min_per_km is not None and pace_min_per_km > 0:
        mean += PACE_CADENCE_SLOPE * (pace_min_per_km - PACE_CADENCE_PIVOT)
    return min(CADENCE_RANGE[1], max(CADENCE_RANGE[0], rng.gauss(mean, CADENCE_SIGMA)))


# --- pace shape -------------------------------------------------------------
# Measured on four captured campus runs (history/local/tasks_else): overall pace 6.3-8.4,
# total spread 12-13 min/km, standard deviation 2.0-2.9, 9-28 separate slow stretches per
# 3 km, and 23-52% of the *time* above 9 min/km. Two earlier attempts missed this badly: a
# tidy schedule of one or two walks gave a spread of 1.2, and a loose random-walk model put
# 71% of the distance into walking. The real shape is continuous variation in the running
# pace, punctuated by a handful of genuine slow stretches.
WALK_COUNT_PER_KM_RANGE = (2.5, 6.0)        # slow stretches per km, observed 3-9
WALK_TIME_SHARE_MAX = 0.30                  # slow stretches stay ~30% of the elapsed time
WALK_SHORT_S_RANGE = (6.0, 22.0)            # easing off for a few seconds
WALK_LONG_S_RANGE = (25.0, 70.0)            # the occasional real break
WALK_LONG_CHANCE = 0.22
WALK_PACE_RANGE = (8.5, 12.5)               # observed tail reaches 12.4-13.5
# The running pace varies continuously: a mean-reverting factor re-drawn every 30-60 m, so
# the trace undulates instead of forming two long flat plateaus.
RUN_FACTOR_STEP_SIGMA = 0.085
RUN_FACTOR_RANGE = (0.90, 1.24)
RUN_PACE_SOLVE_RANGE = (4.2, 7.6)
RUN_FACTOR_INTERVAL_M = (30.0, 60.0)
RUN_FATIGUE_DISTANCE_DRIFT = 0.05


def plan_slow_stretches(rng: random.Random, distance_m: float, duration_s: float,
                        walk_count: int | None = None) -> list[tuple[float, float, float]]:
    """Return `(start_m, end_m, pace_min_per_km)` slow stretches for one run.

    The count is drawn first and the stretches are placed with jitter, rather than rolling a
    low per-step probability: a probability model occasionally produced none at all, which
    leaves two long flat plateaus -- the exact look this is meant to avoid. Each stretch then
    draws its own pace, so the dips in the chart are not all the same depth.

    The result is trimmed against a distance budget at the end. Doing the accounting up front
    does not work (the budget has to be expressed in the stretches' own speeds, which are only
    drawn as they are built), and without the trim a run could end up walking most of its own
    distance -- measured before this guard: up to 91%.
    """
    if distance_m <= 0 or duration_s <= 0 or walk_count == 0:
        return []
    km = distance_m / 1000.0
    if walk_count is None:
        walk_count = max(2, int(round(rng.uniform(*WALK_COUNT_PER_KM_RANGE) * km)))
    first = min(distance_m * 0.08, 90.0)
    last = distance_m - 45.0
    if last <= first:
        return []

    candidates: list[tuple[float, float, float]] = []
    for index in range(walk_count):
        fraction = (index + 0.5) / walk_count
        jitter = rng.uniform(-0.6, 0.6) / walk_count
        start = first + (last - first) * min(max(fraction + jitter, 0.0), 1.0)
        pace = rng.uniform(*WALK_PACE_RANGE)
        speed = 1000.0 / (pace * 60.0)
        seconds = (rng.uniform(*WALK_LONG_S_RANGE) if rng.random() < WALK_LONG_CHANCE
                   else rng.uniform(*WALK_SHORT_S_RANGE))
        length_m = min(speed * seconds, 60.0, max(0.0, last - start))
        if length_m < 5.0:
            continue
        candidates.append((round(start, 1), round(start + length_m, 1), pace))

    # Trim against a *time* budget, which is the quantity that actually matters: the slow
    # stretches are what stretch the record towards the 25-minute ceiling, and the previous
    # distance-based accounting could not be trusted once the running stretches started
    # carrying their own pace factors. Measured before this guard: slow stretches took up to
    # 60% of the elapsed time and 77% of the distance.
    budget_s = duration_s * WALK_TIME_SHARE_MAX
    kept: list[tuple[float, float, float]] = []
    used_s = 0.0
    for start, end, pace in candidates:
        cost = (end - start) / (1000.0 / (pace * 60.0))
        if used_s + cost > budget_s:
            continue
        kept.append((start, end, pace))
        used_s += cost
    return kept



def cadence_for_pace(pace_min_per_km: float) -> float:
    """The deterministic centre of the cadence model: stride follows from (pace, cadence).

    Used where a cadence has to be *consistent* rather than random -- per-point step
    accumulation, and the check that a planned pair implies a plausible stride.
    """
    return min(CADENCE_RANGE[1],
               max(CADENCE_RANGE[0],
                   CADENCE_MEAN + PACE_CADENCE_SLOPE * (pace_min_per_km - PACE_CADENCE_PIVOT)))


def build_speed_profile(rng: random.Random, distance_m: float, duration_s: float,
                        walks: list[tuple[float, float, float]]):
    """Turn a walk plan into a :class:`SpeedProfile`, solving for the running pace.

    The running pace is *solved*, not drawn: the walk stretches already consume part of the
    duration budget, so the running pace has to make up the rest and land the whole record
    inside the requested window. The solution is exact (no search), because for a fixed walk
    layout the total time is ``walk_time + run_distance / run_speed`` -- rewriting that for
    ``run_speed`` is a division.

    Each stretch of running between walks also gets its own pace multiplier and a small
    fatigue drift, so the trace undulates instead of forming long flat plateaus.
    """
    from gps_track_lab.generator import SpeedProfile

    if not walks:
        run_pace = min(RUN_PACE_SOLVE_RANGE[1], max(RUN_PACE_SOLVE_RANGE[0],
                       (duration_s / 60.0) / (distance_m / 1000.0)))
        return SpeedProfile(run_speed_mps=1000.0 / (run_pace * 60.0), segments=(),
                            end_distance_m=distance_m), run_pace, ()

    walk_paces = [pace for _, _, pace in walks]
    walk_speeds = [1000.0 / (pace * 60.0) for pace in walk_paces]
    walk_distance = sum(end - start for start, end, _ in walks)
    walk_seconds = sum((end - start) / speed
                       for (start, end, _), speed in zip(walks, walk_speeds))
    run_distance = max(1.0, distance_m - walk_distance)
    # Time budget for running, converted into a speed and then into a pace.
    run_speed = run_distance / max(1.0, duration_s - walk_seconds)
    run_pace = 1000.0 / (run_speed * 60.0)
    if not RUN_PACE_SOLVE_RANGE[0] <= run_pace <= RUN_PACE_SOLVE_RANGE[1]:
        run_pace = min(RUN_PACE_SOLVE_RANGE[1], max(RUN_PACE_SOLVE_RANGE[0], run_pace))
        run_speed = 1000.0 / (run_pace * 60.0)

    # Assemble the segment list in distance order. The running stretches carry a
    # mean-reverting pace factor re-drawn every 30-60 m, which is what makes the trace
    # undulate; the walk stretches are inserted at their planned positions. The factor's
    # average stays 1.0, so the solved duration still holds.
    segments: list[tuple[float, float, bool]] = []
    cursor = 0.0
    factor = 1.0
    next_factor_at = 0.0
    for index, (start, end, _) in enumerate(walks):
        while cursor < start:
            step = min(rng.uniform(*RUN_FACTOR_INTERVAL_M), start - cursor)
            if cursor >= next_factor_at:
                factor = min(RUN_FACTOR_RANGE[1], max(RUN_FACTOR_RANGE[0],
                             factor + rng.gauss(0.0, RUN_FACTOR_STEP_SIGMA)))
                next_factor_at = cursor + rng.uniform(*RUN_FACTOR_INTERVAL_M)
            progress = cursor / max(1.0, distance_m)
            fatigue = 1.0 + RUN_FATIGUE_DISTANCE_DRIFT * progress   # slightly slower late on
            segments.append((cursor + step, run_speed * factor / fatigue, False))
            cursor += step
        segments.append((end, walk_speeds[index], True))
        cursor = end
    profile = SpeedProfile(run_speed_mps=run_speed, segments=tuple(segments),
                           end_distance_m=distance_m)
    return profile, run_pace, walk_paces


def steps_per_metre(pace_min_per_km: float) -> float:
    """Steps taken per metre at a given pace, through the cadence coupling.

    One metre takes ``pace/60`` minutes and a minute holds ``cadence`` steps, so the count
    is ``cadence * (pace/60) / 1000`` -- equivalently ``cadence / (60 * speed)``, which is
    just ``1 / stride``. Mixing the two up is a factor-of-thousands error that silently
    reported 45 steps for a 3 km run, so the relation is spelled out here rather than
    inlined at the call sites.
    """
    speed_mps = 1000.0 / (pace_min_per_km * 60.0)
    if speed_mps <= 0:
        return 0.0
    return cadence_for_pace(pace_min_per_km) / (60.0 * speed_mps)


def plan_step_metrics(plan: "RunPlan") -> dict[str, float]:
    """Total steps and average cadence implied by a plan's run/walk profile.

    Integrates the pace/cadence coupling over the run: every metre contributes
    ``steps_per_metre(pace_at_that_metre)`` steps. A walking stretch therefore lowers the
    average cadence on its own, with no separate fudge factor -- and the value returned here
    is what both the point list and the record summary are built from, so they cannot
    disagree.
    """
    profile = getattr(plan, "speed_profile", None)
    if profile is None:
        return {"steps": plan.step_total, "cadence": plan.cadence}
    steps = 0.0
    # Walk boundaries are the only place the pace changes, so integrate piecewise.
    boundaries = sorted({0.0, plan.distance_m} |
                        {walk[0] for walk in profile.segments})
    for index in range(len(boundaries) - 1):
        start, end = boundaries[index], boundaries[index + 1]
        if end <= start:
            continue
        speed = profile.speed_at(start)
        pace = 1000.0 / (60.0 * speed) if speed > 0.2 else plan.pace_min_per_km
        pace = min(PACE_CLAMP[1], max(PACE_CLAMP[0], pace))
        steps += (end - start) * steps_per_metre(pace)
    total = max(1, int(round(steps)))
    cadence = total * 60.0 / plan.duration_s if plan.duration_s > 0 else CADENCE_MEAN
    return {"steps": total, "cadence": cadence}


def point_rows(points: Sequence[Any], plan: RunPlan, interval_s: float = SAMPLE_INTERVAL_S) -> list[dict[str, Any]]:
    """Convert simulated points into pointsList rows on a strict sample grid.

    Real exports run on a constant grid (every one of the 36 checked records has
    runTime and ts deltas of exactly 2 s), so the grid is rebuilt from the index rather
    than from each sample's own float timestamp. That keeps ``runTime``, ``ts`` and the
    record's ``duration`` mutually consistent: duration = (rows - 1) * interval.

    Speeds come from the plan's run/walk profile when it has one, so a walk segment is
    reported as a walk (the app draws its pace chart from these values). Steps accumulate
    from the same profile through the pace/cadence coupling rather than from distance
    alone: a walking minute really is fewer steps, and deriving steps from distance would
    have made every point claim a runner's cadence.
    """
    rows: list[dict[str, Any]] = []
    if not points:
        return rows

    traveled_total = float(points[-1].modeled_distance_m)
    # The record's duration is authoritative; every row sits on the 2 s grid and the last
    # row lands exactly on it. When the duration is not a multiple of the interval (the
    # below-minimum guard), the final row carries the shorter interval -- so runTime and
    # ts stay monotonic and the last runTime always equals `duration`.
    # The grid decides the duration (see the loop below): row i sits at i * interval, so the
    # last row is at (rows - 1) * interval. build_tasklist_record reads the duration back off
    # the rows, which is how the plan, the rows and the summary stay in step.
    final_elapsed = int(round((len(points) - 1) * interval_s))
    profile = getattr(plan, "speed_profile", None)
    if profile is not None:
        plan_total_steps = plan_step_metrics(plan)["steps"]
    else:
        plan_total_steps = plan.step_total

    steps_accumulated = 0.0
    previous_distance = 0.0
    # Two passes: first work out the raw step counts from the profile, then scale them so the
    # last row lands exactly on the plan's total. Scaling is what keeps the step column
    # monotonic -- forcing only the final row made it step *backwards* wherever per-row
    # rounding had drifted above the total.
    raw_steps: list[float] = []
    paces: list[float] = []
    distances: list[float] = []
    for point in points:
        distance = float(point.modeled_distance_m)
        segment_m = max(0.0, distance - previous_distance)
        if profile is not None:
            # Sample the profile at the *midpoint* of the segment just covered: the profile
            # is the intent and the simulation's noise is small, but sampling at the start
            # reports the previous segment's pace for the first row of every walk.
            speed = profile.speed_at((previous_distance + distance) / 2.0)   # segment midpoint
        else:
            speed = float(point.modeled_speed_mps)
        pace = 1000.0 / (60.0 * speed) if speed > 0.2 else plan.pace_min_per_km
        pace = min(PACE_CLAMP[1], max(PACE_CLAMP[0], pace))
        # Steps through the pace/cadence coupling: a walking stretch really is fewer steps,
        # and deriving them from distance alone would report a runner's cadence all the way
        # through the slow part.
        steps_accumulated += segment_m * steps_per_metre(pace)
        raw_steps.append(steps_accumulated)
        paces.append(pace)
        distances.append(distance)
        previous_distance = distance

    # Scale onto ``total - 1``: the final row carries the exact total, so the rows before it
    # have to stay strictly below it or rounding can land one of them on the same value.
    scale = ((max(1, plan_total_steps - 1) / raw_steps[-1])
             if raw_steps and raw_steps[-1] > 0 else 1.0)
    previous_steps = 0
    for index, point in enumerate(points):
        # Strict grid: row i sits at i * interval, and the last row therefore lands exactly on
        # the duration. This is the only mapping that both keeps every delta equal to one
        # interval (as real exports do) and avoids a tail of clamped timestamps.
        elapsed = int(round(index * interval_s))
        distance = distances[index]
        pace = paces[index]
        steps = int(round(raw_steps[index] * scale))
        if index == len(points) - 1:
            steps = plan_total_steps
        elif steps <= previous_steps:
            # Strictly increasing: a 2 s tick always contains at least one step, and the
            # server reads this column as a cumulative counter. Rounding could otherwise
            # repeat a value between adjacent rows.
            steps = previous_steps + 1
        previous_steps = steps
        rows.append({
            # Exactly the runner's UpPointModel field set: extra keys are dropped with a
            # warning on the way out, so emitting them would only add noise to the log.
            "point": "%s,%s" % (point.longitude, point.latitude),
            "speed": round(pace, 2),
            "runStatus": 1,
            "runTime": str(elapsed),
            "isFence": "Y",
            "runStep": str(steps),
            "runMileage": str(distance),
            "ts": str(int(plan.start_epoch + elapsed)),
        })
    return rows


def build_tasklist_record(
    points: Sequence[Any],
    plan: RunPlan,
    *,
    dislikes: int = 3,
    school_id: int = 0,
) -> dict[str, Any]:
    """Assemble the tasklist document (same shape as a real server export)."""
    rows = point_rows(points, plan)
    distance_m = float(points[-1].modeled_distance_m) if points else plan.distance_m
    # Duration: with a run/walk profile the plan is authoritative -- the profile states how
    # long this distance takes (run legs at the solved pace, walk legs at the walking pace),
    # and both the per-point paces and the record summary are reported from it. Letting the
    # *sampled* tick count win instead made the record disagree with its own point list (a
    # plan of 1158 s emitted as 1080 s, i.e. 78 s short), because the simulator's noisy speed
    # process does not land exactly on the analytic duration. Without a profile the old
    # behaviour stands: the emitted grid is the record, and the last row must equal it.
    # Duration comes from the rows that exist: runTime has to advance by one sample interval
    # per row (every real export does exactly that), so the row count *decides* the duration.
    # Deriving it from the plan instead left a tail of rows clamped onto the same timestamp.
    duration = (max(1, int(round((len(rows) - 1) * SAMPLE_INTERVAL_S))) if rows
                else max(1, int(plan.duration_s)))
    window = getattr(plan, "duration_window_s", None)
    # Snap to the grid first: durations come in whole sample intervals, so the row count is the
    # only honest source -- a 952 s row count cannot be reported as 960 s without contradicting
    # its own last row.
    duration = max(1, int(round(duration / SAMPLE_INTERVAL_S))) * int(SAMPLE_INTERVAL_S)
    if window and not (math.ceil(window[0]) <= duration <= math.floor(window[1])):
        # Only when that grid value falls outside the window does anything else happen: move to
        # the nearest allowed grid point. The calibration loop normally lands inside the window
        # already (observed: 1 run in 50), so this is the rare correction, and it costs a second
        # or two of duration rather than an internally contradictory record.
        lo = math.ceil(math.ceil(window[0]) / SAMPLE_INTERVAL_S) * int(SAMPLE_INTERVAL_S)
        hi = math.floor(math.floor(window[1]) / SAMPLE_INTERVAL_S) * int(SAMPLE_INTERVAL_S)
        duration = int(min(max(duration, lo), hi))
    plan.duration_s = float(duration)
    km = distance_m / 1000.0

    # Steps must come from the same model the point rows used, or the record's own step
    # total contradicts its points: the point list accumulates steps through the
    # pace/cadence coupling (a walk really is fewer steps), so the totals are recomputed
    # from the plan's profile here rather than from a single average cadence.
    if getattr(plan, "speed_profile", None) is not None:
        # Pin the plan to the emitted duration *before* the step metrics run: those metrics
        # derive the average cadence from plan.duration_s, so computing them against a stale
        # value left the record saying 960 s while the plan still said 956 s.
        plan.duration_s = float(duration)
        result = plan_step_metrics(plan)
        total_steps = result["steps"]
        # Pin the exact count: ``step_total`` is derived from cadence and duration, so it
        # would otherwise come back a few steps off the number the point list actually ends
        # on (2715 vs 2700), which is exactly the kind of disagreement the server's own
        # `cadence == runStep * 60 / duration` identity would expose.
        plan.step_override = total_steps
    else:
        total_steps = plan.step_total
    avg_pace = round((duration / 60.0) / km, 2) if km > 0 else round(plan.pace_min_per_km, 2)
    # Real exports satisfy cadence == runStep * 60 / duration exactly; recomputing it
    # from the emitted integers keeps that identity true after rounding.
    cadence = int(total_steps * 60 / duration) if duration > 0 else int(CADENCE_MEAN)
    # Final stride check. The guard inside ``plan_run`` runs before the emitted duration is
    # pinned here, and cadence follows from that duration -- so without this a record could
    # still report a 1.27 m stride against the observed 0.85-1.26 m band. Raising the cadence
    # is the honest fix: cadence == steps*60/duration with steps == distance/stride, so the
    # three numbers cannot all stay as they are.
    if duration > 0 and km > 0:
        # Cap at the observed ceiling plus a hair: the model may sit slightly above the 19-run
        # maximum of 1.261 m, but 1.31 m starts to look like a stride nobody runs with.
        stride_ceiling = STRIDE_M_RANGE[1] + 0.02
        stride_m = distance_m / (cadence * duration / 60.0)
        if stride_m > stride_ceiling and cadence < CADENCE_RANGE[1]:
            cadence = int(min(CADENCE_RANGE[1],
                              max(cadence, distance_m / (stride_ceiling * duration / 60.0))))
    plan.cadence = float(cadence)
    # Publish the *cadence* back onto the plan, not the raw step count: ``step_total`` is
    # derived from ``cadence``, so storing a count there would make every later reader of
    # plan.step_total (and of recodeCadence) disagree by a factor of ~duration/60.
    plan.cadence = float(cadence)
    # Rebuild the rows with the final duration: the first pass ran before the grid snap and the
    # window correction, so its last row could disagree with the summary (952 against 960).
    if getattr(plan, "speed_profile", None) is not None and points:
        rows = point_rows(points, plan)

    return {
        "code": 200,
        "msg": "操作成功",
        "data": {
            "recordMileage": round(km, 2),
            "recodePace": avg_pace,
            "recodeCadence": cadence,
            "recodeDislikes": dislikes,
            "duration": duration,
            "schoolId": school_id,
            "isQualified": 1 if plan.qualified else 0,
            "manageList": [],
            "pointsList": rows,
        },
    }


def build_tasklist(
    *,
    seed: int | None = None,
    distance_km: float | None = None,
    duration_min: float | None = None,
    pace_min_per_km: float | None = None,
    cadence: float | None = None,
    dislikes: int = 3,
    school_id: int = 0,
    settings_path: str | None = None,
    calibration_path: str | None = None,
    settings_overrides: dict[str, float] | None = None,
    candidates: int = 8,
    duration_range: tuple[float, float] = GEN_DURATION_MIN_RANGE,
    distance_range: tuple[float, float] = GEN_DISTANCE_KM_RANGE,
    walk_segments: int | None = None,
) -> tuple[dict[str, Any], RunPlan, dict[str, Any]]:
    """Plan a run, simulate it, and return (tasklist document, plan, quality report)."""
    from dataclasses import replace
    from pathlib import Path

    from gps_track_lab.calibration import load_calibration
    from gps_track_lab.generator import generate_best_candidate, load_settings

    calibration_file = Path(calibration_path) if calibration_path else default_calibration_path()
    if not calibration_file.is_file():
        raise FileNotFoundError(
            f"还没有标定：{calibration_file} 不存在。先在 /admin 里走一遍「标定操场」，"
            f"或者用 gps_track_lab/tools/recalibrate_track.py 拿历史记录重估一次")
    calibration = load_calibration(calibration_file)
    settings = load_settings(settings_path or default_settings_path())
    if settings_overrides:
        settings = replace(settings, **settings_overrides)

    plan = plan_run(
        seed=seed, distance_km=distance_km, duration_min=duration_min,
        pace_min_per_km=pace_min_per_km, cadence=cadence,
        duration_range=duration_range, distance_range=distance_range,
        walk_segments=walk_segments,
    )
    # The simulator stops on distance, so the emitted duration is an emergent property of
    # the sampled speed process rather than something the caller can dial in: measured
    # over a spread of seeds at the planned pace it lands in ~912-1336 s, i.e. usually
    # inside the window but occasionally under it. The candidate that
    # generate_best_candidate keeps is chosen on physics, so the duration is only noisily
    # related to the requested pace -- bisection and proportional nudging were both tried
    # and both fail on it. Hence: simulate, and if the duration misses the window, retry
    # with a different seed. The point list is never trimmed, because dropping samples
    # would also drop the distance that was just simulated.
    floor_s = duration_range[0] * 60.0
    ceiling_s = duration_range[1] * 60.0
    # Tolerance on the floor: the emitted duration is always a whole number of intervals,
    # and the floor itself is a soft preference while the 30-minute pass limit is the hard
    # one, so half an interval of slack is accepted rather than bending the run.
    accept_s = floor_s - settings.sample_interval_seconds

    points: list[Any] = []
    used_seed = plan.seed
    quality: dict[str, Any] = {}
    sampled = 0.0
    for seed_offset in range(8):
        candidate_seed = plan.seed + seed_offset * 7919
        candidate_points, candidate_seed_used, candidate_quality = generate_best_candidate(
            calibration=calibration,
            settings=settings,
            distance_m=plan.distance_m,
            pace_min_per_km=plan.pace_min_per_km,
            candidates=candidates,
            seed=candidate_seed,
            speed_profile=plan.speed_profile,
        )
        if not candidate_points:
            continue
        candidate_sampled = (len(candidate_points) - 1) * settings.sample_interval_seconds
        points, used_seed, quality, sampled = (
            candidate_points, candidate_seed_used, candidate_quality, candidate_sampled)
        if accept_s <= sampled <= ceiling_s:
            break
    if not points:
        raise RuntimeError("simulation produced no points")

    # With a run/walk profile the duration is *not* an emergent property any more: the
    # profile states how long this particular distance takes (run legs at the solved pace,
    # walk legs at the walking pace), and the per-point paces are reported from that same
    # profile. Clamping the measured simulation time here -- which is what used to happen --
    # silently pushed a 16.5-minute plan to 21.9 minutes and made the record's average pace
    # (8.3) contradict every point it contained (5.2). That contradiction is what the pace
    # chart would have shown.
    if plan.speed_profile is not None:
        profile_seconds = plan.speed_profile.duration_for(plan.distance_m)
        grid_steps = max(1, int(round(profile_seconds / SAMPLE_INTERVAL_S)))
        # Acceptance window: the profile is the intent, but the walk count was already
        # trimmed to respect the ceiling, so this only guards against rounding drift. The
        # 30-minute pass limit stays a hard bound either way.
        plan.duration_s = grid_steps * SAMPLE_INTERVAL_S
    else:
        plan.duration_s = min(max(sampled, floor_s), ceiling_s)
    plan.pace_min_per_km = (plan.duration_s / 60.0) / (plan.distance_m / 1000.0)
    plan.seed = used_seed
    if plan.speed_profile is not None and plan.run_pace_min_per_km is None:
        plan.run_pace_min_per_km = plan.pace_min_per_km

# Calibrate the simulator against the strict grid. The emitted duration is the row count
# times the sample interval, and the simulation walks its speed profile in discrete ticks
# (clipping at every segment boundary), so it lands short of the profile's analytic
# duration -- measured: a 960 s intent simulated in 836 s. Iterating the rescale converges;
# one pass does not.
    if plan.speed_profile is not None and points:
        from gps_track_lab.generator import SpeedProfile

        interval = settings.sample_interval_seconds
        window = getattr(plan, "duration_window_s", None)
        # Iterate, because one open-loop rescale does not converge: the simulation clips each
        # tick at a segment boundary (~75 segments per run) and the speed noise shifts the
        # harmonic mean, so it lands short of the profile's analytic duration -- measured:
        # a 960 s intent simulated in 836 s.
        for _attempt in range(5):
            measured = (len(points) - 1) * interval
            if measured <= 0:
                break
            if window:
                lo = math.ceil(window[0] / interval)
                hi = math.floor(window[1] / interval)
                steps_taken = round(measured / interval)
                if lo <= steps_taken <= hi:
                    # On a grid value inside the window -- nothing left to fix.
                    break
                # Otherwise aim at the bound in *seconds*: snapping the target down to the grid
                # left the loop settling 4 s short of the floor (measured: 956 against 960).
                target = (window[0] + interval if steps_taken < lo else
                          window[1] - interval if steps_taken > hi else measured)
            else:
                target = measured
                if abs(measured - target) <= interval * 0.5:
                    break
            # Speeds scale inversely with time: to make the run last longer, slow it down.
            ratio = measured / target
            profile = plan.speed_profile
            plan.speed_profile = SpeedProfile(
                run_speed_mps=profile.run_speed_mps * ratio,
                segments=tuple((end, speed * ratio, slow)
                               for end, speed, slow in profile.segments),
                end_distance_m=plan.distance_m,
            )
            points, _seed_used, _quality = generate_best_candidate(
                calibration=calibration,
                settings=settings,
                distance_m=plan.distance_m,
                pace_min_per_km=plan.pace_min_per_km,
                candidates=candidates,
                seed=used_seed,
                speed_profile=plan.speed_profile,
            )
            if not points:
                break

    document = build_tasklist_record(points, plan, dislikes=dislikes, school_id=school_id)
    quality = dict(quality)
    quality["planned_duration_s"] = round(plan.duration_s, 2)
    quality["stride_m"] = round(plan.stride_m, 4)
    quality["step_total"] = plan.step_total
    # Hand the raw trajectory back to callers that need the geometry itself (the local web
    # server persists it as a run so the viewer can draw it).
    quality["_points"] = points
    # The per-point pace that was actually submitted. The raw simulated speed in the CSV is the
    # same value with the simulation noise still on it -- it spans 2-21 min/km -- so anything
    # drawing a pace chart must use this clean trace, or the chart shows noise instead of the
    # run/walk shape. Recomputed rather than taken from the document so the server payload
    # itself stays exactly the fields the runner expects.
    quality["pace_trace"] = [float(row["speed"]) for row in point_rows(points, plan)]
    return document, plan, quality
