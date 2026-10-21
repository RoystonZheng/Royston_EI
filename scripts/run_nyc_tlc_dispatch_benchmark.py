#!/usr/bin/env python3
"""Run one NYC TLC-derived case through selected dispatch methods."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dispatch_experiment.models import DispatchConfig
from dispatch_experiment.nyc_tlc_scenario import (
    NYCTLCZoneCentroidPickupOracle,
    load_dispatch_scenario,
)
from dispatch_experiment.runner import DispatchSimulation


METHODS = [
    "D0_greedy",
    "D1_optimizer",
    "D2_agent_optimizer",
    "D3_agent_llm_context_optimizer",
    "D4_agent_llm_feedback_optimizer",
    "D5_pure_llm_direct",
]


def fingerprint(road_map, orders, vehicles) -> str:
    payload = {
        "map": road_map.to_dict(),
        "orders": [order.to_dict() for order in orders],
        "vehicles": [vehicle.to_dict() for vehicle in vehicles],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="run a shared-input NYC TLC dispatch case")
    parser.add_argument("--package", default="outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1")
    parser.add_argument("--case", required=True)
    parser.add_argument("--fleet-id", default=None)
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=["D0_greedy", "D1_optimizer", "D2_agent_optimizer"])
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--single-round-smoke", action="store_true", help="stop after the first dispatch decision; not a service-performance run")
    parser.add_argument(
        "--compact-records",
        action="store_true",
        help="store per-round audits and the full first candidate table without full state snapshots",
    )
    parser.add_argument("--output-dir", default="outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results")
    parser.add_argument("--feedback-mode", choices=["offline_rule", "none", "deepseek"], default="offline_rule")
    parser.add_argument("--allow-live-api", action="store_true", help="required for live D5 Codex CLI, or D4 with feedback-mode=deepseek")
    parser.add_argument("--codex-model", default=os.environ.get("DISPATCH_CODEX_MODEL", "gpt-5.6-luna"))
    args = parser.parse_args()

    package = (ROOT / args.package).resolve()
    manifest = json.loads((package / "benchmark_manifest.json").read_text(encoding="utf-8"))
    pickup_policy = json.loads((package / "pickup_cost_policy.json").read_text(encoding="utf-8"))
    centroid_payload = json.loads((package / "zone_centroids.json").read_text(encoding="utf-8"))
    zone_centroids = {
        int(zone_id): (float(point["lon"]), float(point["lat"]))
        for zone_id, point in centroid_payload.items()
    }
    if args.case not in manifest["cases"]:
        raise SystemExit("unknown case; choose one of: " + ", ".join(manifest["cases"]))
    if "D5_pure_llm_direct" in args.methods and not args.allow_live_api:
        raise SystemExit("D5 performs live Codex CLI model calls; pass --allow-live-api after explicitly approving the run")
    if "D4_agent_llm_feedback_optimizer" in args.methods and args.feedback_mode == "deepseek" and not args.allow_live_api:
        raise SystemExit("D4 deepseek feedback performs live calls; pass --allow-live-api after explicitly approving the run")
    if args.compact_records and any(method not in {"D0_greedy", "D1_optimizer"} for method in args.methods):
        raise SystemExit("--compact-records is currently limited to the traditional D0/D1 baselines")
    needs_live_api = "D4_agent_llm_feedback_optimizer" in args.methods and args.feedback_mode == "deepseek"
    if "D5_pure_llm_direct" in args.methods and not shutil.which("codex"):
        raise SystemExit("codex CLI not found; no run was started")
    if needs_live_api and not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY is not set; no run was started")

    case = manifest["cases"][args.case]
    case_dir = package / case["path"]
    loaded = load_dispatch_scenario(
        case_dir,
        case_dir / "zone_od_cost_matrix.csv.gz",
        fleet_id=args.fleet_id,
    )
    scenario_fingerprint = fingerprint(loaded["road_map"], loaded["orders"], loaded["vehicles"])
    output_dir = (ROOT / args.output_dir / args.case / (args.fleet_id or "single_fleet")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    controls = loaded["scenario"].get("dispatch_controls", {})

    for method_name in args.methods:
        feedback_mode = "none" if method_name == "D5_pure_llm_direct" else args.feedback_mode
        config = DispatchConfig(
            map_size=loaded["road_map"].size,
            vehicle_count=len(loaded["vehicles"]),
            order_count=len(loaded["orders"]),
            horizon_sec=int(controls.get("horizon_sec", 3600)),
            max_pickup_wait_sec=float(controls.get("max_pickup_wait_sec", 300.0)),
            passenger_no_show_rate=float(controls.get("passenger_no_show_rate", 0.0)),
            method_name=method_name,
            feedback_mode=feedback_mode,
            llm_backend="codex_cli",
            llm_model=args.codex_model,
            seed=args.seed,
        )
        simulation = DispatchSimulation(
            config,
            road_map=loaded["road_map"],
            orders=loaded["orders"],
            vehicles=loaded["vehicles"],
            retain_round_records=not args.compact_records,
            route_oracle=NYCTLCZoneCentroidPickupOracle(
                loaded["road_map"],
                loaded["od_costs"],
                zone_centroids,
                speed_mps=float(pickup_policy["speed_mps"]),
                detour_factor=float(pickup_policy["detour_factor"]),
                minimum_intrazonal_distance_m=float(pickup_policy["minimum_intrazonal_distance_m"]),
            ),
            llm_trace_dir=(output_dir / "codex_cli_traces") if method_name == "D5_pure_llm_direct" else None,
        )
        summary, rounds = simulation.run(max_dispatch_rounds=1 if args.single_round_smoke else None)
        payload = {
            "schema_version": config.schema_version,
            "experiment_type": "nyc_tlc_shared_input_method_run",
            "run_mode": "single_round_smoke" if args.single_round_smoke else "complete_simulation",
            "partial_run": bool(args.single_round_smoke),
            "performance_claim": False,
            "recording_mode": (
                "compact_round_audits_with_first_candidate_table"
                if args.compact_records
                else "full_round_records"
            ),
            "case_id": args.case,
            "scenario_id": loaded["scenario"]["scenario_id"],
            "fleet_id": loaded["fleet"]["fleet_id"],
            "method_name": method_name,
            "llm_backend": config.llm_backend if method_name == "D5_pure_llm_direct" else None,
            "llm_model": config.llm_model if method_name == "D5_pure_llm_direct" else None,
            "scenario_fingerprint_sha256": scenario_fingerprint,
            "input_hashes": {
                "scenario_json": loaded["scenario_metadata_sha256"],
                "orders_json": loaded["orders_json_sha256"],
                "zone_od_cost_matrix": loaded["cost_matrix_sha256"],
            },
            "cost_semantics": {
                "pickup": pickup_policy,
                "ride": "direct observed training-period zone OD median proxy; not street routing",
            },
            "config": config.to_dict(),
            "summary": summary.to_dict(),
            "rounds": [record.to_dict() for record in rounds],
        }
        if args.compact_records:
            payload["round_audits"] = simulation.round_audits
            payload["round_validation_errors"] = simulation.round_validation_errors
        path = output_dir / f"{method_name}_seed_{args.seed}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"method": method_name, "summary": summary.to_dict(), "result_path": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
