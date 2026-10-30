#!/usr/bin/env python3
"""Aggregate the frozen Porto R1 v3 fixed-delay matrix."""

from __future__ import annotations

import csv
import json
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parents[1]
RUNS = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/formal_v3_fixed_delay"
METRICS = (
    "elapsed_completion_time_s",
    "sum_experimental_vehicle_operating_time_s",
    "sum_actual_waiting_time_s",
    "sum_actual_distance_km",
    "sum_actual_energy_gross_kwh",
    "sum_actual_energy_regenerated_kwh",
    "sum_actual_energy_net_kwh",
    "sum_route_planning_wall_time_s",
    "mean_planner_wall_time_ms",
    "synthetic_background_vehicles_arrived_by_finish",
    "edge_pass_events",
)


def read_statuses() -> dict[tuple[int, str], str]:
    statuses: dict[tuple[int, str], str] = {}
    for line in (RUNS / "batch_status.tsv").read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            seed, objective, status = parts
            statuses[(int(seed), objective)] = status
    return statuses


def read_failed_tripinfo(seed: int, objective: str) -> dict[str, Any]:
    path = RUNS / f"seed_{seed}" / objective / "tripinfo.xml"
    if not path.exists():
        return {"tripinfo_present": False}
    root = ET.parse(path).getroot()
    rows = root.findall("tripinfo")
    experimental = [row for row in rows if row.get("id", "").startswith("exp_")]
    arrived = [row for row in experimental if row.get("arrival") not in (None, "", "-1.00")]
    passenger_arrived = [row for row in arrived if row.get("id", "").endswith("_passenger")]
    unfinished = [row for row in rows if row.get("arrival") == "-1.00" or row.get("vaporized")]
    unfinished_exp = [row for row in unfinished if row.get("id", "").startswith("exp_")]
    return {
        "tripinfo_present": True,
        "tripinfo_rows": len(rows),
        "experimental_tripinfo_rows": len(experimental),
        "experimental_trip_arrivals": len(arrived),
        "passenger_trip_arrivals": len(passenger_arrived),
        "unfinished_trip_rows": len(unfinished),
        "unfinished_experimental_trip_rows": len(unfinished_exp),
        "unfinished_trip_ids": [row.get("id", "") for row in unfinished],
        "unfinished_experimental_trip_ids": [row.get("id", "") for row in unfinished_exp],
        "24h_cap_s": 86400,
    }


def summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "mean": None, "sd_sample": None, "median": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "sd_sample": statistics.stdev(values) if len(values) > 1 else 0.0,
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    statuses = read_statuses()
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    summaries: dict[tuple[int, str], dict[str, Any]] = {}
    for (seed, objective), status in sorted(statuses.items()):
        summary_path = RUNS / f"seed_{seed}" / objective / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        if summary:
            summaries[(seed, objective)] = summary
        row: dict[str, Any] = {
            "seed": seed,
            "objective": objective,
            "batch_status": status,
            "result_status": summary.get("status", "failed_or_capped" if not summary else "unknown"),
            "orders_completed": summary.get("orders_completed"),
            "expected_orders": summary.get("expected_orders", 100),
            "fleet_size": summary.get("fleet_size", 10),
        }
        row.update({metric: summary.get(metric) for metric in METRICS})
        if not summary:
            detail = read_failed_tripinfo(seed, objective)
            row.update({k: detail.get(k) for k in (
                "experimental_tripinfo_rows", "experimental_trip_arrivals", "passenger_trip_arrivals",
                "unfinished_trip_rows", "unfinished_experimental_trip_rows", "24h_cap_s",
            )})
            failures.append({"seed": seed, "objective": objective, "batch_status": status, **detail})
        rows.append(row)

    completed_by_objective = {
        objective: sorted(seed for (seed, method) in summaries if method == objective)
        for objective in ("FASTEST", "LOWEST_ENERGY")
    }
    paired_seeds = sorted(set(completed_by_objective["FASTEST"]) & set(completed_by_objective["LOWEST_ENERGY"]))
    objective_stats: dict[str, Any] = {}
    for objective in ("FASTEST", "LOWEST_ENERGY"):
        selected = [summaries[(seed, objective)] for seed in completed_by_objective[objective]]
        objective_stats[objective] = {
            "completed_seed_ids": completed_by_objective[objective],
            "completed_runs": len(selected),
            "planned_runs": 10,
            "completion_rate": len(selected) / 10,
            "successful_run_metrics": {
                metric: summarize([float(item[metric]) for item in selected if isinstance(item.get(metric), (int, float))])
                for metric in METRICS
            },
        }

    paired: dict[str, Any] = {}
    paired_rows: list[dict[str, Any]] = []
    for metric in METRICS:
        fast_values: list[float] = []
        low_values: list[float] = []
        deltas: list[float] = []
        changes_pct: list[float] = []
        for seed in paired_seeds:
            fast = float(summaries[(seed, "FASTEST")][metric])
            low = float(summaries[(seed, "LOWEST_ENERGY")][metric])
            delta = low - fast
            pct = delta / fast * 100.0 if fast else 0.0
            fast_values.append(fast); low_values.append(low); deltas.append(delta); changes_pct.append(pct)
            paired_rows.append({"seed": seed, "metric": metric, "FASTEST": fast, "LOWEST_ENERGY": low, "LOWEST_minus_FASTEST": delta, "paired_change_pct": pct})
        paired[metric] = {
            "n": len(deltas),
            "seed_ids": paired_seeds,
            "FASTEST_mean": statistics.mean(fast_values) if fast_values else None,
            "LOWEST_ENERGY_mean": statistics.mean(low_values) if low_values else None,
            "mean_paired_delta_LOWEST_minus_FASTEST": statistics.mean(deltas) if deltas else None,
            "mean_of_seed_level_change_pct": statistics.mean(changes_pct) if changes_pct else None,
            "median_paired_delta": statistics.median(deltas) if deltas else None,
        }

    payload = {
        "experiment_id": "porto_r1_dynamic_sumo_v3_fixed_delay_v3_3",
        "status": "completed_with_incomplete_runs" if len(statuses) == 20 else "incomplete_matrix",
        "input_freeze": "derived/scenarios/v3_3/frozen_inputs_v1_3/manifest_v3_3.json",
        "protocol": "derived/experiments/protocols/r1_dynamic_sumo_protocol_v3.json",
        "latency_trace_manifest": "derived/experiments/r1_dynamic_sumo_v1/planning_latency_traces_v1/manifest.json",
        "simulation": {
            "sumo_version": "1.28.0", "simulation_step_s": 0.1,
            "background_flow_per_hour": 90, "background_flow_hours": 2,
            "background_flow_is_synthetic": True, "max_simulated_time_s": 86400,
            "vehicle_count": 10, "fixed_orders_per_seed": 100,
            "seeds": list(range(10)), "dispatch_or_reassignment": False,
        },
        "planning_delay": "Frozen measured per-order R1 latency replayed on the SUMO clock; live wall time recorded separately.",
        "run_status_counts": {
            objective: {status: sum(1 for (_, method), value in statuses.items() if method == objective and value == status)
                        for status in sorted({value for (_, method), value in statuses.items() if method == objective})}
            for objective in ("FASTEST", "LOWEST_ENERGY")
        },
        "objective_stats_on_completed_runs": objective_stats,
        "paired_success_only_comparison": {
            "paired_success_seeds": paired_seeds, "n": len(paired_seeds),
            "interpretation": "Descriptive paired outcomes among seeds completed by both objectives; always read with completion rates and retained capped runs.",
            "metrics": paired,
        },
        "failed_run_details": failures,
        "limitations": [
            "OSM roads are real map geometry; SUMO signals and traffic are simulated, not observed Porto states.",
            "The 90 vehicles/hour background flow is a synthetic stress assumption shaped by historical taxi hotspot weights.",
            "Net kWh uses SUMO Energy/unknown with a frozen Kia Soul EV proxy; it is not calibrated to Porto vehicles or a currency bill.",
            "Frozen planner delays replay measured v2 wall times as virtual delays; live planner wall time is reported separately.",
            "Capped runs remain in each objective's planned-seed denominator; successful-run averages are not full-matrix averages.",
            "R1 outcomes do not establish an AI map-readability effect; LLM and representation-control comparisons remain separate.",
        ],
    }
    (RUNS / "aggregate_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(RUNS / "results_by_seed.csv", rows, ["seed", "objective", "batch_status", "result_status", "orders_completed", "expected_orders", "fleet_size", *METRICS, "experimental_tripinfo_rows", "experimental_trip_arrivals", "passenger_trip_arrivals", "unfinished_trip_rows", "unfinished_experimental_trip_rows", "24h_cap_s"])
    write_csv(RUNS / "paired_success_metrics.csv", paired_rows, ["seed", "metric", "FASTEST", "LOWEST_ENERGY", "LOWEST_minus_FASTEST", "paired_change_pct"])
    failure_rows = [{k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v for k, v in item.items()} for item in failures]
    write_csv(RUNS / "failed_run_details.csv", failure_rows, ["seed", "objective", "batch_status", "tripinfo_present", "tripinfo_rows", "experimental_tripinfo_rows", "experimental_trip_arrivals", "passenger_trip_arrivals", "unfinished_trip_rows", "unfinished_experimental_trip_rows", "unfinished_trip_ids", "unfinished_experimental_trip_ids", "24h_cap_s"])
    print(json.dumps({"aggregate_summary": str(RUNS / "aggregate_summary.json"), "run_status_counts": payload["run_status_counts"], "paired_success_seeds": paired_seeds}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
