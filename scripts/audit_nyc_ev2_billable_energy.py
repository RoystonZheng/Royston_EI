#!/usr/bin/env python3
"""Audit billable EV v2 energy per saved NYC pickup and ride leg."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INPUT_MANIFEST = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_energy_v2_offline_20261026/manifest.json"
OUTPUT_DIR = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_energy_v2_offline_20261026/billable_energy_audit_20261027_v1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    if OUTPUT_DIR.exists():
        raise SystemExit(f"refusing_to_overwrite:{OUTPUT_DIR}")
    manifest = json.loads(INPUT_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("status") != "completed_with_expected_gaps" or manifest.get("output_coverage_complete") is not True:
        raise SystemExit("source_ev_v2_manifest_not_coverage_complete")
    aggregates: dict[tuple[int, str], dict[str, Any]] = defaultdict(lambda: {
        "source_files": 0,
        "assignments": 0,
        "net_energy_kwh": 0.0,
        "billable_energy_kwh": 0.0,
        "pickup_leg_count": 0,
        "ride_leg_count": 0,
        "negative_net_leg_count": 0,
        "decision_energy_dependency": set(),
    })
    records = []
    for entry in manifest.get("results", []):
        output_path = Path(entry["output"])
        if not output_path.is_file():
            raise FileNotFoundError(output_path)
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "nyc_dispatch_energy_recalculation_v1":
            raise ValueError(f"unexpected_sidecar_schema:{output_path}")
        if payload.get("source_result_sha256") != entry.get("source_sha256"):
            raise ValueError(f"source_result_hash_mismatch:{output_path}")
        if payload.get("energy_model", {}).get("profile_sha256") != manifest.get("energy_model", {}).get("profile_sha256"):
            raise ValueError(f"energy_profile_mismatch:{output_path}")

        key = (int(entry["seed"]), str(entry["method_name"]))
        bucket = aggregates[key]
        bucket["source_files"] += 1
        bucket["decision_energy_dependency"].add(str(entry.get("decision_energy_dependency")))
        file_net = 0.0
        file_billable = 0.0
        negative_legs = 0
        pickup_count = ride_count = 0
        for assignment in payload.get("assignments", []):
            energy = assignment.get("ev_longitudinal_v2_energy") or {}
            for leg_name in ("pickup", "ride"):
                leg = energy.get(leg_name) or {}
                net = float(leg.get("net_kwh", 0.0))
                billable = max(0.0, net)
                file_net += net
                file_billable += billable
                negative_legs += net < 0.0
                if leg_name == "pickup":
                    pickup_count += 1
                else:
                    ride_count += 1
        bucket["assignments"] += len(payload.get("assignments", []))
        bucket["net_energy_kwh"] += file_net
        bucket["billable_energy_kwh"] += file_billable
        bucket["pickup_leg_count"] += pickup_count
        bucket["ride_leg_count"] += ride_count
        bucket["negative_net_leg_count"] += negative_legs
        records.append({
            "seed": int(entry["seed"]),
            "method_name": str(entry["method_name"]),
            "case_id": str(entry["case_id"]),
            "fleet_id": str(entry["fleet_id"]),
            "source_result": str(entry["source"]),
            "source_result_sha256": entry["source_sha256"],
            "ev_v2_sidecar": str(output_path),
            "ev_v2_sidecar_sha256": sha256(output_path),
            "assignment_count": len(payload.get("assignments", [])),
            "net_energy_kwh": file_net,
            "billable_energy_kwh": file_billable,
            "pickup_leg_count": pickup_count,
            "ride_leg_count": ride_count,
            "negative_net_leg_count": negative_legs,
        })

    summary = []
    for (seed, method), bucket in sorted(aggregates.items()):
        summary.append({
            "seed": seed,
            "method_name": method,
            **{k: v for k, v in bucket.items() if k != "decision_energy_dependency"},
            "decision_energy_dependency": sorted(bucket["decision_energy_dependency"]),
            "electricity_tariff": None,
            "electricity_cost_amount": None,
            "electricity_cost_status": "not_computed_no_frozen_nyc_tariff",
        })
    OUTPUT_DIR.mkdir(parents=True, exist_ok=False)
    output = {
        "schema_version": "nyc_ev_v2_billable_energy_audit_v1",
        "status": "completed_offline_billable_energy_audit",
        "source_manifest": str(INPUT_MANIFEST),
        "source_manifest_sha256": sha256(INPUT_MANIFEST),
        "source_manifest_result_count": len(manifest.get("results", [])),
        "reconciled_result_count": len(records),
        "excluded_source_count": manifest.get("excluded_count"),
        "rule": "billable_kwh = sum(max(net_kwh, 0)) over the saved pickup and ride route legs for each assigned order",
        "route_assignments_rerun": False,
        "model_calls": 0,
        "limitations": [
            "Energy is an offline route reconstruction from frozen OD data and a synthetic pickup proxy, not measured vehicle telemetry.",
            "Dispatch assignments are unchanged; some legacy methods used legacy energy in their original decision process.",
            "No NYC electricity tariff was frozen in the EV v2 recalculation package; currency expense remains null rather than zero.",
            "The 13 source results excluded by the EV v2 manifest remain excluded; incomplete or mixed-code results are not promoted.",
        ],
        "summary_by_seed_and_method": summary,
        "files": records,
    }
    out_path = OUTPUT_DIR / "manifest.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "manifest": str(out_path),
        "source_results": len(records),
        "seed_method_groups": len(summary),
        "assignments": sum(x["assignments"] for x in summary),
        "pickup_legs": sum(x["pickup_leg_count"] for x in summary),
        "ride_legs": sum(x["ride_leg_count"] for x in summary),
        "negative_net_legs": sum(x["negative_net_leg_count"] for x in summary),
        "electricity_cost_status": "not_computed_no_frozen_nyc_tariff",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
