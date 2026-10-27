#!/usr/bin/env python3
"""Audit frozen route-layer input equivalence without model requests."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def audit(repo: Path) -> dict[str, Any]:
    route_root = repo / "outputs/route_layer"
    traditional_root = route_root / "traditional_experiment_v1/seeds"
    pure_llm_root = route_root / "pure_llm_experiment_v8/seeds"
    r4_root = route_root / "r4_codex_batch_preflights_20261024_v1"
    r4_manifest = read_json(r4_root / "manifest.json")
    pure_llm_manifest = read_json(route_root / "pure_llm_experiment_v8/manifest.json")
    task9_persistent_preflight = read_json(
        route_root
        / "r4_codex_stepwise_formal_task9_persistent_preflight_20261025_v2/manifest.json"
    )
    formal_batch_manifest = read_json(
        route_root / "r4_codex_formal_batch_v1/manifest.json"
    )

    raw_hash_matches: Counter[str] = Counter()
    normalized_scenario_matches = 0
    per_seed: list[dict[str, Any]] = []
    expected_task_ids: set[str] = set()
    duplicate_expected_task_ids: list[str] = []

    for seed in range(10):
        traditional_dir = traditional_root / f"seed_{seed}"
        pure_llm_dir = pure_llm_root / f"seed_{seed}"
        files = {
            "map": (
                traditional_dir / f"map_100x100_seed_{seed}.json",
                pure_llm_dir / f"map_100x100_seed_{seed}.json",
            ),
            "orders": (
                traditional_dir / f"order_tasks_seed_{seed}.json",
                pure_llm_dir / f"order_tasks_seed_{seed}.json",
            ),
            "formal_scenarios": (
                traditional_dir / f"formal_scenarios_seed_{seed}.json",
                pure_llm_dir / f"formal_scenarios_seed_{seed}.json",
            ),
        }
        hashes: dict[str, str] = {}
        for kind, (traditional_file, pure_llm_file) in files.items():
            traditional_hash = sha256(traditional_file)
            pure_llm_hash = sha256(pure_llm_file)
            hashes[f"{kind}_traditional"] = traditional_hash
            hashes[f"{kind}_pure_llm"] = pure_llm_hash
            raw_hash_matches[kind] += traditional_hash == pure_llm_hash

        traditional_scenarios = read_json(files["formal_scenarios"][0])
        pure_llm_scenarios = read_json(files["formal_scenarios"][1])
        normalized_scenarios = json.loads(json.dumps(pure_llm_scenarios))
        null_road_map_fields = 0
        nonnull_road_map_ids: list[str | None] = []
        for scenario in normalized_scenarios.get("scenarios", []):
            if "road_map" not in scenario:
                continue
            if scenario["road_map"] is None:
                null_road_map_fields += 1
                scenario.pop("road_map")
            else:
                nonnull_road_map_ids.append(scenario.get("scenario_id"))
        scenarios_match = (
            traditional_scenarios == normalized_scenarios
            and not nonnull_road_map_ids
        )
        normalized_scenario_matches += scenarios_match

        order_tasks = read_json(files["orders"][0]).get("tasks", [])
        formal_scenarios = traditional_scenarios.get("scenarios", [])
        formal_tasks = [
            task
            for scenario in formal_scenarios
            for task in scenario.get("tasks", [])
        ]
        seed_task_ids = [task.get("task_id") for task in order_tasks + formal_tasks]
        duplicate_expected_task_ids.extend(
            task_id
            for task_id, count in Counter(seed_task_ids).items()
            if count > 1
        )
        expected_task_ids.update(seed_task_ids)
        per_seed.append(
            {
                "seed": seed,
                "map_sha256_equal": hashes["map_traditional"]
                == hashes["map_pure_llm"],
                "order_file_sha256_equal": hashes["orders_traditional"]
                == hashes["orders_pure_llm"],
                "formal_scenarios_raw_sha256_equal": hashes[
                    "formal_scenarios_traditional"
                ]
                == hashes["formal_scenarios_pure_llm"],
                "formal_scenarios_equal_after_null_road_map_removal": scenarios_match,
                "pure_llm_null_road_map_fields": null_road_map_fields,
                "order_count": len(order_tasks),
                "formal_scenario_count": len(formal_scenarios),
                "formal_task_count": len(formal_tasks),
                "input_sha256": hashes,
            }
        )

    r4_routeable = r4_manifest.get("tasks", [])
    r4_skipped = r4_manifest.get("skipped_tasks", [])
    all_r4_rows = r4_routeable + r4_skipped
    r4_task_ids = [row.get("task_id") for row in all_r4_rows]
    routeable_by_group = Counter(row.get("sample_group") for row in r4_routeable)
    skipped_by_group = Counter(row.get("sample_group") for row in r4_skipped)
    skipped_by_reason = Counter(row.get("route_search_gate") for row in r4_skipped)

    preflight_missing: list[str | None] = []
    preflight_hash_errors: list[dict[str, str | None]] = []
    task_id_mismatches: list[str | None] = []
    map_refs_checked = 0
    task_source_refs_checked = 0
    for row in r4_routeable:
        relative_preflight = row.get("preflight_path")
        if relative_preflight:
            preflight_path = Path(relative_preflight)
            if not preflight_path.is_absolute():
                preflight_path = repo / preflight_path
        else:
            candidates = list(
                (r4_root / "tasks" / f"seed_{row.get('seed')}").glob(
                    f"{row.get('task_id')}*/preflight.json"
                )
            )
            preflight_path = candidates[0] if candidates else None
        if preflight_path is None or not preflight_path.exists():
            preflight_missing.append(row.get("task_id"))
            continue
        preflight = read_json(preflight_path)
        if preflight.get("task_public", {}).get("task_id") != row.get("task_id"):
            task_id_mismatches.append(row.get("task_id"))
        for ref_key in ("map_ref", "task_source"):
            reference = preflight.get(ref_key, {})
            reference_path = reference.get("path")
            if not reference_path:
                preflight_hash_errors.append(
                    {"task_id": row.get("task_id"), "reason": f"{ref_key}_path_missing"}
                )
                continue
            source_file = Path(reference_path)
            if not source_file.is_absolute():
                source_file = repo / source_file
            if ref_key == "map_ref":
                map_refs_checked += 1
            else:
                task_source_refs_checked += 1
            actual_hash = sha256(source_file) if source_file.exists() else None
            if actual_hash != reference.get("sha256"):
                preflight_hash_errors.append(
                    {
                        "task_id": row.get("task_id"),
                        "reason": f"{ref_key}_sha256_mismatch",
                    }
                )

    expected_total = 150
    exact_task_id_match = set(r4_task_ids) == expected_task_ids
    all_ids_unique = len(r4_task_ids) == len(set(r4_task_ids))
    checks = [
        {
            "check": "same_maps",
            "passed": raw_hash_matches["map"] == 10,
            "matched_seeds": raw_hash_matches["map"],
            "total_seeds": 10,
        },
        {
            "check": "same_orders",
            "passed": raw_hash_matches["orders"] == 10,
            "matched_seeds": raw_hash_matches["orders"],
            "total_seeds": 10,
        },
        {
            "check": "same_formal_scenarios_semantically",
            "passed": normalized_scenario_matches == 10,
            "matched_seeds": normalized_scenario_matches,
            "total_seeds": 10,
            "normalization": "remove pure_llm road_map only when its value is null",
        },
        {
            "check": "r4_full_task_id_denominator",
            "passed": len(r4_task_ids) == expected_total
            and exact_task_id_match
            and all_ids_unique
            and not duplicate_expected_task_ids,
            "expected_tasks": expected_total,
            "actual_tasks": len(r4_task_ids),
            "unique_actual_tasks": len(set(r4_task_ids)),
            "exact_id_set_match": exact_task_id_match,
        },
        {
            "check": "r4_routeable_preflight_source_hashes",
            "passed": not preflight_missing
            and not preflight_hash_errors
            and not task_id_mismatches,
            "routeable_tasks": len(r4_routeable),
            "preflights_missing": len(preflight_missing),
            "hash_errors": len(preflight_hash_errors),
            "task_id_errors": len(task_id_mismatches),
            "map_refs_checked": map_refs_checked,
            "task_source_refs_checked": task_source_refs_checked,
        },
        {
            "check": "no_model_requests_in_frozen_preflight",
            "passed": r4_manifest.get("model_requests") == 0,
            "model_requests": r4_manifest.get("model_requests"),
        },
    ]
    r4_task_ids_valid = all(task_id is not None for task_id in r4_task_ids)
    if not r4_task_ids_valid:
        checks[3]["passed"] = False

    pure_llm_protocol = {
        "model": pure_llm_manifest.get("model_requested"),
        "map_input_mode": pure_llm_manifest.get("map_input_mode"),
        "map_input_encoding": pure_llm_manifest.get("map_input_encoding"),
        "route_validation": pure_llm_manifest.get("route_validation"),
        "route_repair": pure_llm_manifest.get("route_repair"),
        "model_requests": pure_llm_manifest.get("api_request_count"),
        "result_count": pure_llm_manifest.get("total_trace_count"),
    }
    r4_protocol = {
        "model": task9_persistent_preflight.get("model"),
        "reasoning_effort": task9_persistent_preflight.get("reasoning_effort"),
        "session_mode": task9_persistent_preflight.get("codex_cli_session_mode"),
        "route_action_budget": task9_persistent_preflight.get("route_action_budget"),
        "candidate_request_limit": task9_persistent_preflight.get("candidate_request_limit"),
        "formal_session_policy": formal_batch_manifest.get("session_policy"),
        "validation_policy": formal_batch_manifest.get("validation_policy"),
        "retry_policy": formal_batch_manifest.get("retry_policy"),
        "formal_model_requests": formal_batch_manifest.get("model_requests_used"),
    }

    return {
        "schema_version": "route_layer_three_group_input_equivalence_audit_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed_offline_no_model_requests"
        if all(check["passed"] for check in checks)
        else "failed_offline_audit",
        "groups": [
            "traditional_experiment_v1",
            "pure_llm_experiment_v8",
            "r4_codex_formal_batch_preflight_20261025_v1",
        ],
        "checks": checks,
        "raw_file_hash_equality_across_10_seeds": {
            kind: raw_hash_matches[kind]
            for kind in ("map", "orders", "formal_scenarios")
        },
        "formal_scenario_raw_hash_note": (
            "Pure LLM v8 adds road_map:null to each formal scenario. Removing only "
            "that null field yields deep JSON equality with traditional inputs for all seeds."
        ),
        "experiment_comparison_readiness": (
            "input_equivalence_passed_but_llm_method_comparison_not_ready"
        ),
        "method_compatibility": {
            "controlled_pure_llm_vs_r4_comparison_ready": False,
            "pure_llm_v8": pure_llm_protocol,
            "r4_current_formal_preflight": r4_protocol,
            "reasons": [
                "Pure LLM v8 used DeepSeek with graph_only input and one whole-route response followed by post-hoc validation.",
                "Current R4 uses Codex CLI in a persistent per-task session and one direction per request, with deterministic validation gating each move.",
                "The model backend and route interaction/validation protocol therefore differ; input equivalence alone cannot attribute outcome differences to the R4 map/query design.",
            ],
            "required_for_strict_llm_comparison": (
                "Run the graph-only control with the same Codex model, stepwise request protocol, validator, frozen 150-task denominator, and budget rules; otherwise report pure_llm_experiment_v8 only as a historical exploratory cohort."
            ),
        },
        "per_seed": per_seed,
        "r4_coverage": {
            "expected_denominator": expected_total,
            "routeable_preflights": len(r4_routeable),
            "skipped_and_retained": len(r4_skipped),
            "routeable_by_group": dict(routeable_by_group),
            "skipped_by_group": dict(skipped_by_group),
            "skipped_by_reason": dict(skipped_by_reason),
            "minimum_route_action_lower_bound": r4_manifest.get(
                "minimum_model_request_lower_bound"
            ),
            "exact_task_id_set_match": exact_task_id_match,
            "all_ids_unique": all_ids_unique,
            "duplicate_expected_task_ids": duplicate_expected_task_ids,
            "preflight_map_reference_count": map_refs_checked,
            "preflight_task_source_reference_count": task_source_refs_checked,
            "preflight_integrity_errors": preflight_hash_errors,
            "missing_preflights": preflight_missing,
            "public_task_id_mismatches": task_id_mismatches,
            "r4_manifest_status": r4_manifest.get("status"),
            "r4_model_requests": r4_manifest.get("model_requests"),
        },
        "limitations": [
            "This is an offline input-equivalence and preflight audit, not evidence of route completion, energy, latency, or comparative performance.",
            "The 21 R4 skipped tasks remain in the 150-task denominator and must retain their skip reasons; generated-route comparisons have a 129-task eligible subset.",
            "Raw formal-scenario hashes differ only because pure_llm_experiment_v8 adds a null road_map field; semantic JSON matches after that normalization.",
            "Matching inputs does not make pure_llm_experiment_v8 and current R4 a controlled comparison: their model backends, request granularity, and validation timing differ.",
            "This audit does not authorize or initiate model requests.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Project directory containing outputs/route_layer.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional report path. Defaults to the current R4 preflight folder.",
    )
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    report = audit(repo)
    output = args.output or (
        repo
        / "outputs/route_layer/r4_codex_formal_batch_preflight_20261025_v1/"
        "three_group_input_equivalence_audit.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"status={report['status']}")
    for check in report["checks"]:
        print(f"{check['check']}={'PASS' if check['passed'] else 'FAIL'}")
    print(f"report={output}")
    return 0 if report["status"] == "passed_offline_no_model_requests" else 1


if __name__ == "__main__":
    raise SystemExit(main())
