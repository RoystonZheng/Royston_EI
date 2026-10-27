#!/usr/bin/env python3
"""Integrity audit for fixed-assignment NYC EVv2 energy recalculation v2."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def selected_pairs(record: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    pairs: List[Tuple[str, str, str]] = []
    for row in record.get("round_audits") or []:
        round_id = str(row.get("dispatch_round_id", ""))
        vehicles = row.get("selected_vehicle_ids") or []
        orders = row.get("selected_order_ids") or []
        if len(vehicles) != len(orders):
            raise ValueError("selected_vehicle_order_list_length_mismatch")
        pairs.extend((round_id, str(vehicle), str(order)) for vehicle, order in zip(vehicles, orders))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="fixed-assignment recalculation v2 directory")
    args = parser.parse_args()
    output_dir = Path(args.output_dir).resolve()
    manifest_path = output_dir / "manifest.json"
    manifest = read_json(manifest_path)
    errors: List[Dict[str, Any]] = []
    verified_outputs: List[Dict[str, Any]] = []
    checked = 0
    pickup_kwh = 0.0
    corrected_kwh = 0.0
    online_kwh = 0.0

    rows = manifest.get("results") or []
    if manifest.get("schema_version") != "nyc_dispatch_energy_recalculation_manifest_v1":
        errors.append({"check": "manifest_schema", "actual": manifest.get("schema_version")})
    if len(rows) != int(manifest.get("included_result_count", -1)):
        errors.append({"check": "manifest_index_count", "rows": len(rows), "included": manifest.get("included_result_count")})
    script_sha = manifest.get("recalculation_script_sha256")
    if not script_sha:
        errors.append({"check": "recalculation_script_sha_missing"})

    for row in rows:
        source_path = Path(row.get("source", ""))
        result_path = Path(row.get("output", ""))
        try:
            if not source_path.is_file() or sha256_file(source_path) != row.get("source_sha256"):
                raise ValueError("source_result_hash_mismatch")
            result = read_json(result_path)
            source = read_json(source_path)
            if result.get("schema_version") != "nyc_dispatch_energy_recalculation_v2":
                raise ValueError("result_schema_mismatch")
            if result.get("source_result_sha256") != row.get("source_sha256"):
                raise ValueError("result_source_hash_mismatch")
            if result.get("recalculation_script_sha256") != script_sha:
                raise ValueError("result_script_hash_mismatch")
            if result.get("energy_model", {}).get("profile_sha256") != manifest.get("energy_model", {}).get("profile_sha256"):
                raise ValueError("energy_profile_hash_mismatch")
            if result.get("online_ev2_reconciliation", {}).get("status") != "passed":
                raise ValueError("online_ev2_reconciliation_not_passed")
            if result.get("selection_correction_status") != "not_applied_fixed_assignments_only":
                raise ValueError("selection_must_remain_fixed")

            metrics = result.get("metrics") or {}
            stored = float(metrics["stored_online_ev_longitudinal_v2_total_energy_kwh"])
            reconstructed = float(metrics["reconstructed_online_ev_longitudinal_v2_total_energy_kwh"])
            corrected = float(metrics["corrected_fixed_assignment_ev_longitudinal_v2_total_energy_kwh"])
            correction = float(metrics["pickup_proxy_correction_kwh"])
            if not all(map(finite, (stored, reconstructed, corrected, correction))):
                raise ValueError("nonfinite_energy_metric")
            tolerance = max(1e-6, abs(stored) * 1e-8)
            if abs(stored - reconstructed) > tolerance:
                raise ValueError("online_energy_reconciliation_delta_exceeds_tolerance")
            if abs(corrected - reconstructed - correction) > tolerance:
                raise ValueError("corrected_energy_component_identity_failed")

            expected = sorted(selected_pairs(source))
            actual = sorted(
                (str(item.get("dispatch_round_id", "")), str(item.get("vehicle_id", "")), str(item.get("order_id", "")))
                for item in (result.get("assignments") or [])
            )
            if expected != actual:
                raise ValueError("fixed_assignment_pair_set_changed")
            if len(actual) != int(result.get("assignment_count", -1)):
                raise ValueError("assignment_count_mismatch")

            for assignment in result.get("assignments") or []:
                route = assignment.get("pickup_route") or {}
                energy = assignment.get("ev_longitudinal_v2_energy") or {}
                if route.get("energy_trace_basis") != "synthetic_centroid_proxy_virtual_flat_edge":
                    raise ValueError("pickup_trace_basis_missing_or_wrong")
                if float(route.get("distance_m") or 0) > 1e-9 and not (route.get("edge_sequence") or []):
                    pickup = energy.get("pickup") or {}
                    if not finite(pickup.get("net_kwh")) or float(pickup.get("net_kwh") or 0) <= 0:
                        raise ValueError("positive_proxy_pickup_has_no_energy")

            checked += 1
            verified_outputs.append({
                "source": str(source_path),
                "source_sha256": row.get("source_sha256"),
                "output": str(result_path),
                "output_sha256": sha256_file(result_path),
                "case_id": result.get("scenario_id"),
                "fleet_id": result.get("fleet_id"),
                "method_name": result.get("method_name"),
                "assignment_count": result.get("assignment_count"),
            })
            pickup_kwh += correction
            corrected_kwh += corrected
            online_kwh += stored
        except Exception as error:
            errors.append({"source": str(source_path), "output": str(result_path), "error": str(error)})

    audit = {
        "schema_version": "nyc_dispatch_energy_recalculation_integrity_audit_v2",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "overall_status": "passed" if not errors and manifest.get("output_coverage_complete") is True else "failed",
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "recalculation_script_sha256": script_sha,
        "source_result_count": len(rows),
        "verified_output_count": checked,
        "online_energy_total_kwh_across_method_runs": online_kwh,
        "fixed_assignment_corrected_energy_total_kwh_across_method_runs": corrected_kwh,
        "synthetic_pickup_proxy_correction_total_kwh_across_method_runs": pickup_kwh,
        "verified_outputs": verified_outputs,
        "interpretation": "Energy totals are sums across method-run files and are not a pooled citywide estimate. Recalculation preserves selected assignments; it does not repair decisions made with incomplete pickup energy.",
        "errors": errors,
    }
    audit_path = output_dir / "integrity_audit_v2.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ["overall_status", "source_result_count", "verified_output_count", "online_energy_total_kwh_across_method_runs", "fixed_assignment_corrected_energy_total_kwh_across_method_runs", "synthetic_pickup_proxy_correction_total_kwh_across_method_runs", "errors"]}, ensure_ascii=False, indent=2))
    return 0 if audit["overall_status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
