# -*- coding: utf-8 -*-
"""仿真跑管线：调用 gps_track_lab 生成模拟轨迹，转换成云运动打表（tasklist）格式。

输出目录 tasks_sim/（与真实抓包的 tasks_else/ 等目录分开存放）。

业务口径（本地健康跑合格标准）
    合格 = 单次里程 > 3.0 km 且 用时 <= 30 min
生成窗口刻意比合格窗口收窄：默认距离 3.01~3.20 km（集中在 3.0x）、用时 16~25 min，
因此生成结果天然远离 30 分钟边界，不会踩线。

随机性来自轨迹生成器（速度过程 + 横向偏移 + GPS 漂移），以及每轮抽取的距离/配速/
步频；步幅由「平均速度 × 60 / 步频」推导，所以里程、用时、步数、步频四个字段彼此
自洽（该关系在 19 份真实记录上逐条验证过，误差 ±0.008 m）。

用法:
    python sim_pipeline.py                       # 生成一条，写入 tasks_sim/
    python sim_pipeline.py --show                # 生成并打印摘要
    python sim_pipeline.py --distance 3.1 --duration 18
    python sim_pipeline.py --count 5             # 批量生成 5 条

程序化调用（机器人插件等）:
    from sim_pipeline import generate_tasklist
    info = generate_tasklist()   # -> {file, name, km, pace, cadence, duration, points, seed}
"""
import argparse
import configparser
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
LAB_ROOT = os.path.join(ROOT, "gps_track_lab")
# tools/ 是打表口径（tasklist_builder）所在目录；重构后它在 yunrun/tools 下，
# 不放进来时 `python sim_pipeline.py` 会直接 ModuleNotFoundError。
for path in (os.path.join(ROOT, "tools"),
             os.path.join(LAB_ROOT, "src"),
             os.path.join(LAB_ROOT, "tools")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tasklist_builder import (  # noqa: E402
    GEN_DISTANCE_KM_RANGE,
    GEN_DURATION_MIN_RANGE,
    MAX_QUALIFIED_MIN,
    MIN_QUALIFIED_KM,
    build_tasklist,
)
from gps_track_lab.retention import KEEP_TASKLISTS, prune_tasklists  # noqa: E402

OUT_DIR = os.path.join(ROOT, "tasks_sim")
KEEP_FILES = KEEP_TASKLISTS       # one retention policy, shared with the bridge


def _read_config():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(os.path.join(ROOT, "config.ini"), encoding="utf-8-sig")
    return parser


def _read_run_conf():
    conf = _read_config()
    run = {"school_id": 0}
    if conf.has_section("Run"):
        for key in ("min_distance", "min_consume", "max_consume", "strides"):
            if conf.has_option("Run", key):
                try:
                    run[key] = float(conf.get("Run", key))
                except ValueError:
                    pass
    if conf.has_section("Yun") and conf.has_option("Yun", "school_id"):
        try:
            run["school_id"] = int(conf.get("Yun", "school_id"))
        except ValueError:
            pass
    return run


def _prune(directory, keep):
    """Delegate to the shared retention policy (newest N by mtime, not by name)."""
    return prune_tasklists(directory, keep)


def generate_tasklist(distance_km=None, pace_min_per_km=None, candidates=8,
                      seed=None, dislikes=3, out_dir=None, keep=KEEP_FILES,
                      duration_min=None, write=True):
    """生成一条仿真轨迹并转换为 tasklist，返回摘要 dict。

    distance_km / pace_min_per_km / duration_min 任一为 None 时，按真实观测分布随机抽取。
    """
    conf = _read_run_conf()
    calibration = os.path.join(LAB_ROOT, "config", "track.json")
    if not os.path.isfile(calibration):
        raise FileNotFoundError(
            "找不到操场标定文件: %s（先运行 gps_track_lab calibrate）" % calibration)

    document, plan, quality = build_tasklist(
        seed=seed,
        distance_km=distance_km,
        pace_min_per_km=pace_min_per_km,
        duration_min=duration_min,
        dislikes=dislikes,
        school_id=conf["school_id"],
        candidates=candidates,
    )

    data = document["data"]
    # ``_points`` carries the raw dataclass trajectory for callers that need the geometry
    # itself (the local web server). It is not JSON-serialisable, so it must not end up in
    # the printed summary.
    quality.pop("_points", None)
    info = {
        "file": None,
        "name": None,
        "km": data["recordMileage"],
        "pace": data["recodePace"],
        "cadence": data["recodeCadence"],
        "duration": data["duration"],
        "points": len(data["pointsList"]),
        "seed": plan.seed,
        "stride_m": round(plan.stride_m, 4),
        "qualified": bool(plan.qualified),
        "quality": quality,
    }

    if write:
        out_dir = out_dir or OUT_DIR
        os.makedirs(out_dir, exist_ok=True)
        # Prune before writing, so the directory never holds more than `keep` tables.
        _prune(out_dir, max(0, keep - 1))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = "tasklist_sim_%s.json" % stamp
        # 同一秒内批量生成时避免互相覆盖
        suffix = 1
        while os.path.exists(os.path.join(out_dir, name)):
            name = "tasklist_sim_%s_%d.json" % (stamp, suffix)
            suffix += 1
        file_path = os.path.join(out_dir, name)
        with open(file_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False)
        info["file"] = file_path
        info["name"] = name

    return info


def main():
    parser = argparse.ArgumentParser(description="生成仿真跑轨迹并转换为打表格式")
    parser.add_argument("--distance", type=float, help="距离 km（默认按观测分布抽取，集中在 3.0x）")
    parser.add_argument("--pace", type=float, help="目标配速 min/km（默认按观测分布抽取）")
    parser.add_argument("--duration", type=float, help="目标用时 min（默认 16~25 之间抽取）")
    parser.add_argument("--count", type=int, default=1, help="生成条数")
    parser.add_argument("--candidates", type=int, default=8, help="候选轨迹数，取物理校验最优")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（默认不固定）")
    parser.add_argument("--show", action="store_true", help="打印质量报告")
    args = parser.parse_args()

    print("业务口径: 合格 = 里程 > %.1f km 且 用时 <= %.0f min"
          % (MIN_QUALIFIED_KM, MAX_QUALIFIED_MIN))
    print("生成窗口: 距离 %.2f~%.2f km / 用时 %.0f~%.0f min\n"
          % (GEN_DISTANCE_KM_RANGE[0], GEN_DISTANCE_KM_RANGE[1],
             GEN_DURATION_MIN_RANGE[0], GEN_DURATION_MIN_RANGE[1]))

    failures = 0
    for index in range(args.count):
        seed = None if args.seed is None else args.seed + index
        try:
            info = generate_tasklist(distance_km=args.distance, pace_min_per_km=args.pace,
                                     duration_min=args.duration, candidates=args.candidates,
                                     seed=seed)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print("[!] 生成失败:", exc)
            continue
        mark = "合格" if info["qualified"] else "不合格"
        print("[+] %s  %.2fkm / 配速%s / 步频%s / 步幅%.3fm / %d点 / %d分%d秒 / seed=%s"
              % (info["name"], info["km"], info["pace"], info["cadence"], info["stride_m"],
                 info["points"], info["duration"] // 60, info["duration"] % 60, info["seed"]))
        print("    %s  %s" % (mark, info["file"]))
        if args.show:
            print(json.dumps(info["quality"], ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
