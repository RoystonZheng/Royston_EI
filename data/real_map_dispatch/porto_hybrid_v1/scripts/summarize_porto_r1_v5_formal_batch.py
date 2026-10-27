#!/usr/bin/env python3
"""Create a paired descriptive summary for the frozen R1 v5 EV v2 matrix."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/formal_v5_energy_v2"
MANIFEST = ROOT / "batch_manifest.json"
OUTPUT = ROOT / "matrix_summary.json"
METRICS = {
    "elapsed_completion_time_s": "elapsed_completion_time_s",
    "sum_actual_vehicle_operating_time_s": "sum_experimental_vehicle_operating_time_s",
    "sum_actual_waiting_time_s": "sum_actual_waiting_time_s",
    "sum_actual_distance_km": "sum_actual_distance_km",
    "sum_actual_energy_gross_kwh": "sum_actual_energy_gross_kwh",
    "sum_actual_energy_regenerated_kwh": "sum_actual_energy_regenerated_kwh",
    "sum_actual_energy_net_kwh": "sum_actual_energy_net_kwh",
}


def read_run(seed: int, objective: str) -> dict[str, Any]:
    summary_path = ROOT / f"seed_{seed}" / objective.lower() / "summary.json"
    if not summary_path.is_file():
        return {"status": "missing_summary"}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return summary


def describe(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "mean": None, "sd_population": None, "median": None}
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "sd_population": statistics.pstdev(values),
        "median": statistics.median(values),
    }


def main() -> int:
    if OUTPUT.exists():
        raise SystemExit(f"refusing_to_overwrite:{OUTPUT}")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    runs: dict[str, dict[str, dict[str, Any]]] = {"FASTEST": {}, "LOWEST_ENERGY": {}}
    run_statuses = {}
    for seed in range(10):
        for objective in runs:
            summary = read_run(seed, objective)
            key = f"seed_{seed:02d}_{objective.lower()}"
            audit_path = ROOT / f"seed_{seed}" / objective.lower() / "run_audit.json"
            audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
            runs[objective][str(seed)] = summary
            run_statuses[key] = {
                "summary_status": summary.get("status"),
                "run_audit_status": audit.get("status"),
                "orders_completed": summary.get("orders_completed"),
                "expected_orders": summary.get("expected_orders"),
                "non_estimable_energy_legs": audit.get("non_estimable_energy_legs"),
            }

    by_objective = {}
    for objective, seed_runs in runs.items():
        metric_stats = {}
        for label, source_key in METRICS.items():
            values = [
                float(run[source_key])
                for run in seed_runs.values()
                if run.get("status") == "completed"
                and run.get("orders_completed") == run.get("expected_orders") == 100
                and run.get(source_key) is not None
            ]
            metric_stats[label] = describe(values)
        by_objective[objective] = {
            "completed_seed_count": sum(1 for run in seed_runs.values() if run.get("status") == "completed" and run.get("orders_completed") == run.get("expected_orders") == 100),
            "metrics": metric_stats,
        }

    paired = {label: [] for label in METRICS}
    paired_seed_count = 0
    for seed in range(10):
        fast = runs["FASTEST"][str(seed)]
        low = runs["LOWEST_ENERGY"][str(seed)]
        valid = all(
            run.get("status") == "completed"
            and run.get("orders_completed") == run.get("expected_orders") == 100
            for run in (fast, low)
        )
        if not valid:
            continue
        fast_audit = ROOT / f"seed_{seed}" / "fastest" / "run_audit.json"
        low_audit = ROOT / f"seed_{seed}" / "lowest_energy" / "run_audit.json"
        fa = json.loads(fast_audit.read_text(encoding="utf-8")) if fast_audit.is_file() else {}
        la = json.loads(low_audit.read_text(encoding="utf-8")) if low_audit.is_file() else {}
        if fa.get("non_estimable_energy_legs") or la.get("non_estimable_energy_legs"):
            continue
        paired_seed_count += 1
        for label, source_key in METRICS.items():
            fv, lv = fast.get(source_key), low.get(source_key)
            if fv is not None and lv is not None:
                paired[label].append(float(lv) - float(fv))

    payload = {
        "schema_version": "porto_r1_v5_ev2_matrix_summary_v1",
        "status": manifest.get("status"),
        "protocol_sha256": manifest.get("protocol_sha256"),
        "runner_sha256": manifest.get("runner_sha256"),
        "expected_runs": 20,
        "expected_seeds": list(range(10)),
        "comparison": "paired descriptive differences are LOWEST_ENERGY minus FASTEST; no inferential test or causal claim is made",
        "electricity_cost_boundary": "net EV v2 kWh is reported as a simulated resource metric; currency cost is null because no frozen electricity tariff is specified",
        "by_objective": by_objective,
        "paired_difference_lowest_energy_minus_fastest": {label: describe(values) for label, values in paired.items()},
        "paired_complete_seed_count": paired_seed_count,
        "run_statuses": run_statuses,
        "interpretation_limit": "SUMO simulation on a real OSM-derived Porto road graph with synthetic background traffic; not observed fleet performance. Missing trace energy remains non-estimable and is excluded from complete paired energy summaries.",
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "completed": {key: value["completed_seed_count"] for key, value in by_objective.items()}, "paired_complete_seed_count": paired_seed_count, "output": str(OUTPUT)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
