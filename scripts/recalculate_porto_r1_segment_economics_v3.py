#!/usr/bin/env python3
"""Create a hash-bound, offline EV v2 segment-economics ledger for Porto R1.

The source matrix is read-only. Since R1 freezes no tariff or passenger fare
and stores only one selected route per leg, monetary costs stay null and the
time-money selector is recorded as blocked rather than inferred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_BATCH_REL = Path(
    "data/real_map_dispatch/porto_hybrid_v1/derived/experiments/"
    "r1_dynamic_sumo_v1/formal_v5_energy_v2"
)
DEFAULT_OUTPUT_NAME = "segment_economics_v3_sidecar_20261027T1744CST"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def resolve_from_root(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non_finite_source_value:{value!r}")
    return number


def task_coordinates(task_id: str) -> Dict[str, Any]:
    parts = task_id.split("_")
    seed = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    objective = parts[2].lower() if len(parts) > 2 else None
    return {"seed": seed, "objective": objective}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", type=Path, default=DEFAULT_BATCH_REL)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    batch_dir = resolve_from_root(repo_root, str(args.batch_dir))
    output_dir = (
        resolve_from_root(repo_root, str(args.output_dir))
        if args.output_dir is not None
        else batch_dir / DEFAULT_OUTPUT_NAME
    )
    if output_dir.exists():
        raise FileExistsError(f"refusing_to_overwrite:{output_dir}")

    source_manifest = batch_dir / "batch_manifest.json"
    manifest_sha = sha256(source_manifest)
    batch = read_json(source_manifest)
    protocol_path = resolve_from_root(repo_root, batch["protocol_path"])
    protocol_sha = sha256(protocol_path)
    protocol = read_json(protocol_path)
    if batch.get("status") != "completed_with_gaps":
        raise ValueError(f"unexpected_batch_status:{batch.get('status')}")
    if batch.get("protocol_sha256") != protocol_sha:
        raise ValueError("protocol_hash_mismatch")

    tasks = batch.get("tasks", [])
    completed = [task for task in tasks if task.get("status") == "completed"]
    failed = [task for task in tasks if task.get("status") == "failed"]
    if len(completed) != 19 or len(failed) != 1:
        raise ValueError(
            f"unexpected_terminal_matrix:{len(completed)}_completed_{len(failed)}_failed"
        )

    output_dir.mkdir(parents=True, exist_ok=False)
    ledger_path = output_dir / "segment_ledger.jsonl"
    decisions_path = output_dir / "time_money_decisions.json"
    source_files: List[Dict[str, str]] = [
        {"path": str(source_manifest), "sha256": manifest_sha},
        {"path": str(protocol_path), "sha256": protocol_sha},
    ]
    rows: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    route_energy_sum_kwh = 0.0
    billable_energy_sum_kwh = 0.0
    missing_energy_rows = 0
    role_counts: Dict[str, int] = {}
    noop_count = 0

    for task in completed:
        task_id = str(task["task_id"])
        output_root = resolve_from_root(repo_root, task["output_dir"])
        legs_path = output_root / "route_legs_actual.json"
        legs_sha = sha256(legs_path)
        source_files.append({"path": str(legs_path), "sha256": legs_sha})
        legs = read_json(legs_path)
        if not isinstance(legs, list):
            raise ValueError(f"unexpected_leg_schema:{legs_path}")

        coordinates = task_coordinates(task_id)
        route_ids = set()
        expected_profile_sha = protocol["cost_model"]["energy_model"]["profile_sha256"]
        for raw in legs:
            role = raw.get("leg_role")
            if role == "vehicle_to_pickup":
                segment_type = "deadhead"
                fare = 0.0
                fare_basis = "known_zero_for_deadhead"
            elif role == "pickup_to_dropoff":
                segment_type = "passenger"
                fare = None
                fare_basis = None
            else:
                raise ValueError(f"unexpected_leg_role:{task_id}:{role}")

            if raw.get("energy_model_version") != "ev_longitudinal_v2":
                raise ValueError(f"unexpected_energy_model:{task_id}:{raw.get('energy_model_version')}")
            if raw.get("energy_profile_sha256") != expected_profile_sha:
                raise ValueError(f"energy_profile_hash_mismatch:{task_id}")

            energy_wh = finite_or_none(raw.get("actual_energy_net_wh"))
            energy_kwh = energy_wh / 1000.0 if energy_wh is not None else None
            billable_kwh = max(0.0, energy_kwh) if energy_kwh is not None else None
            if energy_kwh is None:
                missing_energy_rows += 1
            else:
                route_energy_sum_kwh += energy_kwh
                billable_energy_sum_kwh += billable_kwh or 0.0

            if raw.get("order_id") is not None:
                route_ids.add(str(raw["order_id"]))
            role_counts[role] = role_counts.get(role, 0) + 1
            noop_count += int(bool(raw.get("actual_noop")))
            priced = energy_kwh is not None and raw.get("electricity_price_per_kwh") is not None
            accounting_status = (
                "blocked_missing_tariff_and_passenger_fare"
                if segment_type == "passenger" and not priced
                else (
                    "blocked_missing_tariff"
                    if not priced
                    else "blocked_missing_passenger_fare"
                    if segment_type == "passenger"
                    else "priced_deadhead_energy"
                )
            )
            rows.append({
                "batch_id": batch.get("batch_id"),
                "task_id": task_id,
                **coordinates,
                "order_id": raw.get("order_id"),
                "logical_vehicle_id": raw.get("logical_vehicle_id"),
                "sequence_index": raw.get("sequence_index"),
                "trip_id": raw.get("trip_id"),
                "segment_type": segment_type,
                "leg_role": role,
                "actual_status": raw.get("status"),
                "actual_noop": bool(raw.get("actual_noop")),
                "distance_m": finite_or_none(raw.get("actual_distance_m")),
                "duration_s": finite_or_none(raw.get("actual_duration_s")),
                "energy_model_version": raw.get("energy_model_version"),
                "energy_profile_sha256": raw.get("energy_profile_sha256"),
                "signed_net_energy_wh": energy_wh,
                "signed_net_energy_kwh": energy_kwh,
                "billable_energy_kwh": billable_kwh,
                "electricity_price_per_kwh": None,
                "electricity_currency": None,
                "electricity_cost_amount": None,
                "fare_revenue_amount": fare,
                "fare_currency": None,
                "fare_basis": fare_basis,
                "api_cost_amount": 0.0,
                "compute_cost_amount": None,
                "net_economic_cost_amount": None,
                "accounting_status": accounting_status,
                "source_leg_sha256": legs_sha,
                "source_route_edges": raw.get("route_edges"),
                "source_energy_trace_fidelity": raw.get("energy_trace_fidelity"),
            })

        decisions.append({
            "task_id": task_id,
            **coordinates,
            "objective_used_by_source_run": coordinates["objective"],
            "orders_recorded": len(route_ids),
            "alternative_route_pool_available": False,
            "status": "blocked_missing_fare_tariff_and_alternative_route_pool",
            "profiles": {
                "time_priority_0_6_0_4": {"status": "not_scored"},
                "money_priority_0_4_0_6": {"status": "not_scored"},
            },
        })

    rows.sort(key=lambda row: (
        row["task_id"], row["order_id"] or "", row["sequence_index"] or 0
    ))
    with ledger_path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    write_json(decisions_path, decisions)

    for source in source_files:
        if sha256(Path(source["path"])) != source["sha256"]:
            raise RuntimeError(f"source_changed_during_recalculation:{source['path']}")

    model_info = protocol.get("cost_model", {}).get("energy_model", {})
    summary = {
        "sidecar_version": "porto_r1_segment_economics_v3",
        "status": "ledger_complete_money_scores_blocked",
        "source_batch_status": batch.get("status"),
        "completed_source_tasks": len(completed),
        "failed_source_tasks": [task.get("task_id") for task in failed],
        "segment_rows": len(rows),
        "orders_with_observed_legs": sum(len({r["order_id"] for r in rows if r["task_id"] == d["task_id"]}) for d in decisions),
        "leg_role_counts": role_counts,
        "zero_distance_noop_rows": noop_count,
        "missing_energy_rows": missing_energy_rows,
        "signed_net_energy_kwh_sum": route_energy_sum_kwh if not missing_energy_rows else None,
        "billable_energy_kwh_sum": billable_energy_sum_kwh if not missing_energy_rows else None,
        "electricity_tariff": None,
        "electricity_currency": None,
        "passenger_fare_schedule": None,
        "fare_currency": None,
        "known_model_calls": batch.get("model_requests", 0),
        "api_cost_scope": "known zero model calls for deterministic R1; no API calls charged to these route tasks",
        "compute_cost_amount": None,
        "net_economic_cost_scoring": "blocked: missing tariff, passenger fare, and alternative route pool",
        "energy_model_version": model_info.get("model_version"),
        "energy_profile_id": model_info.get("profile_id"),
        "energy_profile_sha256": model_info.get("profile_sha256"),
        "route_objectives_used": batch.get("objectives"),
        "source_manifest_sha256": manifest_sha,
        "source_protocol_sha256": protocol_sha,
        "source_files": source_files,
        "outputs": {
            str(ledger_path): sha256(ledger_path),
            str(decisions_path): sha256(decisions_path),
        },
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_calls_for_recalculation": 0,
    }
    write_json(output_dir / "manifest.json", summary)
    print(json.dumps({
        "status": summary["status"],
        "output_dir": str(output_dir),
        "completed_source_tasks": len(completed),
        "failed_source_tasks": len(failed),
        "segment_rows": len(rows),
        "leg_role_counts": role_counts,
        "signed_net_energy_kwh_sum": summary["signed_net_energy_kwh_sum"],
        "billable_energy_kwh_sum": summary["billable_energy_kwh_sum"],
        "money_scores": summary["net_economic_cost_scoring"],
        "model_calls_for_recalculation": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
