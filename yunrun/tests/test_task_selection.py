# -*- coding: utf-8 -*-
"""Running-task selection: locked to the stadium the generator is calibrated for.

The defect this replaces: ``_create_task`` used ``cralist[0]``, so trajectories generated for one
stadium were submitted against whatever task the server listed first. On this account the home
info lists three tasks and the calibration matches the middle task, while both real submissions went to the first --
1.47 km away. The app draws a record on its task's own field, so the two have to agree.

By instruction there is no field picker: the calibrated stadium is always the target.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "runner"))
sys.path.insert(0, str(PROJECT))

import pytest  # noqa: E402

import main as M  # noqa: E402


def fence(lon: float, lat: float) -> str:
    """A small rectangle around a stadium, sized like the real ones (~70 x 120 m)."""
    dlon, dlat = 0.00045, 0.00055
    corners = [(lon - dlon, lat - dlat), (lon + dlon, lat - dlat),
               (lon + dlon, lat + dlat), (lon - dlon, lat + dlat)]
    return "|".join(f"{x},{y}" for x, y in corners)


TASKS = [
    {"id": 9001, "raRunArea": "school:run:task:100:6176:R0:2:1:9001",
     "points": fence(117.184676, 31.770441)},
    {"id": 9002, "raRunArea": "school:run:task:100:6176:R0:2:1:9002",
     "points": fence(117.200235, 31.770016)},
    {"id": 9003, "raRunArea": "school:run:task:100:6176:R0:2:1:9003",
     "points": fence(117.194472, 31.777474)},
]
# A stand-in calibration centre. The numbers are the fixture's own -- they are shifted off any real
# campus on purpose, and only the relative geometry to the fences above matters.
CALIBRATION = {"center_lon": 117.20000148506133, "center_lat": 31.77000104008992}


def test_locks_onto_the_calibrated_stadium() -> None:
    task, why = M.select_task(TASKS, CALIBRATION)
    assert task["id"] == 9002, why
    assert "锁定" in why or "围栏内" in why


def test_never_takes_the_first_task_by_accident() -> None:
    """The old behaviour was cralist[0], which on this account is the wrong stadium."""
    task, _why = M.select_task(TASKS, CALIBRATION)
    assert task["id"] != TASKS[0]["id"]
    # Order must not matter either: the calibrated field wins wherever it sits in the list.
    reordered = [TASKS[1], TASKS[0], TASKS[2]]
    assert M.select_task(reordered, CALIBRATION)[0]["id"] == 9002


def test_no_matching_fence_is_an_error_not_a_fallback() -> None:
    """Falling back to another field is the bug; it has to stop instead."""
    elsewhere = [dict(task, points=fence(117.05 + index * 0.02, 32.60))
                 for index, task in enumerate(TASKS)]
    with pytest.raises(Exception) as excinfo:
        M.select_task(elsewhere, CALIBRATION)
    message = str(excinfo.value)
    assert "没有围栏包含标定中心" in message
    for task in elsewhere:
        assert str(task["id"]) in message


def test_single_task_is_reported_honestly() -> None:
    task, why = M.select_task([TASKS[1]], CALIBRATION)
    assert task["id"] == 9002
    assert "围栏内" in why


def test_single_task_without_a_fence_stops() -> None:
    """No fence data means the stadium cannot be verified, so it must not proceed quietly."""
    bare = [{"id": 348, "raName": "甲操场大二男生跑"}]
    with pytest.raises(Exception) as excinfo:
        M.select_task(bare, CALIBRATION)
    assert "未下发围栏坐标" in str(excinfo.value)


def test_missing_calibration_stops() -> None:
    """Without a calibration there is no way to know which stadium the trajectory belongs to."""
    with pytest.raises(Exception) as excinfo:
        M.select_task(TASKS, None)
    assert "没有可用的场地标定" in str(excinfo.value)
    with pytest.raises(Exception):
        M.select_task(TASKS, {})


def test_empty_list_is_an_error() -> None:
    with pytest.raises(Exception):
        M.select_task([], CALIBRATION)


def test_fence_parsing_ignores_junk() -> None:
    parsed = M._fence_points({"points": "117.0,32.0|bad|117.1,32.1|"})
    assert parsed == [(117.0, 32.0), (117.1, 32.1)]


def test_project_calibration_loads_and_matches_a_real_task() -> None:
    """Against the live task list (names/ids only, no submission)."""
    centre = M._load_track_center()
    if not centre:
        # config/track.json 是不入库的本机数据：干净 clone 上还没标定过，这条只能跳过。
        pytest.skip("本机还没有标定（gps_track_lab/config/track.json 不存在）")
    assert centre.get("center_lon") and centre.get("center_lat")
