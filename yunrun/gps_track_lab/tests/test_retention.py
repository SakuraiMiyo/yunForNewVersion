"""Retention tests: the newest N survive, and only real runs are counted.

The interesting failure modes are not "does rmtree work" but the accounting ones: a
directory that carries the run prefix but holds no payload (a OneDrive stub left behind by
dehydration) must not consume one of the N slots, and it must be reported rather than
silently ignored, because it cannot be deleted locally either.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gps_track_lab.retention import (
    PruneReport,
    leftover_count,
    prune_all,
    prune_runs,
    prune_tasklists,
    run_count,
    tasklist_count,
)


def make_run(root: Path, name: str, *, payload: bool = True, mtime: float | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    if payload:
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
    if mtime is not None:
        os.utime(directory, (mtime, mtime))
    return directory


class RunPruning(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_keeps_newest_by_mtime(self) -> None:
        for index in range(15):
            make_run(self.root, f"synthetic-run-{index:02d}", mtime=1_700_000_000 + index)
        report = prune_runs(self.root, keep=10)
        self.assertEqual(len(report.kept), 10)
        self.assertEqual(report.removed_count, 5)
        remaining = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(len(remaining), 10)
        self.assertIn("synthetic-run-14", remaining)
        self.assertNotIn("synthetic-run-00", remaining)

    def test_name_is_the_tie_break_for_equal_mtimes(self) -> None:
        for name in ("synthetic-b", "synthetic-a", "synthetic-c"):
            make_run(self.root, name, mtime=1_700_000_000)
        report = prune_runs(self.root, keep=1)
        self.assertEqual(report.kept, ["synthetic-c"])

    def test_keep_zero_removes_everything(self) -> None:
        make_run(self.root, "synthetic-one")
        report = prune_runs(self.root, keep=0)
        self.assertEqual(report.kept, [])
        self.assertEqual(report.removed_count, 1)
        self.assertEqual(run_count(self.root), 0)

    def test_negative_keep_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            prune_runs(self.root, keep=-1)

    def test_missing_directory_is_not_an_error(self) -> None:
        report = prune_runs(self.root / "nope", keep=10)
        self.assertEqual(report.kept, [])
        self.assertEqual(report.removed_count, 0)

    def test_other_directories_are_left_alone(self) -> None:
        make_run(self.root, "synthetic-real")
        keep_me = self.root / "handwritten-notes"
        keep_me.mkdir()
        prune_runs(self.root, keep=0)
        self.assertTrue(keep_me.is_dir())


class PayloadAccounting(unittest.TestCase):
    """A directory without a manifest is a stub, not a run."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_only_directories_with_a_manifest_are_counted(self) -> None:
        make_run(self.root, "synthetic-real-1")
        make_run(self.root, "synthetic-real-2")
        make_run(self.root, "synthetic-stub-1", payload=False)
        self.assertEqual(run_count(self.root), 2)
        self.assertEqual(leftover_count(self.root), 1)

    def test_deletable_stubs_are_cleaned_up_and_free_the_slots(self) -> None:
        # The stub is the *newest* entry. If it were kept as if it were a run, a real run
        # would be pushed out of the kept set and the user would silently lose a usable
        # trajectory; pruning removes it first instead.
        make_run(self.root, "synthetic-real-old", mtime=1_700_000_000)
        make_run(self.root, "synthetic-real-new", mtime=1_700_000_100)
        make_run(self.root, "synthetic-stub-newest", payload=False, mtime=1_700_000_200)
        report = prune_runs(self.root, keep=2)
        self.assertEqual(sorted(report.kept), ["synthetic-real-new", "synthetic-real-old"])
        self.assertEqual(report.removed, ["synthetic-stub-newest"])
        self.assertEqual(report.stuck, [])
        self.assertTrue((self.root / "synthetic-real-old" / "manifest.json").is_file())

    def test_undeletable_leftovers_are_archived_not_kept(self) -> None:
        # A read-only remnant (OneDrive marks dehydrated dirs read-only) cannot be deleted
        # even after clearing the bit in some cases. It must not keep a real run from
        # surviving the limit, so it is moved to _stale/ -- nothing is destroyed, but it
        # stops competing for the N slots.
        make_run(self.root, "synthetic-real", mtime=1_700_000_000)
        make_run(self.root, "synthetic-stub", payload=False, mtime=1_700_000_200)

        def refuse(path, *args, **kwargs):
            raise PermissionError(5, "拒绝访问")

        with patch("gps_track_lab.retention.shutil.rmtree", side_effect=refuse):
            report = prune_runs(self.root, keep=1)
        self.assertEqual(report.kept, ["synthetic-real"])
        self.assertEqual(report.stuck, [])
        self.assertEqual(report.archived, ["synthetic-stub"])
        self.assertTrue((self.root / "_stale" / "synthetic-stub").is_dir())
        self.assertEqual(run_count(self.root), 1)      # the real one is still on disk

    def test_readonly_stub_is_deleted_after_clearing_the_bit(self) -> None:
        """The real-world case: OneDrive leaves the directory read-only."""
        import stat

        stub = make_run(self.root, "synthetic-stub", payload=False)
        os.chmod(stub, stat.S_IREAD)          # read-only, exactly like the cloud stub
        report = prune_runs(self.root, keep=10)
        self.assertEqual(report.removed, ["synthetic-stub"])
        self.assertEqual(report.stuck, [])
        self.assertFalse(stub.exists())

    def test_readonly_run_is_pruned_after_clearing_the_bit(self) -> None:
        import stat

        make_run(self.root, "synthetic-old", mtime=1_700_000_000)
        newest = make_run(self.root, "synthetic-new", mtime=1_700_000_100)
        os.chmod(self.root / "synthetic-old", stat.S_IREAD)
        os.chmod(self.root / "synthetic-old" / "manifest.json", stat.S_IREAD)
        report = prune_runs(self.root, keep=1)
        self.assertEqual(report.kept, [newest.name])
        self.assertEqual(report.removed, ["synthetic-old"])
        self.assertEqual(report.stuck, [])

    def test_undeletable_directory_is_reported_not_hidden(self) -> None:
        for index in range(3):
            make_run(self.root, f"synthetic-run-{index}", mtime=1_700_000_000 + index)

        def refuse(path, *args, **kwargs):
            raise PermissionError(5, "拒绝访问")

        with patch("gps_track_lab.retention.ARCHIVE_DIR", "_stale-locked"), \
                patch("gps_track_lab.retention._archive_path", return_value=None), \
                patch("gps_track_lab.retention.shutil.rmtree", side_effect=refuse):
            report = prune_runs(self.root, keep=1)
        self.assertEqual(len(report.kept), 1)
        self.assertEqual(len(report.stuck), 2)
        self.assertEqual(report.removed_count, 0)
        self.assertEqual(len(report.errors), 2)
        # The count stays honest: the stuck directories are still there.
        self.assertEqual(run_count(self.root), 3)

    def test_report_shape_is_stable(self) -> None:
        report = PruneReport(kept=["a"], removed=["b"], stuck=["c"], archived=["d"])
        self.assertEqual(report.as_dict()["kept"], 1)
        self.assertEqual(report.as_dict()["removed_count"], 1)
        self.assertEqual(report.as_dict()["stuck"], 1)
        self.assertEqual(report.as_dict()["archived"], 1)
        self.assertEqual(report.as_dict()["errors"], [])


