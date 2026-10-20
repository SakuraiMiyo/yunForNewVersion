# -*- coding: utf-8 -*-
"""Retention for generated artefacts: keep the most recent N, drop the rest.

Two kinds of output accumulate every time a trajectory is generated:

* ``gps_track_lab/runs/synthetic-*``  -- the simulated run (CSV, GeoJSON, manifest, card)
* ``tasks_sim/tasklist_sim_*.json``   -- the staged table handed to the runner

Both grow without bound, so every entry point prunes *before* it generates. Pruning
first keeps the invariant "the cache never holds more than N" true at all times, which is
what a periodic "trim after writing" approach cannot promise.

Deletion is deliberately narrow: only directories matching the run prefix, only regular
files in the staging directory, and only files older than the newest N.
"""
from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Default retention. 10 runs is enough to compare a few generations and still keeps the
# directory small (each run is roughly 380 KB with its card).
KEEP_RUNS = 10
KEEP_TASKLISTS = 10

RUN_PREFIX = "synthetic-"
TASKLIST_PREFIX = "tasklist_sim_"
# Logs written by the detached worker (one per started run).
RUN_LOG_PREFIX = "run_"
# Where a run goes when it has to leave the top of the directory but cannot be deleted.
ARCHIVE_DIR = "_stale"


