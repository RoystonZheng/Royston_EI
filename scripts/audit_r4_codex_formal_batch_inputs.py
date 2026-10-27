#!/usr/bin/env python3
"""Audit frozen R4 task/map coverage and shared EV v2 baseline artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ROUTE_ROOT = ROOT / "outputs/route_layer"
TRAD_ROOT = ROUTE_ROOT / "traditional_experiment_v1"
R4_PREFLIGHT = ROUTE_ROOT / "r4_codex_batch_preflights_20261024_v1/manifest.json"
R4_BATCH = ROUTE_ROOT / "r4_codex_formal_batch_20261027_v2"
ENERGY_V2 = ROUTE_ROOT / "traditional_experiment_v1_energy_v2_billable_20261027_v2/manifest.json"
OUTPUT = R4_BATCH / "input_comparability_audit.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    preflight = read_json(R4_PREFLIGHT)
    batch = read_json(R4_BATCH / "manifest.json")
    traditional_manifest = read_json(TRAD_ROOT / "manifest.json")
    pure_manifest = read_json(ROUTE_ROOT / "pure_llm_experiment_v8/manifest.json")
    energy = read_json(ENERGY_V2)

    routeable = {row["task_id"]: row for row in preflight["tasks"]}
    skipped = {row["task_id"]: row for row in preflight["skipped_tasks"]}
    errors: list[str] = []
    per_seed: list[dict[str, Any]] = []
    all_baseline_ids: set[str] = set()
    map_hashes: dict[str, str] = {}

    for seed in range(10):
        order_path = TRAD_ROOT / f"seeds/seed_{seed}/traditional_order_routes_costs_seed_{seed}.json"
        formal_path = TRAD_ROOT / f"seeds/seed_{seed}/traditional_formal_routes_costs_seed_{seed}.json"
        order_rows = read_json(order_path)["results"]
        formal_rows = read_json(formal_path)["results"]
        baseline_ids = {row["task_id"] for row in order_rows + formal_rows}
        all_baseline_ids |= baseline_ids
        map_path = TRAD_ROOT / f"seeds/seed_{seed}/map_100x100_seed_{seed}.json"
        map_hash = sha256(map_path)
        map_hashes[str(seed)] = map_hash

        seed_routeable = 0
        seed_skipped = 0
        for task_id in baseline_ids:
            row = routeable.get(task_id)
            if row and row["seed"] == seed:
                seed_routeable += 1
                pf_path = ROOT / row["preflight_path"]
                pf = read_json(pf_path)
                if pf["map_ref"]["sha256"] != map_hash:
                    errors.append(f"{task_id}:r4_map_hash_mismatch")
                if pf["route_search"].get("evaluator_truth_in_prompt") is not False:
                    errors.append(f"{task_id}:evaluator_truth_prompt_gate_not_false")
                if pf.get("model_requests") != 0:
                    errors.append(f"{task_id}:preflight_contains_model_request")
            elif task_id in skipped and skipped[task_id]["seed"] == seed:
                seed_skipped += 1
            else:
                errors.append(f"{task_id}:missing_from_r4_frozen_denominator")

        per_seed.append({
            "seed": seed,
            "traditional_order_tasks": len(order_rows),
            "traditional_formal_tasks": len(formal_rows),
            "r4_routeable_tasks": seed_routeable,
            "r4_preflight_skipped_tasks": seed_skipped,
            "map_sha256": map_hash,
        })

    if len(all_baseline_ids) != 150:
        errors.append("traditional_task_denominator_not_150")
    if set(routeable) | set(skipped) != all_baseline_ids:
        errors.append("r4_task_ids_do_not_match_traditional_frozen_denominator")
    if len(routeable) != 129 or len(skipped) != 21:
        errors.append("r4_routeable_or_skipped_count_mismatch")

    energy_errors: list[str] = []
    for item in energy["files"]:
        source = TRAD_ROOT / item["source"]
        output = ROOT / item["output"]
        if not source.is_file() or sha256(source) != item["source_sha256"]:
            energy_errors.append(f"{item['source']}:source_missing_or_hash_mismatch")
        if not output.is_file() or sha256(output) != item["output_sha256"]:
            energy_errors.append(f"{item['output']}:sidecar_missing_or_hash_mismatch")
        if item["route_structure_sha256_before"] != item["route_structure_sha256_after"]:
            energy_errors.append(f"{item['source']}:route_structure_changed")

    if energy.get("status") != "completed_offline_recalculation":
        energy_errors.append("traditional_ev_v2_sidecar_not_complete")
    if energy["energy_model"].get("model_version") != "ev_longitudinal_v2":
        energy_errors.append("traditional_energy_model_not_ev_longitudinal_v2")

    r4_model_observations: dict[str, int] = {}
    for task in batch.get("tasks", []):
        output_dir = task.get("output_dir")
        state_path = Path(output_dir) / "state.json" if output_dir else None
        if not state_path or not state_path.is_file():
            continue
        state = read_json(state_path)
        for attempt in state.get("attempts", []):
            model = (attempt.get("provider_record") or {}).get("model")
            if model:
                r4_model_observations[model] = r4_model_observations.get(model, 0) + 1

    result = {
        "schema_version": "r4_codex_formal_batch_input_comparability_audit_v1",
        "status": "passed" if not errors and not energy_errors else "failed",
        "batch_status_at_audit": batch.get("status"),
        "batch_requests_at_audit": batch.get("model_requests_used"),
        "traditional_manifest_sha256": sha256(TRAD_ROOT / "manifest.json"),
        "r4_preflight_manifest_sha256": sha256(R4_PREFLIGHT),
        "r4_batch_manifest_sha256": sha256(R4_BATCH / "manifest.json"),
        "frozen_denominator": {
            "seed_count": 10,
            "task_count": len(all_baseline_ids),
            "routeable_task_count": len(routeable),
            "preflight_skipped_task_count": len(skipped),
            "task_ids_match_traditional": set(routeable) | set(skipped) == all_baseline_ids,
            "per_seed": per_seed,
        },
        "maps": {
            "size": traditional_manifest.get("map_size"),
            "r4_map_hash_matches_traditional_for_all_tasks": not any("map_hash" in e for e in errors),
            "sha256_by_seed": map_hashes,
        },
        "evaluator_truth_leak": {
            "routeable_task_preflights_checked": len(routeable),
            "all_false": not any("evaluator_truth_prompt_gate" in e for e in errors),
        },
        "traditional_ev_v2_sidecar": {
            "status": energy.get("status"),
            "files_checked": len(energy.get("files", [])),
            "evaluated_route_method_pairs": energy.get("evaluated_route_method_pairs"),
            "not_evaluated_route_method_pairs": energy.get("not_evaluated_route_method_pairs"),
            "energy_model": energy["energy_model"].get("model_version"),
            "energy_profile_sha256": energy["energy_model"].get("profile_sha256"),
            "route_structure_unchanged": not any("route_structure_changed" in e for e in energy_errors),
            "integrity_errors": energy_errors,
        },
        "model_conditions": {
            "r4_models_observed_so_far": r4_model_observations,
            "pure_llm_v8_models_observed": pure_manifest.get("models_observed", []),
            "same_model_family_held_constant": bool(
                len(r4_model_observations) == 1
                and set(r4_model_observations) == set(pure_manifest.get("models_observed", []))
            ),
        },
        "method_comparison_limitations": [
            "Pure LLM v8 used DeepSeek Flash; this R4 batch uses Codex CLI, so model family is not held constant.",
            "R4 endpoint resolution uses frozen feature/intent context; traditional and pure LLM endpoint interpretations may differ. Report semantic endpoint accuracy separately from route search metrics.",
            "The audit validates frozen task/map coverage and energy-sidecar integrity; it does not assert current R4 route outcomes or overall method superiority.",
        ],
        "errors": errors,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"],
        "tasks": result["frozen_denominator"]["task_count"],
        "routeable": result["frozen_denominator"]["routeable_task_count"],
        "skipped": result["frozen_denominator"]["preflight_skipped_task_count"],
        "energy_sidecar_integrity_errors": len(energy_errors),
        "errors": errors,
        "output": str(OUTPUT),
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