class TasklistPruning(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.staging = self.root / "tasks_sim"
        self.staging.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_keeps_newest_tables_only(self) -> None:
        for index in range(13):
            path = self.staging / f"tasklist_sim_{index:04d}.json"
            path.write_text("{}", encoding="utf-8")
            os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
        report = prune_tasklists(self.staging, keep=10)
        self.assertEqual(report.removed_count, 3)
        self.assertEqual(tasklist_count(self.staging), 10)

    def test_foreign_json_files_survive(self) -> None:
        (self.staging / "tasklist_sim_0001.json").write_text("{}", encoding="utf-8")
        (self.staging / "notes.json").write_text("{}", encoding="utf-8")
        (self.staging / "tasklist_sim_0002.txt").write_text("x", encoding="utf-8")
        prune_tasklists(self.staging, keep=0)
        survivors = sorted(p.name for p in self.staging.iterdir())
        self.assertEqual(survivors, ["notes.json", "tasklist_sim_0002.txt"])

    def test_subdirectories_are_ignored(self) -> None:
        (self.staging / "tasklist_sim_nested").mkdir()
        report = prune_tasklists(self.staging, keep=0)
        self.assertEqual(report.removed_count, 0)
        self.assertTrue((self.staging / "tasklist_sim_nested").is_dir())

    def test_worker_logs_follow_the_same_limit(self) -> None:
        """A log is useful while diagnosing a run and worthless afterwards -- no growth."""
        from gps_track_lab.retention import prune_run_logs

        for index in range(14):
            path = self.staging / f"run_token{index:02d}_170000{index:04d}.log"
            path.write_text("lines\n", encoding="utf-8")
            os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
        report = prune_run_logs(self.staging, keep=10)
        self.assertEqual(report.removed_count, 4)
        self.assertEqual(len(list(self.staging.glob("run_*.log"))), 10)

    def test_prune_all_covers_logs_too(self) -> None:
        from gps_track_lab.retention import prune_all

        runs = self.root / "runs"
        runs.mkdir()
        for index in range(3):
            path = self.staging / f"run_x{index}.log"
            path.write_text("x", encoding="utf-8")
            os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
        report = prune_all(runs, self.staging, keep_runs=1, keep_tasklists=1)
        self.assertIsNotNone(report["logs"])
        self.assertEqual(report["logs"]["kept"], 1)
        self.assertEqual(report["logs"]["removed_count"], 2)


class PruneAll(unittest.TestCase):
    def test_both_caches_in_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs, staging = root / "runs", root / "tasks_sim"
            runs.mkdir()
            staging.mkdir()
            for index in range(12):
                make_run(runs, f"synthetic-{index:02d}", mtime=1_700_000_000 + index)
            for index in range(12):
                path = staging / f"tasklist_sim_{index:04d}.json"
                path.write_text("{}", encoding="utf-8")
                os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
            report = prune_all(runs, staging, keep_runs=10, keep_tasklists=5)
            self.assertEqual(report["runs"]["removed_count"], 2)
            self.assertEqual(report["tasklists"]["removed_count"], 7)
            self.assertEqual(run_count(runs), 10)
            self.assertEqual(tasklist_count(staging), 5)


if __name__ == "__main__":
    unittest.main()
