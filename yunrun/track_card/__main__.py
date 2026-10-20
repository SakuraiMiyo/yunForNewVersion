# -*- coding: utf-8 -*-
"""CLI: render a trajectory card from a tasklist export or a simulated run.

    python -m track_card --tasklist history/server_gt3km/tasklist_xxx.json --output renders/a.png
    python -m track_card --run <run_id> --output renders/b.png
    python -m track_card --demo --output renders/c.png
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python track_card/__main__.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from track_card import (  # noqa: E402
    Basemap,
    CardData,
    card_from_tasklist,
    render_card,
)
from track_card.basemap import DEFAULT_PROVIDER, PROVIDERS  # noqa: E402


def data_from_run(run_id: str, runs_path: Path) -> CardData:
    """Build a card from a local simulated run directory."""
    directory = runs_path / run_id
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    points: list[tuple[float, float]] = []
    distances: list[float] = []
    import csv

    with (directory / "track.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            points.append((float(row["longitude"]), float(row["latitude"])))
            distances.append(float(row["modeled_distance_m"]))
    if not points:
        raise ValueError(f"run {run_id} has no points")
    plan = manifest.get("quality", {}).get("run_plan", {}) or {}
    calibration = manifest.get("calibration", {})
    distance_m = distances[-1]
    duration_s = float(manifest.get("duration_seconds") or 0.0)
    distance_km = distance_m / 1000.0
    lap_length = calibration.get("lap_length_m")
    cadence = plan.get("cadence")
    steps = plan.get("step_total") or (int(round(cadence * duration_s / 60.0)) if cadence else None)
    return CardData(
        points=points,
        subtitle=run_id,
        distance_km=distance_km,
        duration_s=duration_s,
        pace_min_per_km=(duration_s / 60.0) / distance_km if distance_km else None,
        cadence=cadence,
        stride_m=plan.get("stride_m") or (distance_m / steps if steps else None),
        step_total=steps,
        laps=(distance_m / float(lap_length)) if lap_length else None,
        lap_length_m=float(lap_length) if lap_length else None,
        seed=manifest.get("seed"),
        qualified=plan.get("qualified"),
        labels=["本地仿真 · 不用于上传"],
    )


def demo_data(steps: int = 500) -> CardData:
    """A synthetic oval, so the renderer can be exercised with no data files at all."""
    import math

    points = []
    for index in range(steps + 1):
        angle = 2 * math.pi * index / steps
        points.append((117.2000015 + 0.0009 * math.cos(angle),
                       31.7700010 + 0.0009 * math.sin(angle)))
    return CardData(
        points=points, subtitle="demo-oval", distance_km=3.02, duration_s=1080.0,
        pace_min_per_km=5.96, cadence=147, stride_m=1.14, step_total=2646,
        laps=3.02 * 1000 / 400.0, lap_length_m=400.0, seed=42, qualified=True,
        labels=["演示数据"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="轨迹卡片渲染器")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tasklist", help="云运动 tasklist 导出文件")
    source.add_argument("--run", help="本地仿真 run_id（gps_track_lab/runs 下的目录名）")
    source.add_argument("--demo", action="store_true", help="用内置椭圆轨迹渲染一张示例卡")
    parser.add_argument("--output", required=True, help="输出 PNG 路径")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, choices=sorted(PROVIDERS),
                        help=f"底图源（默认 {DEFAULT_PROVIDER}）")
    parser.add_argument("--runs-dir", default=None, help="runs 目录（配合 --run）")
    parser.add_argument("--no-html", action="store_true", help="不保留中间 HTML")
    parser.add_argument("--json", action="store_true", help="额外打印渲染元数据")
    args = parser.parse_args()

    if args.tasklist:
        data = card_from_tasklist(args.tasklist)
    elif args.run:
        runs_path = Path(args.runs_dir) if args.runs_dir else (
            Path(__file__).resolve().parents[1] / "gps_track_lab" / "runs")
        data = data_from_run(args.run, runs_path)
    else:
        data = demo_data()

    basemap = Basemap(provider=args.provider)
    meta = render_card(data, Path(args.output), basemap=basemap, keep_html=not args.no_html)
    print(f"[+] {meta['png']}  {meta['png_bytes']} bytes  "
          f"{meta['render_seconds']}s  zoom={meta['zoom']}  "
          f"tiles={meta['tiles']}(fetched {meta['tiles_fetched']}, cached {meta['tiles_cached']}, "
          f"failed {meta['tiles_failed']})")
    if args.json:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
