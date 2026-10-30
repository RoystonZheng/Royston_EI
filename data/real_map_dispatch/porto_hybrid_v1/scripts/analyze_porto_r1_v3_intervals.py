#!/usr/bin/env python3
"""Additive uncertainty analysis for the frozen Porto R1 v3 results.

This script reads the completed v3 aggregate and paired seed table, and writes
to a new output directory. It never modifies the formal run artifacts.
"""

import csv
import json
import math
from pathlib import Path
from statistics import mean, stdev


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "derived/experiments/r1_dynamic_sumo_v1/formal_v3_fixed_delay"
OUTPUT = ROOT / "derived/experiments/r1_dynamic_sumo_v1/analysis_ci_v1"
T_975_DF8 = 2.306004135204166


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054):
    p = successes / trials
    z2 = z * z
    denom = 1 + z2 / trials
    center = (p + z2 / (2 * trials)) / denom
    half = z * math.sqrt((p * (1 - p) / trials) + z2 / (4 * trials * trials)) / denom
    return center - half, center + half


def main():
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite existing analysis directory: {OUTPUT}")
    summary = json.loads((INPUT / "aggregate_summary.json").read_text(encoding="utf-8"))
    with (INPUT / "paired_success_metrics.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    metrics = {
        "elapsed_completion_time_s": "Fleet makespan (s)",
        "sum_actual_energy_net_kwh": "Net simulated energy (kWh)",
        "sum_actual_waiting_time_s": "Actual waiting time sum (s)",
        "sum_actual_distance_km": "Actual distance (km)",
    }
    results = {
        "analysis_id": "porto_r1_v3_paired_intervals_v1",
        "source": "formal_v3_fixed_delay; source files are not modified",
        "interval_method": "two-sided 95% paired Student-t interval; df=8; t*=2.306004135204166",
        "paired_seed_set": [0, 1, 2, 3, 5, 6, 7, 8, 9],
        "interpretation": "Intervals describe variability across the nine paired completed simulation seeds only; they are not evidence about real Porto operations or a population of real trips.",
        "completion_rate_wilson_95": {},
        "paired_metrics": [],
    }
    for method, successes in (("FASTEST", 9), ("LOWEST_ENERGY", 10)):
        low, high = wilson_interval(successes, 10)
        results["completion_rate_wilson_95"][method] = {
            "successes": successes,
            "trials": 10,
            "estimate": successes / 10,
            "lower": low,
            "upper": high,
        }

    csv_rows = []
    for metric, label in metrics.items():
        matching = [r for r in rows if r["metric"] == metric]
        deltas = [float(r["LOWEST_minus_FASTEST"]) for r in matching]
        if len(deltas) != 9:
            raise SystemExit(f"expected 9 paired observations for {metric}, got {len(deltas)}")
        avg = mean(deltas)
        half = T_975_DF8 * stdev(deltas) / math.sqrt(len(deltas))
        record = {
            "metric": metric,
            "label": label,
            "n_paired_seeds": len(deltas),
            "mean_delta_LOWEST_ENERGY_minus_FASTEST": avg,
            "sd_paired_delta": stdev(deltas),
            "ci95_lower": avg - half,
            "ci95_upper": avg + half,
            "ci_method": "paired t interval, df=8",
        }
        results["paired_metrics"].append(record)
        csv_rows.append(record)

    OUTPUT.mkdir(parents=True)
    (OUTPUT / "confidence_intervals.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (OUTPUT / "paired_metric_ci95.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)

    lines = [
        "# Porto R1 v3 uncertainty analysis",
        "",
        "This additive analysis reads the completed v3 aggregate and paired seed table. It does not alter formal runs or replace missing runs.",
        "",
        "Completion-rate intervals use Wilson 95% intervals over 10 pre-registered seeds. Paired metric intervals use two-sided Student-t 95% intervals over the 9 seeds where both methods completed (df=8). Delta is LOWEST_ENERGY minus FASTEST.",
        "",
        "These intervals summarize variation across this frozen set of simulated seeds. They do not establish a real-world effect, a calibrated operational distribution, or an AI map-readability effect.",
        "",
        "## Paired metric differences",
        "",
        "| Metric | n | Mean paired difference | 95% CI |",
        "|---|---:|---:|---:|",
    ]
    for r in csv_rows:
        lines.append(
            f"| {r['label']} | {r['n_paired_seeds']} | {r['mean_delta_LOWEST_ENERGY_minus_FASTEST']:.3f} | [{r['ci95_lower']:.3f}, {r['ci95_upper']:.3f}] |"
        )
    lines += ["", "## Completion rate Wilson intervals", "", "| Method | Completed | Estimate | 95% Wilson CI |", "|---|---:|---:|---:|"]
    for method, r in results["completion_rate_wilson_95"].items():
        lines.append(f"| {method} | {r['successes']}/{r['trials']} | {r['estimate']:.0%} | [{r['lower']:.1%}, {r['upper']:.1%}] |")
    (OUTPUT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
