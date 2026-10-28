#!/usr/bin/env python3
"""Create a read-only v5 segment-economics sidecar from audited NYC EV2 runs.

This recalculates segment accounting with the shared v5 decision module. It
does not alter dispatch assignments and does not rank routes: the source run
contains executed assignments, not a complete shared alternative-route pool.
Missing electricity prices or revenues remain null.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_ev2_segment_ledger_v4_20261027/seed_0/"
    "time_money_ev2_coverage_snapshots/"
    "ev2_segment_ledger_audit_live_20261028T0345Z.json"
)
DEFAULT_OUTPUT = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_ev2_segment_ledger_v4_20261027/seed_0/"
    "time_money_v5_segment_sidecar_20261028T0347Z"
)

sys.path.insert(0, str(PROJECT_ROOT))
from route_experiment.decision import (  # noqa: E402
    DECISION_MODEL_VERSION,
    aggregate_route_segment_economics,
    calculate_segment_economics,
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    temp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temp.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
            count += 1
    temp.replace(path)
    return count


def resolve(root: Path, path_value: str | Path) -> Path:
    path = Path(path_value).expanduser()
    return path if path.is_absolute() else root / path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-json", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--incremental-from-manifest",
        type=Path,
        default=None,
        help="Build an immutable delta sidecar for source results absent from a prior v5 manifest.",
    )
    parser.add_argument(
        "--electricity-price-per-kwh",
        type=float,
        default=None,
        help="Optional explicit scenario tariff. Omit it when no tariff is frozen.",
    )
    parser.add_argument(
        "--tariff-basis",
        default=None,
        help="Required provenance when an explicit scenario tariff is supplied.",
    )
    args = parser.parse_args()

    if args.electricity_price_per_kwh is not None:
        if args.electricity_price_per_kwh < 0:
            raise SystemExit("electricity_price_must_be_nonnegative")
        if not args.tariff_basis:
            raise SystemExit("explicit_tariff_requires_tariff_basis")

    audit_path = resolve(PROJECT_ROOT, args.audit_json)
    output_dir = resolve(PROJECT_ROOT, args.output_dir)
    if output_dir.exists():
        raise SystemExit(f"refusing_to_overwrite:{output_dir}")
    audit_raw = audit_path.read_bytes()
    audit = json.loads(audit_raw)
    if audit.get("schema_version") != "nyc-tlc-dispatch-ev2-segment-ledger-audit-v1":
        raise SystemExit("unexpected_audit_schema")
    if audit.get("valid_method_runs") != len(audit.get("runs", [])):
        raise SystemExit("audit_valid_run_count_mismatch")

    all_audited_runs = list(audit["runs"])
    prior_manifest_path = None
    prior_manifest_sha256 = None
    prior_source_hashes: Dict[str, str] = {}
    if args.incremental_from_manifest is not None:
        prior_manifest_path = resolve(PROJECT_ROOT, args.incremental_from_manifest)
        prior_manifest_raw = prior_manifest_path.read_bytes()
        prior_manifest_sha256 = sha256_bytes(prior_manifest_raw)
        prior_manifest = json.loads(prior_manifest_raw)
        if prior_manifest.get("schema_version") != "nyc_tlc_ev2_time_money_v5_segment_sidecar_v1":
            raise SystemExit("unexpected_prior_sidecar_schema")
        prior_run_path = prior_manifest_path.parent / "run_economics_v5.jsonl"
        prior_output = prior_manifest.get("outputs", {}).get("run_economics_v5.jsonl", {})
        if not prior_run_path.is_file():
            raise SystemExit(f"missing_prior_run_economics:{prior_run_path}")
        if sha256_file(prior_run_path) != prior_output.get("sha256"):
            raise SystemExit("prior_run_economics_hash_mismatch")
        prior_rows = [json.loads(line) for line in prior_run_path.read_text(encoding="utf-8").splitlines() if line]
        if len(prior_rows) != prior_output.get("rows") or len(prior_rows) != prior_manifest.get("source_result_count"):
            raise SystemExit("prior_source_result_count_mismatch")
        for row in prior_rows:
            source_path = str(Path(row["source_result_path"]).resolve())
            source_hash = str(row["source_result_sha256"])
            if source_path in prior_source_hashes and prior_source_hashes[source_path] != source_hash:
                raise SystemExit(f"prior_source_path_hash_conflict:{source_path}")
            prior_source_hashes[source_path] = source_hash

        audited_by_path = {str(Path(row["path"]).resolve()): str(row["sha256"]) for row in all_audited_runs}
        missing_prior_paths = sorted(set(prior_source_hashes) - set(audited_by_path))
        if missing_prior_paths:
            raise SystemExit("prior_sources_missing_from_current_audit:" + ",".join(missing_prior_paths[:5]))
        changed_prior_paths = sorted(
            path for path, prior_hash in prior_source_hashes.items()
            if audited_by_path[path] != prior_hash
        )
        if changed_prior_paths:
            raise SystemExit("prior_source_hash_changed:" + ",".join(changed_prior_paths[:5]))
        audit_runs_to_process = [
            row for row in all_audited_runs
            if str(Path(row["path"]).resolve()) not in prior_source_hashes
        ]
        if len(prior_source_hashes) + len(audit_runs_to_process) != len(all_audited_runs):
            raise SystemExit("incremental_coverage_union_mismatch")
    else:
        audit_runs_to_process = all_audited_runs

    decision_path = PROJECT_ROOT / "route_experiment" / "decision.py"
    energy_path = PROJECT_ROOT / "route_experiment" / "energy_model.py"
    if DECISION_MODEL_VERSION != "route_time_money_segment_v5_signed_fare_audit":
        raise SystemExit("unexpected_decision_model_version")

    segment_rows = []
    run_rows = []
    run_hashes_before: Dict[str, str] = {}
    counters: Counter[str] = Counter()
    total_segments = 0
    complete_energy_segments = 0

    for audited in audit_runs_to_process:
        source_path = Path(audited["path"])
        source_bytes = source_path.read_bytes()
        source_hash = sha256_bytes(source_bytes)
        if source_hash != audited["sha256"]:
            raise SystemExit(f"source_result_hash_mismatch:{source_path}")
        run_hashes_before[str(source_path)] = source_hash
        result = json.loads(source_bytes)
        source_segments = result.get("executed_segment_ledger")
        if not isinstance(source_segments, list):
            raise SystemExit(f"missing_executed_segment_ledger:{source_path}")

        money = result.get("money_accounting") or {}
        method_costs = result.get("method_costs") or {}
        currency = (
            money.get("currency")
            or method_costs.get("currency")
            or next(
                (
                    row.get("fare_currency")
                    for row in source_segments
                    if row.get("fare_currency")
                ),
                None,
            )
        )
        if not currency:
            raise SystemExit(f"missing_source_currency:{source_path}")
        tariff = (
            args.electricity_price_per_kwh
            if args.electricity_price_per_kwh is not None
            else money.get("electricity_price_per_kwh")
        )
        tariff_basis = (
            args.tariff_basis
            if args.electricity_price_per_kwh is not None
            else money.get("electricity_price_source")
        )

        aggregate_inputs = []
        per_run_segment_rows = []
        for segment_index, segment in enumerate(source_segments):
            total_segments += 1
            counters["segment_type:" + str(segment.get("segment_type"))] += 1
            counters["source_fare_status:" + str(segment.get("fare_status"))] += 1
            complete_energy_segments += segment.get("signed_net_kwh") is not None

            segment_id = str(segment.get("candidate_id") or segment_index)
            economics = calculate_segment_economics(
                distance_m=segment.get("distance_m"),
                energy_kwh=segment.get("signed_net_kwh"),
                currency=currency,
                electricity_price_per_kwh=tariff,
                segment_type=str(segment.get("segment_type")),
                observed_fare_revenue_amount=segment.get("fare_revenue_amount"),
                price_basis=tariff_basis,
                fare_basis=segment.get("fare_basis"),
            )
            row = {
                "source_result_path": str(source_path),
                "source_result_sha256": source_hash,
                "case_id": result.get("case_id"),
                "fleet_id": result.get("fleet_id"),
                "method_name": result.get("method_name"),
                "segment_index": segment_index,
                "segment_id": segment_id,
                "order_id": segment.get("order_id"),
                "vehicle_id": segment.get("vehicle_id"),
                "execution_status": segment.get("execution_status"),
                "source_fare_status": segment.get("fare_status"),
                "source_fare_source": segment.get("fare_source"),
                "source_fare_currency": segment.get("fare_currency"),
                "source_money_score_status": segment.get("money_score_status"),
                "energy_model_version": (
                    (result.get("energy_accounting") or {}).get("model_version")
                ),
                "energy_profile_sha256": (
                    (result.get("energy_accounting") or {}).get("profile_sha256")
                ),
                "v5_segment_economics": economics,
            }
            segment_rows.append(row)
            per_run_segment_rows.append(row)
            aggregate_inputs.append(
                {
                    "segment_id": segment_id,
                    "distance_m": segment.get("distance_m"),
                    "energy_kwh": segment.get("signed_net_kwh"),
                    "segment_type": segment.get("segment_type"),
                    "fare_revenue_amount": segment.get("fare_revenue_amount"),
                    "fare_basis": segment.get("fare_basis"),
                    "fare_per_km": None,
                    "electricity_price_per_kwh": tariff,
                    "price_basis": tariff_basis,
                }
            )

        aggregate = aggregate_route_segment_economics(
            aggregate_inputs,
            currency=currency,
            electricity_price_per_kwh=tariff,
            price_basis=tariff_basis,
        )
        missing = list(money.get("missing_inputs") or [])
        if tariff is None:
            missing.append("scenario_specific_electricity_tariff")
        missing.extend(
            [
                "complete_hash_audited_observed_fare_crosswalk",
                "frozen_method_level_compute_cost_rate",
                "complete_shared_feasible_route_candidate_pool",
            ]
        )
        run_row = {
            "source_result_path": str(source_path),
            "source_result_sha256": source_hash,
            "case_id": result.get("case_id"),
            "fleet_id": result.get("fleet_id"),
            "method_name": result.get("method_name"),
            "segment_count": len(source_segments),
            "source_money_accounting": money,
            "method_costs_once_per_run": method_costs,
            "v5_run_segment_aggregate": aggregate,
            "route_time_money_decision_status": "blocked_no_complete_shared_route_candidate_pool",
            "route_time_money_decision_evaluated": False,
            "missing_inputs": sorted(set(missing)),
        }
        run_rows.append(run_row)
        counters["run_money_gate:" + str(money.get("status"))] += 1
        counters["method_cost_status:" + str(method_costs.get("status"))] += 1
        counters["v5_run_accounting:" + str(aggregate.get("accounting_status"))] += 1

    # Ensure no source result was changed while the sidecar was being built.
    for source_path, expected_hash in run_hashes_before.items():
        if sha256_file(Path(source_path)) != expected_hash:
            raise SystemExit(f"source_result_changed_during_recalculation:{source_path}")

    output_dir.mkdir(parents=True, exist_ok=False)
    segment_path = output_dir / "segment_economics_v5.jsonl"
    run_path = output_dir / "run_economics_v5.jsonl"
    segment_count = write_jsonl(segment_path, segment_rows)
    run_count = write_jsonl(run_path, run_rows)
    manifest = {
        "schema_version": "nyc_tlc_ev2_time_money_v5_segment_sidecar_v1",
        "status": "completed_segment_economics_route_choice_blocked",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "decision_model_version": DECISION_MODEL_VERSION,
        "decision_module_path": str(decision_path),
        "decision_module_sha256": sha256_file(decision_path),
        "energy_model_path": str(energy_path),
        "energy_model_sha256": sha256_file(energy_path),
        "source_audit_path": str(audit_path),
        "source_audit_sha256": sha256_bytes(audit_raw),
        "incremental_from_manifest_path": str(prior_manifest_path) if prior_manifest_path else None,
        "incremental_from_manifest_sha256": prior_manifest_sha256,
        "experiment_manifest_sha256": audit.get("experiment_manifest_sha256"),
        "package_manifest_sha256": audit.get("package_manifest_sha256"),
        "expected_method_runs": audit.get("expected_method_runs"),
        "audited_valid_method_runs": audit.get("valid_method_runs"),
        "audited_missing_method_runs": len(audit.get("missing_method_runs", [])),
        "source_result_hashes_reverified_after_recalculation": True,
        "source_result_count": len(run_rows),
        "previously_covered_source_result_count": len(prior_source_hashes),
        "new_source_result_count": len(run_rows),
        "combined_source_result_count": len(prior_source_hashes) + len(run_rows),
        "segment_row_count": segment_count,
        "run_row_count": run_count,
        "segment_type_counts": {
            key.removeprefix("segment_type:"): value
            for key, value in counters.items()
            if key.startswith("segment_type:")
        },
        "source_fare_status_counts": {
            key.removeprefix("source_fare_status:"): value
            for key, value in counters.items()
            if key.startswith("source_fare_status:")
        },
        "source_energy_complete_segment_count": complete_energy_segments,
        "run_money_gate_counts": {
            key.removeprefix("run_money_gate:"): value
            for key, value in counters.items()
            if key.startswith("run_money_gate:")
        },
        "method_cost_status_counts": {
            key.removeprefix("method_cost_status:"): value
            for key, value in counters.items()
            if key.startswith("method_cost_status:")
        },
        "v5_run_accounting_status_counts": {
            key.removeprefix("v5_run_accounting:"): value
            for key, value in counters.items()
            if key.startswith("v5_run_accounting:")
        },
        "electricity_price_per_kwh": args.electricity_price_per_kwh,
        "tariff_basis": args.tariff_basis,
        "electricity_costs_null_when_tariff_missing": True,
        "fare_rule": "passenger fare preserved once per journey segment; deadhead revenue is known zero",
        "fare_status_limit": "historical fare proxies are not current realized revenue",
        "api_compute_cost_rule": "preserved once per source run; no invented missing compute rate",
        "shared_route_pool_available": False,
        "weighted_rankings_emitted": False,
        "source_results_modified": False,
        "dispatch_replayed": False,
        "new_model_calls": 0,
        "sumo_reruns": 0,
        "outputs": {
            "segment_economics_v5.jsonl": {
                "sha256": sha256_file(segment_path),
                "rows": segment_count,
            },
            "run_economics_v5.jsonl": {
                "sha256": sha256_file(run_path),
                "rows": run_count,
            },
        },
        "generator_path": str(Path(__file__).resolve()),
        "generator_sha256": sha256_file(Path(__file__).resolve()),
    }
    write_json(output_dir / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "source_runs": len(run_rows),
                "segment_rows": segment_count,
                "energy_complete_segments": complete_energy_segments,
                "rankings": 0,
                "source_results_modified": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
