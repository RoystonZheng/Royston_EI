#!/usr/bin/env python3
"""Audit endpoint pairing for the frozen R4 route batch without model calls."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ROUTE = ROOT / "outputs/route_layer"
PREFLIGHT_ROOT = ROUTE / "r4_codex_batch_preflights_20261024_v1"
TRAD_ROOT = ROUTE / "traditional_experiment_v1"
INTENT_FILE = ROUTE / "r4_relative_grounding_postprocess_dev_v2/route_intents.json"
OUTPUT_ROOT = Path(__file__).resolve().parent


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_seed_sources(seed: int):
    seed_root = TRAD_ROOT / f"seeds/seed_{seed}"
    orders = read_json(seed_root / f"order_tasks_seed_{seed}.json")
    formal = read_json(seed_root / f"formal_scenarios_seed_{seed}.json")
    order_results = read_json(seed_root / f"traditional_order_routes_costs_seed_{seed}.json")
    formal_results = read_json(seed_root / f"traditional_formal_routes_costs_seed_{seed}.json")
    return {
        "orders": {row["task_id"]: row for row in orders["tasks"]},
        "order_truth": {row["task_id"]: row for row in orders["ground_truth"]},
        "order_results": {row["task_id"]: row for row in order_results["results"]},
        "formal": {row["task_id"]: row for group in formal["scenarios"] for row in group["tasks"]},
        "formal_results": {row["task_id"]: row for row in formal_results["results"]},
        "map_path": seed_root / f"map_100x100_seed_{seed}.json",
    }


def traditional_endpoints(raw: dict, result: dict):
    if raw.get("pickup_node_id") is not None and raw.get("dropoff_node_id") is not None:
        return raw["pickup_node_id"], raw["dropoff_node_id"], "frozen_exact_endpoint"
    resolutions = []
    for method in result.get("methods", []):
        value = method.get("metadata", {}).get("semantic_resolution", {})
        if value.get("resolved"):
            resolutions.append((method.get("method_name"), value.get("pickup_node_id"), value.get("dropoff_node_id")))
    pairs = {(pickup, dropoff) for _, pickup, dropoff in resolutions}
    if len(pairs) != 1:
        raise ValueError("traditional_semantic_endpoint_missing_or_method_mismatch")
    pickup, dropoff = next(iter(pairs))
    return pickup, dropoff, "traditional_graph_only_semantic_resolution"


def main():
    manifest = read_json(PREFLIGHT_ROOT / "manifest.json")
    batch_root = ROUTE / "r4_codex_formal_batch_20261027_v2"
    batch_manifest_path = batch_root / "manifest.json"
    batch_manifest_bytes = batch_manifest_path.read_bytes()
    batch = json.loads(batch_manifest_bytes)
    intent_payload = read_json(INTENT_FILE)
    intents = {
        (int(row["seed"]), row["task_id"], row.get("map_input_mode")): row
        for row in intent_payload.get("records", [])
    }
    seed_sources = {seed: load_seed_sources(seed) for seed in range(10)}
    rows = []
    errors = []
    for frozen in manifest.get("tasks", []):
        seed, task_id = int(frozen["seed"]), frozen["task_id"]
        preflight_path = ROOT / frozen["preflight_path"]
        preflight = read_json(preflight_path)
        sources = seed_sources[seed]
        is_order = frozen["sample_group"] == "orders"
        raw = (sources["orders"].get(task_id) if is_order else sources["formal"].get(task_id))
        truth = (sources["order_truth"].get(task_id) if is_order else None)
        result = (sources["order_results"].get(task_id) if is_order else sources["formal_results"].get(task_id))
        if raw is None or result is None:
            errors.append(f"{task_id}:traditional_source_or_result_missing")
            continue
        if sha256(sources["map_path"]) != preflight["map_ref"].get("sha256"):
            errors.append(f"{task_id}:map_hash_mismatch")
        task_source = ROOT / preflight["task_source"]["path"]
        if sha256(task_source) != preflight["task_source"].get("sha256"):
            errors.append(f"{task_id}:task_source_hash_mismatch")
        query = json.loads(preflight["route_search"]["messages"][-1]["content"])
        q = query["q"]
        truth = truth or {
            "true_pickup_node_id": raw.get("true_pickup_node_id", raw.get("pickup_node_id")),
            "true_dropoff_node_id": raw.get("true_dropoff_node_id", raw.get("dropoff_node_id")),
        }
        if truth != preflight.get("evaluation_truth"):
            # The order truth row contains task_id; compare only endpoint fields.
            endpoint_truth = {
                key: truth.get(key)
                for key in ("true_pickup_node_id", "true_dropoff_node_id")
            }
            if endpoint_truth != preflight.get("evaluation_truth"):
                errors.append(f"{task_id}:evaluation_truth_disagrees_with_frozen_task")
            truth = endpoint_truth
        else:
            truth = preflight["evaluation_truth"]
        if q.get("s") != raw.get("start_node_id"):
            errors.append(f"{task_id}:start_node_mismatch")
        r4_pickup = q.get("p")
        r4_dropoff = q.get("d")
        task_public = preflight.get("task_public", {})
        for field in ("task_id", "start_node_id", "task_type"):
            if task_public.get(field) != raw.get(field):
                errors.append(f"{task_id}:public_task_source_mismatch:{field}")
        if preflight["route_search"].get("evaluator_truth_in_prompt") is not False:
            errors.append(f"{task_id}:truth_leak_gate_not_false")
        rendered = preflight["route_search"].get("rendered_prompt", "")
        if any(marker in rendered for marker in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")):
            errors.append(f"{task_id}:truth_marker_in_prompt")
        if is_order and raw.get("pickup_node_id") is None:
            intent = intents.get((seed, task_id, "road_feature_protocol_v1"))
            source_audit = preflight.get("source_preflight_row", {}).get("intent_source_audit", {})
            if not intent or intent.get("status") != "resolved" or intent.get("uses_evaluator_truth") is not False:
                errors.append(f"{task_id}:independent_semantic_intent_source_invalid")
            if not source_audit.get("passed"):
                errors.append(f"{task_id}:semantic_query_hash_audit_failed")
            if intent and (intent.get("pickup_node_id") != r4_pickup or
                           intent.get("dropoff_node_id") != r4_dropoff):
                errors.append(f"{task_id}:intent_endpoint_differs_from_prompt")
        else:
            if r4_pickup != raw.get("pickup_node_id") or r4_dropoff != raw.get("dropoff_node_id"):
                errors.append(f"{task_id}:exact_prompt_endpoint_differs_from_source")
        base_pickup, base_dropoff, base_source = traditional_endpoints(raw, result)
        task_type = raw.get("task_type") or frozen.get("scenario_type") or "formal"
        category = "semantic" if is_order and raw.get("pickup_node_id") is None else ("exact_order" if is_order else "formal_exact")
        rows.append({
            "seed": seed,
            "sample_group": frozen["sample_group"],
            "task_id": task_id,
            "task_type": task_type,
            "category": category,
            "r4_pickup_node_id": r4_pickup,
            "r4_dropoff_node_id": r4_dropoff,
            "truth_pickup_node_id": truth.get("true_pickup_node_id"),
            "truth_dropoff_node_id": truth.get("true_dropoff_node_id"),
            "r4_pickup_matches_truth": r4_pickup == truth.get("true_pickup_node_id"),
            "r4_dropoff_matches_truth": r4_dropoff == truth.get("true_dropoff_node_id"),
            "r4_both_endpoints_match_truth": r4_pickup == truth.get("true_pickup_node_id") and r4_dropoff == truth.get("true_dropoff_node_id"),
            "traditional_pickup_node_id": base_pickup,
            "traditional_dropoff_node_id": base_dropoff,
            "traditional_endpoint_source": base_source,
            "traditional_both_endpoints_match_truth": base_pickup == truth.get("true_pickup_node_id") and base_dropoff == truth.get("true_dropoff_node_id"),
            "r4_and_traditional_endpoints_equal": r4_pickup == base_pickup and r4_dropoff == base_dropoff,
            "map_sha256": preflight["map_ref"]["sha256"],
            "task_source_sha256": preflight["task_source"]["sha256"],
        })

    skipped = manifest.get("skipped_tasks", [])
    stats = defaultdict(Counter)
    for row in rows:
        c = stats[row["category"]]
        c["n"] += 1
        c["r4_both_truth"] += int(row["r4_both_endpoints_match_truth"])
        c["traditional_both_truth"] += int(row["traditional_both_endpoints_match_truth"])
        c["same_endpoints"] += int(row["r4_and_traditional_endpoints_equal"])
    semantic = [row for row in rows if row["category"] == "semantic"]
    terminal_route_audits = []
    for task in batch.get("tasks", []):
        if task.get("status") not in {"route_complete", "failed", "budget_paused"}:
            continue
        audit_ref = task.get("final_audit_path")
        if not audit_ref:
            continue
        audit_path = Path(audit_ref)
        if not audit_path.is_absolute():
            audit_path = batch_root / audit_path
        if audit_path.is_file():
            try:
                terminal_route_audits.append(read_json(audit_path))
            except (OSError, ValueError):
                pass
    result = {
        "schema_version": "r4_route_endpoint_alignment_audit_v1",
        "audit_status": "passed" if not errors and len(rows) == 129 else "failed",
        "audited_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "input_scope": {
            "frozen_task_count": len(manifest.get("tasks", [])) + len(skipped),
            "routeable_task_count": len(rows),
            "preflight_skipped_task_count": len(skipped),
            "preflight_manifest_sha256": sha256(PREFLIGHT_ROOT / "manifest.json"),
            "active_batch_manifest_sha256": hashlib.sha256(batch_manifest_bytes).hexdigest(),
            "active_batch_status_at_audit": batch.get("status"),
            "active_batch_requests_recorded_at_audit": batch.get("model_requests_used"),
            "maps_and_task_source_hashes_verified": not any("hash_mismatch" in error for error in errors),
            "truth_leak_checks_passed": not any("truth_" in error for error in errors),
        },
        "routeable_endpoint_summary": {
            "r4_endpoints_match_truth": sum(row["r4_both_endpoints_match_truth"] for row in rows),
            "traditional_endpoints_match_truth": sum(row["traditional_both_endpoints_match_truth"] for row in rows),
            "r4_and_traditional_share_endpoints": sum(row["r4_and_traditional_endpoints_equal"] for row in rows),
            "r4_semantic_successes": sum(row["r4_both_endpoints_match_truth"] for row in semantic),
            "r4_semantic_resolved_tasks": len(semantic),
            "r4_semantic_preflight_failures_in_frozen_denominator": sum(
                item.get("sample_group") == "orders" and item.get("endpoint_gate") == "intent_resolution_failed"
                for item in skipped
            ),
            "interpretation": "Static endpoint-resolution/input audit only; no route outcome or method-effect result. Route-length/time/energy comparisons require a common endpoint pair."
        },
        "counts_by_category": {key: dict(value) for key, value in stats.items()},
        "endpoint_mismatches": [row for row in rows if not row["r4_and_traditional_endpoints_equal"]],
        "preflight_skips_by_reason": dict(Counter(
            item.get("route_search_gate", "unknown")
            for item in skipped
        )),
        "route_outcomes_observed": len(terminal_route_audits),
        "completed_routes_observed": sum(
            item.get("status") == "completed_route" for item in terminal_route_audits
        ),
        "terminal_route_audit_statuses": dict(Counter(
            item.get("status", "missing_status") for item in terminal_route_audits
        )),
        "errors": errors,
    }
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "active_batch_manifest_snapshot.json").write_bytes(batch_manifest_bytes)
    json_path = OUTPUT_ROOT / "endpoint_alignment_audit.json"
    csv_path = OUTPUT_ROOT / "task_endpoint_alignment.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({
        "status": result["audit_status"],
        "routeable": len(rows),
        "skipped": len(skipped),
        "r4_endpoint_exact": result["routeable_endpoint_summary"]["r4_endpoints_match_truth"],
        "traditional_endpoint_exact": result["routeable_endpoint_summary"]["traditional_endpoints_match_truth"],
        "shared_endpoints": result["routeable_endpoint_summary"]["r4_and_traditional_share_endpoints"],
        "errors": len(errors),
        "output": str(json_path),
    }, ensure_ascii=False, indent=2))
    return 0 if result["audit_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
