from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from .calibration import calibrate, cluster_point_files, load_calibration, save_calibration
from .dataset import analyze_task_exports, save_analysis
from .generator import generate_best_candidate, load_settings, write_outputs
from .geo import haversine_m


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline athletic-track GPS calibration and simulation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    calibrate_parser = subparsers.add_parser("calibrate", help="fit a stadium model to exported GPS points")
    calibrate_parser.add_argument(
        "--input", required=True, action="append",
        help="GPS export file or a task directory; repeat to combine inputs",
    )
    calibrate_parser.add_argument("--output", required=True, help="destination calibration JSON")
    calibrate_parser.add_argument("--coordinate-system", default="inherited-from-input")
    calibrate_parser.add_argument("--cluster-radius-m", type=float, default=250.0)
    calibrate_parser.add_argument(
        "--cluster-index", type=int, default=0,
        help="location cluster to fit after sorting by point count (default: largest)",
    )

    generate_parser = subparsers.add_parser("generate", help="generate a new local synthetic track")
    generate_parser.add_argument("--track", required=True, help="calibration JSON")
    generate_parser.add_argument("--settings", help="simulation settings JSON")
    generate_parser.add_argument("--distance-km", required=True, type=float)
    generate_parser.add_argument("--pace-min-per-km", required=True, type=float)
    generate_parser.add_argument("--seed", type=int, help="optional deterministic seed")
    generate_parser.add_argument("--output", default="runs", help="output directory root")
    generate_parser.add_argument(
        "--candidates", type=int, default=8,
        help="generate and assess this many candidates, then keep the best valid one",
    )

    inspect_parser = subparsers.add_parser("inspect", help="summarize a generated CSV")
    inspect_parser.add_argument("--input", required=True, help="generated track.csv")

    analyze_parser = subparsers.add_parser("analyze", help="analyze local server-exported task datasets")
    analyze_parser.add_argument("--input", required=True, action="append", help="task file or directory")
    analyze_parser.add_argument("--rule-min-km", type=float, default=3.0)
    analyze_parser.add_argument("--rule-max-minutes", type=float, default=30.0)
    analyze_parser.add_argument("--output", help="optional JSON report path")

    serve_parser = subparsers.add_parser("serve", help="run the local generator and map viewer")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8437)
    serve_parser.add_argument("--open-browser", action="store_true")
    serve_parser.add_argument("--open-console", action="store_true",
                              help="open the running console (/ui) instead of the preview page")
    serve_parser.add_argument("--open-admin", action="store_true",
                              help="open the guided admin page (/admin) instead of the preview page")
    return parser


def _run_calibrate(args: argparse.Namespace) -> int:
    groups = cluster_point_files(args.input, cluster_radius_m=args.cluster_radius_m)
    if not 0 <= args.cluster_index < len(groups):
        raise ValueError(f"cluster-index must be between 0 and {len(groups) - 1}")
    files, points = groups[args.cluster_index]
    result = calibrate(
        points,
        coordinate_system=args.coordinate_system,
        source_files=[str(path.resolve()) for path in files],
    )
    save_calibration(result, args.output)
    print(f"detected {len(groups)} location cluster(s):")
    for index, (cluster_files, cluster_points) in enumerate(groups):
        selected = " [selected]" if index == args.cluster_index else ""
        print(f"  cluster {index}: {len(cluster_files)} files / {len(cluster_points)} points{selected}")
        for path in cluster_files:
            print(f"    - {path.name}")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    print(f"saved calibration: {Path(args.output).resolve()}")
    return 0


def _run_generate(args: argparse.Namespace) -> int:
    calibration = load_calibration(args.track)
    settings = load_settings(args.settings)
    distance_m = args.distance_km * 1000.0
    points, seed, quality = generate_best_candidate(
        calibration=calibration,
        settings=settings,
        distance_m=distance_m,
        pace_min_per_km=args.pace_min_per_km,
        candidates=args.candidates,
        seed=args.seed,
    )
    output = write_outputs(points, args.output, seed, calibration, settings, quality=quality)
    elapsed = (datetime.fromisoformat(points[-1].timestamp.replace("Z", "+00:00")) -
               datetime.fromisoformat(points[0].timestamp.replace("Z", "+00:00"))).total_seconds()
    print(f"generated {len(points)} synthetic points with seed {seed}")
    print(
        "candidate validation: "
        f"{quality['valid_candidate_count']}/{quality['candidate_count']} valid; "
        f"selected score={quality['score']}"
    )
    print(f"modeled distance: {points[-1].modeled_distance_m:.1f} m; sampled duration: {elapsed:.1f} s")
    print(f"output: {output.resolve()}")
    return 0


def _run_inspect(args: argparse.Namespace) -> int:
    rows = []
    with Path(args.input).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) < 2:
        raise ValueError("at least two points are required")
    if any(str(row.get("synthetic", "")).lower() not in {"true", "1"} for row in rows):
        raise ValueError("input is not consistently marked as synthetic")
    observed = 0.0
    max_step = 0.0
    for first, second in zip(rows, rows[1:]):
        step = haversine_m(
            (float(first["longitude"]), float(first["latitude"])),
            (float(second["longitude"]), float(second["latitude"])),
        )
        observed += step
        max_step = max(max_step, step)
    started = datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(rows[-1]["timestamp"].replace("Z", "+00:00"))
    summary = {
        "synthetic": True,
        "points": len(rows),
        "duration_seconds": (ended - started).total_seconds(),
        "modeled_distance_m": float(rows[-1]["modeled_distance_m"]),
        "observed_polyline_distance_m": round(observed, 2),
        "maximum_observed_step_m": round(max_step, 2),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _run_analyze(args: argparse.Namespace) -> int:
    report = analyze_task_exports(args.input, args.rule_min_km, args.rule_max_minutes)
    if args.output:
        save_analysis(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        print(f"saved analysis: {Path(args.output).resolve()}")
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    if not 1 <= args.port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    from .web import run_server

    run_server(host=args.host, port=args.port, open_browser=args.open_browser,
               console=args.open_console, admin=args.open_admin)
    return 0


def main() -> None:
    args = _build_parser().parse_args()
    handlers = {
        "calibrate": _run_calibrate,
        "generate": _run_generate,
        "inspect": _run_inspect,
        "analyze": _run_analyze,
        "serve": _run_serve,
    }
    raise SystemExit(handlers[args.command](args))
