#!/usr/bin/env python3
"""Run the frozen 10-seed, two-objective R1 v5 SUMO matrix without overwrites."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parents[1]
RUNNER = PACKAGE / "scripts/run_porto_r1_dynamic_sumo_v5.py"
PROTOCOL = PACKAGE / "derived/experiments/protocols/r1_dynamic_sumo_protocol_v5_energy_v2.json"
PREFLIGHT = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/formal_v5_energy_v2/zero_call_route_preflight_v5.json"
LATENCIES = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/planning_latency_traces_v1"
OUTPUT = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/formal_v5_energy_v2"
SUMO_ROOT = Path("/Users/didi/Library/Application Support/Porto SUMO/1.28.0/Versions/1.28.0/EclipseSUMO")
LOCK = threading.Lock()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def update_task(task_id: str, **fields: Any) -> None:
    with LOCK:
        manifest_path = OUTPUT / "batch_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        task = next(row for row in manifest["tasks"] if row["task_id"] == task_id)
        task.update(fields)
        task["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        states = [row["status"] for row in manifest["tasks"]]
        if all(state in {"completed", "completed_with_energy_gaps", "failed", "preserved_existing_output"} for state in states):
            manifest["status"] = "completed_with_gaps" if any(state != "completed" for state in states) else "completed"
        else:
            manifest["status"] = "running"
        manifest["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_json(manifest_path, manifest)
        with (OUTPUT / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"task_id": task_id, "status": task["status"], "time_utc": manifest["updated_at_utc"], **fields}, ensure_ascii=False) + "\n")


def inspect_result(output_dir: Path) -> dict[str, Any]:
    summary_path = output_dir / "summary.json"
    legs_path = output_dir / "route_legs_actual.json"
    traces_path = output_dir / "sumo_motion_traces_v2.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    legs = json.loads(legs_path.read_text(encoding="utf-8"))
    traces = json.loads(traces_path.read_text(encoding="utf-8"))["trips"]
    non_estimable = []
    observed = 0
    zero_noop = 0
    for row in legs:
        fidelity = row.get("energy_trace_fidelity")
        trip_id = row.get("trip_id")
        if row.get("status") == "zero_distance_completed" or fidelity == "zero_distance_noop":
            zero_noop += 1
        elif fidelity == "observed_sumo_speed_trace_grade_missing" and traces.get(trip_id):
            observed += 1
        else:
            non_estimable.append({"trip_id": trip_id, "status": row.get("status"), "fidelity": fidelity, "samples": row.get("motion_trace_samples")})
    audit = {
        "status": summary.get("status"),
        "orders_completed": summary.get("orders_completed"),
        "expected_orders": summary.get("expected_orders"),
        "route_legs_planned": summary.get("route_legs_planned"),
        "actual_leg_rows": len(legs),
        "observed_ev_v2_motion_trace_legs": observed,
        "zero_distance_noop_legs": zero_noop,
        "non_estimable_energy_legs": non_estimable,
        "sum_actual_energy_gross_kwh": summary.get("sum_actual_energy_gross_kwh"),
        "sum_actual_energy_regenerated_kwh": summary.get("sum_actual_energy_regenerated_kwh"),
        "sum_actual_energy_net_kwh": summary.get("sum_actual_energy_net_kwh"),
        "energy_model": summary.get("energy_model"),
        "energy_profile_sha256": summary.get("energy_model_profile", {}).get("profile_sha256"),
    }
    audit["status"] = (
        "completed"
        if summary.get("status") == "completed"
        and summary.get("orders_completed") == summary.get("expected_orders") == 100
        and not non_estimable
        else "completed_with_energy_gaps"
        if summary.get("status") == "completed"
        else "incomplete"
    )
    atomic_json(output_dir / "run_audit.json", audit)
    return audit


def run_one(task: dict[str, Any]) -> dict[str, Any]:
    task_id = task["task_id"]
    output_dir = Path(task["output_dir"])
    if output_dir.exists() and any(output_dir.iterdir()):
        update_task(task_id, status="preserved_existing_output", error="output_directory_nonempty_not_overwritten")
        return {"task_id": task_id, "status": "preserved_existing_output"}
    output_dir.mkdir(parents=True, exist_ok=True)
    trace = LATENCIES / f"seed_{task['seed']}" / f"{task['objective']}.json"
    log_dir = OUTPUT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{task_id}.log"
    env = os.environ.copy()
    tools_path = str(SUMO_ROOT / "share/sumo/tools")
    env["SUMO_HOME"] = str(SUMO_ROOT / "share/sumo")
    env["PORTO_SUMO_BIN"] = str(SUMO_ROOT / "bin/sumo")
    env["PYTHONPATH"] = tools_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    command = [sys.executable, str(RUNNER), "--seed", str(task["seed"]), "--objective", task["objective"], "--planning-latency-trace", str(trace), "--output-dir", str(output_dir)]
    update_task(task_id, status="running", started_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(), command=command, log_path=str(log))
    started = time.perf_counter()
    with log.open("w", encoding="utf-8") as stream:
        proc = subprocess.run(command, cwd=str(PACKAGE), env=env, stdout=stream, stderr=subprocess.STDOUT, check=False)
    elapsed = round(time.perf_counter() - started, 3)
    if proc.returncode != 0 or not (output_dir / "summary.json").is_file():
        update_task(task_id, status="failed", exit_code=proc.returncode, elapsed_wall_s=elapsed, error="runner_exit_nonzero_or_summary_missing")
        return {"task_id": task_id, "status": "failed", "exit_code": proc.returncode}
    audit = inspect_result(output_dir)
    update_task(task_id, status=audit["status"], exit_code=0, elapsed_wall_s=elapsed, run_audit_path=str(output_dir / "run_audit.json"), audit_summary={k: audit[k] for k in ("orders_completed", "expected_orders", "observed_ev_v2_motion_trace_legs", "zero_distance_noop_legs", "non_estimable_energy_legs", "sum_actual_energy_net_kwh")})
    return {"task_id": task_id, "status": audit["status"], "elapsed_wall_s": elapsed}


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    preflight = json.loads(PREFLIGHT.read_text(encoding="utf-8"))
    if preflight.get("status") != "passed" or preflight.get("error_count") != 0:
        raise SystemExit("refusing_formal_start_preflight_not_passed")
    if sha256(RUNNER) != preflight.get("runner_sha256"):
        raise SystemExit("refusing_formal_start_runner_hash_mismatch")
    if sha256(PROTOCOL) != preflight.get("protocol_sha256"):
        raise SystemExit("refusing_formal_start_protocol_hash_mismatch")
    tasks = []
    for seed in range(10):
        for objective in ("FASTEST", "LOWEST_ENERGY"):
            tasks.append({"task_id": f"seed_{seed:02d}_{objective.lower()}", "seed": seed, "objective": objective, "output_dir": str(OUTPUT / f"seed_{seed}" / objective.lower()), "status": "queued"})
    manifest = {
        "batch_id": "porto_r1_dynamic_sumo_v5_ev2_formal_matrix_20261027",
        "status": "running",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "protocol_path": str(PROTOCOL),
        "protocol_sha256": sha256(PROTOCOL),
        "runner_path": str(RUNNER),
        "runner_sha256": sha256(RUNNER),
        "zero_call_preflight_path": str(PREFLIGHT),
        "zero_call_preflight_sha256": sha256(PREFLIGHT),
        "frozen_seed_count": 10,
        "orders_per_seed": 100,
        "fixed_fleet_per_seed": 10,
        "objectives": ["FASTEST", "LOWEST_ENERGY"],
        "run_count": len(tasks),
        "concurrency": 2,
        "retry_policy": "no automatic replay; failed runs remain separately auditable",
        "dispatch_or_reassignment": False,
        "model_requests": 0,
        "tasks": tasks,
    }
    manifest_path = OUTPUT / "batch_manifest.json"
    if manifest_path.exists():
        raise SystemExit(f"refusing_to_overwrite_batch_manifest:{manifest_path}")
    atomic_json(manifest_path, manifest)
    with (OUTPUT / "progress.jsonl").open("w", encoding="utf-8") as stream:
        stream.write(json.dumps({"batch_id": manifest["batch_id"], "status": "running", "time_utc": manifest["created_at_utc"]}, ensure_ascii=False) + "\n")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_one, task) for task in tasks]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    with LOCK:
        final = json.loads(manifest_path.read_text(encoding="utf-8"))
        final["results"] = results
        final["status"] = "completed" if all(item["status"] == "completed" for item in final["tasks"]) else "completed_with_gaps"
        final["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_json(manifest_path, final)
    return 0 if final["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
