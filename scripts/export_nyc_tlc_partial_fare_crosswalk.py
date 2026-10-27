#!/usr/bin/env python3
"""Export only fares present in the audited legacy segment sidecar.

This deliberately emits an incomplete crosswalk when the source sidecar does
not cover all 20,216 reconciled order IDs. It never fills missing fares.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECONCILIATION_DIR = ROOT / (
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_multimonth_energy_v2_offline_20261026/time_money_fare_reconciliation_v1"
)
DEFAULT_OUTPUT = ROOT / (
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/dispatch_ev2_proxy_v3_inputs/"
    "nyc_tlc_observed_fares_partial_from_audited_sidecar_v1.json"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export(source: Path, reconciliation_dir: Path, output: Path) -> Dict[str, Any]:
    manifest_path = reconciliation_dir / "manifest.json"
    summary_path = reconciliation_dir / "fare_reconciliation_summary.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    expected_order_count = int(summary["order_join_validation"]["unique_order_ids"])
    fares: Dict[str, Dict[str, Any]] = {}
    row_counts: Dict[str, int] = {}
    conflicts = []
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            row = json.loads(line)
            if row.get("segment_type") != "passenger":
                continue
            order_id = str(row["order_id"])
            fare = float(row["observed_base_passenger_fare_usd"])
            value = {
                "fare_amount": fare,
                "currency": "USD",
                "fare_basis": "nyc_tlc_2024_base_passenger_fare",
                "source": {
                    "raw_file": row.get("fare_source_file"),
                    "raw_row_number": row.get("fare_source_row_number"),
                    "crosswalk_artifact": "orders_observed_fares.parquet",
                },
            }
            if order_id in fares and fares[order_id]["fare_amount"] != fare:
                conflicts.append({"order_id": order_id, "existing": fares[order_id]["fare_amount"], "new": fare})
            fares[order_id] = value
            row_counts[order_id] = row_counts.get(order_id, 0) + 1
    if conflicts:
        raise ValueError("conflicting observed fare values for order IDs: " + json.dumps(conflicts[:5]))

    parquet_path = reconciliation_dir / "orders_observed_fares.parquet"
    value = {
        "schema_version": "nyc_tlc_observed_base_fares_v1",
        "status": "complete" if len(fares) == expected_order_count else "partial",
        "complete": len(fares) == expected_order_count,
        "currency": "USD",
        "fare_field": "base_passenger_fare",
        "source": "NYC TLC 2024 observed historical base passenger fare proxy; not realized revenue",
        "source_dataset_id": "bqxa-629f",
        "reconciled_order_count": expected_order_count,
        "exported_unique_order_count": len(fares),
        "coverage_fraction": len(fares) / expected_order_count if expected_order_count else 0.0,
        "passenger_sidecar_rows": sum(row_counts.values()),
        "duplicate_passenger_sidecar_rows": sum(max(count - 1, 0) for count in row_counts.values()),
        "fare_conflict_count": len(conflicts),
        "negative_fares_in_exported_subset": sorted(order_id for order_id, record in fares.items() if record["fare_amount"] < 0),
        "negative_fare_count_in_complete_source_crosswalk": int(summary["order_join_validation"]["negative_base_passenger_fare"]),
        "coverage_warning": "Partial excerpt only; a negative fare exists in the complete source crosswalk but is not present in this excerpt.",
        "reconciliation_manifest_sha256": sha256(manifest_path),
        "reconciliation_summary_sha256": sha256(summary_path),
        "parquet_crosswalk_sha256": sha256(parquet_path),
        "extraction_source": str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else str(source),
        "extraction_source_sha256": sha256(source),
        "orders": fares,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(output)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_RECONCILIATION_DIR / "assigned_route_segments_fare_accounting.jsonl.gz")
    parser.add_argument("--reconciliation-dir", type=Path, default=DEFAULT_RECONCILIATION_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = export(args.source, args.reconciliation_dir, args.output)
    print(json.dumps({k: result[k] for k in result if k != "orders"}, ensure_ascii=False, indent=2))
    print("output=" + str(args.output))


if __name__ == "__main__":
    main()
