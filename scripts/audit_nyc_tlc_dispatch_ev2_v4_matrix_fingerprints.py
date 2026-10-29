#!/usr/bin/env python3
"""Audit frozen inputs, scenario fingerprints, EV profile, and method contracts
for the completed cells in an official NYC EV2 v4 audit snapshot.

This is a read-only audit. It never opens or rewrites experiment result files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE = Path("outputs/dispatch_layer/nyc_tlc_hvfhv_2024/dispatch_formal_multimonth_v1")
DEFAULT_SEED_ROOT = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_ev2_segment_ledger_v4_20261027/seed_0"
)
DEFAULT_AUDIT = DEFAULT_SEED_ROOT / "audit_snapshots/ev2_fullmatrix_audit_20261029T1107CST.json"
FARE_DIR = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_multimonth_energy_v2_offline_20261026/time_money_fare_reconciliation_v1"
)
FARE_CROSSWALK = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/dispatch_ev2_proxy_v3_inputs/"
    "nyc_tlc_observed_fares_partial_from_audited_sidecar_v1.json"
)
PROFILE_SHA256 = "b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee"
LEXICOGRAPHIC_PRIORITY = [
    "maximize_served_orders",
    "minimize_total_pickup_wait",
    "minimize_total_empty_distance",
    "minimize_total_ev_energy",
]
OPTIMIZER_METHODS = {
    "D1_optimizer",
    "D2_agent_optimizer",
    "D6_agent_codex_context_optimizer",
    "D7_agent_codex_capacity_shield_optimizer",
    "D8_agent_codex_nn_capacity_shield_optimizer",
}
METHODS = [
    "D0_greedy", "D1_optimizer", "D2_agent_optimizer", "D5_pure_llm_direct",
    "D6_agent_codex_context_optimizer", "D7_agent_codex_capacity_shield_optimizer",
    "D8_agent_codex_nn_capacity_shield_optimizer",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def expected_inputs(package: Path, package_manifest: Dict[str, Any], experiment_manifest: Dict[str, Any], case_id: str) -> Dict[str, Optional[str]]:
    case = package_manifest["cases"][case_id]
    files = case["files"]
    fare_manifest = FARE_DIR / "manifest.json"
    fare_parquet = FARE_DIR / "orders_observed_fares.parquet"
    return {
        "scenario_json": files["scenario.json"]["sha256"],
        "orders_json": files["orders.json"]["sha256"],
        "zone_od_cost_matrix": files["zone_od_cost_matrix.csv.gz"]["sha256"],
        "pickup_cost_policy": package_manifest["pickup_cost_policy"]["sha256"],
        "zone_centroids": package_manifest["zone_centroids"]["sha256"],
        "observed_fare_crosswalk": experiment_manifest["observed_fare_crosswalk"]["sha256"],
        "available_fare_crosswalk_parquet": sha256(ROOT / fare_parquet) if fare_parquet.is_file() else None,
        "available_fare_reconciliation_manifest": sha256(ROOT / fare_manifest) if fare_manifest.is_file() else None,
    }


def contract_errors(method: str, decision: Dict[str, Any]) -> list[str]:
    errors = []
    if decision.get("schema") != "nyc_dispatch_decision_contract_v4":
        errors.append("decision_contract_not_v4")
    if method == "D0_greedy":
        if decision.get("final_assignment_rule") != "deterministic_greedy_v1":
            errors.append("D0_assignment_rule_mismatch")
        if decision.get("energy_dependency") != "not_used_by_the_assignment_rule":
            errors.append("D0_energy_must_not_enter_assignment")
        if decision.get("global_optimizer"):
            errors.append("D0_unexpected_global_optimizer")
    elif method == "D5_pure_llm_direct":
        steps = " ".join(str(x) for x in decision.get("method_steps", []))
        if "EV v2 signed-net energy" not in steps:
            errors.append("D5_EV2_not_in_model_input_contract")
        if "No global mathematical optimizer is applied" not in steps:
            errors.append("D5_global_optimizer_contract_mismatch")
        if decision.get("global_optimizer"):
            errors.append("D5_unexpected_global_optimizer")
    elif method in OPTIMIZER_METHODS:
        if decision.get("final_assignment_rule") != "global_min_cost_flow_lexicographic_v2":
            errors.append("optimizer_assignment_rule_mismatch")
        if decision.get("energy_dependency") != "pickup_and_ride_ev_v2_signed_net_energy_sum_is_the_last_global_optimizer_objective_tier":
            errors.append("optimizer_EV2_not_last_objective_tier")
        optimizer = decision.get("global_optimizer") or {}
        if optimizer.get("priority_order") != LEXICOGRAPHIC_PRIORITY:
            errors.append("optimizer_priority_order_mismatch")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--seed-root", type=Path, default=DEFAULT_SEED_ROOT)
    parser.add_argument("--audit-json", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    package = (ROOT / args.package).resolve()
    seed_root = (ROOT / args.seed_root).resolve()
    audit_path = (ROOT / args.audit_json).resolve()
    output_path = (ROOT / args.output).resolve()
    if output_path.exists():
        raise SystemExit(f"refusing_to_overwrite:{output_path}")

    package_manifest_path = package / "benchmark_manifest.json"
    experiment_manifest_path = seed_root / "experiment_manifest.json"
    package_manifest = read_json(package_manifest_path)
    experiment_manifest = read_json(experiment_manifest_path)
    audit_raw = audit_path.read_bytes()
    audit = json.loads(audit_raw)
    if audit.get("schema_version") != "nyc-tlc-dispatch-ev2-segment-ledger-audit-v1":
        raise SystemExit("unexpected_ev2_audit_schema")
    if audit.get("package_manifest_sha256") != sha256(package_manifest_path):
        raise SystemExit("package_manifest_hash_mismatch")

    run_entries = []
    errors = []
    groups: Dict[tuple[str, str], list[Dict[str, Any]]] = defaultdict(list)
    profile_counts: Counter[str] = Counter()
    method_counts: Counter[str] = Counter()

    for audited in audit.get("runs", []):
        path = Path(audited["path"])
        result = read_json(path)
        method = str(result.get("method_name"))
        case_id = str(result.get("case_id"))
        fleet_id = str(result.get("fleet_id"))
        run_errors = []
        if sha256(path) != audited.get("sha256"):
            run_errors.append("result_sha256_mismatch_with_official_audit")
        if result.get("case_id") != audited.get("case_id") or result.get("fleet_id") != audited.get("fleet_id") or method != audited.get("method"):
            run_errors.append("result_identity_mismatch_with_official_audit")
        case_manifest = package_manifest.get("cases", {}).get(case_id)
        if not case_manifest:
            run_errors.append("case_missing_from_frozen_package")
            expected = {}
        else:
            expected = expected_inputs(package, package_manifest, experiment_manifest, case_id)
        actual_inputs = result.get("input_hashes") or {}
        mismatches = {key: {"expected": expected.get(key), "actual": actual_inputs.get(key)}
                      for key in expected if actual_inputs.get(key) != expected.get(key)}
        if mismatches:
            run_errors.append("input_hash_mismatch")
        if result.get("scenario_id") != case_id:
            run_errors.append("scenario_id_mismatch")
        if result.get("partial_run") is not False:
            run_errors.append("partial_run_not_false")
        if not result.get("scenario_fingerprint_sha256"):
            run_errors.append("scenario_fingerprint_missing")
        decision = result.get("decision_function") or {}
        method_errors = contract_errors(method, decision)
        run_errors.extend(method_errors)
        energy = result.get("energy_accounting") or {}
        profile_sha = energy.get("profile_sha256")
        profile_counts[str(profile_sha)] += 1
        if profile_sha != PROFILE_SHA256:
            run_errors.append("energy_profile_sha_mismatch")
        rounds = result.get("round_audits") or []
        invalid_rounds = sum(row.get("validator_status") != "valid" for row in rounds)
        hard_errors = sum(len(row.get("hard_constraint_errors") or []) for row in rounds)
        one_to_one_errors = sum(row.get("one_to_one_valid") is False for row in rounds)
        if not rounds or invalid_rounds or hard_errors or one_to_one_errors:
            run_errors.append("round_validator_failure_or_missing")

        run = {
            "case_id": case_id,
            "fleet_id": fleet_id,
            "method": method,
            "result_path": str(path),
            "result_sha256": audited.get("sha256"),
            "scenario_fingerprint_sha256": result.get("scenario_fingerprint_sha256"),
            "input_hashes": actual_inputs,
            "input_hash_mismatches": mismatches,
            "profile_sha256": profile_sha,
            "decision_contract_schema": decision.get("schema"),
            "final_assignment_rule": decision.get("final_assignment_rule"),
            "energy_dependency": decision.get("energy_dependency"),
            "optimizer_priority_order": (decision.get("global_optimizer") or {}).get("priority_order"),
            "round_count": len(rounds),
            "invalid_round_count": invalid_rounds,
            "hard_constraint_error_count": hard_errors,
            "one_to_one_error_count": one_to_one_errors,
            "errors": sorted(set(run_errors)),
        }
        run_entries.append(run)
        groups[(case_id, fleet_id)].append(run)
        method_counts[method] += 1
        if run["errors"]:
            errors.append({"case_id": case_id, "fleet_id": fleet_id, "method": method, "errors": run["errors"]})

    cell_entries = []
    fingerprint_cell_mismatches = 0
    input_cell_mismatches = 0
    for (case_id, fleet_id), cell_runs in sorted(groups.items()):
        fps = sorted({row["scenario_fingerprint_sha256"] for row in cell_runs})
        inputs = {json.dumps(row["input_hashes"], sort_keys=True) for row in cell_runs}
        cell_errors = []
        if len(fps) != 1:
            fingerprint_cell_mismatches += 1
            cell_errors.append("scenario_fingerprint_not_shared_across_methods")
        if len(inputs) != 1:
            input_cell_mismatches += 1
            cell_errors.append("input_hashes_not_shared_across_methods")
        cell_entries.append({
            "case_id": case_id,
            "fleet_id": fleet_id,
            "present_method_count": len(cell_runs),
            "present_methods": sorted(row["method"] for row in cell_runs),
            "scenario_fingerprint_sha256": fps[0] if len(fps) == 1 else fps,
            "shared_input_hashes": len(inputs) == 1,
            "errors": cell_errors,
        })
        if cell_errors:
            errors.append({"case_id": case_id, "fleet_id": fleet_id, "errors": cell_errors})

    output = {
        "schema_version": "nyc-tlc-dispatch-ev2-v4-input-fingerprint-audit-v1",
        "audited_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source_official_audit_path": str(audit_path),
        "source_official_audit_sha256": hashlib.sha256(audit_raw).hexdigest(),
        "package_manifest_sha256": sha256(package_manifest_path),
        "experiment_manifest_sha256": sha256(experiment_manifest_path),
        "seed": audit.get("seed"),
        "expected_methods": METHODS,
        "audited_result_count": len(run_entries),
        "method_result_counts": dict(sorted(method_counts.items())),
        "scenario_fingerprint_cell_mismatch_count": fingerprint_cell_mismatches,
        "input_hash_cell_mismatch_count": input_cell_mismatches,
        "profile_sha_counts": dict(sorted(profile_counts.items())),
        "per_run_errors": errors,
        "all_present_results_match_frozen_inputs_and_contract": not errors,
        "interpretation_limits": [
            "This audit proves each present complete result matches frozen package inputs and shared cell fingerprints; missing method cells remain missing.",
            "NYC demand is mapped to a zone-level OD cost proxy; pickup deadhead uses a synthetic centroid proxy, not a street network.",
            "EV2 output is simulated battery energy, not vehicle telemetry; no ranking or full experiment effect is inferred here.",
        ],
        "cells": cell_entries,
        "runs": run_entries,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "audit": str(output_path),
        "audited_result_count": len(run_entries),
        "scenario_fingerprint_cell_mismatch_count": fingerprint_cell_mismatches,
        "input_hash_cell_mismatch_count": input_cell_mismatches,
        "per_run_or_cell_error_count": len(errors),
        "all_present_results_match_frozen_inputs_and_contract": not errors,
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
