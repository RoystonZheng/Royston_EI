#!/usr/bin/env python3
"""Verify the versioned offline NYC dispatch energy output and its indexes."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_OUTPUT = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_energy_v2_offline_20261026"
)
EXPECTED_PROFILE_SHA256 = (
    "b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_row(row: Dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    root = Path(args.output_dir).resolve()
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    errors: List[str] = []

    if manifest.get("schema_version") != "nyc_dispatch_energy_recalculation_manifest_v1":
        errors.append("overall_manifest_schema_mismatch")
    if manifest.get("energy_model", {}).get("profile_sha256") != EXPECTED_PROFILE_SHA256:
        errors.append("overall_manifest_profile_hash_mismatch")
    if not manifest.get("output_coverage_complete"):
        errors.append("overall_manifest_output_coverage_not_complete")

    manifest_results = manifest.get("results", [])
    top_result_outputs = [str(Path(row["output"]).resolve()) for row in manifest_results]
    if len(set(top_result_outputs)) != len(top_result_outputs):
        errors.append("duplicate_output_paths_in_overall_manifest")

    all_seed_results = []
    seed_summaries = []
    per_seed_index_rows = []
    for summary in manifest.get("seed_summaries", []):
        seed = int(summary["seed"])
        seed_root = root / f"seed_{seed}"
        seed_manifest_path = seed_root / "manifest.json"
        seed_index_path = seed_root / "recalculation_index.jsonl"
        if not seed_manifest_path.is_file() or not seed_index_path.is_file():
            errors.append("seed_{}_manifest_or_index_missing".format(seed))
            continue
        seed_manifest = read_json(seed_manifest_path)
        if int(seed_manifest.get("seed", -1)) != seed:
            errors.append("seed_{}_manifest_identity_mismatch".format(seed))
        if seed_manifest.get("energy_model", {}).get("profile_sha256") != EXPECTED_PROFILE_SHA256:
            errors.append("seed_{}_profile_hash_mismatch".format(seed))
        if not seed_manifest.get("output_coverage_complete"):
            errors.append("seed_{}_output_coverage_not_complete".format(seed))
        seed_results = seed_manifest.get("results", [])
        if len(seed_results) != int(seed_manifest.get("recalculated_output_count", -1)):
            errors.append("seed_{}_result_count_mismatch".format(seed))
        all_seed_results.extend(seed_results)
        rows = [json.loads(line) for line in seed_index_path.read_text(encoding="utf-8").splitlines() if line]
        per_seed_index_rows.extend(rows)
        if len(rows) != (
            len(seed_manifest.get("results", []))
            + len(seed_manifest.get("excluded", []))
            + len(seed_manifest.get("excluded_preexisting_outputs", []))
            + len(seed_manifest.get("unrecomputable", []))
        ):
            errors.append("seed_{}_index_row_count_mismatch".format(seed))
        seed_summaries.append({
            "seed": seed,
            "status": seed_manifest.get("status"),
            "source_result_count": seed_manifest.get("source_result_count"),
            "recalculated_output_count": seed_manifest.get("recalculated_output_count"),
            "paired_completed_cells": seed_manifest.get("paired_completed_cells"),
            "eligible_evaluation_cells": seed_manifest.get("eligible_evaluation_cells"),
            "missing_expected_method_result_count": seed_manifest.get("missing_expected_method_result_count"),
            "missing_by_method": seed_manifest.get("missing_by_method"),
        })

    seed_result_outputs = [str(Path(row["output"]).resolve()) for row in all_seed_results]
    if collections.Counter(top_result_outputs) != collections.Counter(seed_result_outputs):
        errors.append("overall_and_per_seed_result_output_sets_differ")

    output_hash_rows = []
    for row in manifest_results:
        output_path = Path(row["output"])
        if not output_path.is_file():
            errors.append("output_missing:" + str(output_path))
            continue
        try:
            output = read_json(output_path)
        except Exception as error:
            errors.append("output_unreadable:{}:{}".format(output_path, error))
            continue
        if output.get("schema_version") != "nyc_dispatch_energy_recalculation_v1":
            errors.append("output_schema_mismatch:" + str(output_path))
        if output.get("source_result_sha256") != row.get("source_sha256"):
            errors.append("output_source_hash_disagrees_with_index:" + str(output_path))
        source_path = Path(output.get("source_result", ""))
        if not source_path.is_file():
            errors.append("source_result_missing:" + str(source_path))
        elif sha256_file(source_path) != output.get("source_result_sha256"):
            errors.append("source_result_hash_changed:" + str(source_path))
        if output.get("energy_model", {}).get("profile_sha256") != EXPECTED_PROFILE_SHA256:
            errors.append("output_profile_hash_mismatch:" + str(output_path))
        if output.get("legacy_reconciliation", {}).get("status") != "passed":
            errors.append("output_legacy_reconciliation_not_passed:" + str(output_path))
        assignments = output.get("assignments", [])
        if len(assignments) != int(output.get("assignment_count", -1)):
            errors.append("output_assignment_count_mismatch:" + str(output_path))
        for assignment in assignments:
            if assignment.get("pickup_route", {}).get("energy_trace_basis") != "synthetic_centroid_proxy_virtual_flat_edge":
                errors.append("pickup_energy_basis_mismatch:" + str(output_path))
                break
        if not math.isfinite(float(output.get("metrics", {}).get("ev_longitudinal_v2_total_energy_kwh", float("nan")))):
            errors.append("v2_energy_metric_not_finite:" + str(output_path))
        output_hash_rows.append({
            "output": str(output_path),
            "output_sha256": sha256_file(output_path),
            "source_result": str(source_path),
            "source_result_sha256": output.get("source_result_sha256"),
            "seed": output.get("seed"),
            "case_id": output.get("scenario_id"),
            "fleet_id": output.get("fleet_id"),
            "method_name": output.get("method_name"),
            "profile_sha256": output.get("energy_model", {}).get("profile_sha256"),
        })

    actual_output_files = {
        str(path.resolve())
        for path in root.rglob("*.energy_v2_offline.json")
    }
    indexed_output_files = set(top_result_outputs) | {
        str(Path(row["output"]).resolve())
        for row in manifest.get("excluded_preexisting_outputs", [])
        if row.get("output")
    }
    if actual_output_files != indexed_output_files:
        errors.append("physical_output_files_do_not_match_indexed_and_excluded_outputs")

    overall_index = [
        json.loads(line)
        for line in (root / "recalculation_index.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    if collections.Counter(map(canonical_row, overall_index)) != collections.Counter(map(canonical_row, per_seed_index_rows)):
        errors.append("overall_index_differs_from_per_seed_indexes")
    if len(overall_index) != (
        manifest.get("included_result_count", 0)
        + manifest.get("excluded_count", 0)
        + manifest.get("excluded_preexisting_output_count", 0)
        + manifest.get("unrecomputable_count", 0)
    ):
        errors.append("overall_index_row_count_mismatch")

    hash_ledger_path = root / "output_hashes.jsonl"
    hash_ledger_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output_hash_rows),
        encoding="utf-8",
    )
    audit = {
        "schema_version": "nyc_dispatch_energy_recalculation_integrity_audit_v1",
        "status": "passed" if not errors else "failed",
        "created_at_local": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "overall_manifest": str(manifest_path),
        "overall_index": str(root / "recalculation_index.jsonl"),
        "output_hash_ledger": str(hash_ledger_path),
        "manifest_input_result_count": manifest.get("input_result_count"),
        "verified_output_count": len(output_hash_rows),
        "physical_output_file_count": len(actual_output_files),
        "overall_index_row_count": len(overall_index),
        "seed_summaries": seed_summaries,
        "errors": errors,
    }
    audit_path = root / "integrity_audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
