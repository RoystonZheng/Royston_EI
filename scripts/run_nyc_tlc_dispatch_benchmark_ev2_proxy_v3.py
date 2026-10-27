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

from dispatch_experiment_ev2_proxy_v3.models import DispatchConfig
from dispatch_experiment_ev2_proxy_v3.decision_contract import decision_function_record
from dispatch_experiment_ev2_proxy_v3.llm import CODEX_DIRECT_ASSIGNMENT_INPUT_POLICY
from dispatch_experiment_ev2_proxy_v3.nyc_tlc_scenario import (
    NYCTLCZoneCentroidPickupOracle,
    load_dispatch_scenario,
)
from dispatch_experiment_ev2_proxy_v3.runner import DispatchSimulation
from route_experiment.energy_model import model_metadata


METHODS = [
    "D0_greedy",
    "D1_optimizer",
    "D2_agent_optimizer",
    "D3_agent_llm_context_optimizer",
    "D4_agent_llm_feedback_optimizer",
    "D5_pure_llm_direct",
    "D6_agent_codex_context_optimizer",
    "D7_agent_codex_capacity_shield_optimizer",
    "D8_agent_codex_nn_capacity_shield_optimizer",
]

FARE_RECONCILIATION_DIR = (
    ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_multimonth_energy_v2_offline_20261026/time_money_fare_reconciliation_v1"
)
DEFAULT_FARE_CROSSWALK = (
    ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/dispatch_ev2_proxy_v3_inputs/"
    "nyc_tlc_observed_fares_partial_from_audited_sidecar_v1.json"
)


