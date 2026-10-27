#!/usr/bin/env python3
"""Audit v4 dispatch results for decision-function and EV segment-ledger fidelity."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE = Path("outputs/dispatch_layer/nyc_tlc_hvfhv_2024/dispatch_formal_multimonth_v1")
DEFAULT_SEED_ROOT = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_ev2_segment_ledger_v4_20261027/seed_0"
)
METHODS = [
    "D0_greedy", "D1_optimizer", "D2_agent_optimizer", "D5_pure_llm_direct",
    "D6_agent_codex_context_optimizer", "D7_agent_codex_capacity_shield_optimizer",
    "D8_agent_codex_nn_capacity_shield_optimizer",
]
FLEETS = ["F1", "F2", "F4"]
TOL = 1e-8


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def close(a: Any, b: Any) -> bool:
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and math.isclose(
        float(a), float(b), rel_tol=0.0, abs_tol=TOL
    )


def audit_trace_usage(payload: Dict[str, Any], result_path: Path, method: str) -> Dict[str, Any]:
    trace_dir = result_path.parent / "codex_cli_traces" / method
    trace_paths = set()
    for round_audit in payload.get("round_audits", []):
        for field in ("direct_llm_assignment", "codex_context_policy"):
            record = round_audit.get(field)
            trace = record.get("trace_jsonl_path") if isinstance(record, dict) else None
            if isinstance(trace, str) and trace:
                candidate = Path(trace)
                if not candidate.is_absolute():
                    candidate = result_path.parent / candidate
                if candidate.is_file():
                    trace_paths.add(candidate.resolve())
    if not trace_paths and trace_dir.is_dir():
        trace_paths.update(path.resolve() for path in trace_dir.glob("*.jsonl"))
    trace_files = sorted(trace_paths)
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
              "reported_total_tokens": 0}
    completed = failed = usage_turns = malformed_lines = cli_error_events = 0
    rate_limit_error_events = retry_limit_error_events = 0
    for path in trace_files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            event_type = event.get("type")
            if event_type == "turn.completed":
                completed += 1
                usage = event.get("usage")
                if isinstance(usage, dict):
                    usage_turns += 1
                    for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                        value = usage.get(field)
                        if isinstance(value, int) and not isinstance(value, bool):
                            totals[field] += value
                    total = usage.get("total_tokens")
                    if isinstance(total, int) and not isinstance(total, bool):
                        totals["reported_total_tokens"] += total
                    elif isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int):
                        totals["reported_total_tokens"] += usage["input_tokens"] + usage["output_tokens"]
            elif event_type == "turn.failed":
                failed += 1
            elif event_type == "error":
                cli_error_events += 1
                message = event.get("message")
                if isinstance(message, dict):
                    message = json.dumps(message, ensure_ascii=False)
                message = str(message or "").lower()
                if "429" in message or "too many requests" in message:
                    rate_limit_error_events += 1
                if "exceeded retry limit" in message or "retry limit" in message:
                    retry_limit_error_events += 1
    unclassified = max(0, len(trace_files) - completed - failed)
    return {
        "trace_directory": str(trace_dir),
        "trace_file_count": len(trace_files),
        "trace_files": [str(path) for path in trace_files],
        "turn_completed_count": completed,
        "turn_failed_count": failed,
        "unclassified_trace_count": unclassified,
        "usage_available_turn_count": usage_turns,
        "usage_coverage_complete": bool(trace_files) and usage_turns == completed and failed == 0 and unclassified == 0,
        **totals,
        "cli_error_event_count": cli_error_events,
        "http_429_error_event_count": rate_limit_error_events,
        "cli_retry_limit_error_event_count": retry_limit_error_events,
        "malformed_line_count": malformed_lines,
        "interpretation": (
            "Token totals sum only completed turns with raw reported usage. Failed CLI turns are counted separately; "
            "they are not assigned zero tokens or converted to account billing."
        ),
    }


def audit_result(payload: Dict[str, Any], path: Path, expected: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    if payload.get("schema_version") != "dispatch-agent-ev2-segment-ledger-v4":
        errors.append("schema_version_mismatch")
    if payload.get("run_mode") != "complete_simulation" or payload.get("partial_run") is not False:
        errors.append("result_not_complete_simulation")
    for key, value in expected.items():
        if payload.get(key) != value:
            errors.append("{}_mismatch".format(key))

    summary = payload.get("summary") or {}
    decision = payload.get("decision_function") or {}
    contract = decision.get("schema")
    if contract != "nyc_dispatch_decision_contract_v4":
        errors.append("decision_function_contract_missing_or_old")
    if not decision.get("method_steps") or not decision.get("final_assignment_rule"):
        errors.append("decision_function_steps_or_final_rule_missing")

    ledger = payload.get("executed_segment_ledger")
    if not isinstance(ledger, list):
        ledger = []
        errors.append("executed_segment_ledger_missing")
    completed = [row for row in ledger if row.get("execution_status") == "completed"]
    if len(completed) != len(ledger):
        errors.append("noncompleted_segment_in_executed_ledger")

    sums = {
        "total_energy_kwh": sum(float(row.get("signed_net_kwh") or 0.0) for row in completed),
        "total_propulsion_draw_kwh": sum(float(row.get("propulsion_draw_kwh") or 0.0) for row in completed),
        "total_regenerated_kwh": sum(float(row.get("regenerated_kwh") or 0.0) for row in completed),
        "total_auxiliary_kwh": sum(float(row.get("auxiliary_kwh") or 0.0) for row in completed),
        "total_billable_energy_kwh": sum(float(row.get("billable_energy_kwh") or 0.0) for row in completed),
        "total_pickup_net_energy_kwh": sum(
            float(row.get("signed_net_kwh") or 0.0) for row in completed if row.get("segment_type") == "deadhead"
        ),
        "total_ride_net_energy_kwh": sum(
            float(row.get("signed_net_kwh") or 0.0) for row in completed if row.get("segment_type") == "passenger"
        ),
    }
    identity_errors = []
    for index, row in enumerate(completed):
        if not close(row.get("signed_net_kwh"),
                     float(row.get("propulsion_draw_kwh") or 0.0)
                     - float(row.get("regenerated_kwh") or 0.0)
                     + float(row.get("auxiliary_kwh") or 0.0)):
            identity_errors.append(index)
        if not close(row.get("billable_energy_kwh"), max(float(row.get("signed_net_kwh") or 0.0), 0.0)):
            identity_errors.append(index)
        if row.get("electricity_price_per_kwh") is not None or row.get("electricity_cost_amount") is not None:
            errors.append("electricity_cost_imputed_without_frozen_tariff")
        if row.get("money_score_status") != "blocked_missing_frozen_tariff_method_cost_or_comparable_route_pool":
            errors.append("money_score_status_unexpected")
        if row.get("segment_type") == "passenger" and row.get("fare_revenue_amount") is None:
            if row.get("fare_status") != "null_missing_fare_crosswalk_row":
                errors.append("missing_fare_not_explicitly_null")
        if row.get("segment_type") == "deadhead" and row.get("fare_revenue_amount") != 0.0:
            errors.append("deadhead_fare_not_known_zero")
    if identity_errors:
        errors.append("segment_energy_identity_mismatch")
    for key, value in sums.items():
        if not close(summary.get(key), value):
            errors.append("{}_does_not_reconcile_to_segment_ledger".format(key))

    round_audits = payload.get("round_audits") or []
    invalid_rounds = sum(1 for row in round_audits if row.get("validator_status") != "valid")
    hard_errors = sum(len(row.get("hard_constraint_errors") or []) for row in round_audits)
    one_to_one_errors = sum(row.get("one_to_one_valid") is False for row in round_audits)
    if not round_audits or invalid_rounds or hard_errors or one_to_one_errors:
        errors.append("round_validator_failure_or_missing_audits")

    costs = payload.get("method_costs") or {}
    money = payload.get("money_accounting") or {}
    trace_usage = audit_trace_usage(payload, path, payload.get("method_name", ""))
    if money.get("money_score_applied_to_assignment") is not False:
        errors.append("money_score_should_not_change_dispatch_assignment")
    if money.get("electricity_price_per_kwh") is not None:
        errors.append("electricity_tariff_should_remain_null")
    if costs.get("accounting_scope") != "method_level_once":
        errors.append("method_cost_accounting_scope_mismatch")

    return {
        "case_id": payload.get("case_id"),
        "fleet_id": payload.get("fleet_id"),
        "method": payload.get("method_name"),
        "path": str(path),
        "sha256": sha256(path),
        "complete": not errors,
        "errors": sorted(set(errors)),
        "round_count": len(round_audits),
        "invalid_round_count": invalid_rounds,
        "segment_count": len(completed),
        "deadhead_segment_count": sum(row.get("segment_type") == "deadhead" for row in completed),
        "passenger_segment_count": sum(row.get("segment_type") == "passenger" for row in completed),
        "summary_simulated_energy_kwh": summary.get("total_energy_kwh"),
        "summary_assigned_candidate_energy_kwh": summary.get("total_assigned_candidate_energy_kwh"),
        "ledger_simulated_energy_kwh": sums["total_energy_kwh"],
        "decision_function_contract": contract,
        "decision_function_method_steps": decision.get("method_steps"),
        "llm_invocations": summary.get("total_codex_cli_invocations"),
        "llm_input_tokens": summary.get("total_llm_input_tokens"),
        "llm_output_tokens": summary.get("total_llm_output_tokens"),
        "llm_total_tokens": summary.get("total_llm_tokens"),
        "raw_cli_trace_usage": trace_usage,
        "method_costs": costs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=str(DEFAULT_PACKAGE))
    parser.add_argument("--seed-root", default=str(DEFAULT_SEED_ROOT))
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=METHODS,
                        help="method subset to audit; by default audit the complete seven-method matrix")
    parser.add_argument("--output", default=None, help="audit JSON output path")
    args = parser.parse_args()

    package_dir = ROOT / args.package
    seed_root = ROOT / args.seed_root
    output = ROOT / args.output if args.output else seed_root / "ev2_segment_ledger_audit.json"
    manifest = load_json(package_dir / "benchmark_manifest.json")
    if not manifest:
        raise FileNotFoundError(package_dir / "benchmark_manifest.json")
    case_ids = manifest.get("evaluation_case_ids", [])
    selected_methods = list(args.methods)
    results = []
    missing = []
    for case_id in case_ids:
        for fleet in FLEETS:
            for method in selected_methods:
                path = seed_root / "evaluation" / case_id / fleet / (method + "_seed_0.json")
                payload = load_json(path)
                if payload is None:
                    missing.append({"case_id": case_id, "fleet_id": fleet, "method": method, "path": str(path)})
                    continue
                results.append(audit_result(payload, path, {"case_id": case_id, "fleet_id": fleet, "method_name": method}))

    expected_count = len(case_ids) * len(FLEETS) * len(selected_methods)
    failed = [row for row in results if not row["complete"]]
    usage_by_method: Dict[str, Dict[str, Any]] = {}
    for row in results:
        method = row["method"]
        trace = row["raw_cli_trace_usage"]
        summary = usage_by_method.setdefault(method, {
            "result_count": 0, "calls_reported": 0, "trace_file_count": 0, "turn_completed_count": 0,
            "turn_failed_count": 0, "unclassified_trace_count": 0, "usage_available_turn_count": 0,
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "reported_total_tokens": 0, "cli_error_event_count": 0,
            "http_429_error_event_count": 0, "cli_retry_limit_error_event_count": 0,
            "malformed_line_count": 0,
        })
        summary["result_count"] += 1
        summary["calls_reported"] += int(row.get("llm_invocations") or 0)
        for key in ("trace_file_count", "turn_completed_count", "turn_failed_count",
                    "unclassified_trace_count", "usage_available_turn_count", "input_tokens", "cached_input_tokens",
                    "output_tokens", "reported_total_tokens", "cli_error_event_count",
                    "http_429_error_event_count", "cli_retry_limit_error_event_count",
                    "malformed_line_count"):
            summary[key] += trace[key]
    for summary in usage_by_method.values():
        summary["trace_count_matches_calls_reported"] = summary["trace_file_count"] == summary["calls_reported"]
        summary["usage_coverage_complete"] = (
            summary["trace_count_matches_calls_reported"]
            and summary["turn_completed_count"] + summary["turn_failed_count"] == summary["trace_file_count"]
            and summary["turn_completed_count"] == summary["usage_available_turn_count"]
            and summary["turn_failed_count"] == 0 and summary["unclassified_trace_count"] == 0
        )
    overall = {
        "schema_version": "nyc-tlc-dispatch-ev2-segment-ledger-audit-v1",
        "audited_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "package_manifest_sha256": sha256(package_dir / "benchmark_manifest.json"),
        "experiment_manifest_sha256": sha256(seed_root / "experiment_manifest.json")
            if (seed_root / "experiment_manifest.json").is_file() else None,
        "seed": 0,
        "audited_methods": selected_methods,
        "expected_method_runs": expected_count,
        "present_method_runs": len(results),
        "missing_method_runs": missing,
        "valid_method_runs": len(results) - len(failed),
        "invalid_method_runs": failed,
        "all_runs_complete_and_valid": len(results) == expected_count and not missing and not failed,
        "raw_cli_trace_usage_by_method": usage_by_method,
        "summary_energy_reconciliation_tolerance_kwh": TOL,
        "runs": results,
        "interpretation_limits": [
            "The energy ledger records simulated EV v2 battery energy, not measured vehicle telemetry.",
            "NYC pickup deadhead energy is a synthetic centroid start-stop proxy; grade is unknown and treated as flat.",
            "The observed fare crosswalk is partial; missing fares remain null. Electricity tariff and time-money score remain blocked.",
            "Codex CLI token counts are usage records and rate-card equivalents are estimates; actual account billing is unknown.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(overall, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "audit": str(output),
        "expected_method_runs": expected_count,
        "present_method_runs": len(results),
        "valid_method_runs": len(results) - len(failed),
        "missing_method_runs": len(missing),
        "invalid_method_runs": len(failed),
        "all_runs_complete_and_valid": overall["all_runs_complete_and_valid"],
    }, ensure_ascii=False, indent=2))
    return 0 if overall["all_runs_complete_and_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
