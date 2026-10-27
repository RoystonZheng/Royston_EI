#!/usr/bin/env python3
"""Audit missing pickup-energy traces in saved NYC candidate tables."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any


EXPECTED_PROFILE_SHA256 = "b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee"
SUPERSEDED_AUDIT_ID = "nyc_seed0_pickup_energy_coverage_audit_20261027T132501Z"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(payload.encode("utf-8"))


def has_pickup_energy_gap(candidate: dict[str, Any]) -> bool:
    pickup = candidate.get("pickup_leg") or {}
    components = candidate.get("energy_components") or {}
    try:
        positive_motion = float(pickup.get("distance_m") or 0) > 0 and float(
            pickup.get("duration_sec") or 0
        ) > 0
        zero_pickup_energy = all(
            float(components.get(key) or 0) == 0
            for key in (
                "pickup_propulsion_draw_kwh",
                "pickup_regenerated_kwh",
                "pickup_auxiliary_kwh",
            )
        )
    except (TypeError, ValueError):
        return False
    return positive_motion and not pickup.get("edge_sequence") and zero_pickup_energy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--energy-manifest", required=True)
    parser.add_argument("--official-audit", required=True)
    parser.add_argument("--previous-audit")
    args = parser.parse_args()

    energy_manifest_path = Path(args.energy_manifest).resolve()
    official_audit_path = Path(args.official_audit).resolve()
    energy_manifest = json.loads(energy_manifest_path.read_text(encoding="utf-8"))
    official_audit = json.loads(official_audit_path.read_text(encoding="utf-8"))
    previous_path = Path(args.previous_audit).resolve() if args.previous_audit else None
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path else None

    errors: list[str] = []
    per_method: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    per_method_valid_pairs: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    method_energy_dependency: dict[str, set[str]] = collections.defaultdict(set)
    source_hashes: list[dict[str, str]] = []
    table_count = candidate_count = gap_count = 0
    selected_count = selected_matched = selected_gap_count = 0
    duplicate_or_missing_pair_count = 0
    table_round_count = 0
    valid_pair_totals = collections.Counter()
    valid_cells = {
        (str(row.get("case_id")), str(row.get("fleet_id")))
        for row in official_audit.get("paired_cells", [])
        if row.get("paired_cell_valid") is True
    }

    if energy_manifest.get("energy_model", {}).get("profile_sha256") != EXPECTED_PROFILE_SHA256:
        errors.append("energy_profile_hash_mismatch")
    entries = energy_manifest.get("results", [])
    if int(official_audit.get("present_result_files", -1)) != len(entries):
        errors.append("official_audit_and_energy_manifest_source_counts_differ")

    for entry in entries:
        source_path = Path(entry["source"])
        data = source_path.read_bytes()
        source_hash = sha256_bytes(data)
        if source_hash != entry.get("source_sha256"):
            errors.append(f"source_hash_mismatch:{source_path}")
            continue
        source_hashes.append({"path": str(source_path), "sha256": source_hash})
        record = json.loads(data)
        method = str(record.get("method_name", "unknown"))
        in_valid_pair = (
            int(entry.get("seed", -1)) == 0
            and (str(record.get("case_id")), str(record.get("fleet_id"))) in valid_cells
        )
        method_counts = per_method[method]
        method_counts["source_files"] += 1
        paired_counts = per_method_valid_pairs[method]
        if in_valid_pair:
            paired_counts["source_files"] += 1
        method_counts["all_round_selected_assignments"] += sum(
            int(row.get("selected_assignment_count", 0))
            for row in record.get("round_audits", [])
        )

        for round_row in record.get("round_audits", []):
            table = round_row.get("common_candidate_table") or []
            if not table:
                continue
            table_count += 1
            table_round_count += 1
            method_counts["candidate_tables"] += 1
            dependency = (round_row.get("decision_function") or {}).get("energy_dependency")
            if dependency is not None:
                method_energy_dependency[method].add(str(dependency))
            method_counts["candidate_rows"] += len(table)
            method_counts["pickup_energy_gap_candidates"] += sum(
                has_pickup_energy_gap(candidate) for candidate in table
            )
            candidate_count += len(table)
            gap_count += sum(has_pickup_energy_gap(candidate) for candidate in table)
            if in_valid_pair:
                paired_counts["candidate_tables"] += 1
                paired_counts["candidate_rows"] += len(table)
                paired_gap_rows = sum(has_pickup_energy_gap(candidate) for candidate in table)
                paired_counts["pickup_energy_gap_candidates"] += paired_gap_rows
                valid_pair_totals["candidate_tables"] += 1
                valid_pair_totals["candidate_rows"] += len(table)
                valid_pair_totals["pickup_energy_gap_candidates"] += paired_gap_rows

            expected_table_hash = round_row.get("common_candidate_table_sha256")
            if expected_table_hash and canonical_hash(table) != expected_table_hash:
                errors.append(f"candidate_table_hash_mismatch:{source_path}:{round_row.get('dispatch_round_id')}")
            if any(
                candidate.get("energy_profile_sha256") != EXPECTED_PROFILE_SHA256
                for candidate in table
            ):
                errors.append(f"candidate_energy_profile_mismatch:{source_path}:{round_row.get('dispatch_round_id')}")

            vehicles = round_row.get("selected_vehicle_ids") or []
            orders = round_row.get("selected_order_ids") or []
            if len(vehicles) != len(orders):
                duplicate_or_missing_pair_count += 1
                errors.append(f"selected_pair_list_length_mismatch:{source_path}:{round_row.get('dispatch_round_id')}")
                continue
            candidate_index: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
            for candidate in table:
                candidate_index[(str(candidate.get("vehicle_id")), str(candidate.get("order_id")))].append(candidate)
            for vehicle_id, order_id in zip(vehicles, orders):
                selected_count += 1
                method_counts["selected_pairs_in_saved_candidate_table"] += 1
                if in_valid_pair:
                    paired_counts["selected_pairs_in_saved_candidate_table"] += 1
                    valid_pair_totals["selected_pairs_in_saved_candidate_table"] += 1
                matches = candidate_index.get((str(vehicle_id), str(order_id)), [])
                if len(matches) != 1:
                    duplicate_or_missing_pair_count += 1
                    errors.append(
                        f"selected_pair_not_unique_in_saved_table:{source_path}:{round_row.get('dispatch_round_id')}"
                    )
                    continue
                selected_matched += 1
                method_counts["selected_pairs_exactly_matched"] += 1
                if in_valid_pair:
                    paired_counts["selected_pairs_exactly_matched"] += 1
                    valid_pair_totals["selected_pairs_exactly_matched"] += 1
                if has_pickup_energy_gap(matches[0]):
                    selected_gap_count += 1
                    method_counts["selected_gap_pairs_in_saved_table"] += 1
                    if in_valid_pair:
                        paired_counts["selected_gap_pairs_in_saved_table"] += 1
                        valid_pair_totals["selected_gap_pairs_in_saved_table"] += 1

    if int(official_audit.get("present_result_files", -1)) != len(source_hashes):
        errors.append("official_result_file_count_does_not_match_hashed_sources")
    expected_valid_pair_sources = len(valid_cells) * 7
    actual_valid_pair_sources = sum(int(row.get("source_files", 0)) for row in per_method_valid_pairs.values())
    if actual_valid_pair_sources != expected_valid_pair_sources:
        errors.append("valid_paired_source_count_mismatch")
    if valid_pair_totals["candidate_rows"] != valid_pair_totals["pickup_energy_gap_candidates"]:
        errors.append("valid_pair_candidate_gap_count_mismatch")
    if valid_pair_totals["selected_pairs_in_saved_candidate_table"] != valid_pair_totals["selected_pairs_exactly_matched"]:
        errors.append("valid_pair_selected_match_count_mismatch")

    previous_metadata = None
    if previous is not None:
        previous_metadata = {
            "path": str(previous_path),
            "sha256": sha256_file(previous_path),
            "audit_id": previous.get("audit_id"),
            "reported_candidate_rows": previous.get("candidate_row_count"),
            "reported_selected_gap_candidates": previous.get("selected_gap_candidate_count"),
            "reported_selection_alignment_errors": previous.get("selection_alignment_errors"),
            "disposition": "the selection_alignment_errors field has no recorded formula/source in this JSON and was not reproduced; do not interpret it as a confirmed mismatch count",
        }

    report = {
        "schema_version": "nyc_pickup_energy_coverage_audit_v2",
        "audit_id": "nyc_seed0_pickup_energy_coverage_audit_v2_20261027T1335Z",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": "passed" if not errors else "failed",
        "scope": "Hash-matched source results listed in the EV v2 offline manifest; saved common candidate tables and their persisted selection only.",
        "energy_manifest": {
            "path": str(energy_manifest_path),
            "sha256": sha256_file(energy_manifest_path),
            "included_result_count": len(entries),
            "excluded_result_count": energy_manifest.get("excluded_count"),
            "unrecomputable_count": energy_manifest.get("unrecomputable_count"),
        },
        "official_paired_audit": {
            "path": str(official_audit_path),
            "sha256": sha256_file(official_audit_path),
            "present_result_files": official_audit.get("present_result_files"),
            "fully_valid_paired_cells": official_audit.get("fully_valid_paired_cells"),
            "expected_paired_cells": official_audit.get("expected_paired_cells"),
        },
        "pickup_energy_gap_condition": "pickup distance_m > 0 AND duration_sec > 0 AND edge_sequence is empty AND pickup propulsion, regeneration, and auxiliary energy components are all zero",
        "candidate_table_round_count": table_round_count,
        "candidate_row_count": candidate_count,
        "pickup_energy_gap_candidate_count": gap_count,
        "selected_pair_count_with_saved_candidate_table": selected_count,
        "selected_pairs_exactly_matched_to_one_candidate": selected_matched,
        "selected_gap_pair_count_with_saved_candidate_table": selected_gap_count,
        "selected_pair_alignment_errors_within_saved_tables": duplicate_or_missing_pair_count,
        "per_method": {method: dict(counts) for method, counts in sorted(per_method.items())},
        "fully_valid_paired_scope": {
            "paired_cell_count": len(valid_cells),
            "expected_source_file_count": expected_valid_pair_sources,
            "source_file_count": actual_valid_pair_sources,
            **dict(valid_pair_totals),
            "per_method": {
                method: dict(counts) for method, counts in sorted(per_method_valid_pairs.items())
            },
        },
        "per_method_energy_dependency": {
            method: sorted(values) for method, values in sorted(method_energy_dependency.items())
        },
        "previous_audit": previous_metadata,
        "selection_pair_evidence": {
            "source_file": "dispatch_experiment/runner.py",
            "source_sha256": sha256_file(Path(__file__).resolve().parents[1] / "dispatch_experiment/runner.py"),
            "rule": "selected_vehicle_ids and selected_order_ids are generated by separate projections over the same selected candidate sequence",
            "scope_limit": "Only the saved initial common_candidate_table is available in the audited compact records; this audit cannot attribute later-round selections to their full candidate tables.",
        },
        "source_files": source_hashes,
        "model_calls": 0,
        "simulator_calls": 0,
        "limitations": [
            "The audited zero pickup-energy components are unsupported by road-edge motion traces and must not be interpreted as measured zero energy.",
            "The candidate-table audit covers the saved common initial decision input, not every later dispatch round.",
            "This audit establishes input coverage only; it does not estimate method performance or prove that the saved final assignment was selected using corrected pickup energy.",
            "Energy-sensitive policy comparisons require a corrected input dataflow and an isolated, paired rerun of only affected frozen slots.",
        ],
        "errors": errors,
    }
    output_path = energy_manifest_path.parent / "pickup_energy_coverage_audit_v2.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "audit": str(output_path),
        "status": report["status"],
        "candidate_rows": candidate_count,
        "pickup_energy_gap_candidates": gap_count,
        "selected_pairs_with_saved_candidate_table": selected_count,
        "selected_gap_pairs": selected_gap_count,
        "selection_pair_alignment_errors": duplicate_or_missing_pair_count,
        "errors": errors[:10],
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