def _clear_readonly(path: Path) -> bool:
    """Drop the read-only bit so the entry can be removed. Returns True if it changed.

    Windows refuses to delete a read-only entry, and a project inside OneDrive ends up
    with read-only directories whose content was dehydrated to the cloud. Without this,
    pruning reports "access denied" forever and the retention limit silently stops
    holding -- which is exactly what happened here: 89 stubs plus 89 real runs all stuck.
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    if not mode & stat.S_IWRITE:
        try:
            os.chmod(path, mode | stat.S_IWRITE)
            return True
        except OSError:
            return False
    return False


def _clear_tree_readonly(root: Path) -> None:
    """Best effort: clear the read-only bit on every entry under ``root``."""
    _clear_readonly(root)
    try:
        entries = list(root.rglob("*"))
    except OSError:
        return
    for entry in entries:
        _clear_readonly(entry)


def _delete_path(path: Path) -> None:
    """Remove a file or directory, retrying once without the read-only bit."""
    remove = path.unlink if path.is_file() else shutil.rmtree
    try:
        remove(path)
        return
    except OSError:
        pass
    # A single read-only entry anywhere in the tree blocks the whole delete.
    _clear_readonly(path)
    if path.is_dir():
        _clear_tree_readonly(path)
    remove(path)


def _archive_path(path: Path) -> Path | None:
    """Move a stubborn entry into ``_stale/`` so it stops counting as a run.

    Used only when nothing else works: the run stays on disk (nothing is destroyed) but
    leaves the pool of candidates, which is what keeps the retention limit meaningful.
    Returns the new path, or None if even the rename failed.
    """
    try:
        target = path.parent / ARCHIVE_DIR
        target.mkdir(exist_ok=True)
        destination = target / path.name
        if destination.exists():
            return destination
        path.rename(destination)
        return destination
    except OSError:
        return None


@dataclass
class PruneReport:
    """What a prune removed, so callers can report it instead of acting silently."""

    kept: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Entries moved to ``_stale/`` because they could not be deleted: out of the running
    # for the limit, but not destroyed either.
    archived: list[str] = field(default_factory=list)
    # Entries that could neither be deleted nor archived. These still occupy a slot.
    stuck: list[str] = field(default_factory=list)

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    def as_dict(self) -> dict:
        return {
            "kept": len(self.kept),
            "removed": self.removed,
            "removed_count": self.removed_count,
            "errors": self.errors,
            "archived": len(self.archived),
            "stuck": len(self.stuck),
        }


def _has_payload(path: Path) -> bool:
    """True when a run directory actually holds a run (its manifest is readable).

    This is the only reliable test for "is this a run": a cloud stub left behind by
    OneDrive looks like a directory but holds no content, so counting directories instead
    of payloads makes the retention figure disagree with the limit.
    """
    try:
        return (path / "manifest.json").is_file()
    except OSError:
        return False


def _is_cloud_placeholder(path: Path) -> bool:
    """True for a OneDrive-style on-demand stub rather than a local directory.

    Worth detecting explicitly: such a path cannot be deleted while its content lives only
    in the cloud, and the raw OSError ("access denied") reads like a permissions bug when
    it is really a sync state.
    """
    try:
        stat_result = path.stat()
    except OSError:
        return True
    attrs = getattr(stat_result, "st_file_attributes", 0)
    return bool(attrs & 0x400)   # FILE_ATTRIBUTE_REPARSE_POINT


def _newest_first(paths: Iterable[Path]) -> list[Path]:
    """Order by mtime, newest first, with the name as a deterministic tie-break.

    Run directory names carry a timestamp, so the name order matches the mtime order and
    makes the result stable when several runs land in the same second. Entries whose
    metadata cannot be read (a cloud placeholder with no local content) sort last.
    """
    def key(path: Path) -> tuple[int, float, str]:
        try:
            return (0, path.stat().st_mtime, path.name)
        except OSError:
            return (1, 0.0, path.name)

    return sorted(paths, key=key, reverse=True)


def prune_runs(runs_dir: str | Path, keep: int = KEEP_RUNS) -> PruneReport:
    """Keep the newest ``keep`` simulated runs; delete older ones.

    Directories that carry the run prefix but hold no payload (cloud stubs) are cleaned up
    first. Anything that still refuses to be deleted is archived under ``_stale/`` instead,
    so it stops competing for the N slots -- a read-only remnant must never be the reason a
    real, usable trajectory gets thrown away.
    """
    report = PruneReport()
    root = Path(runs_dir)
    if keep < 0:
        raise ValueError("keep must be >= 0")
    if not root.is_dir():
        return report

    candidates = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(RUN_PREFIX)]
    # A stub is not a run, so try to get rid of it before choosing what to keep: that is
    # the only way the limit can actually hold when the folder is full of leftovers.
    remaining: list[Path] = []
    for path in _newest_first(candidates):
        if not _has_payload(path):
            try:
                _delete_path(path)
                report.removed.append(path.name)
                continue
            except OSError as exc:
                hint = ("（按需同步的云占位符：内容只在云端；已清只读仍删不掉）"
                        if _is_cloud_placeholder(path) else "")
                report.errors.append(f"{path.name}: {exc}{hint}")
                if _archive_path(path) is not None:
                    report.archived.append(path.name)
                    continue
                report.stuck.append(path.name)
        remaining.append(path)

    report.kept = [p.name for p in remaining[:keep]]
    for stale in remaining[keep:]:
        try:
            _delete_path(stale)
            report.removed.append(stale.name)
        except OSError as exc:
            hint = ("（按需同步的云占位符：内容只在云端；已清只读仍删不掉）"
                    if _is_cloud_placeholder(stale) else "")
            report.errors.append(f"{stale.name}: {exc}{hint}")
            if _archive_path(stale) is not None:
                report.archived.append(stale.name)
            else:
                report.stuck.append(stale.name)
    return report


def prune_tasklists(staging_dir: str | Path, keep: int = KEEP_TASKLISTS) -> PruneReport:
    """Keep the newest ``keep`` staged tables; delete older ones."""
    report = PruneReport()
    root = Path(staging_dir)
    if keep < 0:
        raise ValueError("keep must be >= 0")
    if not root.is_dir():
        return report

    candidates = [
        p for p in root.iterdir()
        if p.is_file() and p.name.startswith(TASKLIST_PREFIX) and p.suffix == ".json"
    ]
    ordered = _newest_first(candidates)
    report.kept = [p.name for p in ordered[:keep]]
    for stale in ordered[keep:]:
        try:
            _delete_path(stale)
            report.removed.append(stale.name)
        except OSError as exc:
            report.errors.append(f"{stale.name}: {exc}")
    return report


def prune_run_logs(staging_dir: str | Path, keep: int = KEEP_TASKLISTS) -> PruneReport:
    """Keep the newest ``keep`` detached-worker logs.

    Each started run leaves a small log next to its table; they are useful while diagnosing
    a run and worthless afterwards, so they follow the same limit rather than growing.
    """
    report = PruneReport()
    root = Path(staging_dir)
    if keep < 0:
        raise ValueError("keep must be >= 0")
    if not root.is_dir():
        return report

    candidates = [p for p in root.iterdir()
                  if p.is_file() and p.name.startswith(RUN_LOG_PREFIX) and p.suffix == ".log"]
    ordered = _newest_first(candidates)
    report.kept = [p.name for p in ordered[:keep]]
    for stale in ordered[keep:]:
        try:
            _delete_path(stale)
            report.removed.append(stale.name)
        except OSError as exc:
            report.errors.append(f"{stale.name}: {exc}")
    return report


def prune_all(runs_dir: str | Path | None = None, staging_dir: str | Path | None = None,
              keep_runs: int = KEEP_RUNS, keep_tasklists: int = KEEP_TASKLISTS) -> dict:
    """Prune both caches; returns a combined report."""
    return {
        "runs": prune_runs(runs_dir, keep_runs).as_dict() if runs_dir else None,
        "tasklists": prune_tasklists(staging_dir, keep_tasklists).as_dict() if staging_dir else None,
        "logs": prune_run_logs(staging_dir, keep_tasklists).as_dict() if staging_dir else None,
    }


def run_count(runs_dir: str | Path) -> int:
    """Number of actual runs on disk (directories that hold a manifest)."""
    root = Path(runs_dir)
    if not root.is_dir():
        return 0
    return sum(1 for p in root.iterdir()
               if p.is_dir() and p.name.startswith(RUN_PREFIX) and _has_payload(p))


def archived_count(runs_dir: str | Path) -> int:
    """Entries parked under ``_stale/`` (undeletable remnants), for reporting only."""
    archive = Path(runs_dir) / ARCHIVE_DIR
    if not archive.is_dir():
        return 0
    try:
        return sum(1 for _ in archive.iterdir())
    except OSError:
        return 0


def leftover_count(runs_dir: str | Path) -> int:
    """Directories with the run prefix but no payload: cloud stubs, not runs.

    Reported so a folder that looks over-full can be explained ("10 runs + 89 stubs")
    instead of leaving the retention figure looking wrong.
    """
    root = Path(runs_dir)
    if not root.is_dir():
        return 0
    return sum(1 for p in root.iterdir()
               if p.is_dir() and p.name.startswith(RUN_PREFIX) and not _has_payload(p))


def tasklist_count(staging_dir: str | Path) -> int:
    root = Path(staging_dir)
    if not root.is_dir():
        return 0
    return sum(1 for p in root.iterdir()
               if p.is_file() and p.name.startswith(TASKLIST_PREFIX) and p.suffix == ".json")
