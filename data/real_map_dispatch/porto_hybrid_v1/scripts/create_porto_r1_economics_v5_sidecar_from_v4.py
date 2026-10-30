from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODEL_VERSION = "route_time_money_segment_v5_signed_fare_audit"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Version the frozen Porto R1 v4 segment ledger into v5 accounting fields without rerunning SUMO or a model.")
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--v4-sidecar-dir", type=Path, required=True)
    parser.add_argument("--v3-ledger", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--decision-module", type=Path, required=True)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    v4_dir = args.v4_sidecar_dir.resolve()
    v4_manifest_path = v4_dir / "manifest.json"
    v4_manifest = read_json(v4_manifest_path)
    if v4_manifest.get("status") != "segment_ledger_reused_candidate_scores_blocked":
        raise SystemExit("unexpected_v4_sidecar_status")
    source_ledger = args.v3_ledger.resolve()
    v4_source_ledger_hash = v4_manifest.get("source_segment_ledger_sha256")
    if sha256(source_ledger) != v4_source_ledger_hash:
        raise SystemExit("v3_ledger_hash_does_not_match_v4_manifest")
    module_path = args.decision_module.resolve()
    if args.output_dir.exists():
        raise SystemExit(f"refusing_to_overwrite:{args.output_dir}")

    rows = [json.loads(line) for line in source_ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != int(v4_manifest.get("segment_rows_reused", -1)):
        raise SystemExit("v3_segment_count_mismatch")
    v5_rows = []
    energy_nonestimable = 0
    link_attribution_unavailable = 0
    for source in rows:
        row = dict(source)
        segment_type = row.get("segment_type")
        if segment_type not in {"deadhead", "passenger"}:
            raise SystemExit(f"unexpected_segment_type:{segment_type}")
        net = row.get("signed_net_energy_kwh")
        billable = row.get("billable_energy_kwh")
        if net is None:
            energy_nonestimable += 1
        elif abs(float(billable) - max(float(net), 0.0)) > 1e-9:
            raise SystemExit(f"billable_energy_mismatch:{row.get('task_id')}:{row.get('order_id')}:{row.get('leg_role')}")
        row["currency"] = "EUR"
        row["currency_basis"] = "Porto scenario accounting currency; monetary values remain null without a price source"
        row["fare_source_basis"] = "known_zero_for_deadhead" if segment_type == "deadhead" else None
        row["fare_sign_semantics"] = "known_zero_deadhead_revenue" if segment_type == "deadhead" else None
        row["electricity_source_basis"] = None
        null_reasons = {}
        if net is None:
            null_reasons["signed_net_energy_kwh"] = "source_segment_energy_missing_or_not_estimable"
        null_reasons["electricity_price_per_kwh"] = "no_frozen_Portugal_electricity_tariff_in_R1_inputs"
        null_reasons["electricity_cost_amount"] = "electricity_tariff_missing"
        if segment_type == "passenger":
            null_reasons["fare_revenue_amount"] = "no_observed_or_frozen_passenger_fare_for_R1"
        null_reasons["net_economic_cost_amount"] = "required_tariff_and_or_passenger_fare_missing"
        null_reasons["per_link_energy_kwh"] = "source_EV2_trace_reports_energy_at_route_segment_level_not_link_level"
        row["null_reasons"] = null_reasons
        row["method_cost_ref"] = f"{row.get('objective')}:seed_{row.get('seed')}"
        # API/compute charges are recorded once below at method level.
        row.pop("api_cost_amount", None)
        row.pop("compute_cost_amount", None)
        row["electricity_cost_amount"] = None
        row["electricity_currency"] = None
        row["fare_currency"] = "EUR" if segment_type == "deadhead" else None
        row["net_economic_cost_amount"] = None
        row["accounting_status"] = "blocked_missing_tariff_and_passenger_fare" if segment_type == "passenger" else "blocked_missing_tariff"
        row["v5_decision_model_version"] = MODEL_VERSION
        if row.get("source_route_edges"):
            row["ordered_road_links"] = [
                {"link_id": edge, "energy_kwh": None,
                 "null_reason": "EV2_trace_not_attributed_to_individual_link"}
                for edge in row["source_route_edges"]
            ]
            link_attribution_unavailable += 1
        v5_rows.append(row)

    args.output_dir.mkdir(parents=True, exist_ok=False)
    ledger_path = args.output_dir / "segment_ledger_v5.jsonl"
    with ledger_path.open("w", encoding="utf-8") as stream:
        for row in v5_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    method_rows = []
    for objective in ("fastest", "lowest_energy"):
        method_rows.append({
            "method_record_id": objective,
            "method": objective.upper(),
            "currency": "EUR",
            "api_token_usage_count": 0,
            "api_token_cost_amount": 0.0,
            "api_token_cost_currency": None,
            "api_token_cost_basis": "no_model_api_calls_in_R1_traditional_route_methods",
            "compute_cost_amount": None,
            "compute_cost_currency": None,
            "compute_cost_status": "unknown_rate",
            "null_reasons": {"compute_cost_amount": "local_compute_cost_rate_not_supplied"},
            "record_once_per_method_not_per_segment": True,
        })
    method_path = args.output_dir / "method_costs_v5.jsonl"
    method_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in method_rows), encoding="utf-8")
    gate = {
        "model_version": MODEL_VERSION,
        "status": "blocked_missing_same_snapshot_complete_candidate_pool_and_passenger_fare",
        "profiles": {
            "time_priority_0_6_0_4": {"time_weight": 0.6, "money_weight": 0.4, "status": "blocked"},
            "money_priority_0_4_0_6": {"time_weight": 0.4, "money_weight": 0.6, "status": "blocked"},
        },
        "candidate_pool_status": "blocked_no_complete_same_snapshot_cross_method_candidate_pool",
        "currency": "EUR",
        "electricity_tariff": None,
        "passenger_fare_schedule": None,
        "method_costs_path": str(method_path),
        "method_costs_sha256": sha256(method_path),
        "weighted_rankings_emitted": False,
        "runtime_selector_switched": False,
        "runtime_selector_scope": "offline v5 accounting migration only; frozen R1 FASTEST and LOWEST_ENERGY source runs remain unchanged",
    }
    gate_path = args.output_dir / "decision_gate_v5.json"
    gate_path.write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema": "porto_r1_route_time_money_segment_v5_sidecar",
        "decision_model_version": MODEL_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_batch_manifest_path": str(experiment / "batch_manifest.json"),
        "source_batch_manifest_sha256": sha256(experiment / "batch_manifest.json"),
        "source_v4_manifest_path": str(v4_manifest_path),
        "source_v4_manifest_sha256": sha256(v4_manifest_path),
        "source_v3_segment_ledger_path": str(source_ledger),
        "source_v3_segment_ledger_sha256": sha256(source_ledger),
        "decision_module_path": str(module_path),
        "decision_module_sha256": sha256(module_path),
        "segments": len(v5_rows),
        "deadhead_segments": sum(row["segment_type"] == "deadhead" for row in v5_rows),
        "passenger_segments": sum(row["segment_type"] == "passenger" for row in v5_rows),
        "energy_nonestimable_segments": energy_nonestimable,
        "link_energy_attribution_unavailable_segments": link_attribution_unavailable,
        "electricity_tariff": None,
        "passenger_fare_schedule": None,
        "currency": "EUR scenario context only; not evidence of price source",
        "model_calls": 0,
        "sumo_reruns": 0,
        "runtime_selector_switched": False,
        "runtime_selector_scope": "offline v5 accounting migration only; no R1 route selector or frozen run changed",
        "source_results_modified": False,
        "old_sidecars_modified": False,
        "ledger_path": str(ledger_path),
        "ledger_sha256": sha256(ledger_path),
        "method_costs_path": str(method_path),
        "method_costs_sha256": sha256(method_path),
        "decision_gate_path": str(gate_path),
        "decision_gate_sha256": sha256(gate_path),
        "selection_claim_limit": "offline signed-fare-compatible ledger schema; no new route selection, no net-cost ranking, no invoice or full-profit claim",
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), "segments": len(v5_rows), "status": gate["status"], "model_calls": 0, "sumo_reruns": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
