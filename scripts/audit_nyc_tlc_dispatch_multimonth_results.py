#!/usr/bin/env python3
"""Audit the frozen multi-month dispatch matrix and produce paired summaries."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
import random
import re
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE = Path("outputs/dispatch_layer/nyc_tlc_hvfhv_2024/dispatch_formal_multimonth_v1")
DEFAULT_RESULTS = Path("outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_v1")
METHODS = [
    "D0_greedy", "D1_optimizer", "D2_agent_optimizer", "D5_pure_llm_direct",
    "D6_agent_codex_context_optimizer", "D7_agent_codex_capacity_shield_optimizer",
    "D8_agent_codex_nn_capacity_shield_optimizer",
]
FLEETS = ["F1", "F2", "F4"]
METRICS = [
    "served_orders", "service_rate", "average_pickup_wait_sec", "p95_pickup_wait_sec",
    "unserved_orders", "total_empty_distance_m", "total_energy_kwh",
    "total_dispatch_rounds", "candidate_count_before_filter", "candidate_count_after_filter",
    "fallback_count", "commitment_violations", "optimizer_time_ms", "total_decision_time_ms",
    "total_llm_calls", "total_llm_input_tokens", "total_llm_cached_input_tokens",
    "total_llm_output_tokens", "total_llm_tokens",
]
PRIMARY_METRICS = ["service_rate", "average_pickup_wait_sec", "p95_pickup_wait_sec"]
BASELINES = ["D0_greedy", "D1_optimizer", "D2_agent_optimizer", "D5_pure_llm_direct",
            "D6_agent_codex_context_optimizer", "D7_agent_codex_capacity_shield_optimizer"]
TOOL_ITEM_TYPES = {
    "browser_call", "command_execution", "computer_call", "file_change", "function_call",
    "image_generation_call", "local_shell_call", "mcp_tool_call", "patch_apply_end",
    "shell_command", "tool_call", "web_search", "web_search_call",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: List[Dict[str, Any]], columns: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def is_tool_item_event(item: Dict[str, Any]) -> bool:
    item_type = item.get("type")
    if not isinstance(item_type, str):
        return False
    return item_type in TOOL_ITEM_TYPES or item_type.endswith("_call") or item_type.endswith("_tool_call")


def case_metadata(case_id: str) -> Dict[str, Any]:
    match = re.match(r"EV_(\d{4})(\d{2})(\d{2})_\d{6}_q(\d+)$", case_id)
    if not match:
        return {"case_id": case_id, "service_date": None, "month": None, "stratum": None}
    year, month, day, q = match.groups()
    return {"case_id": case_id, "service_date": "{}-{}-{}".format(year, month, day),
            "month": int(month), "stratum": "q" + q}


def policy_records(payload: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    output = []
    for audit in payload.get("round_audits", []):
        record = audit.get(key)
        if isinstance(record, dict):
            output.append(record)
    return output


def policy_made_live_invocation(record: Dict[str, Any]) -> bool:
    if record.get("replayed_from_existing_call") is True:
        return False
    return any(record.get(key) is True for key in ("request_made", "invocation_made", "new_invocation_made"))


def run_audit(payload: Optional[Dict[str, Any]], path: Path, method: str,
              expected_seed: Optional[int] = None) -> Dict[str, Any]:
    if not payload:
        return {"present": path.exists(), "complete": False, "method": method,
                "path": str(path), "reason": "missing_or_invalid_json"}
    audits = payload.get("round_audits", [])
    invalid_rounds = sum(audit.get("validator_status") != "valid" for audit in audits)
    hard_errors = sum(len(audit.get("hard_constraint_errors", [])) for audit in audits)
    one_to_one_failures = sum(audit.get("one_to_one_valid") is False for audit in audits)
    direct_records = policy_records(payload, "direct_llm_assignment")
    context_records = policy_records(payload, "codex_context_policy")
    ranker_records = policy_records(payload, "neural_candidate_ranker")
    shield_records = policy_records(payload, "max_cardinality_shield")
    complete = payload.get("partial_run") is False and payload.get("run_mode") == "complete_simulation"
    policies = direct_records + context_records
    accepted_policies = sum(record.get("status") == "accepted" for record in policies)
    live_invocation_records = [record for record in policies if policy_made_live_invocation(record)]
    replayed_records = [record for record in policies if record.get("replayed_from_existing_call") is True]
    policy_attempt_or_replay_records = [record for record in policies if (
        policy_made_live_invocation(record) or record.get("replayed_from_existing_call") is True
    )]
    accepted_live_invocations = sum(record.get("status") == "accepted" for record in live_invocation_records)
    nonaccepted_live_invocations = len(live_invocation_records) - accepted_live_invocations
    accepted_replays = sum(record.get("status") == "accepted" for record in replayed_records)
    nonaccepted_replays = len(replayed_records) - accepted_replays
    no_invocation_records = [record for record in policies if not (
        policy_made_live_invocation(record) or record.get("replayed_from_existing_call") is True
    )]
    no_feasible_candidate_records = sum(record.get("status") == "no_feasible_candidates" for record in no_invocation_records)
    replayed = len(replayed_records)
    new_invocations = len(live_invocation_records)
    recorded_seed = payload.get("summary", {}).get("seed", payload.get("config", {}).get("seed"))
    return {
        "present": True,
        "complete": complete,
        "method": method,
        "recorded_seed": recorded_seed,
        "seed_match": expected_seed is None or recorded_seed == expected_seed,
        "path": str(path),
        "sha256": sha256(path),
        "scenario_fingerprint_sha256": payload.get("scenario_fingerprint_sha256"),
        "input_hashes": payload.get("input_hashes"),
        "round_audit_count": len(audits),
        "validator_invalid_rounds": invalid_rounds,
        "hard_constraint_error_count": hard_errors,
        "one_to_one_failure_count": one_to_one_failures,
        "summary_commitment_violations": payload.get("summary", {}).get("commitment_violations"),
        "summary_llm_calls": payload.get("summary", {}).get("total_llm_calls"),
        "policy_record_count": len(policies),
        "accepted_policy_count": accepted_policies,
        "replayed_policy_count": replayed,
        "policy_new_invocation_records": new_invocations,
        "policy_attempt_or_replay_record_count": len(policy_attempt_or_replay_records),
        "accepted_live_invocation_count": accepted_live_invocations,
        "nonaccepted_live_invocation_count": nonaccepted_live_invocations,
        "accepted_replay_record_count": accepted_replays,
        "nonaccepted_replay_record_count": nonaccepted_replays,
        "no_invocation_policy_record_count": len(no_invocation_records),
        "no_feasible_candidate_round_count": no_feasible_candidate_records,
        "live_invocation_acceptance_rate": (accepted_live_invocations / len(live_invocation_records)
                                             if live_invocation_records else None),
        "policy_statuses": [record.get("status") for record in policies],
        "policy_prompt_hashes": [record.get("prompt_sha256") for record in policies if record.get("prompt_sha256")],
        "ranker_record_count": len(ranker_records),
        "shield_record_count": len(shield_records),
        "validators_pass": invalid_rounds == 0 and hard_errors == 0 and one_to_one_failures == 0,
    }


def percentile(values: List[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    low = int(math.floor(index))
    high = int(math.ceil(index))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def cluster_effects(by_date_seed: Dict[str, Dict[int, List[float]]]) -> List[float]:
    effects = []
    for date in sorted(by_date_seed):
        seed_effects = [sum(values) / len(values)
                        for values in by_date_seed[date].values() if values]
        if seed_effects:
            effects.append(sum(seed_effects) / len(seed_effects))
    return effects


def bootstrap_interval(
    by_date_seed: Dict[str, Dict[int, List[float]]], *, seed: int, iterations: int = 10000
) -> Tuple[float, float]:
    dates = sorted(by_date_seed)
    seeds = sorted({seed_id for per_seed in by_date_seed.values() for seed_id in per_seed})
    if not dates or not seeds:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        sampled_dates = [rng.choice(dates) for _ in dates]
        sampled_seeds = [rng.choice(seeds) for _ in seeds]
        date_effects = []
        for date in sampled_dates:
            seed_effects = []
            for seed_id in sampled_seeds:
                values = by_date_seed.get(date, {}).get(seed_id, [])
                if values:
                    seed_effects.append(sum(values) / len(values))
            if seed_effects:
                date_effects.append(sum(seed_effects) / len(seed_effects))
        if date_effects:
            estimates.append(sum(date_effects) / len(date_effects))
    return percentile(estimates, 0.025), percentile(estimates, 0.975)


def parse_usage_traces(evaluation_dirs: Dict[int, Path]) -> Dict[str, Any]:
    totals = {key: 0 for key in ["turn_started_count", "turn_completed_count", "input_tokens",
                                  "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
                                  "tool_event_count", "non_tool_notice_item_count", "cli_error_event_count",
                                  "reconnect_notice_count", "malformed_line_count"]}
    seed_totals = {str(seed): dict(totals) for seed in evaluation_dirs}
    seed_trace_counts = {str(seed): 0 for seed in evaluation_dirs}
    records = []
    trace_paths = sorted(
        (seed, path) for seed, evaluation_dir in evaluation_dirs.items()
        if evaluation_dir.exists() for path in evaluation_dir.rglob("*.jsonl")
    )
    for seed, path in trace_paths:
        seed_key = str(seed)
        before_seed = dict(seed_totals[seed_key])
        malformed = []
        event_types = []
        item_types: Counter = Counter()
        trace_tool_event_count = 0
        trace_non_tool_notice_item_count = 0
        trace_cli_error_event_count = 0
        trace_reconnect_notice_count = 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            records.append({"path": str(path), "error": type(error).__name__})
            continue
        for line_no, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                malformed.append(line_no)
                continue
            event_type = item.get("type")
            if event_type:
                event_types.append(event_type)
            if event_type == "error":
                trace_cli_error_event_count += 1
                totals["cli_error_event_count"] += 1
                seed_totals[seed_key]["cli_error_event_count"] += 1
                if "reconnect" in str(item.get("message", "")).lower():
                    trace_reconnect_notice_count += 1
                    totals["reconnect_notice_count"] += 1
                    seed_totals[seed_key]["reconnect_notice_count"] += 1
            if event_type == "turn.started":
                totals["turn_started_count"] += 1
                seed_totals[seed_key]["turn_started_count"] += 1
            elif event_type == "turn.completed":
                totals["turn_completed_count"] += 1
                seed_totals[seed_key]["turn_completed_count"] += 1
                usage = item.get("usage", {})
                if isinstance(usage, dict):
                    for field, target in (("input_tokens", "input_tokens"),
                                          ("cached_input_tokens", "cached_input_tokens"),
                                          ("output_tokens", "output_tokens"),
                                          ("reasoning_output_tokens", "reasoning_tokens"),
                                          ("total_tokens", "total_tokens")):
                        value = usage.get(field)
                        if isinstance(value, int):
                            totals[target] += value
                            seed_totals[seed_key][target] += value
                    if "total_tokens" not in usage and isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int):
                        fallback_total = usage["input_tokens"] + usage["output_tokens"]
                        totals["total_tokens"] += fallback_total
                        seed_totals[seed_key]["total_tokens"] += fallback_total
            payload_item = item.get("item")
            if isinstance(payload_item, dict):
                item_type = str(payload_item.get("type", "<missing>"))
                item_types[item_type] += 1
                if is_tool_item_event(payload_item):
                    trace_tool_event_count += 1
                    totals["tool_event_count"] += 1
                    seed_totals[seed_key]["tool_event_count"] += 1
                elif item_type not in {"agent_message", "reasoning", "plan"}:
                    trace_non_tool_notice_item_count += 1
                    totals["non_tool_notice_item_count"] += 1
                    seed_totals[seed_key]["non_tool_notice_item_count"] += 1
        totals["malformed_line_count"] += len(malformed)
        seed_totals[seed_key]["malformed_line_count"] += len(malformed)
        seed_trace_counts[seed_key] += 1
        records.append({"seed": seed, "path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size,
                        "line_count": len(lines), "event_types": event_types,
                        "item_type_counts": dict(item_types), "tool_event_count": trace_tool_event_count,
                        "non_tool_notice_item_count": trace_non_tool_notice_item_count,
                        "cli_error_event_count": trace_cli_error_event_count,
                        "reconnect_notice_count": trace_reconnect_notice_count,
                        "malformed_line_numbers": malformed,
                        "turn_started_count": seed_totals[seed_key]["turn_started_count"] - before_seed["turn_started_count"],
                        "turn_completed_count": seed_totals[seed_key]["turn_completed_count"] - before_seed["turn_completed_count"]})
    by_seed = {seed: {"unique_trace_file_count": seed_trace_counts[seed], "totals": values}
               for seed, values in seed_totals.items()}
    return {"unique_trace_file_count": len(trace_paths), "totals": totals, "by_seed": by_seed, "traces": records,
            "interpretation": "Counts unique JSONL files physically present under the evaluation output. Tool events count classified tool-call item types; other non-message items and top-level CLI errors are reported separately. Replay methods are not double-counted unless they created a duplicate trace file."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=str(DEFAULT_PACKAGE))
    parser.add_argument("--results", default=str(DEFAULT_RESULTS))
    parser.add_argument("--seed-results", nargs="*", default=None,
                        help="optional input roots as SEED=PATH pairs; --results remains the analysis output directory")
    parser.add_argument("--iterations", type=int, default=10000)
    args = parser.parse_args()
    package_dir = ROOT / args.package
    results_dir = ROOT / args.results
    if args.seed_results is None:
        seed_roots = {0: results_dir}
    else:
        seed_roots = {}
        for item in args.seed_results:
            try:
                seed_text, path_text = item.split("=", 1)
                seed = int(seed_text)
            except (ValueError, TypeError):
                parser.error("--seed-results entries must be SEED=PATH")
            if seed in seed_roots:
                parser.error("each seed may be listed only once")
            seed_roots[seed] = ROOT / path_text
        if not seed_roots:
            parser.error("--seed-results requires at least one SEED=PATH entry")
    evaluation_dirs = {seed: root / "evaluation" for seed, root in seed_roots.items()}
    package_manifest = read_json(package_dir / "benchmark_manifest.json")
    if not package_manifest:
        raise FileNotFoundError(package_dir / "benchmark_manifest.json")

    run_rows: List[Dict[str, Any]] = []
    paired_rows: List[Dict[str, Any]] = []
    cells: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    cell_audits: List[Dict[str, Any]] = []
    missing = []
    for seed, evaluation_dir in sorted(evaluation_dirs.items()):
      for case_id in package_manifest.get("evaluation_case_ids", []):
        meta = case_metadata(case_id)
        for fleet in FLEETS:
            payloads: Dict[str, Optional[Dict[str, Any]]] = {}
            audits: Dict[str, Dict[str, Any]] = {}
            for method in METHODS:
                path = evaluation_dir / case_id / fleet / (method + "_seed_" + str(seed) + ".json")
                payload = read_json(path)
                payloads[method] = payload
                audit = run_audit(payload, path, method, expected_seed=seed)
                audit.update(meta)
                audit["fleet_id"] = fleet
                audit["seed"] = seed
                audits[method] = audit
                cell_audits.append(audit)
                if not payload or not audit.get("complete"):
                    missing.append({"case_id": case_id, "fleet_id": fleet, "seed": seed, "method": method,
                                    "path": str(path), "present": bool(payload),
                                    "complete": audit.get("complete", False)})
                if payload:
                    summary = payload.get("summary", {})
                    row = {**meta, "fleet_id": fleet, "seed": seed, "method": method,
                           "result_path": str(path), "result_sha256": audit.get("sha256"),
                           "complete": audit.get("complete"), "validator_invalid_rounds": audit.get("validator_invalid_rounds"),
                           "hard_constraint_error_count": audit.get("hard_constraint_error_count"),
                           "policy_record_count": audit.get("policy_record_count"),
                           "policy_statuses": ";".join(map(str, audit.get("policy_statuses", []))),
                           "accepted_policy_count": audit.get("accepted_policy_count"),
                           "replayed_policy_count": audit.get("replayed_policy_count"),
                           "policy_new_invocation_records": audit.get("policy_new_invocation_records"),
                           "policy_attempt_or_replay_record_count": audit.get("policy_attempt_or_replay_record_count"),
                           "accepted_live_invocation_count": audit.get("accepted_live_invocation_count"),
                           "nonaccepted_live_invocation_count": audit.get("nonaccepted_live_invocation_count"),
                           "accepted_replay_record_count": audit.get("accepted_replay_record_count"),
                           "nonaccepted_replay_record_count": audit.get("nonaccepted_replay_record_count"),
                           "no_invocation_policy_record_count": audit.get("no_invocation_policy_record_count"),
                           "no_feasible_candidate_round_count": audit.get("no_feasible_candidate_round_count"),
                           "live_invocation_acceptance_rate": audit.get("live_invocation_acceptance_rate")}
                    row.update({metric: summary.get(metric) for metric in METRICS})
                    run_rows.append(row)
            fingerprints = {method: audit.get("scenario_fingerprint_sha256") for method, audit in audits.items()
                            if audit.get("complete")}
            input_hashes = {method: json.dumps(audit.get("input_hashes"), sort_keys=True)
                            for method, audit in audits.items() if audit.get("complete")}
            same_fp = len(fingerprints) == len(METHODS) and len(set(fingerprints.values())) == 1
            same_hashes = len(input_hashes) == len(METHODS) and len(set(input_hashes.values())) == 1
            validators = all(audits[method].get("validators_pass") for method in METHODS)
            complete = all(audits[method].get("complete") for method in METHODS)
            seed_matches = all(audits[method].get("seed_match", False) for method in METHODS)
            d6 = audits["D6_agent_codex_context_optimizer"]
            d7 = audits["D7_agent_codex_capacity_shield_optimizer"]
            d8 = audits["D8_agent_codex_nn_capacity_shield_optimizer"]
            d8_accepted = d8.get("accepted_policy_count", 0) > 0
            prompt_replay_valid = (
                bool(d6.get("policy_prompt_hashes")) and
                d6.get("policy_prompt_hashes") == d7.get("policy_prompt_hashes") and
                d7.get("replayed_policy_count", 0) > 0 and
                d7.get("policy_new_invocation_records", 0) == 0 and
                (not d8_accepted or (
                    d6.get("policy_prompt_hashes") == d8.get("policy_prompt_hashes") and
                    d6.get("replayed_policy_count", 0) > 0 and
                    d6.get("policy_new_invocation_records", 0) == 0
                ))
            )
            d8_policy_accepted = d8.get("accepted_policy_count", 0) > 0
            # LLM acceptance/fallback is reported separately; the paired endpoint
            # is intention-to-treat when configured simulations complete safely.
            pair_valid = complete and same_fp and same_hashes and seed_matches and validators and prompt_replay_valid
            policy_hashes = {}
            for method in ["D6_agent_codex_context_optimizer", "D7_agent_codex_capacity_shield_optimizer",
                           "D8_agent_codex_nn_capacity_shield_optimizer"]:
                policy_hashes[method] = audits[method].get("policy_prompt_hashes", [])
            cell = {"case_id": case_id, "fleet_id": fleet, "seed": seed,
                    "service_date": meta["service_date"],
                    "month": meta["month"], "stratum": meta["stratum"], "complete_all_methods": complete,
                    "same_scenario_fingerprint": same_fp, "same_input_hashes": same_hashes,
                    "all_methods_seed_match": seed_matches,
                    "context_prompt_replay_valid": prompt_replay_valid,
                    "d8_policy_accepted": d8_policy_accepted,
                    "all_validators_pass": validators, "paired_cell_valid": pair_valid,
                    "scenario_fingerprints": fingerprints, "input_hashes": input_hashes,
                    "context_policy_prompt_hashes": policy_hashes}
            cells[(case_id, fleet, seed)] = {"payloads": payloads, "audit": cell,
                                             "method_audits": audits}
            paired_rows.append(cell)

    # Validate each D8/reference pair independently; context ablations also require shared accepted policy replay.
    difference_rows = []
    for (case_id, fleet, seed), cell in cells.items():
        meta = case_metadata(case_id)
        cell_audit = cell["audit"]
        method_audits = cell["method_audits"]
        d8_payload = cell["payloads"]["D8_agent_codex_nn_capacity_shield_optimizer"]
        d8_run_audit = method_audits["D8_agent_codex_nn_capacity_shield_optimizer"]
        if not d8_payload:
            continue
        d8 = d8_payload["summary"]
        for baseline in BASELINES:
            baseline_payload = cell["payloads"].get(baseline)
            baseline_run_audit = method_audits.get(baseline, {})
            if not baseline_payload:
                continue
            same_pair_fingerprint = (d8_run_audit.get("scenario_fingerprint_sha256") is not None and
                                     d8_run_audit.get("scenario_fingerprint_sha256") ==
                                     baseline_run_audit.get("scenario_fingerprint_sha256"))
            same_pair_inputs = (d8_run_audit.get("input_hashes") is not None and
                                d8_run_audit.get("input_hashes") == baseline_run_audit.get("input_hashes"))
            baseline_valid = (
                d8_run_audit.get("complete") and baseline_run_audit.get("complete") and
                d8_run_audit.get("seed_match") and baseline_run_audit.get("seed_match") and
                d8_run_audit.get("validators_pass") and baseline_run_audit.get("validators_pass") and
                same_pair_fingerprint and same_pair_inputs
            )
            context_pair_valid = True
            if baseline in {"D6_agent_codex_context_optimizer", "D7_agent_codex_capacity_shield_optimizer"}:
                context_pair_valid = (
                    d8_run_audit.get("accepted_policy_count", 0) > 0 and
                    d8_run_audit.get("policy_prompt_hashes") == baseline_run_audit.get("policy_prompt_hashes") and
                    bool(d8_run_audit.get("policy_prompt_hashes")) and
                    baseline_run_audit.get("replayed_policy_count", 0) > 0 and
                    baseline_run_audit.get("policy_new_invocation_records", 0) == 0
                )
            method_pair_valid = bool(baseline_valid)
            if not method_pair_valid:
                continue
            reference = baseline_payload["summary"]
            row = {**meta, "fleet_id": fleet, "seed": seed,
                   "method": "D8_agent_codex_nn_capacity_shield_optimizer",
                   "baseline": baseline, "paired_cell_valid": method_pair_valid,
                   "full_matrix_cell_valid": cell_audit["paired_cell_valid"],
                   "context_ablation_pair_valid": context_pair_valid if baseline in {
                       "D6_agent_codex_context_optimizer", "D7_agent_codex_capacity_shield_optimizer"
                   } else None,
                   "d8_policy_accepted": cell_audit["d8_policy_accepted"]}
            for metric in METRICS:
                a, b = d8.get(metric), reference.get(metric)
                row[metric + "_difference_D8_minus_baseline"] = (
                    float(a) - float(b) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
                )
            difference_rows.append(row)

    # Two-way cluster bootstrap keeps each date's fleet variants together and resamples whole run seeds.
    comparison_summaries = []
    seed_effect_summaries = []
    for baseline in BASELINES:
        for metric in PRIMARY_METRICS + ["served_orders", "total_empty_distance_m", "total_energy_kwh",
                                         "optimizer_time_ms", "total_decision_time_ms"]:
            for fleet in ["ALL"] + FLEETS:
                by_date_seed: Dict[str, Dict[int, List[float]]] = {}
                for row in difference_rows:
                    if row["baseline"] != baseline or row["fleet_id"] not in (FLEETS if fleet == "ALL" else [fleet]):
                        continue
                    value = row.get(metric + "_difference_D8_minus_baseline")
                    if value is not None:
                        by_date_seed.setdefault(row["service_date"], {}).setdefault(int(row["seed"]), []).append(float(value))
                values = [value for seed_rows in by_date_seed.values()
                          for rows in seed_rows.values() for value in rows]
                if not values:
                    continue
                effects = cluster_effects(by_date_seed)
                low, high = bootstrap_interval(by_date_seed, seed=20261024 + len(comparison_summaries),
                                               iterations=args.iterations)
                comparison_summaries.append({
                    "baseline": baseline, "metric": metric, "fleet_id": fleet,
                    "paired_valid_date_count": len(effects), "paired_value_count": len(values),
                    "paired_seed_count": len({seed_id for seed_rows in by_date_seed.values() for seed_id in seed_rows}),
                    "mean_D8_minus_baseline": sum(effects) / len(effects),
                    "sd_D8_minus_baseline": (sum((x - sum(effects) / len(effects)) ** 2 for x in effects) / max(1, len(effects) - 1)) ** 0.5,
                    "date_seed_cluster_bootstrap_95ci_low": low,
                    "date_seed_cluster_bootstrap_95ci_high": high,
                    "bootstrap_method": "two_way_service_date_and_seed_clusters; fleet variants retained within date_seed",
                    "bootstrap_iterations": args.iterations,
                })
                for seed_id in sorted(seed_roots):
                    per_seed: Dict[str, Dict[int, List[float]]] = {}
                    for row in difference_rows:
                        if (row["seed"] != seed_id or row["baseline"] != baseline or
                                row["fleet_id"] not in (FLEETS if fleet == "ALL" else [fleet])):
                            continue
                        value = row.get(metric + "_difference_D8_minus_baseline")
                        if value is not None:
                            per_seed.setdefault(row["service_date"], {}).setdefault(seed_id, []).append(float(value))
                    effects = cluster_effects(per_seed)
                    if not effects:
                        continue
                    seed_effect_summaries.append({
                        "seed": seed_id, "baseline": baseline, "metric": metric, "fleet_id": fleet,
                        "paired_valid_date_count": len(effects),
                        "paired_value_count": sum(len(values) for seed_rows in per_seed.values()
                                                   for values in seed_rows.values()),
                        "mean_D8_minus_baseline": sum(effects) / len(effects),
                        "sd_across_date_effects": (sum((x - sum(effects) / len(effects)) ** 2
                                                       for x in effects) / max(1, len(effects) - 1)) ** 0.5,
                    })

    accepted_policy_summaries = []
    for baseline in BASELINES:
        for metric in PRIMARY_METRICS + ["served_orders", "total_empty_distance_m", "total_energy_kwh",
                                         "total_decision_time_ms", "total_llm_tokens"]:
            for fleet in ["ALL"] + FLEETS:
                by_date_seed: Dict[str, Dict[int, List[float]]] = {}
                for row in difference_rows:
                    if (not row["d8_policy_accepted"] or row["baseline"] != baseline or
                            row["fleet_id"] not in (FLEETS if fleet == "ALL" else [fleet])):
                        continue
                    if (baseline in {"D6_agent_codex_context_optimizer", "D7_agent_codex_capacity_shield_optimizer"}
                            and not row.get("context_ablation_pair_valid")):
                        continue
                    value = row.get(metric + "_difference_D8_minus_baseline")
                    if value is not None:
                        by_date_seed.setdefault(row["service_date"], {}).setdefault(int(row["seed"]), []).append(float(value))
                values = [value for per_seed in by_date_seed.values()
                          for per_fleet in per_seed.values() for value in per_fleet]
                effects = cluster_effects(by_date_seed)
                if not values or not effects:
                    continue
                low, high = bootstrap_interval(by_date_seed, seed=20301025 + len(accepted_policy_summaries),
                                               iterations=args.iterations)
                accepted_policy_summaries.append({
                    "baseline": baseline, "metric": metric, "fleet_id": fleet,
                    "accepted_policy_date_count": len(effects), "accepted_policy_paired_value_count": len(values),
                    "accepted_policy_seed_count": len({seed_id for per_seed in by_date_seed.values() for seed_id in per_seed}),
                    "mean_D8_minus_baseline": sum(effects) / len(effects),
                    "sd_across_date_effects": (sum((x - sum(effects) / len(effects)) ** 2 for x in effects) /
                                               max(1, len(effects) - 1)) ** 0.5,
                    "date_seed_cluster_bootstrap_95ci_low": low,
                    "date_seed_cluster_bootstrap_95ci_high": high,
                    "bootstrap_iterations": args.iterations,
                    "interpretation": "accepted D8 policy cells only; selection-biased sensitivity analysis",
                })

    policy_summaries = []
    for seed_id in sorted(seed_roots):
        for method in METHODS:
            rows = [row for row in run_rows if row["seed"] == seed_id and row["method"] == method]
            policy_records = sum(int(row.get("policy_record_count") or 0) for row in rows)
            accepted_records = sum(int(row.get("accepted_policy_count") or 0) for row in rows)
            policy_summaries.append({
                "seed": seed_id, "method": method, "present_result_count": len(rows),
                "complete_result_count": sum(bool(row.get("complete")) for row in rows),
                "selected_cell_count": len(package_manifest.get("evaluation_case_ids", [])) * len(FLEETS),
                "summary_llm_invocations": sum(int(row.get("total_llm_calls") or 0) for row in rows),
                "policy_record_count": policy_records,
                "accepted_policy_count": accepted_records,
                "replayed_policy_count": sum(int(row.get("replayed_policy_count") or 0) for row in rows),
                "new_policy_invocation_records": sum(int(row.get("policy_new_invocation_records") or 0) for row in rows),
                "policy_attempt_or_replay_record_count": sum(int(row.get("policy_attempt_or_replay_record_count") or 0) for row in rows),
                "accepted_live_invocation_count": sum(int(row.get("accepted_live_invocation_count") or 0) for row in rows),
                "nonaccepted_live_invocation_count": sum(int(row.get("nonaccepted_live_invocation_count") or 0) for row in rows),
                "accepted_replay_record_count": sum(int(row.get("accepted_replay_record_count") or 0) for row in rows),
                "nonaccepted_replay_record_count": sum(int(row.get("nonaccepted_replay_record_count") or 0) for row in rows),
                "no_invocation_policy_record_count": sum(int(row.get("no_invocation_policy_record_count") or 0) for row in rows),
                "no_feasible_candidate_round_count": sum(int(row.get("no_feasible_candidate_round_count") or 0) for row in rows),
                "live_invocation_acceptance_rate": (
                    sum(int(row.get("accepted_live_invocation_count") or 0) for row in rows) /
                    sum(int(row.get("policy_new_invocation_records") or 0) for row in rows)
                    if sum(int(row.get("policy_new_invocation_records") or 0) for row in rows) else None
                ),
            })

    method_summary = []
    for method in METHODS:
        for fleet in ["ALL"] + FLEETS:
            rows = [row for row in run_rows if row["method"] == method and
                    row["fleet_id"] in (FLEETS if fleet == "ALL" else [fleet])]
            row_out = {"method": method, "fleet_id": fleet, "complete_result_count": sum(bool(row["complete"]) for row in rows),
                       "selected_cell_count": 30 * (3 if fleet == "ALL" else 1) * len(seed_roots)}
            for metric in METRICS:
                vals = [float(row[metric]) for row in rows if isinstance(row.get(metric), (int, float))]
                row_out[metric + "_mean_per_window"] = sum(vals) / len(vals) if vals else None
                row_out[metric + "_sd_per_window"] = ((sum((v - sum(vals) / len(vals)) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5) if len(vals) > 1 else None
            row_out["replicate_seed_count"] = len(seed_roots)
            method_summary.append(row_out)

    usage_audit = parse_usage_traces(evaluation_dirs)
    overall = {
        "schema_version": "nyc-tlc-dispatch-multimonth-audit-v1",
        "audited_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "scenario_package_manifest_sha256": sha256(package_dir / "benchmark_manifest.json"),
        "seed_roots": {str(seed): str(path) for seed, path in sorted(seed_roots.items())},
        "replicate_seed_count": len(seed_roots),
        "expected_method_cells": len(package_manifest.get("evaluation_case_ids", [])) * len(FLEETS) * len(METHODS) * len(seed_roots),
        "present_result_files": len(run_rows),
        "fully_valid_paired_cells": sum(row["paired_cell_valid"] for row in paired_rows),
        "fully_complete_input_matched_validator_valid_cells": sum(row["paired_cell_valid"] for row in paired_rows),
        "expected_paired_cells": len(package_manifest.get("evaluation_case_ids", [])) * len(FLEETS) * len(seed_roots),
        "missing_or_partial_results": missing,
        "paired_cells": paired_rows,
        "usage_audit": usage_audit,
        "interpretation_limits": [
            "TLC completed trips and synthetic fleets are proxy inputs, not real requests and vehicle trajectories.",
            "Thirty selected calendar dates are date clusters, but dates were quantile-stratified rather than sampled from all NYC dates; intervals describe selected-window variation and do not establish citywide effects.",
            "Replicate seeds estimate simulation/model-run variation only on these selected inputs; dates remain the demand clusters.",
            "Token usage is not actual Codex account billing; replayed responses must not be counted twice.",
        ],
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    write_json(results_dir / "run_audit.json", overall)
    write_json(results_dir / "usage_audit.json", usage_audit)
    usage_by_seed_rows = [
        {"seed": int(seed), "unique_trace_file_count": item["unique_trace_file_count"], **item["totals"]}
        for seed, item in sorted(usage_audit["by_seed"].items(), key=lambda pair: int(pair[0]))
    ]
    write_csv(results_dir / "usage_by_seed.csv", usage_by_seed_rows,
              list(usage_by_seed_rows[0]) if usage_by_seed_rows else ["seed", "unique_trace_file_count"])
    write_json(results_dir / "paired_statistics.json", comparison_summaries)
    write_csv(results_dir / "seed_effects.csv", seed_effect_summaries,
              list(seed_effect_summaries[0]) if seed_effect_summaries else ["seed", "baseline", "metric", "fleet_id"])
    write_csv(results_dir / "accepted_policy_sensitivity.csv", accepted_policy_summaries,
              list(accepted_policy_summaries[0]) if accepted_policy_summaries else ["baseline", "metric", "fleet_id"])
    write_csv(results_dir / "method_policy_summary_by_seed.csv", policy_summaries,
              list(policy_summaries[0]) if policy_summaries else ["seed", "method"])
    write_csv(results_dir / "method_runs.csv", run_rows,
              ["case_id", "service_date", "month", "stratum", "fleet_id", "method", "result_path", "result_sha256",
               "seed", "complete", "validator_invalid_rounds", "hard_constraint_error_count", "policy_record_count",
               "policy_statuses", "accepted_policy_count", "replayed_policy_count",
               "policy_new_invocation_records", "policy_attempt_or_replay_record_count",
               "accepted_live_invocation_count", "nonaccepted_live_invocation_count",
               "accepted_replay_record_count", "nonaccepted_replay_record_count",
               "no_invocation_policy_record_count", "no_feasible_candidate_round_count",
               "live_invocation_acceptance_rate"] + METRICS)
    write_csv(results_dir / "paired_differences.csv", difference_rows,
              ["case_id", "service_date", "month", "stratum", "fleet_id", "seed", "method", "baseline",
               "paired_cell_valid", "full_matrix_cell_valid", "context_ablation_pair_valid", "d8_policy_accepted"] +
              [metric + "_difference_D8_minus_baseline" for metric in METRICS])
    write_csv(results_dir / "method_summary_by_fleet.csv", method_summary,
              list(method_summary[0]) if method_summary else ["method", "fleet_id"])
    write_csv(results_dir / "paired_statistics.csv", comparison_summaries,
              list(comparison_summaries[0]) if comparison_summaries else ["baseline", "metric", "fleet_id"])

    report_lines = [
        "# NYC TLC 多月调度实验结果审计",
        "",
        "审计时间（UTC）：{}".format(overall["audited_at_utc"]),
        "",
        "## 覆盖情况",
        "",
        "- 预期配对单元：{}（30 日期 × 3 车队 × {} 个 seed）".format(overall["expected_paired_cells"], len(seed_roots)),
        "- 全方法完整、同输入、seed 一致、逐轮校验有效且回放审计通过：{}".format(overall["fully_valid_paired_cells"]),
        "- 方法结果文件：{}/{}".format(overall["present_result_files"], overall["expected_method_cells"]),
        "- 唯一 Codex JSONL 文件：{}；turn 完成：{}；输入 token：{}；缓存输入 token：{}；输出 token：{}；总 token：{}；工具调用事件：{}；CLI 错误事件：{}（其中重连提示：{}）；非工具提示项：{}；损坏行：{}".format(
            usage_audit["unique_trace_file_count"], usage_audit["totals"]["turn_completed_count"],
            usage_audit["totals"]["input_tokens"], usage_audit["totals"]["cached_input_tokens"],
            usage_audit["totals"]["output_tokens"], usage_audit["totals"]["total_tokens"],
            usage_audit["totals"]["tool_event_count"], usage_audit["totals"]["cli_error_event_count"],
            usage_audit["totals"]["reconnect_notice_count"], usage_audit["totals"]["non_tool_notice_item_count"],
            usage_audit["totals"]["malformed_line_count"]),
        "- 每个 seed 的 trace 数与 token 统计见 `usage_by_seed.csv`；Codex 账户实际扣费仍无法由 CLI trace 得知。",
        "",
        "## 方法汇总",
        "",
        "见 `method_summary_by_fleet.csv`。均值按窗口等权计算；F1/F2/F4 分开列出，ALL 为每日期的三个车队窗口等权汇总。",
        "",
        "## D8 配对差值",
        "",
        "见 `paired_statistics.csv`。正值表示该指标 D8 高于对应基线；服务率和服务数越高通常越好，等待、空驶、电耗和计算时间越低通常越好。95% 区间按日期与 seed 双重成簇重采样，同一日期的三种车队一起保留，并对日期等权。",
        "",
        "各 seed 的单独配对效应与日期间标准差见 `seed_effects.csv`，用于检查主结论是否依赖某一个运行 seed。",
        "",
        "只保留 D8 策略调用有效的敏感性分析见 `accepted_policy_sensitivity.csv`；该子集可能受策略接受与场景难度共同影响，属于选择偏差敏感性分析。",
        "",
        "策略调用接受率以实际发起调用为分母；调用失败、回放接受/失败和无可行候选的未调用轮次分开报告，详见 `method_policy_summary_by_seed.csv`。",
        "",
        "## 解释限制",
        "",
    ]
    report_lines.extend("- " + item for item in overall["interpretation_limits"])
    report_lines.extend([
        "- 全矩阵完整率要求全部方法单元覆盖、同输入哈希、seed 一致、validator 通过并符合 D6/D7 回放协议。主配对效应按 D8 与各基线分别核验，并按 intention-to-treat 保留配置回退；某个消融缺项不会剔除有效的 D8-D2 主比较。D6/D7 的组件归因只看接受 D8 策略且逐字节 prompt 哈希一致、没有新增回放调用的敏感性子集，详见 `accepted_policy_sensitivity.csv`。失败或缺项列在 `run_audit.json`。",
        "- LLM 接受率、回退、请求用量和每轮安全校验须与乘客结果并列查看，不能只看一个汇总指标。",
        "",
    ])
    (results_dir / "comparison_report.md").write_text("\n".join(report_lines), encoding="utf-8")
    print(json.dumps({"run_audit": str(results_dir / "run_audit.json"),
                      "complete_paired_cells": overall["fully_valid_paired_cells"],
                      "expected_paired_cells": overall["expected_paired_cells"],
                      "usage": usage_audit["totals"]}, ensure_ascii=False, indent=2))
    return 0 if not missing and overall["fully_valid_paired_cells"] == overall["expected_paired_cells"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
