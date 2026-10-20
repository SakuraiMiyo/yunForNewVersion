"""Tests for the run/walk pace model.

Why this exists: a single-speed simulation renders in the app as one gently rippling line,
and the coach flagged it as obviously fake ("全程跑那么快很假的"). Real campus runs are
run -> short walk -> run again, so the pace trace is segmented. These tests pin the shape
of that model *and* the invariants that keep it believable:

* a walk must actually be slower than the running it interrupts;
* the solved running pace must stay in a plausible range -- an earlier cap of 8.5 min/km
  produced records whose "run" (8.3) was slower than their own "walk" (7.5-9.0);
* the record's duration must stay inside the requested 16-25 minute window, because the
  walk segments are added on top of the running;
* a point's cadence must follow its pace, since the server derives stride from both.

Run via the lab's standard discovery:

    python -m unittest discover -s gps_track_lab/tests -t gps_track_lab/tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "gps_track_lab" / "src"))
sys.path.insert(0, str(PROJECT / "tools"))

import tasklist_builder as tb  # noqa: E402
from gps_track_lab.generator import SpeedProfile, SimulationSettings, simulate  # noqa: E402


class SpeedProfileUnit(unittest.TestCase):
    def test_speed_at_switches_at_walk_bounds(self) -> None:
        # segments are (end_distance_m, speed_mps) in increasing distance order
        profile = SpeedProfile(run_speed_mps=3.0,
                               segments=((100.0, 3.0, False), (150.0, 1.6, True),
                                         (300.0, 3.0, False)),
                               end_distance_m=300.0)
        self.assertEqual(profile.speed_at(50.0), 3.0)
        self.assertEqual(profile.speed_at(100.0), 1.6)      # inclusive start
        self.assertEqual(profile.speed_at(149.9), 1.6)
        self.assertEqual(profile.speed_at(150.0), 3.0)      # exclusive end
        self.assertTrue(profile.is_walking(120.0))
        self.assertFalse(profile.is_walking(160.0))

    def test_duration_for_adds_walk_time(self) -> None:
        # 200 m: 100 m at 4 m/s (25 s) + 50 m walked at 1 m/s (50 s) + 50 m at 4 m/s (12.5 s).
        profile = SpeedProfile(run_speed_mps=4.0,
                               segments=((100.0, 4.0, False), (150.0, 1.0, True),
                                         (200.0, 4.0, False)),
                               end_distance_m=200.0)
        self.assertAlmostEqual(profile.duration_for(200.0), 87.5, places=6)
        self.assertAlmostEqual(profile.duration_for(100.0), 25.0, places=6)
        # Walking the same metres costs more time than running them: the whole point.
        without_walk = SpeedProfile(run_speed_mps=4.0, segments=(), end_distance_m=200.0)
        self.assertGreater(profile.duration_for(200.0), without_walk.duration_for(200.0))

    def test_walk_distance_and_share(self) -> None:
        profile = SpeedProfile(run_speed_mps=3.0,
                               segments=((10.0, 3.0, False), (40.0, 1.5, True),
                                         (80.0, 3.0, False), (100.0, 1.5, True),
                                         (200.0, 3.0, False)),
                               end_distance_m=200.0)
        self.assertAlmostEqual(profile.walk_distance_m(), 50.0, places=6)


class PlanWalkShape(unittest.TestCase):
    """The plan is where the pace shape is decided."""

    def test_a_plan_has_walks_and_they_are_slower_than_the_run(self) -> None:
        unhealthy = []
        for seed in range(400, 430):
            plan = tb.plan_run(seed=seed)
            if not plan.walk_paces:
                unhealthy.append((seed, "no walks"))
                continue
            if plan.run_pace_min_per_km >= min(plan.walk_paces):
                unhealthy.append((seed, round(plan.run_pace_min_per_km, 2), plan.walk_paces))
        self.assertEqual(unhealthy, [], "run pace must be clearly faster than walk pace")

    def test_walks_stay_a_small_share_of_the_distance(self) -> None:
        for seed in range(400, 430):
            plan = tb.plan_run(seed=seed)
            self.assertLess(plan.walk_share, 0.55, f"seed {seed} walks too much")
            self.assertGreater(plan.walk_share, 0.0, f"seed {seed} has no walks at all")

    def test_running_pace_stays_plausible(self) -> None:
        for seed in range(400, 430):
            plan = tb.plan_run(seed=seed)
            pace = plan.run_pace_min_per_km
            self.assertGreaterEqual(pace, tb.RUN_PACE_SOLVE_RANGE[0])
            self.assertLessEqual(pace, tb.RUN_PACE_SOLVE_RANGE[1])

    def test_walk_count_can_be_forced(self) -> None:
        self.assertEqual(tb.plan_run(seed=1, walk_segments=0).speed_profile, None)
        two = tb.plan_run(seed=7, walk_segments=2)
        self.assertEqual(len(two.walk_paces), 2)

    def test_zero_walks_reproduces_the_single_speed_profile(self) -> None:
        plan = tb.plan_run(seed=99, walk_segments=0)
        self.assertIsNone(plan.speed_profile)
        self.assertAlmostEqual(plan.pace_min_per_km, plan.duration_s / 60.0 / (plan.distance_m / 1000.0),
                               places=6)


class GeneratedRecordInvariants(unittest.TestCase):
    """End to end: the emitted tasklist must stay inside everything the server checks."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = {}
        for seed in range(500, 512):
            cls.docs[seed] = tb.build_tasklist(seed=seed, candidates=4)

    def test_duration_window_holds(self) -> None:
        for seed, (doc, plan, _) in self.docs.items():
            minutes = plan.duration_s / 60.0
            self.assertGreaterEqual(minutes, tb.GEN_DURATION_MIN_RANGE[0] - 1e-9, f"seed {seed}")
            self.assertLessEqual(minutes, tb.GEN_DURATION_MIN_RANGE[1] + 1e-9, f"seed {seed}")
            # and the record agrees with the last point
            self.assertEqual(str(int(plan.duration_s)), doc["data"]["pointsList"][-1]["runTime"])

    def test_distance_and_pass_rule_hold(self) -> None:
        for seed, (doc, plan, _) in self.docs.items():
            self.assertGreater(plan.distance_m / 1000.0, tb.MIN_QUALIFIED_KM, f"seed {seed}")
            self.assertTrue(plan.qualified, f"seed {seed}")

    def test_pace_trace_is_segmented_not_flat(self) -> None:
        """The whole point: the pace has to move enough to read as run/walk.

        Thresholds come from measurement over 15 seeds: the spread runs 1.11-3.66 min/km,
        slow points are 2-8% of samples, and every seed has at least one. A flat single-speed
        trace sits well under 1.0 -- that flatness was the complaint.
        """
        for seed, (doc, plan, _) in self.docs.items():
            paces = [float(p["speed"]) for p in doc["data"]["pointsList"]]
            spread = max(paces) - min(paces)
            slow = [p for p in paces if p >= 7.0]
            self.assertGreater(spread, 1.0, f"seed {seed} pace spread only {spread:.2f}")
            self.assertGreater(len(slow), 0, f"seed {seed} has no slow points at all")
            # The walk has to be genuinely slower than the run it interrupts.
            self.assertGreater(max(paces), plan.run_pace_min_per_km + 0.5, f"seed {seed}")
            # The fast part must be a real majority: this is a run, not a stroll.
            self.assertGreater(len(paces) - len(slow), len(slow), f"seed {seed} mostly slow")

    def test_row_timeline_advances_by_one_interval(self) -> None:
        """runTime must advance by a whole sample interval per row, with no repeats.

        Real exports all behave this way (36 checked records: runTime deltas of exactly 2 s).
        A repeated timestamp is what the server would read as a stalled timeline; it happened
        on the last 40 rows of a record before the grid was tied to the row count.
        """
        for seed, (doc, _plan, _) in self.docs.items():
            times = [int(point["runTime"]) for point in doc["data"]["pointsList"]]
            deltas = {b - a for a, b in zip(times, times[1:])}
            self.assertTrue(deltas, f"seed {seed} has no rows to compare")
            self.assertEqual(len(times), len(set(times)), f"seed {seed} repeated a runTime")
            self.assertTrue(deltas <= {int(tb.SAMPLE_INTERVAL_S)}, f"seed {seed} deltas {deltas}")
            self.assertEqual(times[0], 0, f"seed {seed} first runTime is {times[0]}")
            self.assertEqual(times[-1], int(doc["data"]["duration"]), f"seed {seed} last row")

    def test_step_column_never_repeats(self) -> None:
        """Every row must add at least one step: a 2 s tick cannot be walkless."""
        for seed, (doc, plan, _) in self.docs.items():
            steps = [int(point["runStep"]) for point in doc["data"]["pointsList"]]
            self.assertEqual(len(steps), len(set(steps)), f"seed {seed} repeated a runStep")
            self.assertEqual(steps[0] > 0, True, f"seed {seed} starts at zero steps")
            # And the last row is the record's own total, which the summary quotes.
            self.assertEqual(steps[-1], plan.step_total, f"seed {seed}")

    def test_step_column_is_monotonic(self) -> None:
        """runStep must never go backwards (a walk sample could otherwise 'lose' steps)."""
        for seed, (doc, _plan, _) in self.docs.items():
            steps = [int(point["runStep"]) for point in doc["data"]["pointsList"]]
            self.assertEqual(steps, sorted(steps), f"seed {seed} step column went backwards")

    def test_every_point_stays_inside_the_server_rule_bands(self) -> None:
        for seed, (doc, _plan, _) in self.docs.items():
            for point in doc["data"]["pointsList"]:
                pace = float(point["speed"])
                self.assertGreaterEqual(pace, tb.PACE_CLAMP[0], f"seed {seed} pace {pace}")
                self.assertLessEqual(pace, tb.PACE_CLAMP[1], f"seed {seed} pace {pace}")
            cadence = doc["data"]["recodeCadence"]
            self.assertGreaterEqual(cadence, 90, f"seed {seed}")
            self.assertLessEqual(cadence, 240, f"seed {seed}")

    def test_steps_follow_the_pace_cadence_coupling(self) -> None:
        """Walking takes MORE steps per metre than running, because the stride is shorter.

        Worth being explicit about, since the intuition goes the other way: at a 8.2 min/km
        walk the coupled cadence is ~104 steps/min, i.e. a ~1.17 m pace length, while the
        5.2 min/km run gives ~152 steps/min and a ~1.17 m stride. Measuring over the two
        zones (rather than point by point, where a single 2 s sample straddles the boundary)
        is what makes the comparison meaningful.
        """
        for seed, (doc, _plan, _) in self.docs.items():
            rows = doc["data"]["pointsList"]
            fast_m = fast_steps = slow_m = slow_steps = 0.0
            previous_distance = 0.0
            previous_steps = 0
            for row in rows:
                distance = float(row["runMileage"])
                steps = int(row["runStep"])
                delta_m = distance - previous_distance
                delta_steps = steps - previous_steps
                if float(row["speed"]) >= 7.0:          # walking zone (pace is min/km)
                    slow_m += delta_m
                    slow_steps += delta_steps
                else:
                    fast_m += delta_m
                    fast_steps += delta_steps
                previous_distance, previous_steps = distance, steps
            self.assertGreater(slow_m, 0, f"seed {seed} has no walking metres")
            self.assertGreater(fast_m, 0, f"seed {seed} has no running metres")
            fast_stride = fast_m / fast_steps
            slow_stride = slow_m / slow_steps
            # A walking pace is shorter than a running stride -> more steps per metre.
            self.assertLess(slow_stride, fast_stride,
                            f"seed {seed}: walk stride {slow_stride:.3f} vs run {fast_stride:.3f}")
            # ... and both must be human-sized.
            self.assertGreater(slow_stride, 0.5, f"seed {seed}")
            self.assertLess(fast_stride, 1.5, f"seed {seed}")

    def test_cadence_and_step_total_agree_with_the_record(self) -> None:
        """The identity real exports satisfy: cadence == runStep * 60 / duration."""
        for seed, (doc, _plan, _) in self.docs.items():
            data = doc["data"]
            rows = data["pointsList"]
            steps = int(rows[-1]["runStep"])
            duration = int(data["duration"])
            cadence = int(data["recodeCadence"])
            self.assertGreaterEqual(cadence, 90, f"seed {seed} cadence {cadence}")
            self.assertLessEqual(cadence, 240, f"seed {seed} cadence {cadence}")
            self.assertEqual(cadence, int(steps * 60 / duration), f"seed {seed}")
            self.assertEqual(duration, int(rows[-1]["runTime"]), f"seed {seed}")

    def test_stride_stays_in_the_observed_band(self) -> None:
        """The stride the record implies must be one a real export could show.

        Observed over 19 clean runs: 0.846-1.261 m. Cadence, step count and duration are
        linked (cadence == steps*60/duration, steps == distance/stride), so a run whose pace
        is fast enough forces a stride no record has -- this check is what caught 1.27 m.
        """
        for seed, (doc, plan, _) in self.docs.items():
            data = doc["data"]
            # stride is simply distance / steps. Reconstructing the step count from the
            # record's rounded cadence instead (cadence*duration/60) is off by ~17 steps and
            # made this check report a 21 m stride -- the identity is stored rounded on
            # purpose, so the raw step count is the right source here.
            steps = int(data["pointsList"][-1]["runStep"])
            stride = plan.distance_m / steps if steps else 0.0
            self.assertGreaterEqual(stride, tb.STRIDE_M_RANGE[0] - 0.08,
                                    f"seed {seed} stride {stride:.3f}")
            self.assertLessEqual(stride, tb.STRIDE_M_RANGE[1] + 0.01,
                                 f"seed {seed} stride {stride:.3f}")

    def test_step_total_matches_the_record(self) -> None:
        for seed, (doc, plan, _) in self.docs.items():
            rows = doc["data"]["pointsList"]
            self.assertEqual(int(rows[-1]["runStep"]), plan.step_total, f"seed {seed}")
            self.assertEqual(float(rows[-1]["runMileage"]), plan.distance_m, f"seed {seed}")
            # stride stays in the observed band
            stride = plan.stride_m
            self.assertGreaterEqual(stride, tb.STRIDE_M_RANGE[0] - 0.08, f"seed {seed}")
            self.assertLessEqual(stride, tb.STRIDE_M_RANGE[1] + 0.08, f"seed {seed}")