def fingerprint(road_map, orders, vehicles) -> str:
    payload = {
        "map": road_map.to_dict(),
        "orders": [order.to_dict() for order in orders],
        "vehicles": [vehicle.to_dict() for vehicle in vehicles],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
    parser.add_argument(
        "--compact-training-labels",
        action="store_true",
        help="for D1 only, retain compact hard-feasible candidate features and selected IDs for pairwise ranker training",
    )
    parser.add_argument("--output-dir", default="outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results")
    parser.add_argument("--feedback-mode", choices=["offline_rule", "none", "deepseek"], default="offline_rule")
    parser.add_argument("--allow-live-api", action="store_true", help="required for live D5/D6/D7/D8 Codex CLI, or D4 with feedback-mode=deepseek")
    parser.add_argument("--codex-model", default=os.environ.get("DISPATCH_CODEX_MODEL", "gpt-5.6-luna"))
    parser.add_argument("--codex-policy-replay", default=None, help="reuse a previously captured D6/D7/D8-compatible context-policy response without a new model call")
    parser.add_argument("--nn-ranker-model", default=None, help="trained JSON checkpoint required by the integrated D8 method")
    parser.add_argument("--observed-fares-json", default=str(DEFAULT_FARE_CROSSWALK), help="hash-audited NYC TLC order_id to base_passenger_fare crosswalk JSON; incomplete joins remain null")
    parser.add_argument("--nn-top-k", type=int, default=1, help="maximum candidates retained per vehicle after neural scoring")
    parser.add_argument(
        "--pickup-energy-proxy-mode",
        choices=["centroid_start_stop_ev2_v1"],
        default="centroid_start_stop_ev2_v1",
        help="estimate edge-less positive-distance centroid pickups with the explicitly labeled EV v2 start-stop profile",
    )
    args = parser.parse_args()

    if args.compact_training_labels and args.methods != ["D1_optimizer"]:
        raise SystemExit("--compact-training-labels is only valid for a single D1_optimizer run")
    if args.compact_training_labels and args.compact_records:
        raise SystemExit("--compact-training-labels already enables compact recording")

    package = (ROOT / args.package).resolve()
    manifest = json.loads((package / "benchmark_manifest.json").read_text(encoding="utf-8"))
    pickup_policy_path = package / "pickup_cost_policy.json"
    centroid_path = package / "zone_centroids.json"
    pickup_policy = json.loads(pickup_policy_path.read_text(encoding="utf-8"))
    observed_fares_by_order = {}
    fare_crosswalk_path = Path(args.observed_fares_json).resolve() if args.observed_fares_json else None
    fare_crosswalk = None
    if fare_crosswalk_path is not None:
        fare_crosswalk = json.loads(fare_crosswalk_path.read_text(encoding="utf-8"))
        if fare_crosswalk.get("schema_version") != "nyc_tlc_observed_base_fares_v1":
            raise SystemExit("unsupported observed fare crosswalk schema")
        if fare_crosswalk.get("currency") != "USD" or not isinstance(fare_crosswalk.get("orders"), dict):
            raise SystemExit("observed fare crosswalk requires USD currency and an orders map")
        observed_fares_by_order = fare_crosswalk["orders"]
    centroid_payload = json.loads(centroid_path.read_text(encoding="utf-8"))
    zone_centroids = {
        int(zone_id): (float(point["lon"]), float(point["lat"]))
        for zone_id, point in centroid_payload.items()
    }
    if args.case not in manifest["cases"]:
        raise SystemExit("unknown case; choose one of: " + ", ".join(manifest["cases"]))
    if "D5_pure_llm_direct" in args.methods and not args.allow_live_api:
        raise SystemExit("D5 performs live Codex CLI model calls; pass --allow-live-api after explicitly approving the run")
    codex_context_methods = {
        "D6_agent_codex_context_optimizer",
        "D7_agent_codex_capacity_shield_optimizer",
        "D8_agent_codex_nn_capacity_shield_optimizer",
    }
    selected_context_methods = codex_context_methods.intersection(args.methods)
    if selected_context_methods and not args.codex_policy_replay and not args.allow_live_api:
        raise SystemExit("D6/D7/D8 perform a live Codex CLI model call; pass --allow-live-api after explicitly approving the run")
    if "D4_agent_llm_feedback_optimizer" in args.methods and args.feedback_mode == "deepseek" and not args.allow_live_api:
        raise SystemExit("D4 deepseek feedback performs live calls; pass --allow-live-api after explicitly approving the run")
    if args.compact_records and any(
        method not in {
            "D0_greedy",
            "D1_optimizer",
            "D2_agent_optimizer",
            "D5_pure_llm_direct",
            "D6_agent_codex_context_optimizer",
            "D7_agent_codex_capacity_shield_optimizer",
            "D8_agent_codex_nn_capacity_shield_optimizer",
        }
        for method in args.methods
    ):
        raise SystemExit("--compact-records is supported for D0/D1/D2/D5 and Codex CLI D6/D7/D8 runs")
    needs_live_api = "D4_agent_llm_feedback_optimizer" in args.methods and args.feedback_mode == "deepseek"
    if (
        ("D5_pure_llm_direct" in args.methods or (selected_context_methods and not args.codex_policy_replay))
        and not shutil.which("codex")
    ):
        raise SystemExit("codex CLI not found; no run was started")
    if args.codex_policy_replay and not Path(args.codex_policy_replay).is_file():
        raise SystemExit("Codex policy replay JSON does not exist; no run was started")
    if args.codex_policy_replay and not selected_context_methods:
        raise SystemExit("--codex-policy-replay is only valid with a D6/D7/D8 Codex context method")
    if "D8_agent_codex_nn_capacity_shield_optimizer" in args.methods and not args.nn_ranker_model:
        raise SystemExit("D8 requires --nn-ranker-model")
    if args.nn_top_k < 1:
        raise SystemExit("--nn-top-k must be positive")
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
            context_function_mode=(
                "codex_cli_generated_linear_clip_with_manual_fallback"
                if method_name in codex_context_methods
                else "manual_linear_clip"
            ),
            llm_backend="codex_cli",
            llm_model=args.codex_model,
            neural_ranker_model_path=args.nn_ranker_model,
            neural_ranker_top_k=args.nn_top_k,
            pickup_energy_proxy_mode=args.pickup_energy_proxy_mode,
            schema_version="dispatch-agent-ev2-segment-ledger-v4",
            seed=args.seed,
        )
        simulation = DispatchSimulation(
            config,
            road_map=loaded["road_map"],
            orders=loaded["orders"],
            vehicles=loaded["vehicles"],
            retain_round_records=not (args.compact_records or args.compact_training_labels),
            retain_training_rounds=args.compact_training_labels,
            observed_fares_by_order=observed_fares_by_order,
            route_oracle=NYCTLCZoneCentroidPickupOracle(
                loaded["road_map"],
                loaded["od_costs"],
                zone_centroids,
                speed_mps=float(pickup_policy["speed_mps"]),
                detour_factor=float(pickup_policy["detour_factor"]),
                minimum_intrazonal_distance_m=float(pickup_policy["minimum_intrazonal_distance_m"]),
            ),
            llm_trace_dir=(
                output_dir / ("codex_context_policy_traces" if method_name in codex_context_methods else "codex_cli_traces") / method_name
                if method_name in {"D5_pure_llm_direct", *codex_context_methods}
                else None
            ),
            codex_policy_replay_path=(
                Path(args.codex_policy_replay)
                if method_name in codex_context_methods and args.codex_policy_replay
                else None
            ),
        )
        summary, rounds = simulation.run(max_dispatch_rounds=1 if args.single_round_smoke else None)
        summary_payload = summary.to_dict()
        executed_passenger_segments = [
            item for item in simulation.executed_segment_ledger if item.get("segment_type") == "passenger"
        ]
        observed_passenger_fares = [
            item["fare_revenue_amount"] for item in executed_passenger_segments
            if item.get("fare_revenue_amount") is not None
        ]
        llm_usage_recorded = summary_payload.get("llm_usage_status") == "available_best_effort"
        llm_call_count = int(summary_payload.get("total_codex_cli_invocations") or 0)
        if not llm_call_count:
            method_cost_status = "no_model_calls"
        elif llm_usage_recorded:
            method_cost_status = "usage_recorded_rate_card_estimate_only"
        else:
            method_cost_status = "usage_missing_or_partial"
        method_costs = {
            "accounting_scope": "method_level_once",
            "codex_cli_invocations": summary_payload.get("total_codex_cli_invocations"),
            "input_tokens": summary_payload.get("total_llm_input_tokens"),
            "cached_input_tokens": summary_payload.get("total_llm_cached_input_tokens"),
            "output_tokens": summary_payload.get("total_llm_output_tokens"),
            "total_tokens": summary_payload.get("total_llm_tokens"),
            "codex_standard_credit_equivalent": summary_payload.get("total_codex_standard_credit_equivalent"),
            "rate_card_estimate_usd": summary_payload.get("total_api_equivalent_usd"),
            "compute_cost_amount": None,
            "actual_account_charge_amount": None,
            "currency": "USD",
            "status": method_cost_status,
        }
        payload = {
            "schema_version": config.schema_version,
            "experiment_type": "nyc_tlc_shared_input_method_run",
            "run_mode": "single_round_smoke" if args.single_round_smoke else "complete_simulation",
            "partial_run": bool(args.single_round_smoke),
            "performance_claim": False,
            "recording_mode": (
                "compact_training_label_rounds"
                if args.compact_training_labels
                else ("compact_round_audits_with_first_candidate_table"
                      if args.compact_records else "full_round_records")
            ),
            "case_id": args.case,
            "scenario_id": loaded["scenario"]["scenario_id"],
            "fleet_id": loaded["fleet"]["fleet_id"],
            "method_name": method_name,
            "llm_backend": config.llm_backend if method_name in {"D5_pure_llm_direct", *codex_context_methods} else None,
            "llm_model": config.llm_model if method_name in {"D5_pure_llm_direct", *codex_context_methods} else None,
            "llm_input_protocol": (
                CODEX_DIRECT_ASSIGNMENT_INPUT_POLICY
                if method_name == "D5_pure_llm_direct"
                else None
            ),
            "scenario_fingerprint_sha256": scenario_fingerprint,
            "input_hashes": {
                "scenario_json": loaded["scenario_metadata_sha256"],
                "orders_json": loaded["orders_json_sha256"],
                "zone_od_cost_matrix": loaded["cost_matrix_sha256"],
                "pickup_cost_policy": file_sha256(pickup_policy_path),
                "zone_centroids": file_sha256(centroid_path),
                "observed_fare_crosswalk": file_sha256(fare_crosswalk_path) if fare_crosswalk_path else None,
                "available_fare_crosswalk_parquet": (
                    file_sha256(FARE_RECONCILIATION_DIR / "orders_observed_fares.parquet")
                    if (FARE_RECONCILIATION_DIR / "orders_observed_fares.parquet").is_file()
                    else None
                ),
                "available_fare_reconciliation_manifest": (
                    file_sha256(FARE_RECONCILIATION_DIR / "manifest.json")
                    if (FARE_RECONCILIATION_DIR / "manifest.json").is_file()
                    else None
                ),
            },
            "cost_semantics": {
                "pickup": pickup_policy,
                "ride": "direct observed training-period zone OD median proxy; not street routing",
                "pickup_energy_proxy_mode": args.pickup_energy_proxy_mode,
                "financial_scoring": "never changes the frozen lexicographic dispatch objective; monetary route-choice score stays blocked without required inputs and a common route candidate pool",
            },
            "decision_function": decision_function_record(
                method_name,
                pickup_energy_proxy_mode=args.pickup_energy_proxy_mode,
            ),
            "energy_accounting": {
                **model_metadata(config.energy_profile),
                "trace_fidelity": {
                    "pickup_deadhead": "synthetic centroid-proxy distance/time evaluated with EV v2 start-stop profile; grade unknown and treated flat",
                    "passenger_service": "estimated_from direct zone OD edge conditions; grade unknown and treated flat",
                },
                "signed_net_basis": "propulsion_draw_kwh - regenerated_kwh + auxiliary_kwh",
                "billable_energy_basis": "sum signed Link/segment energy first, then max(segment_signed_net_kwh, 0)",
                "reported_outcome": "simulated net battery energy; not measured vehicle telemetry",
                "assignment_dependency": decision_function_record(
                    method_name,
                    pickup_energy_proxy_mode=args.pickup_energy_proxy_mode,
                )["energy_dependency"],
            },
            "money_accounting": {
                "status": "blocked_incomplete_economic_inputs",
                "known_deadhead_fare_revenue": 0.0,
                "passenger_fare_source": fare_crosswalk.get("source") if fare_crosswalk else None,
                "passenger_fare_currency": "USD" if fare_crosswalk else None,
                "fare_crosswalk_status": (
                    "complete_joined" if fare_crosswalk and fare_crosswalk.get("complete")
                    else ("partial_joined" if fare_crosswalk else "not_attached")
                ),
                "fare_crosswalk_order_count": len(observed_fares_by_order) if fare_crosswalk else 0,
                "fare_crosswalk_coverage_fraction": fare_crosswalk.get("coverage_fraction") if fare_crosswalk else 0.0,
                "available_fare_crosswalk_artifact": (
                    str((FARE_RECONCILIATION_DIR / "orders_observed_fares.parquet").relative_to(ROOT))
                    if (FARE_RECONCILIATION_DIR / "orders_observed_fares.parquet").is_file()
                    else None
                ),
                "available_fare_crosswalk_not_joined_sha256": (
                    file_sha256(FARE_RECONCILIATION_DIR / "orders_observed_fares.parquet")
                    if not fare_crosswalk and (FARE_RECONCILIATION_DIR / "orders_observed_fares.parquet").is_file()
                    else None
                ),
                "executed_passenger_segment_count": len(executed_passenger_segments),
                "executed_passenger_segments_with_observed_fare_count": len(observed_passenger_fares),
                "known_observed_passenger_fare_sum_usd": sum(observed_passenger_fares) if executed_passenger_segments else None,
                "observed_passenger_fare_sum_usd": (
                    sum(observed_passenger_fares)
                    if executed_passenger_segments and len(observed_passenger_fares) == len(executed_passenger_segments)
                    else None
                ),
                "executed_passenger_segments_missing_observed_fare_count": len(executed_passenger_segments) - len(observed_passenger_fares),
                "electricity_price_per_kwh": None,
                "currency": None,
                "missing_inputs": [
                    *( ["complete hash-audited observed fare crosswalk"] if not fare_crosswalk or not fare_crosswalk.get("complete") else [] ),
                    "scenario-specific electricity tariff",
                    "frozen method-level compute-cost rate",
                    "common alternative-route pool for time-money route choice",
                ],
                "money_score_applied_to_assignment": False,
                "api_compute_cost_accounting": "method-level usage/cost is reported once in method_costs and raw CLI traces; never duplicated in segment rows",
            },
            "method_costs": method_costs,
            "executed_segment_ledger": simulation.executed_segment_ledger,
            "config": config.to_dict(),
            "summary": summary_payload,
            "rounds": [record.to_dict() for record in rounds],
        }
        if args.compact_training_labels:
            payload["training_rounds"] = simulation.training_rounds
            payload["round_audits"] = simulation.round_audits
            payload["round_validation_errors"] = simulation.round_validation_errors
        elif args.compact_records:
            payload["round_audits"] = simulation.round_audits
            payload["round_validation_errors"] = simulation.round_validation_errors
        path = output_dir / f"{method_name}_seed_{args.seed}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"method": method_name, "summary": summary.to_dict(), "result_path": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
