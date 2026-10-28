#!/usr/bin/env python3
"""Bind saved pure-LLM grid EV v2 energy rows to their recorded token usage.

This is an offline input-inventory sidecar. It does not score routes, alter
source results, make model calls, or infer account billing from token rates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENERGY_MANIFEST = ROOT / (
    "outputs/route_layer/"
    "pure_llm_grid_energy_v2_recalculation_20261027_current_module_path_recovered_v2/"
    "manifest.json"
)
DEFAULT_OUTPUT = ROOT / (
    "outputs/route_layer/"
    "pure_llm_energy_token_input_alignment_v1_20261028"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def token_status(resources: dict[str, Any], metadata: dict[str, Any], method: dict[str, Any]) -> str:
    input_tokens = resources.get("input_tokens")
    output_tokens = resources.get("output_tokens")
    if not (isinstance(input_tokens, int) and isinstance(output_tokens, int)):
        return "usage_missing"
    if input_tokens + output_tokens == 0 and (metadata.get("llm_error") or method.get("status") == "failed"):
        return "zero_usage_reported_request_error_billing_unverified"
    if input_tokens + output_tokens == 0:
        return "zero_usage_recorded_billing_unverified"
    return "usage_recorded_rate_estimate_only"


def summarize(records: list[dict[str, Any]], source_runs: list[dict[str, Any]]) -> dict[str, Any]:
    totals = Counter()
    by_run: dict[str, Counter[str]] = defaultdict(Counter)
    by_run_scope: dict[str, Counter[str]] = defaultdict(Counter)
    for row in records:
        run_key = row["source_experiment"]
        scope_key = f"{run_key}:{row['scope']}"
        for counter in (totals, by_run[run_key], by_run_scope[scope_key]):
            counter["records"] += 1
            counter[f"energy_status:{row['energy_status']}"] += 1
            counter[f"token_status:{row['token_usage_status']}"] += 1
            counter[f"score_status:{row['decision_score_status']}"] += 1
            if row["input_tokens"] is not None:
                counter["input_tokens"] += row["input_tokens"]
            if row["output_tokens"] is not None:
                counter["output_tokens"] += row["output_tokens"]
            if row["api_cost_cny_rate_estimate"] is not None:
                counter["api_cost_cny_rate_estimate"] += row["api_cost_cny_rate_estimate"]
            if row["api_cost_source_record_cny"] is not None:
                counter["api_cost_source_record_cny"] += row["api_cost_source_record_cny"]
            if row["api_cost_rate_match"] is True:
                counter["api_cost_rate_match"] += 1
            elif row["api_cost_rate_match"] is False:
                counter["api_cost_rate_mismatch"] += 1
            if row["route_candidate_count"] is not None:
                counter["route_candidate_count"] += row["route_candidate_count"]
            if row["method_count"] is not None:
                counter["method_count"] += row["method_count"]

    manifest_mismatches = []
    for run in source_runs:
        source_manifest = run["source_manifest_data"]
        for scope, summary_key in (("formal", "formal_summary"), ("orders", "order_summary")):
            matching = [
                row
                for row in records
                if row["source_experiment"] == run["source_experiment"] and row["scope"] == scope
            ]
            manifest_totals = Counter()
            for seed_detail in source_manifest.get("seeds_detail", []):
                summary = seed_detail.get(summary_key, {})
                manifest_totals["input_tokens"] += summary.get("total_input_tokens", 0) or 0
                manifest_totals["output_tokens"] += summary.get("total_output_tokens", 0) or 0
                manifest_totals["llm_calls"] += summary.get("total_llm_calls", 0) or 0
            joined_totals = Counter()
            for row in matching:
                joined_totals["input_tokens"] += row["input_tokens"] or 0
                joined_totals["output_tokens"] += row["output_tokens"] or 0
                joined_totals["llm_calls"] += row["llm_calls_recorded"] or 0
            equal = all(
                manifest_totals[key] == joined_totals[key]
                for key in ("input_tokens", "output_tokens", "llm_calls")
            )
            if not equal:
                manifest_mismatches.append(
                    {
                        "source_experiment": run["source_experiment"],
                        "scope": scope,
                        "joined_result_totals": dict(joined_totals),
                        "source_manifest_totals": dict(manifest_totals),
                    }
                )

    return {
        "schema_version": "pure_llm_energy_token_input_alignment_summary_v1",
        "status": "completed_offline_input_inventory",
        "record_count": len(records),
        "totals": dict(totals),
        "by_run": {key: dict(value) for key, value in sorted(by_run.items())},
        "by_run_scope": {key: dict(value) for key, value in sorted(by_run_scope.items())},
        "source_manifest_token_total_mismatches": manifest_mismatches,
        "decision_score_status": "not_computed",
        "decision_score_blockers": [
            "no frozen local electricity tariff bound to this generic EV v2 grid scenario",
            "the saved pure-LLM records do not provide a complete shared feasible candidate pool per decision",
            "the meeting's profit threshold and normalization rule remain unspecified",
            "order-level revenue is not present in this route-planning result set",
        ],
        "billing_boundary": (
            "Token amounts are usage multiplied by the rate recorded in each saved result. "
            "They are rate-based estimates, not account invoices. Zero usage attached to an "
            "error remains billing-unverified."
        ),
        "electricity_tariff_boundary": (
            "No electricity cost was calculated. Legacy Tesla Supercharger references embedded "
            "in source result files are preserved as source metadata and are not treated as a "
            "frozen tariff for the generic EV v2 model."
        ),
        "source_runs": [
            {k: v for k, v in run.items() if k != "source_manifest_data"}
            for run in source_runs
        ],
    }


def build(output: Path) -> dict[str, Any]:
    initial_energy_manifest_sha = sha256(ENERGY_MANIFEST)
    energy_manifest = read_json(ENERGY_MANIFEST)
    records: list[dict[str, Any]] = []
    source_runs: list[dict[str, Any]] = []
    result_cache: dict[Path, tuple[dict[str, Any], str]] = {}
    result_hashes: dict[str, str] = {}
    bound_source_paths: dict[Path, str] = {ENERGY_MANIFEST: initial_energy_manifest_sha}

    for run in energy_manifest["runs"]:
        source_experiment = ROOT / run["source_experiment"]
        audit_path = ROOT / run["output"]
        source_manifest_path = source_experiment / "manifest.json"
        source_manifest_data = read_json(source_manifest_path)
        bound_source_paths[source_manifest_path] = sha256(source_manifest_path)
        bound_source_paths[audit_path] = sha256(audit_path)
        source_runs.append(
            {
                "source_experiment": run["source_experiment"],
                "source_status": run.get("source_status"),
                "source_manifest_path": str(source_manifest_path.relative_to(ROOT)),
                "source_manifest_sha256": sha256(source_manifest_path),
                "expected_source_manifest_sha256": run.get("source_manifest_sha256"),
                "source_manifest_hash_matches_energy_manifest": (
                    sha256(source_manifest_path) == run.get("source_manifest_sha256")
                ),
                "energy_audit_path": str(audit_path.relative_to(ROOT)),
                "energy_audit_sha256": sha256(audit_path),
                "expected_energy_audit_sha256": run.get("output_sha256"),
                "energy_audit_hash_matches_energy_manifest": sha256(audit_path) == run.get("output_sha256"),
                "source_manifest_data": source_manifest_data,
            }
        )
        energy_audit = read_json(audit_path)

        for energy_record in energy_audit["records"]:
            scope = energy_record.get("scope")
            scope_file = {"formal": "formal", "orders": "order"}.get(scope)
            seed = energy_record.get("seed")
            task_id = energy_record.get("source_task_id") or energy_record.get("task_id")
            method_name = energy_record.get("method_name")
            if scope_file is None:
                raise ValueError(f"unsupported scope {scope!r} in {audit_path}")
            result_path = (
                source_experiment
                / "seeds"
                / f"seed_{seed}"
                / f"pure_llm_{scope_file}_routes_costs_seed_{seed}.json"
            )
            if result_path not in result_cache:
                result_cache[result_path] = (read_json(result_path), sha256(result_path))
                bound_source_paths[result_path] = result_cache[result_path][1]
            result_document, result_sha = result_cache[result_path]
            result_hashes[str(result_path.relative_to(ROOT))] = result_sha

            matching_rows = [r for r in result_document.get("results", []) if r.get("task_id") == task_id]
            if len(matching_rows) != 1:
                raise ValueError(
                    f"expected one source result for {task_id}, found {len(matching_rows)} in {result_path}"
                )
            source_row = matching_rows[0]
            methods = source_row.get("methods") or []
            matching_methods = [m for m in methods if m.get("method_name") == method_name]
            if len(matching_methods) != 1:
                raise ValueError(
                    f"expected one method {method_name} for {task_id}, found {len(matching_methods)}"
                )
            method = matching_methods[0]
            resources = method.get("resources") or {}
            metadata = method.get("metadata") or {}
            monetary = method.get("monetary_cost") or {}
            price_profile = result_document.get("price_profile") or {}
            input_tokens = resources.get("input_tokens")
            output_tokens = resources.get("output_tokens")
            usage_present = isinstance(input_tokens, int) and isinstance(output_tokens, int)
            token_total = input_tokens + output_tokens if usage_present else None
            rate = price_profile.get("api_price_per_token")
            estimated_api_cost = token_total * rate if token_total is not None and is_number(rate) else None
            source_api_cost = monetary.get("api_cost_amount")
            if source_api_cost is None:
                rate_match = None
            elif estimated_api_cost is None:
                rate_match = None
            else:
                rate_match = math.isclose(
                    float(source_api_cost), float(estimated_api_cost), rel_tol=1e-9, abs_tol=1e-9
                )
            energy = energy_record.get("proposal_energy_audit_v2") or {}
            route_candidates = method.get("routes")
            route_candidate_count = len(route_candidates) if isinstance(route_candidates, list) else None
            route_energy_components = energy.get("hypothetical_energy_components") or {}
            records.append(
                {
                    "source_experiment": run["source_experiment"],
                    "source_manifest_status": run.get("source_status"),
                    "seed": seed,
                    "scope": scope,
                    "task_id": task_id,
                    "method_name": method_name,
                    "source_result_path": str(result_path.relative_to(ROOT)),
                    "source_result_sha256": result_sha,
                    "source_result_method_count": len(methods),
                    "method_status": method.get("status"),
                    "failure_reason": method.get("failure_reason"),
                    "response_received": energy_record.get("response_received"),
                    "validation_status": (method.get("validation") or {}).get("status"),
                    "valid_route_count": method.get("valid_route_count"),
                    "route_candidate_count": route_candidate_count,
                    "method_count": len(methods),
                    "energy_model_version": energy.get("energy_model_version"),
                    "energy_profile_sha256": energy.get("energy_profile_sha256"),
                    "energy_status": energy.get("status", "not_estimable"),
                    "energy_trace_fidelity": energy.get("energy_trace_fidelity"),
                    "route_energy_kwh": energy.get("route_energy_estimate_kwh"),
                    "hypothetical_net_energy_kwh": route_energy_components.get("net_kwh"),
                    "validated_prefix_energy_kwh": energy.get("validated_prefix_energy_estimate_kwh"),
                    "inferred_edge_count": energy.get("reverse_edge_inferred_moves", 0)
                    + energy.get("proxy_edge_moves", 0),
                    "input_tokens": input_tokens if usage_present else None,
                    "output_tokens": output_tokens if usage_present else None,
                    "token_usage_status": token_status(resources, metadata, method),
                    "llm_calls_recorded": resources.get("llm_calls"),
                    "api_price_per_token_cny_reference": rate if is_number(rate) else None,
                    "api_cost_cny_rate_estimate": estimated_api_cost,
                    "api_cost_source_record_cny": source_api_cost if is_number(source_api_cost) else None,
                    "api_cost_rate_match": rate_match,
                    "compute_cost_cny_source_record": monetary.get("compute_cost_amount"),
                    "source_currency": monetary.get("currency"),
                    "billing_status": (
                        "rate_estimate_not_account_invoice"
                        if token_total and token_total > 0 and is_number(rate)
                        else "billing_unverified"
                    ),
                    "legacy_electricity_reference": {
                        "price_per_kwh": price_profile.get("electricity_price_per_kwh"),
                        "pricing_version": price_profile.get("pricing_version"),
                        "source": price_profile.get("price_source"),
                        "used_for_ev2_billing": False,
                    },
                    "order_revenue": None,
                    "order_revenue_status": "not_present_in_route_planning_result",
                    "electricity_cost_cny": None,
                    "electricity_cost_status": "blocked_missing_frozen_grid_tariff",
                    "decision_score_status": "blocked_no_frozen_common_candidate_pool_or_profit_rule",
                }
            )

    records.sort(key=lambda row: (row["source_experiment"], row["scope"], row["seed"], row["task_id"]))
    for source_path, expected_sha in bound_source_paths.items():
        if sha256(source_path) != expected_sha:
            raise RuntimeError(f"source_changed_during_inventory:{source_path}")
    summary = summarize(records, source_runs)
    output.mkdir(parents=True, exist_ok=True)
    records_path = output / "energy_token_inputs_v1.jsonl"
    summary_path = output / "summary.json"
    write_jsonl(records_path, records)
    write_json(summary_path, summary)

    run_bindings = [
        {k: v for k, v in run.items() if k != "source_manifest_data"}
        for run in source_runs
    ]
    manifest = {
        "schema_version": "pure_llm_energy_token_input_alignment_manifest_v1",
        "status": "completed_offline_input_inventory",
        "record_count": len(records),
        "source_energy_manifest_path": str(ENERGY_MANIFEST.relative_to(ROOT)),
        "source_energy_manifest_sha256": sha256(ENERGY_MANIFEST),
        "energy_model_module_sha256": energy_manifest.get("energy_model_module_sha256"),
        "calculation_script_path": str(Path(__file__).resolve().relative_to(ROOT)),
        "calculation_script_sha256": sha256(Path(__file__).resolve()),
        "source_runs": run_bindings,
        "source_result_files": [
            {"path": path, "sha256": digest} for path, digest in sorted(result_hashes.items())
        ],
        "decision_scores_generated": False,
        "source_results_modified": False,
        "model_calls": 0,
        "route_reruns": 0,
        "outputs": {
            records_path.name: sha256(records_path),
            summary_path.name: sha256(summary_path),
        },
        "limitations": summary["decision_score_blockers"] + [
            "Recorded token costs use the saved per-token reference rate and are not actual billing.",
            "Rows with zero tokens and request errors are billing-unverified, not proven zero-cost.",
            "EV v2 estimates for invalid or truncated proposals remain hypothetical, not executed energy.",
        ],
    }
    write_json(output / "manifest.json", manifest)
    return {"manifest_path": output / "manifest.json", "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not ENERGY_MANIFEST.exists():
        raise SystemExit(f"missing EV v2 manifest: {ENERGY_MANIFEST}")
    result = build(args.output.resolve())
    totals = result["summary"]["totals"]
    print(
        json.dumps(
            {
                "manifest": str(result["manifest_path"]),
                "record_count": result["summary"]["record_count"],
                "energy_status_counts": {
                    key.split(":", 1)[1]: value
                    for key, value in totals.items()
                    if key.startswith("energy_status:")
                },
                "token_status_counts": {
                    key.split(":", 1)[1]: value
                    for key, value in totals.items()
                    if key.startswith("token_status:")
                },
                "route_candidate_count": totals.get("route_candidate_count"),
                "input_tokens": totals.get("input_tokens"),
                "output_tokens": totals.get("output_tokens"),
                "api_cost_cny_rate_estimate": totals.get("api_cost_cny_rate_estimate"),
                "source_manifest_mismatch_count": len(
                    result["summary"]["source_manifest_token_total_mismatches"]
                ),
                "decision_scores_generated": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
