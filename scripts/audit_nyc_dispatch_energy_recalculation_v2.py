#!/usr/bin/env python3
"""Integrity audit for NYC EV v2 offline recalculation sidecars."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any


EXPECTED_PROFILE_SHA256 = "b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-audit")
    args = parser.parse_args()

    root = Path(args.output_dir).resolve()
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    errors: list[str] = []
    verified: list[dict[str, Any]] = []

    if manifest.get("schema_version") != "nyc_dispatch_energy_recalculation_manifest_v1":
        errors.append("manifest_schema_mismatch")
    if manifest.get("energy_model", {}).get("profile_sha256") != EXPECTED_PROFILE_SHA256:
        errors.append("manifest_energy_profile_mismatch")
    if manifest.get("output_coverage_complete") is not True:
        errors.append("manifest_output_coverage_incomplete")

    entries = manifest.get("results", [])
    output_paths: list[str] = []
    source_paths: list[str] = []
    for index, entry in enumerate(entries):
        source = Path(entry.get("source", ""))
        output = Path(entry.get("output", ""))
        source_paths.append(str(source.resolve()))
        output_paths.append(str(output.resolve()))
        label = f"result[{index}]"
        if not source.is_file():
            errors.append(f"{label}:source_missing")
            continue
        source_sha = sha256(source)
        if source_sha != entry.get("source_sha256"):
            errors.append(f"{label}:source_hash_mismatch")
        if not output.is_file():
            errors.append(f"{label}:output_missing")
            continue
        try:
            payload = read_json(output)
        except Exception as exc:  # keep the exact unreadable artifact visible
            errors.append(f"{label}:output_unreadable:{type(exc).__name__}")
            continue

        if payload.get("schema_version") != "nyc_dispatch_energy_recalculation_v2":
            errors.append(f"{label}:output_schema_mismatch")
        if payload.get("source_result_sha256") != source_sha:
            errors.append(f"{label}:output_source_hash_mismatch")
        if payload.get("energy_model", {}).get("profile_sha256") != EXPECTED_PROFILE_SHA256:
            errors.append(f"{label}:output_energy_profile_mismatch")
        reconcile = payload.get("online_ev2_reconciliation", {})
        if reconcile.get("status") != "passed":
            errors.append(f"{label}:online_ev2_reconciliation_not_passed")
        for key in ("energy_delta_kwh", "empty_distance_delta_m"):
            if not finite_number(reconcile.get(key)) or abs(float(reconcile[key])) > 1e-8:
                errors.append(f"{label}:reconciliation_delta_invalid:{key}")

        assignments = payload.get("assignments", [])
        if len(assignments) != int(payload.get("assignment_count", -1)):
            errors.append(f"{label}:assignment_count_mismatch")
        summed_net = 0.0
        for assignment_index, assignment in enumerate(assignments):
            energy = assignment.get("ev_longitudinal_v2_energy", {})
            if not finite_number(energy.get("net_kwh")):
                errors.append(f"{label}:assignment_energy_invalid:{assignment_index}")
            else:
                summed_net += float(energy["net_kwh"])
            if assignment.get("pickup_route", {}).get("energy_trace_basis") != "synthetic_centroid_proxy_virtual_flat_edge":
                errors.append(f"{label}:pickup_proxy_basis_mismatch:{assignment_index}")
        corrected = payload.get("metrics", {}).get(
            "corrected_fixed_assignment_ev_longitudinal_v2_total_energy_kwh"
        )
        if not finite_number(corrected) or abs(float(corrected) - summed_net) > 1e-6:
            errors.append(f"{label}:corrected_energy_total_mismatch")

        verified.append({
            "source": str(source),
            "source_sha256": source_sha,
            "output": str(output),
            "output_sha256": sha256(output),
            "seed": entry.get("seed"),
            "case_id": entry.get("case_id"),
            "fleet_id": entry.get("fleet_id"),
            "method_name": entry.get("method_name"),
            "assignment_count": len(assignments),
        })

    if len(set(source_paths)) != len(source_paths):
        errors.append("duplicate_source_paths")
    if len(set(output_paths)) != len(output_paths):
        errors.append("duplicate_output_paths")
    actual_output_paths = {
        str(path.resolve()) for path in root.rglob("*.energy_v2_offline.json")
    }
    indexed_output_paths = set(output_paths) | {
        str(Path(row["output"]).resolve())
        for row in manifest.get("excluded_preexisting_outputs", [])
        if row.get("output")
    }
    if actual_output_paths != indexed_output_paths:
        errors.append("physical_outputs_do_not_match_index")
    if len(verified) != int(manifest.get("recalculated_count", -1)):
        errors.append("verified_output_count_mismatch")

    source_audit_path = Path(args.source_audit).resolve() if args.source_audit else None
    source_audit_sha = None
    source_audit_summary = None
    if source_audit_path:
        source_audit = read_json(source_audit_path)
        source_audit_sha = sha256(source_audit_path)
        if int(source_audit.get("present_result_files", -1)) != len(entries):
            errors.append("source_official_audit_result_count_mismatch")
        source_audit_summary = {
            "path": str(source_audit_path),
            "sha256": source_audit_sha,
            "present_result_files": source_audit.get("present_result_files"),
            "fully_valid_paired_cells": source_audit.get("fully_valid_paired_cells"),
            "expected_paired_cells": source_audit.get("expected_paired_cells"),
        }

    report = {
        "schema_version": "nyc_dispatch_energy_recalculation_v2_integrity_audit_v1",
        "status": "passed" if not errors else "failed",
        "audited_at_local": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_manifest": str(manifest_path),
        "source_manifest_sha256": sha256(manifest_path),
        "source_manifest_status": manifest.get("status"),
        "input_result_count": manifest.get("input_result_count"),
        "excluded_count": manifest.get("excluded_count"),
        "unrecomputable_count": manifest.get("unrecomputable_count"),
        "verified_output_count": len(verified),
        "physical_output_count": len(actual_output_paths),
        "output_coverage_complete": actual_output_paths == indexed_output_paths,
        "energy_profile_sha256": manifest.get("energy_model", {}).get("profile_sha256"),
        "source_official_audit": source_audit_summary,
        "model_calls": 0,
        "dispatch_policy_rerun": False,
        "limitations": [
            "EV v2 energy is an offline estimate for frozen assignments, not measured vehicle telemetry.",
            "The synthetic centroid pickup proxy is not a measured road-level pickup trajectory.",
            "The corrected energy values do not show that EV v2 energy affected the original dispatch decision.",
            "Missing evaluation cells remain missing and are not inferred from the recalculation sidecar.",
        ],
        "errors": errors,
        "verified_outputs": verified,
    }
    report_path = root / "v2_integrity_audit.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "audit": str(report_path),
        "status": report["status"],
        "verified_output_count": len(verified),
        "physical_output_count": len(actual_output_paths),
        "errors": errors[:12],
        "source_manifest_sha256": report["source_manifest_sha256"],
        "source_official_audit_sha256": source_audit_sha,
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
