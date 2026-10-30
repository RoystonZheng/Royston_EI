#!/usr/bin/env python3
"""Recalculate saved traditional-grid route energy by empty/loaded phase.

The source route and EV v2 results are preserved. This offline pass uses the
same validated route edges, payload 0 kg before pickup, and the profile's
passenger load after pickup. It does not rerun a route planner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from route_experiment.energy_model import EnergyModelConfig, evaluate_route_edges, model_metadata  # noqa: E402
from route_experiment.models import EvaluationConfig  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def lookup_tasks(source_root: Path, seed_detail: Dict[str, Any], kind: str) -> Dict[str, Dict[str, Any]]:
    if kind == "orders":
        record = read_json(source_root / seed_detail["orders_json"])
        truth = {row["task_id"]: row for row in record.get("ground_truth", [])}
        result = {}
        for task in record.get("tasks", []):
            merged = dict(task)
            merged.update(truth.get(task.get("task_id"), {}))
            result[str(task["task_id"])] = merged
        return result
    record = read_json(source_root / seed_detail["formal_scenarios_json"])
    return {
        str(task["task_id"]): task
        for scenario in record.get("scenarios", [])
        for task in scenario.get("tasks", [])
    }


def select_route(method: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    routes = method.get("routes", [])
    best_id = method.get("best_route_id")
    if best_id:
        for route in routes:
            if route.get("route_id") == best_id:
                return route
    return next((r for r in routes if r.get("route_reachability")
                 and r.get("validation_status") in (None, "", "valid")), None)


def evaluate_route(route: Dict[str, Any], task: Dict[str, Any], method: Dict[str, Any], map_data: Dict[str, Any],
                   profile: EnergyModelConfig, evaluation: EvaluationConfig) -> Dict[str, Any]:
    nodes = list(route.get("node_sequence") or [])
    edge_ids = list(route.get("edge_sequence") or [])
    search_legs = ((method.get("metadata") or {}).get("search_trace") or {}).get("legs") or []
    planned_pickup = (
        search_legs[0].get("target_node_id")
        if search_legs else (task.get("pickup_node_id") or task.get("true_pickup_node_id"))
    )
    planned_dropoff = (
        search_legs[-1].get("target_node_id")
        if search_legs else (task.get("dropoff_node_id") or task.get("true_dropoff_node_id"))
    )
    true_pickup = task.get("true_pickup_node_id") or task.get("pickup_node_id")
    true_dropoff = task.get("true_dropoff_node_id") or task.get("dropoff_node_id")
    pickup, dropoff = planned_pickup, planned_dropoff
    if not nodes or len(nodes) != len(edge_ids) + 1:
        raise ValueError("route_node_edge_count_mismatch")
    if not pickup or not dropoff or pickup not in nodes or nodes[-1] != dropoff:
        raise ValueError("route_does_not_cover_frozen_pickup_and_dropoff")
    split = nodes.index(pickup)
    edge_by_id = {str(edge["edge_id"]): edge for edge in map_data["edges"]}
    ordered = []
    for i, edge_id in enumerate(edge_ids):
        edge = edge_by_id.get(str(edge_id))
        if edge is None:
            raise ValueError("route_edge_missing_from_frozen_map:" + str(edge_id))
        if edge.get("from_node") != nodes[i] or edge.get("to_node") != nodes[i + 1]:
            raise ValueError("route_edge_sequence_or_direction_mismatch:" + str(edge_id))
        ordered.append(edge)
    empty_edges, loaded_edges = ordered[:split], ordered[split:]
    empty = evaluate_route_edges(empty_edges, evaluation.congestion_speed_multipliers,
                                 profile, payload_mass_kg=0.0)
    loaded = evaluate_route_edges(loaded_edges, evaluation.congestion_speed_multipliers,
                                  profile, payload_mass_kg=float(profile.passenger_load_kg))
    full_loaded = evaluate_route_edges(ordered, evaluation.congestion_speed_multipliers,
                                       profile, payload_mass_kg=float(profile.passenger_load_kg))
    old_energy = (route.get("evaluation") or {}).get("energy_total_kwh")
    return {
        "pickup_node_id": pickup,
        "dropoff_node_id": dropoff,
        "true_pickup_node_id": true_pickup,
        "true_dropoff_node_id": true_dropoff,
        "pickup_matches_task_truth": pickup == true_pickup,
        "dropoff_matches_task_truth": dropoff == true_dropoff,
        "split_node_index": split,
        "empty_edge_count": len(empty_edges),
        "passenger_edge_count": len(loaded_edges),
        "empty_leg_signed_net_energy_kwh": float(empty.net_kwh),
        "passenger_leg_signed_net_energy_kwh": float(loaded.net_kwh),
        "total_signed_net_energy_kwh": float(empty.net_kwh + loaded.net_kwh),
        "empty_leg_billable_energy_kwh": float(max(empty.net_kwh, 0.0)),
        "passenger_leg_billable_energy_kwh": float(max(loaded.net_kwh, 0.0)),
        "total_billable_energy_kwh": float(max(empty.net_kwh, 0.0) + max(loaded.net_kwh, 0.0)),
        "prior_all_passenger_payload_energy_kwh": float(old_energy) if old_energy is not None else None,
        "same_route_all_passenger_payload_recalculated_kwh": float(full_loaded.net_kwh),
        "payload_phase_delta_vs_prior_kwh": (
            float(empty.net_kwh + loaded.net_kwh) - float(old_energy)
            if old_energy is not None else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT / "outputs/route_layer/traditional_experiment_v1")
    parser.add_argument("--energy-v2-root", type=Path, default=ROOT / "outputs/route_layer/traditional_experiment_v1_energy_v2")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source_root, energy_root, output_dir = args.source_root.resolve(), args.energy_v2_root.resolve(), args.output_dir.resolve()
    if output_dir.exists():
        raise SystemExit("refusing_to_overwrite_existing_output_dir:" + str(output_dir))
    source_manifest_path = source_root / "manifest.json"
    energy_manifest_path = energy_root / "manifest.json"
    if not source_manifest_path.is_file() or not energy_manifest_path.is_file():
        raise SystemExit("source_or_energy_v2_manifest_missing")
    source_manifest, energy_manifest = read_json(source_manifest_path), read_json(energy_manifest_path)
    profile = EnergyModelConfig()
    evaluation = EvaluationConfig(energy_profile=profile)
    output_dir.mkdir(parents=True)

    rows: List[Dict[str, Any]] = []
    slots = 0
    failures = []
    method_totals: Dict[str, Dict[str, float]] = defaultdict(lambda: {
        "expected_slots": 0, "recalculated_routes": 0, "not_estimable_or_not_completed": 0,
        "signed_net_energy_kwh": 0.0, "billable_energy_kwh": 0.0, "electricity_cost": 0.0,
    })
    for seed_detail in source_manifest.get("seeds_detail", []):
        seed = int(seed_detail["seed"])
        map_path = source_root / seed_detail["map_json"]
        map_data = read_json(map_path)
        map_sha = sha256(map_path)
        for kind, source_key, sidecar_name in (
            ("orders", "order_routes_costs", "order_routes_energy_v2.json"),
            ("formal", "formal_routes_costs", "formal_routes_energy_v2.json"),
        ):
            sidecar_path = energy_root / "seeds" / f"seed_{seed}" / sidecar_name
            if not sidecar_path.is_file():
                continue
            sidecar = read_json(sidecar_path)
            task_by_id = lookup_tasks(source_root, seed_detail, kind)
            source_route_path = source_root / seed_detail[source_key]
            source_route_sha = sha256(source_route_path) if source_route_path.is_file() else None
            price_profile = sidecar.get("price_profile") or energy_manifest.get("tariff_profile") or {}
            unit_price = price_profile.get("electricity_price_per_kwh")
            currency = price_profile.get("currency", "CNY")
            for item in sidecar.get("results", []):
                task_id = str(item.get("task_id", ""))
                task = task_by_id.get(task_id)
                if task is None:
                    failures.append({"seed": seed, "task_id": task_id, "status": "task_truth_missing"})
                    continue
                for method in item.get("methods", []):
                    slots += 1
                    name = str(method.get("method_name", "unknown"))
                    totals = method_totals[name]
                    totals["expected_slots"] += 1
                    route = select_route(method) if method.get("status") == "completed" else None
                    base = {
                        "seed": seed, "scope": kind, "task_id": task_id,
                        "method_name": name, "method_status": method.get("status"),
                        "route_id": route.get("route_id") if route else None,
                        "source_energy_sidecar": str(sidecar_path),
                        "source_energy_sidecar_sha256": sha256(sidecar_path),
                        "source_route_file": str(source_route_path),
                        "source_route_file_sha256": source_route_sha,
                        "map_id": map_data.get("map_id"), "map_sha256": map_sha,
                        "profile_sha256": profile.fingerprint(),
                        "energy_model_version": profile.model_version,
                        "decision_replayed": False,
                    }
                    if route is None:
                        base.update({"status": "not_completed_or_no_valid_route", "energy": None,
                                     "electricity_cost": None})
                        totals["not_estimable_or_not_completed"] += 1
                        rows.append(base)
                        continue
                    try:
                        metrics = evaluate_route(route, task, method, map_data, profile, evaluation)
                        billable = metrics["total_billable_energy_kwh"]
                        money = billable * float(unit_price) if unit_price is not None else None
                        base.update({"status": "recalculated_completed_route", "route_metrics": metrics,
                                     "electricity_price_per_kwh": unit_price,
                                     "currency": currency if money is not None else None,
                                     "electricity_cost": money,
                                     "energy_basis": "signed net by trip phase; billing max(net,0) separately per empty and passenger leg"})
                        totals["recalculated_routes"] += 1
                        totals["signed_net_energy_kwh"] += metrics["total_signed_net_energy_kwh"]
                        totals["billable_energy_kwh"] += billable
                        if money is not None:
                            totals["electricity_cost"] += money
                        rows.append(base)
                    except Exception as error:
                        failures.append({"seed": seed, "scope": kind, "task_id": task_id,
                                         "method_name": name, "status": "recalculation_error",
                                         "reason": str(error), "route_id": route.get("route_id")})

    index_path = output_dir / "phase_energy_index.jsonl"
    with index_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "schema_version": "traditional_grid_ev_v2_payload_phase_recalculation_v1",
        "status": "passed" if not failures else "completed_with_errors",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "calculation_script": str(Path(__file__).resolve()),
        "calculation_script_sha256": sha256(Path(__file__).resolve()),
        "source_root": str(source_root), "energy_v2_root": str(energy_root),
        "source_manifest_sha256": sha256(source_manifest_path),
        "energy_v2_manifest_sha256": sha256(energy_manifest_path),
        "route_algorithm_rerun": False, "frozen_route_choices_changed": False,
        "model_calls": 0, "energy_model": model_metadata(profile),
        "profile_sha256": profile.fingerprint(),
        "payload_phases": {"empty_kg": 0.0, "passenger_kg": float(profile.passenger_load_kg)},
        "expected_task_method_slots": slots,
        "completed_route_recalculation_count": sum(int(v["recalculated_routes"]) for v in method_totals.values()),
        "noncompleted_or_no_valid_route_slots": sum(int(v["not_estimable_or_not_completed"]) for v in method_totals.values()),
        "index_path": str(index_path), "index_sha256": sha256(index_path),
        "summary_by_method": dict(method_totals), "error_count": len(failures), "errors": failures,
        "limitations": [
            "EV v2 route-phase values are simulation estimates from saved edge conditions, not measured vehicle telemetry.",
            "This sidecar changes reporting/accounting only; the historical route selection used its original objective.",
            "Energy and electricity cost for incomplete or unestimable routes remain null, never zero.",
        ],
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), "status": manifest["status"],
                      "task_method_slots": slots,
                      "completed_route_recalculations": manifest["completed_route_recalculation_count"],
                      "not_completed_or_no_valid_route_slots": manifest["noncompleted_or_no_valid_route_slots"],
                      "errors": len(failures), "profile_sha256": profile.fingerprint()}, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
