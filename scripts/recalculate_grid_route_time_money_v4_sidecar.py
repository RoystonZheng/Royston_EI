#!/usr/bin/env python3
"""Recalculate the completed 100x100 grid route ledger with shared-pool decision v4.

Source route files and the prior v2 sidecar are read-only. The previous
sidecar's station-price scenario is carried forward only as a dated scenario
reference; passenger fares remain unknown, so net-cost scores stay blocked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


SOURCE_AUDIT_REL = Path(
    "outputs/route_layer/traditional_experiment_v1/"
    "route_decision_segment_v2_sidecar_20261026/audit.json"
)
OUTPUT_REL = Path(
    "outputs/route_layer/traditional_experiment_v1/"
    "route_decision_segment_v4_sidecar_20261027T1800CST"
)
MODEL_VERSION = "route_time_money_segment_v4_shared_candidate_pool"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_REL)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source_audit_path = root / SOURCE_AUDIT_REL
    output_root = resolve(root, str(args.output_dir))
    if output_root.exists():
        raise FileExistsError(f"refusing_to_overwrite:{output_root}")

    source_audit_sha = sha256(source_audit_path)
    source_audit = load_json(source_audit_path)
    files = source_audit.get("files", [])
    if len(files) != 20:
        raise ValueError(f"expected_20_frozen_inputs_got_{len(files)}")
    pricing = source_audit["pricing_config"]

    for entry in files:
        source = resolve(root, entry["input"])
        map_path = resolve(root, entry["map"])
        old_output = resolve(root, entry["output"])
        if sha256(source) != entry["input_sha256"]:
            raise ValueError(f"source_route_hash_mismatch:{source}")
        if sha256(map_path) != entry["map_sha256"]:
            raise ValueError(f"source_map_hash_mismatch:{map_path}")
        prior = load_json(old_output)
        if prior.get("cost_config", {}).get("energy_model", {}).get("profile_sha256") != "b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee":
            raise ValueError(f"prior_ev2_profile_mismatch:{old_output}")

    output_root.mkdir(parents=True, exist_ok=False)
    outputs: List[Dict[str, Any]] = []
    progress_path = output_root / "progress.json"
    progress: Dict[str, Any] = {
        "status": "running",
        "source_audit_sha256": source_audit_sha,
        "expected_files": len(files),
        "completed_files": [],
        "model_calls": 0,
    }
    write_json(progress_path, progress)

    calc_script = root / "scripts/calculate_route_costs.py"
    for entry in files:
        source = resolve(root, entry["input"])
        map_path = resolve(root, entry["map"])
        seed = int(entry["seed"])
        kind = str(entry["kind"])
        filename = (
            "orders_routes_time_money_segment_v4.json"
            if kind == "orders"
            else "formal_routes_time_money_segment_v4.json"
        )
        out_path = output_root / f"seed_{seed}" / filename
        cmd = [
            sys.executable,
            str(calc_script),
            "--map-json", str(map_path),
            "--routes-json", str(source),
            "--output-json", str(out_path),
            "--enable-route-decisions",
            "--electricity-price-per-kwh", str(pricing["electricity_price_per_kwh"]),
            "--currency", str(pricing["currency"]),
            "--pricing-version", str(pricing["pricing_version"]),
            "--billing-period", str(pricing["billing_period"]),
            "--pricing-status", str(pricing["pricing_status"]),
            "--price-source", str(pricing["price_source"]),
            "--price-source-url", str(pricing["price_source_url"]),
            "--observed-at", str(pricing["observed_at"]),
            "--price-basis", str(pricing["price_basis"]),
        ]
        if pricing.get("api_price_per_token") is not None:
            cmd.extend(["--api-price-per-token", str(pricing["api_price_per_token"])])
        if pricing.get("compute_price_per_ms") is not None:
            cmd.extend(["--compute-price-per-ms", str(pricing["compute_price_per_ms"])])
        result = subprocess.run(cmd, cwd=root, text=True, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"recalculation_failed:{entry['kind']}:{seed}:"
                f"{result.stderr[-2000:]}"
            )
        recalculated = load_json(out_path)
        config = recalculated.get("route_decision_config", {})
        if config.get("model_version") != MODEL_VERSION:
            raise ValueError(f"wrong_decision_model_version:{out_path}")
        if config.get("candidate_pool_scope") != "same_task_scenario_across_all_methods":
            raise ValueError(f"output_candidate_pool_scope_mismatch:{out_path}")
        if not config.get("candidate_pool_shared_across_profiles"):
            raise ValueError(f"output_profiles_do_not_share_pool:{out_path}")
        if not config.get("results_are_sidecar_only"):
            raise ValueError(f"output_not_marked_sidecar_only:{out_path}")
        if config.get("source_routes_sha256") != entry["input_sha256"]:
            raise ValueError(f"output_source_hash_mismatch:{out_path}")
        if config.get("source_map_sha256") != entry["map_sha256"]:
            raise ValueError(f"output_map_hash_mismatch:{out_path}")
        outputs.append({
            "seed": seed,
            "kind": kind,
            "input": str(source),
            "input_sha256": entry["input_sha256"],
            "map": str(map_path),
            "map_sha256": entry["map_sha256"],
            "output": str(out_path),
            "output_sha256": sha256(out_path),
            "task_count": len(recalculated.get("results", [])),
            "v4_status": recalculated.get("route_decision_config", {}).get("model_version"),
        })
        progress["completed_files"] = [item["output"] for item in outputs]
        write_json(progress_path, progress)

    # Recheck every source after the calculations; the original matrix and v2
    # sidecar must remain byte-identical throughout this offline pass.
    for entry in files:
        if sha256(resolve(root, entry["input"])) != entry["input_sha256"]:
            raise RuntimeError(f"source_route_changed:{entry['input']}")
        if sha256(resolve(root, entry["map"])) != entry["map_sha256"]:
            raise RuntimeError(f"source_map_changed:{entry['map']}")

    from collections import Counter

    scenario_statuses = Counter()
    profile_statuses = Counter()
    hard_feasible_candidates = 0
    blocked_candidates = 0
    for output in outputs:
        payload = load_json(Path(output["output"]))
        for result_item in payload.get("results", []):
            decisions = result_item.get("route_decisions", {})
            scenario_statuses[decisions.get("status", "missing")] += 1
            pool = decisions.get("candidate_pool", {})
            hard_feasible_candidates += int(pool.get("hard_feasible_candidate_count", 0))
            blocked_candidates += int(pool.get("blocked_candidate_count", 0))
            for profile_result in decisions.get("profiles", {}).values():
                profile_statuses[profile_result.get("status", "missing")] += 1

    summary = {
        "schema_version": 1,
        "status": "completed_sidecar_recalculation",
        "decision_model_version": MODEL_VERSION,
        "source_sidecar_audit": str(source_audit_path),
        "source_sidecar_audit_sha256": source_audit_sha,
        "source_route_file_count": len(files),
        "source_tasks": sum(int(entry["task_count"]) for entry in files),
        "output_file_count": len(outputs),
        "output_tasks": sum(int(entry["task_count"]) for entry in outputs),
        "source_results_modified": False,
        "previous_v2_sidecar_modified": False,
        "results_are_sidecar_only": True,
        "pricing_scenario": pricing,
        "fare_schedule": None,
        "fare_status": "unknown; no observed fare or frozen fare schedule in source grid records",
        "money_score_status": "blocked_incomplete_economic_inputs; the frozen grid records have no fare schedule; scores remain blocked wherever a passenger fare or other declared cost input is missing",
        "candidate_pool_note": "All hard-feasible routes across methods for the same task share both profiles; any incomplete comparable money input blocks selection.",
        "candidate_pool_scope": "same_task_scenario_across_all_methods",
        "hard_feasible_candidate_routes": hard_feasible_candidates,
        "blocked_candidate_routes": blocked_candidates,
        "scenario_status_counts": dict(scenario_statuses),
        "profile_status_counts": dict(profile_statuses),
        "no_new_model_calls": True,
        "outputs": outputs,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "script_sha256": sha256(Path(__file__)),
        "calculate_route_costs_sha256": sha256(calc_script),
    }
    write_json(output_root / "manifest.json", summary)
    progress.update({"status": "completed", "manifest_sha256": sha256(output_root / "manifest.json")})
    write_json(progress_path, progress)
    print(json.dumps({
        "status": summary["status"],
        "model_version": MODEL_VERSION,
        "output_dir": str(output_root),
        "output_files": len(outputs),
        "output_tasks": summary["output_tasks"],
        "money_score_status": summary["money_score_status"],
        "model_calls": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
