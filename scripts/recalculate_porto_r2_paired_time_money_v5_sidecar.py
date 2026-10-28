from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


EXPECTED_MODEL = "route_time_money_segment_v5_signed_fare_audit"
EXPECTED_PROFILE_SHA256 = "b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee"
TARIFF_EUR_PER_KWH = 0.30
TARIFF_BASIS = "frozen Porto scenario assumption EUR 0.30/kWh; not a market invoice"
OBJECTIVES = ("FASTEST", "LOWEST_ENERGY")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot_load_module:{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a read-only EV v2 and time-money sidecar for the completed Porto R2 paired matrix."
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--supersedes-sidecar-manifest", type=Path)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    run_dir = args.run_dir.resolve()
    batch_manifest_path = run_dir / "batch_manifest.json"
    batch_summary_path = run_dir / "batch_summary.json"
    if not batch_manifest_path.is_file() or not batch_summary_path.is_file():
        raise SystemExit("missing_batch_manifest_or_summary")

    batch_manifest = read_json(batch_manifest_path)
    batch_summary = read_json(batch_summary_path)
    if batch_manifest.get("status") != "traditional_matrix_complete":
        raise SystemExit("batch_manifest_not_terminal")
    if batch_manifest.get("traditional_cells_expected") != 20:
        raise SystemExit("unexpected_expected_cell_count")
    if batch_manifest.get("traditional_cells_terminal") != 20:
        raise SystemExit("batch_manifest_not_20_of_20_terminal")
    if batch_summary.get("batch_status") != "traditional_matrix_complete":
        raise SystemExit("batch_summary_not_terminal")
    if batch_summary.get("completed_cells") != 20:
        raise SystemExit("batch_summary_not_20_of_20_complete")
    if batch_summary.get("orders_completed") != 2000:
        raise SystemExit("batch_summary_order_count_mismatch")
    if batch_summary.get("orders_failed") or batch_summary.get("orders_blocked"):
        raise SystemExit("batch_summary_has_failed_or_blocked_orders")
    if batch_summary.get("codex_model_calls") != 0:
        raise SystemExit("unexpected_model_calls_in_traditional_matrix")
    if list(batch_manifest.get("selection_objectives", [])) != list(OBJECTIVES):
        raise SystemExit("unexpected_objective_pair")
    if "signed-net" not in str(batch_manifest.get("selector_contract", "")).lower():
        raise SystemExit("source_selector_contract_not_signed_net")

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else run_dir
        / (
            "segment_economics_v5_paired_sidecar_"
            + datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%dT%H%M%SCST")
        )
    )
    if output_dir.exists():
        raise SystemExit(f"refusing_to_overwrite:{output_dir}")

    decision_path = project_root / "route_experiment" / "decision.py"
    energy_path = project_root / "route_experiment" / "energy_model.py"
    helper_path = (
        project_root
        / "data/real_map_dispatch/.worktrees/porto_r2_execute_20261025/scripts/"
        / "create_porto_r2_segment_economics_v5_sidecar.py"
    )
    if not helper_path.is_file():
        raise SystemExit(f"missing_verified_segment_reconciler:{helper_path}")
    decision = load_module("porto_r2_paired_v5_decision", decision_path)
    energy_model = load_module("porto_r2_paired_v5_energy", energy_path)
    helper = load_module("porto_r2_existing_v5_segment_reconciler", helper_path)
    if decision.DECISION_MODEL_VERSION != EXPECTED_MODEL:
        raise SystemExit("decision_model_version_mismatch")
    if energy_model.EnergyModelConfig().fingerprint() != EXPECTED_PROFILE_SHA256:
        raise SystemExit("ev_v2_profile_fingerprint_mismatch")

    seeds = list(batch_summary.get("selected_seeds", []))
    if len(seeds) != 10 or sorted(seeds) != list(range(10)):
        raise SystemExit("unexpected_seed_set")

    cell_dirs: dict[tuple[int, str], Path] = {}
    for seed in seeds:
        for objective in OBJECTIVES:
            source = run_dir / f"seed_{seed}" / objective
            if not source.is_dir():
                raise SystemExit(f"missing_cell:{source}")
            cell_dirs[(seed, objective)] = source
    if len(cell_dirs) != 20:
        raise SystemExit("unexpected_cell_directory_count")

    required_files = [
        "summary.json",
        "route_legs_actual.json",
        "order_execution.csv",
        "route_validation.jsonl",
        "sumo_motion_traces_v2.json",
    ]
    source_hashes = {}
    for key, source in sorted(cell_dirs.items()):
        source_hashes[f"seed_{key[0]}/{key[1]}"] = {
            filename: sha256(source / filename) for filename in required_files
        }
    source_manifest_hash = sha256(batch_manifest_path)
    source_summary_hash = sha256(batch_summary_path)
    superseded_manifest_path = (
        args.supersedes_sidecar_manifest.resolve()
        if args.supersedes_sidecar_manifest
        else None
    )
    if superseded_manifest_path is not None and not superseded_manifest_path.is_file():
        raise SystemExit(f"missing_superseded_sidecar_manifest:{superseded_manifest_path}")

    output_dir.mkdir(parents=True, exist_ok=False)
    cell_records = []
    candidate_segments: dict[tuple[int, str], dict[str, list[dict]]] = {}
    combined_ledger = []
    for (seed, objective), source in sorted(cell_dirs.items()):
        cell_output = output_dir / "cells" / f"seed_{seed}" / objective
        record = helper.reconcile_cell(source, cell_output, decision, energy_model)
        record["seed"] = seed
        record["objective"] = objective
        record["source_cell"] = str(source)
        cell_records.append(record)
        ledger_rows = read_jsonl(cell_output / "journey_segments.jsonl")
        by_order: dict[str, list[dict]] = defaultdict(list)
        for row in ledger_rows:
            enriched = {
                **row,
                "seed": seed,
                "objective": objective,
                "source_cell": str(source),
            }
            combined_ledger.append(enriched)
            by_order[str(row["order_id"])].append(row)
        if len(by_order) != 100 or any(len(rows) != 2 for rows in by_order.values()):
            raise SystemExit(f"per_order_two_segment_reconciliation_failed:{seed}:{objective}")
        candidate_segments[(seed, objective)] = by_order

    write_jsonl(output_dir / "journey_segments_v5.jsonl", combined_ledger)

    method_cost_rows = []
    for record in cell_records:
        source = cell_dirs[(int(record["seed"]), str(record["objective"]))]
        summary = read_json(source / "summary.json")
        method_cost_rows.append({
            "method_record_id": f"R2_TRADITIONAL_TD_DIJKSTRA_LOCAL_CROP:{record['objective']}:seed_{record['seed']}",
            "method": "R2_TRADITIONAL_TD_DIJKSTRA_LOCAL_CROP",
            "objective": record["objective"],
            "seed": record["seed"],
            "currency": "EUR",
            "model_api_calls": 0,
            "api_token_cost_amount": 0.0,
            "api_token_cost_basis": "no_model_api_usage_in_traditional_run",
            "measured_planning_wall_time_sec": summary.get("sum_route_planning_wall_time_s"),
            "compute_cost_amount": None,
            "compute_cost_basis": None,
            "null_reasons": {
                "compute_cost_amount": "local_compute_cost_rate_not_frozen_or_supplied"
            },
            "method_cost_added_once_per_generated_alternative": True,
            "not_allocated_to_route_segments_or_links": True,
        })
    write_jsonl(output_dir / "method_costs_v5.jsonl", method_cost_rows)
    method_cost_by_key = {
        (int(row["seed"]), str(row["objective"])): row
        for row in method_cost_rows
    }

    route_economics_rows = []
    decision_rows = []
    status_counts = Counter()
    source_pool_gate = str(
        batch_manifest.get("weighted_net_cost_scoring_status", "unknown")
    )
    candidate_pool_gate_passed = source_pool_gate.lower() in {
        "passed",
        "complete",
        "complete_predeclared_candidate_pool",
    }
    for seed in seeds:
        fastest = candidate_segments[(seed, "FASTEST")]
        lowest = candidate_segments[(seed, "LOWEST_ENERGY")]
        common_orders = sorted(set(fastest) & set(lowest))
        if len(common_orders) != 100:
            raise SystemExit(f"paired_order_pool_mismatch:{seed}:{len(common_orders)}")
        for order_id in common_orders:
            candidates = []
            candidate_economics = {}
            for objective, rows in [
                ("FASTEST", fastest[order_id]),
                ("LOWEST_ENERGY", lowest[order_id]),
            ]:
                economics_inputs = []
                for row in rows:
                    economics_inputs.append({
                        "segment_id": row["segment_id"],
                        "segment_type": row["segment_type"],
                        "distance_m": row["distance_m"],
                        "energy_kwh": row["energy_kwh"],
                        "travel_time_sec": row["travel_time_sec"],
                        "fare_revenue_amount": row.get("fare_revenue_amount"),
                        "fare_basis": row.get("fare_basis"),
                        "price_basis": TARIFF_BASIS,
                        "road_links": row.get("ordered_road_links", []),
                    })
                economics = decision.aggregate_route_segment_economics(
                    economics_inputs,
                    currency="EUR",
                    electricity_price_per_kwh=TARIFF_EUR_PER_KWH,
                    fare_per_km=None,
                    price_basis=TARIFF_BASIS,
                )
                candidate_id = f"seed_{seed}:{order_id}:{objective}"
                candidate_economics[candidate_id] = economics
                time_s = sum(float(row["travel_time_sec"]) for row in rows)
                # A money score requires all segment economics, the method
                # compute cost, and a source-certified complete candidate pool.
                # Missing any one of them keeps the score input null.
                method_cost_amount = method_cost_by_key[
                    (seed, objective)
                ]["compute_cost_amount"]
                route_cost_amount = economics.get("net_economic_cost")
                money_cost = (
                    float(route_cost_amount) + float(method_cost_amount)
                    if (
                        candidate_pool_gate_passed
                        and route_cost_amount is not None
                        and method_cost_amount is not None
                    )
                    else None
                )
                candidates.append({
                    "candidate_id": candidate_id,
                    "feasible": True,
                    "travel_time_sec": time_s,
                    "money_cost": money_cost,
                })
            for candidate_id, economics in candidate_economics.items():
                route_economics_rows.append({
                    "decision_scenario_id": f"seed_{seed}:{order_id}",
                    "candidate_id": candidate_id,
                    "currency": "EUR",
                    "candidate_pool_gate": source_pool_gate,
                    "candidate_pool_status": "blocked_by_source_batch_gate",
                    "candidate_pool_reason": "source batch does not certify a complete predeclared same-snapshot route alternative pool",
                    "method_cost_record_scope": "method-level; see method_costs_v5.jsonl",
                    **economics,
                })
            for profile_id in decision.DECISION_PROFILES:
                result = decision.score_route_candidates(
                    candidates, profile_id, currency="EUR"
                )
                status_counts[f"{profile_id}:{result['status']}"] += 1
                decision_rows.append({
                    "decision_scenario_id": f"seed_{seed}:{order_id}",
                    "seed": seed,
                    "order_id": order_id,
                    "profile_id": profile_id,
                    "candidate_pool_status": "blocked_by_source_batch_gate",
                    "candidate_pool_reason": "source batch does not certify a complete predeclared same-snapshot route alternative pool",
                    "source_weighted_net_cost_gate": source_pool_gate,
                    "scorer_result": result,
                    "selected_candidate_id": None,
                    "weighted_ranking_emitted": False,
                })

    write_jsonl(output_dir / "route_candidate_economics_v5.jsonl", route_economics_rows)
    write_jsonl(output_dir / "time_money_decisions_v5.jsonl", decision_rows)

    after_hashes = {}
    for key, source in sorted(cell_dirs.items()):
        after_hashes[f"seed_{key[0]}/{key[1]}"] = {
            filename: sha256(source / filename) for filename in required_files
        }
    if source_hashes != after_hashes:
        raise SystemExit("source_result_hash_changed_during_sidecar")
    if sha256(batch_manifest_path) != source_manifest_hash:
        raise SystemExit("source_batch_manifest_changed_during_sidecar")
    if sha256(batch_summary_path) != source_summary_hash:
        raise SystemExit("source_batch_summary_changed_during_sidecar")

    gate = {
        "model_version": EXPECTED_MODEL,
        "status": "blocked_incomplete_candidate_pool_and_economic_inputs",
        "profiles": {
            profile_id: {
                "time_weight": profile.time_weight,
                "money_weight": profile.money_weight,
                "status": "blocked",
                "scenarios": 1000,
                "weighted_rankings_emitted": 0,
            }
            for profile_id, profile in decision.DECISION_PROFILES.items()
        },
        "candidate_pool": {
            "source_status": source_pool_gate,
            "status": "blocked",
            "reason": "source batch does not certify a complete predeclared same-snapshot route alternative pool for v5 decisions",
            "paired_objective_outputs_per_seed_order": 2,
            "paired_orders": 1000,
            "note": "The presence of FASTEST and LOWEST_ENERGY outputs for the same order ID does not alone prove that both candidates were evaluated from one identical routing-time state or exhaust the predeclared candidate set.",
        },
        "economic_inputs": {
            "currency": "EUR",
            "electricity_price_eur_per_kwh": TARIFF_EUR_PER_KWH,
            "electricity_price_basis": TARIFF_BASIS,
            "passenger_fare_schedule": None,
            "passenger_fare_missing_segments": sum(
                row["segment_type"] == "passenger" and row.get("fare_revenue_amount") is None
                for row in combined_ledger
            ),
            "method_api_token_cost": 0.0,
            "method_compute_cost_rate": None,
            "method_compute_cost_null_reason": "local_compute_cost_rate_not_frozen_or_supplied",
            "unknowns_filled_as_zero": False,
        },
        "scorer_status_counts": dict(status_counts),
        "weighted_rankings_emitted": False,
        "runtime_selector_switched": False,
        "model_calls": 0,
        "sumo_reruns": 0,
    }
    gate_path = output_dir / "decision_gate_v5.json"
    write_json(gate_path, gate)

    ledger_path = output_dir / "journey_segments_v5.jsonl"
    method_cost_path = output_dir / "method_costs_v5.jsonl"
    route_economics_path = output_dir / "route_candidate_economics_v5.jsonl"
    decisions_path = output_dir / "time_money_decisions_v5.jsonl"
    manifest = {
        "schema": "porto_r2_paired_route_time_money_segment_v5_sidecar",
        "decision_model_version": EXPECTED_MODEL,
        "sidecar_generator_path": str(Path(__file__).resolve()),
        "sidecar_generator_sha256": sha256(Path(__file__).resolve()),
        "created_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "source_run_dir": str(run_dir),
        "source_batch_manifest_path": str(batch_manifest_path),
        "source_batch_manifest_sha256": source_manifest_hash,
        "source_batch_summary_path": str(batch_summary_path),
        "source_batch_summary_sha256": source_summary_hash,
        "supersedes_previous_sidecar_manifest_path": (
            str(superseded_manifest_path) if superseded_manifest_path else None
        ),
        "supersedes_previous_sidecar_manifest_sha256": (
            sha256(superseded_manifest_path) if superseded_manifest_path else None
        ),
        "supersedes_reason": (
            "strengthen fail-closed money_cost construction so no score can include unresolved method compute cost or an uncertified candidate pool"
            if superseded_manifest_path
            else None
        ),
        "source_runner_sha256": batch_manifest.get("r2_runner_sha256"),
        "source_route_problem_sha256": batch_manifest.get("route_problem_sha256"),
        "decision_module_path": str(decision_path),
        "decision_module_sha256": sha256(decision_path),
        "energy_model_module_path": str(energy_path),
        "energy_model_module_sha256": sha256(energy_path),
        "ev_profile_sha256": EXPECTED_PROFILE_SHA256,
        "source_cell_artifact_sha256": source_hashes,
        "sidecar_source_hashes_reverified_after_read": source_hashes == after_hashes,
        "source_results_modified": False,
        "sidecar_only": True,
        "model_calls": 0,
        "sumo_reruns": 0,
        "runtime_selector_switched": False,
        "electricity_price_eur_per_kwh_scenario": TARIFF_EUR_PER_KWH,
        "electricity_price_basis": TARIFF_BASIS,
        "route_leg_energy_input_scope": "EV v2 motion-trace-reconciled route-leg aggregate; ordered Link energy remains null when source has no per-Link motion attribution",
        "fare_rule": "deadhead revenue known zero; passenger fare recorded once per passenger journey segment and remains null without observed or frozen fare data",
        "method_cost_rule": "API/token usage is zero for this traditional method; measured planning time is retained; compute cost remains null without a frozen local compute rate",
        "batch_integrity": {
            "expected_cells": batch_summary.get("expected_cells"),
            "completed_cells": batch_summary.get("completed_cells"),
            "failed_cells": batch_summary.get("failed_cells"),
            "orders_completed": batch_summary.get("orders_completed"),
            "total_expected_orders": batch_summary.get("total_expected_orders"),
            "model_calls": batch_summary.get("codex_model_calls"),
        },
        "cell_count": len(cell_records),
        "segment_count": len(combined_ledger),
        "route_candidate_count": len(route_economics_rows),
        "paired_order_scenarios": 1000,
        "time_money_profile_decisions": len(decision_rows),
        "scorer_status_counts": dict(status_counts),
        "weighted_rankings_emitted": False,
        "segment_ledger_path": str(ledger_path),
        "segment_ledger_sha256": sha256(ledger_path),
        "method_costs_path": str(method_cost_path),
        "method_costs_sha256": sha256(method_cost_path),
        "route_candidate_economics_path": str(route_economics_path),
        "route_candidate_economics_sha256": sha256(route_economics_path),
        "time_money_decisions_path": str(decisions_path),
        "time_money_decisions_sha256": sha256(decisions_path),
        "decision_gate_path": str(gate_path),
        "decision_gate_sha256": sha256(gate_path),
        "cells": cell_records,
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)
    readback = read_json(manifest_path)
    if readback.get("source_batch_manifest_sha256") != source_manifest_hash:
        raise SystemExit("manifest_readback_source_hash_mismatch")
    if readback.get("segment_count") != len(combined_ledger):
        raise SystemExit("manifest_readback_segment_count_mismatch")
    print(json.dumps({
        "manifest": str(manifest_path),
        "manifest_sha256": sha256(manifest_path),
        "status": gate["status"],
        "cells": len(cell_records),
        "segments": len(combined_ledger),
        "paired_orders": 1000,
        "profile_decisions": len(decision_rows),
        "weighted_rankings_emitted": False,
        "source_results_modified": False,
        "model_calls": 0,
        "sumo_reruns": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
