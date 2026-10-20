# -*- coding: utf-8 -*-
"""Stadium lock and checkpoint check: the guards against filing a run under another stadium.

坐标说明：下面的打卡点**是虚构的**。原值来自我本机真实的标定结果，已整体平移
（经度 +1.170734 / 纬度 -1.779484）。相对几何基本不变——严格说经度方向的「度→米」缩放随纬度
差约 2%（cos 31.77°≈0.850 vs cos 33.55°≈0.833），下面的距离断言在容差内照旧成立，
但绝对位置不再指向任何真实操场。要在自己机器上拿真实历史做回算，把 ``CENTER``
与两个打卡点列表换回服务器下发的真值即可。

Context for the numbers used here:

* the calibration centre is ``CENTER``, which sits inside the matching task's checkpoints
  (1 m / 4 m away), while the other field is ~850 m away;
* on the historical 甲操场 exports each of the six checkpoints lies 0.4-4.5 m from the real path,
  so "the trajectory passes the checkpoints" is the criterion -- not "the trajectory is inside a
  box", which only 7% of those real points satisfy because a run loops around the field;
* task ids change between semesters (the same field got a different id each term), which is why
  selection is geometric rather than id- or position-based.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "gps_track_lab" / "src"))

import pytest  # noqa: E402

from mcp_bridge import CHECKPOINT_TOLERANCE_M, YunLocalBridge  # noqa: E402

# 虚构坐标（见文件头的脱敏说明）：甲操场田径场 checkpoints as the server sends them.
CENTER = (117.200001, 31.770001)
XINYI = [(117.200346, 31.770588), (117.200395, 31.770031), (117.200387, 31.769492),
         (117.199633, 31.769525), (117.199649, 31.770011), (117.199637, 31.770503)]
# 乙操场田径场, for the negative case.
ZHICHENG = [(117.194455, 31.777523), (117.195282, 31.776462)]


def worst_gap(points, checkpoints) -> float:
    """Largest distance from a checkpoint to the nearest trajectory point, in metres."""
    worst = 0.0
    for lon, lat in checkpoints:
        nearest = min(math.hypot((x - lon) * 111320.0 * math.cos(math.radians(lat)),
                                 (y - lat) * 111320.0) for x, y in points)
        worst = max(worst, nearest)
    return worst


def test_tolerance_admits_real_runs_and_rejects_the_next_field() -> None:
    # The historical gap is a few metres, so the tolerance must comfortably exceed that...
    assert CHECKPOINT_TOLERANCE_M >= 10.0
    assert CHECKPOINT_TOLERANCE_M <= 60.0
    # ... while the nearest other stadium is ~850 m away, so it must stay far below that.
    assert CHECKPOINT_TOLERANCE_M < 900.0


def test_historical_xinyi_records_pass_the_check() -> None:
    """Recompute the gap from the exported coordinates, end to end.

    发布版说明：个人历史数据（history/）不随发布携带，没有导出时本测试跳过而不是失败。
    注意上面那些打卡点是**脱敏后的虚构坐标**，所以拿真实导出跑这条时要先把 ``CENTER``
    与 ``XINYI`` 换回服务器下发的真值，否则经纬度对不上、每份都会被过滤掉、最后一路 skip。
    """
    source = PROJECT / "history" / "local" / "tasks_else"
    if not source.is_dir() or not list(source.glob("*.json")):
        pytest.skip("无个人历史导出（发布版默认状态）：跳过真实轨迹回算")
    checked = 0
    for path in sorted(source.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))["data"]
        except Exception:  # noqa: BLE001 - a malformed export is not this test's business
            continue
        points = [(float(p["point"].split(",")[0]), float(p["point"].split(",")[1]))
                  for p in (data.get("pointsList") or [])]
        if len(points) < 10 or abs(points[0][0] - CENTER[0]) > 0.002:
            continue                     # only the 甲操场 exports（用真实数据时把 CENTER 换回真值）
        gap = worst_gap(points, XINYI)
        assert gap <= CHECKPOINT_TOLERANCE_M, f"{path.name}: 最远打卡点距离 {gap:.0f} m"
        checked += 1
    if checked < 3:
        pytest.skip(f"该数据集里甲操场导出不足 3 份（checked {checked}）：跳过统计断言")


def test_another_fields_checkpoints_against_the_xinyi_path_fail() -> None:
    """The negative case: a 甲操场 trajectory cannot satisfy 乙操场's checkpoints."""
    xinyi_path = [(117.199644, 31.769371), (117.200434, 31.770016), (117.199434, 31.770716)]
    assert worst_gap(xinyi_path, ZHICHENG) > CHECKPOINT_TOLERANCE_M


def test_bridge_reports_the_locked_stadium() -> None:
    """Against the live task list: the lock must resolve to a named, calibrated stadium."""
    bridge = YunLocalBridge(PROJECT, allow_submit=True)
    status = bridge.stadium_status()
    if not status.get("ok"):
        pytest.skip(f"school server unavailable: {status.get('error')}")
    assert status["task_id"], status
    assert status["stadium_name"], status
    assert status["fence_points"] >= 3, status
    assert status["task_count"] >= 1, status
    assert status["calibration"]["center_lon"], status
    assert "围栏内" in status["reason"] or "最近" in status["reason"], status["reason"]