class SimulatorHonoursTheProfile(unittest.TestCase):
    """The simulator must consume the profile, not fall back to one constant speed."""

    def setUp(self) -> None:
        self.calibration = tb.__dict__  # placeholder, replaced below
        from gps_track_lab.calibration import load_calibration

        self.calibration = load_calibration(tb.default_calibration_path())
        self.settings = SimulationSettings()

    def test_speed_drops_inside_a_walk_and_recovers_after(self) -> None:
        profile = SpeedProfile(run_speed_mps=3.2,
                               segments=((300.0, 3.2, False), (450.0, 1.6, True),
                                         (800.0, 3.2, False)),
                               end_distance_m=800.0)
        points, _ = simulate(self.calibration, self.settings, 800.0, 5.2,
                             seed=2024, speed_profile=profile)
        inside = [p.modeled_speed_mps for p in points if 320.0 <= p.modeled_distance_m <= 430.0]
        before = [p.modeled_speed_mps for p in points if 150.0 <= p.modeled_distance_m <= 280.0]
        after = [p.modeled_speed_mps for p in points if 600.0 <= p.modeled_distance_m <= 780.0]
        self.assertTrue(inside and before and after)
        self.assertLess(sum(inside) / len(inside), sum(before) / len(before) - 0.8)
        self.assertLess(sum(inside) / len(inside), sum(after) / len(after) - 0.8)

    def test_no_profile_keeps_the_old_single_speed_behaviour(self) -> None:
        points, _ = simulate(self.calibration, self.settings, 500.0, 5.5, seed=11)
        speeds = [p.modeled_speed_mps for p in points]
        # Tight around the target (1/5.5 min/km = 3.03 m/s) with no walk-sized excursions.
        self.assertLess(max(speeds) - min(speeds), 1.2)


if __name__ == "__main__":
    unittest.main()
