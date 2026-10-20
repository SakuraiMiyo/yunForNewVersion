# -*- coding: utf-8 -*-
"""Generate a batch of synthetic records and diff their statistics against real exports.

This is the feedback loop for tuning the generator: it measures the *same* metrics on
both sides (via ``track_stats``), then reports per-metric deltas plus one aggregate
score, so a parameter change can be judged instead of guessed.

The generated batch is measured in memory, so a round costs no disk writes.

Usage:
    python tools/compare_generator.py --reference <dir> --count 36
    python tools/compare_generator.py --reference <dir> --count 36 --json round1.json
    python tools/compare_generator.py --reference <dir> --count 36 --keep out/round1
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LAB_ROOT = HERE.parent
sys.path.insert(0, str(LAB_ROOT / "src"))

from track_stats import collect_files, summarise, summarise_documents  # noqa: E402
from tasklist_builder import build_tasklist  # noqa: E402

# Metrics whose distribution must line up, with a weight reflecting how visible the
# mismatch would be to a reviewer looking at a single record.
COMPARE_FIELDS = {
    "mileage_km": 3.0,
    "duration_s": 3.0,
    "pace_min_per_km": 2.0,
    "point_count": 1.5,
    "sample_step_s": 3.0,
    "point_spacing_m": 2.0,
    "point_spacing_mean_m": 2.0,
    "point_spacing_p95_m": 1.5,
    "mileage_step_m": 1.5,
    "mean_speed_mps": 2.5,
    "obs_speed_med_mps": 2.0,
    "obs_speed_sd_mps": 2.5,
    "obs_speed_p95_mps": 2.0,
    "obs_speed_max_mps": 1.5,
    "point_pace_sd": 4.0,
    "offset_sigma_m": 2.0,
    "offset_p95_m": 2.0,
    "stride_m": 3.0,
    "cadence": 3.0,
    "fit_residual_m": 1.0,
}

# Real exports split cleanly into two populations by how far the run strayed from the
# track centreline: clean laps (offset sigma 0.87-3.24 m) and runs that wandered off
# (3.8-29.5 m, some with fitted "laps" of 500-1700 m). The default reference excludes
# the wanderers: matching them would mean making ordinary runs look broken too.
DEFAULT_REF_MAX_OFFSET_SIGMA = 3.5


def build_one(seed: int | None, candidates: int) -> tuple[str, dict, dict]:
    document, plan, quality = build_tasklist(seed=seed, candidates=candidates)
    name = f"gen_{plan.distance_m / 1000.0:.2f}km_{plan.duration_s / 60.0:.1f}min_{plan.seed}"
    summary = {
        "file": name,
        "distance_km": round(plan.distance_m / 1000.0, 4),
        "duration_min": round(plan.duration_s / 60.0, 3),
        "pace": round(plan.pace_min_per_km, 3),
        "stride_m": round(plan.stride_m, 4),
        "seed": plan.seed,
        "qualified": plan.qualified,
        "valid_candidates": quality.get("valid_candidate_count"),
    }
    return name, document, summary


def generate_batch(count: int, *, candidates: int = 8, seed: int | None = None,
                   keep: Path | None = None) -> tuple[list[tuple[str, dict]], list[dict]]:
    """Build ``count`` records. Returns (named documents, per-run summaries)."""
    documents: list[tuple[str, dict]] = []
    plans: list[dict[str, Any]] = []
    for index in range(count):
        run_seed = None if seed is None else seed + index
        name, document, summary = build_one(run_seed, candidates)
        documents.append((name, document))
        plans.append(summary)
        if keep is not None:
            (keep / f"{name}.json").write_text(json.dumps(document, ensure_ascii=False),
                                               encoding="utf-8")
    return documents, plans


def compare_reports(reference: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    rows = []
    weighted_error = 0.0
    weight_total = 0.0
    for field, weight in COMPARE_FIELDS.items():
        ref = reference["summary"].get(field)
        gen = generated["summary"].get(field)
        if not ref or not gen:
            rows.append({"field": field, "rel": None, "weight": weight})
            continue
        delta = gen["median"] - ref["median"]
        rel = delta / ref["median"] if ref["median"] else None
        rows.append({
            "field": field,
            "reference_median": ref["median"],
            "generated_median": gen["median"],
            "reference_range": [ref["min"], ref["max"]],
            "generated_range": [gen["min"], gen["max"]],
            "reference_sd": ref["stddev"],
            "generated_sd": gen["stddev"],
            "delta": round(delta, 4),
            "rel": round(rel, 4) if rel is not None else None,
            "weight": weight,
        })
        if rel is not None:
            weighted_error += weight * abs(rel)
            weight_total += weight

    score = weighted_error / weight_total if weight_total else None
    return {
        "reference_files": reference["files"],
        "generated_files": generated["files"],
        "score": round(score, 5) if score is not None else None,
        "metrics": rows,
        "reference_summary": reference["summary"],
        "generated_summary": generated["summary"],
    }


def build_reference(source: Path, max_offset_sigma: float | None) -> dict[str, Any]:
    """Measure the reference exports, optionally keeping only clean laps."""
    report = summarise(collect_files(source))
    if max_offset_sigma is None:
        return report
    kept = [r for r in report["records"]
            if r.get("offset_sigma_m") is not None
            and r["offset_sigma_m"] <= max_offset_sigma]
    dropped = [r["file"] for r in report["records"] if r not in kept]
    from track_stats import _report  # shared aggregation so both sides use one definition

    filtered = _report(kept)
    filtered["dropped"] = dropped
    return filtered


def print_comparison(result: dict[str, Any]) -> None:
    print(f"reference {result['reference_files']} records  vs  generated {result['generated_files']} records")
    if result.get("reference_dropped"):
        print(f"  ({len(result['reference_dropped'])} wandering reference records excluded)")
    print(f"alignment score (weighted mean |rel delta|): {result['score']}")
    print()
    print(f"  {'metric':<22} {'ref median':>11} {'gen median':>11} {'delta':>10} {'rel':>8}  | "
          f"{'ref range':>21} {'gen range':>21}")
    for row in result["metrics"]:
        if row.get("rel") is None:
            print(f"  {row['field']:<22} (missing on one side)")
            continue
        print(f"  {row['field']:<22} {row['reference_median']:>11.3f} {row['generated_median']:>11.3f} "
              f"{row['delta']:>10.3f} {row['rel']:>+8.2%}  | "
              f"{row['reference_range'][0]:>9.2f}~{row['reference_range'][1]:<10.2f} "
              f"{row['generated_range'][0]:>9.2f}~{row['generated_range'][1]:<10.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reference", required=True, help="directory of real exports")
    parser.add_argument("--count", type=int, default=36, help="how many synthetic records to generate")
    parser.add_argument("--candidates", type=int, default=8)
    parser.add_argument("--seed", type=int, default=None, help="master seed for the batch")
    parser.add_argument("--keep", default=None, help="also write generated records here")
    parser.add_argument("--json", default=None, help="write the comparison result as JSON")
    parser.add_argument("--quiet", action="store_true", help="only print the score line")
    parser.add_argument("--ref-max-offset-sigma", type=float, default=DEFAULT_REF_MAX_OFFSET_SIGMA,
                        help="drop reference records whose offset sigma exceeds this "
                             "(0 disables filtering, keeping the wandering runs)")
    args = parser.parse_args()

    keep = None
    if args.keep:
        keep = Path(args.keep)
        if keep.exists():
            shutil.rmtree(keep)
        keep.mkdir(parents=True, exist_ok=True)

    documents, plans = generate_batch(args.count, candidates=args.candidates,
                                      seed=args.seed, keep=keep)

    qualified = sum(1 for p in plans if p["qualified"])
    durations = sorted(p["duration_min"] for p in plans)
    distances = sorted(p["distance_km"] for p in plans)
    if not args.quiet:
        print(f"generated {len(plans)} records ({qualified} within >3km & <=30min)")
        print(f"  duration_min: {durations[0]:.2f} .. {durations[-1]:.2f} "
              f"(median {statistics.median(durations):.2f})")
        print(f"  distance_km : {distances[0]:.2f} .. {distances[-1]:.2f}")
        print()

    reference = build_reference(
        Path(args.reference),
        None if args.ref_max_offset_sigma <= 0 else args.ref_max_offset_sigma,
    )
    generated = summarise_documents(documents)
    result = compare_reports(reference, generated)
    result["plans"] = plans
    result["reference_dropped"] = reference.get("dropped", [])

    if args.quiet:
        print(f"score={result['score']} qualified={qualified}/{len(plans)} "
              f"dur={durations[0]:.1f}~{durations[-1]:.1f}min")
    else:
        print_comparison(result)
        worst = sorted((r for r in result["metrics"] if r.get("rel") is not None),
                       key=lambda r: -abs(r["rel"]))[:4]
        print()
        print("worst mismatches:")
        for row in worst:
            print(f"  {row['field']:<22} {row['rel']:>+8.2%}   "
                  f"ref {row['reference_median']:.3f} -> gen {row['generated_median']:.3f}")

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
