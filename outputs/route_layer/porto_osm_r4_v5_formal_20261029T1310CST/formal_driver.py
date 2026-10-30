#!/usr/bin/env python3
"""Run the frozen Porto OSM R4 v5 order cohort once per order.

The accepted ord_0001 full-flow pilot is excluded from the formal denominator.
Each remaining order gets a zero-call preflight and at most one model request.
Successful or delivery-unknown orders are never replayed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


BATCH_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BATCH_DIR.parents[2]
PILOT_SCRIPT = PROJECT_ROOT / "scripts/run_porto_r4_osm_first_pilot.py"
PILOT_ACCEPTANCE = PROJECT_ROOT / "outputs/route_layer/porto_osm_r4_v5_live_pilot_20261029T1303CST/manifest.json"
MANIFEST_PATH = BATCH_DIR / "formal_manifest.json"
LOCK_PATH = BATCH_DIR / ".runner.lock"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp.{os.getpid()}")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_source() -> tuple[Any, Any, list[str]]:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import run_porto_r4_osm_first_pilot as pilot

    runner, runtime = pilot.load_runtime()
    context = runtime[1].load(runtime[0], pilot.SEED)
    orders, _ = runner.orders_and_route_pairs(context)
    return pilot, runner, [str(row["order_id"]) for row in orders]


def initial_manifest(pilot: Any, order_ids: list[str]) -> dict[str, Any]:
    accepted = load_json(PILOT_ACCEPTANCE)
    accepted_order = accepted.get("pilot_scope", {}).get("order_id")
    if accepted.get("status") != "pilot_route_complete" or accepted_order not in order_ids:
        raise RuntimeError("accepted_current_sha_pilot_missing_or_invalid")
    provenance = accepted.get("source_provenance", {})
    current_sha = sha256_file(PILOT_SCRIPT)
    if provenance.get("pilot_runner_sha256") != current_sha:
        raise RuntimeError("accepted_pilot_runner_sha_is_stale")
    if accepted.get("llm_actual_model_calls") != 1:
        raise RuntimeError("accepted_pilot_call_count_mismatch")

    cohort = [oid for oid in order_ids if oid != accepted_order]
    return {
        "schema_version": "porto_osm_r4_v5_formal_matrix_v1",
        "created_at_utc": now(),
        "updated_at_utc": now(),
        "status": "planned",
        "experiment_family": "R4_real_OSM_route_layer",
        "map_type": "real OSM-derived Porto SUMO network",
        "layer": "route planning; fixed vehicle/order assignment; no dispatch optimization",
        "formal_denominator": len(cohort),
        "cohort_order_ids": cohort,
        "excluded_pilot": {
            "order_id": accepted_order,
            "manifest_path": str(PILOT_ACCEPTANCE),
            "manifest_sha256": sha256_file(PILOT_ACCEPTANCE),
            "status": accepted.get("status"),
            "excluded_from_formal_denominator": True,
        },
        "source_provenance": {
            **provenance,
            "formal_runner_sha256": sha256_file(Path(__file__).resolve()),
        },
        "decision_model_version": "route_time_money_segment_v5_signed_fare_audit",
        "candidate_methods": [
            "model_proposal_or_repaired",
            "dynamic_fastest",
            "signed_net_ev2",
            "static_free_flow_dijkstra",
            "shared_candidate_link_dijkstra",
        ],
        "profiles": {
            "4:6": {"time_weight": 0.4, "money_weight": 0.6},
            "6:4": {"time_weight": 0.6, "money_weight": 0.4},
        },
        "fare_assumption": {
            "cny_per_passenger_km": pilot.FARE_PER_PASSENGER_KM_CNY,
            "basis": "user-confirmed scenario assumption; not observed fare",
        },
        "electricity_assumption": {
            "eur_per_kwh": pilot.ELECTRICITY_PRICE_EUR_PER_KWH,
            "eur_cny_reference_rate": pilot.ECB_EUR_CNY,
            "profile_id": provenance.get("energy_profile_id"),
            "profile_sha256": provenance.get("energy_profile_sha256"),
            "energy_module_sha256": provenance.get("energy_model_module_sha256"),
        },
        "request_policy": {
            "model_calls_per_order_max": 1,
            "project_level_manual_cap": "none",
            "successful_or_delivery_unknown_requests_replayed": False,
            "preflight_before_each_request": True,
            "provider_429_retry_after_respected": True,
        },
        "tasks": [
            {
                "order_id": oid,
                "status": "pending",
                "run_dir": str(BATCH_DIR / "tasks" / oid),
                "model_calls": 0,
            }
            for oid in cohort
        ],
        "events": [],
        "model_calls_used": 0,
    }


def classify_child(run_dir: Path, return_code: int) -> tuple[str, dict[str, Any]]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return "failed_preflight_or_missing_manifest", {}
    child = load_json(manifest_path)
    status = child.get("status")
    calls = int(child.get("llm_actual_model_calls", child.get("model_calls", 0)) or 0)
    ledger_path = run_dir / "llm_call_ledger.jsonl"
    ledger = None
    if ledger_path.exists():
        lines = [x for x in ledger_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        if lines:
            ledger = json.loads(lines[-1])
    if status == "pilot_route_complete":
        outcome = "completed_pass"
    elif status in {
        "pilot_route_guard_failed",
        "pilot_v5_decision_blocked_or_profile_replay_incomplete",
        "pilot_model_response_received_simulation_not_completed",
    }:
        outcome = "completed_route_or_gate_failure"
    elif status in {"pilot_provider_or_cli_request_failed_no_route", "pilot_blocked_before_model_call"}:
        outcome = "failed_no_usable_route"
    elif status == "running_first_model_trial" or (calls and ledger and ledger.get("status") in {"started", "delivery_unknown", "in_progress"}):
        outcome = "outcome_unknown"
    elif status == "preflight_only_complete" and calls == 0:
        outcome = "preflight_complete_no_model_call"
    else:
        outcome = "failed_or_incomplete" if return_code else "outcome_unknown"
    return outcome, {"child_status": status, "model_calls": calls, "ledger_status": ledger.get("status") if ledger else None}


def run_cmd(mode: str, order_id: str, run_dir: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(PILOT_SCRIPT), mode, "--run-dir", str(run_dir), "--order-id", order_id]
    with log_path.open("ab") as log:
        log.write(("\n$ " + " ".join(argv) + "\n").encode())
        proc = subprocess.run(argv, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, check=False)
    return proc.returncode


def _retry_after_value(value: Any) -> float | None:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        target = parsedate_to_datetime(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())


def _nested_items(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _nested_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_items(child)


def _nested_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _nested_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_strings(child)


def retry_after_seconds(run_dir: Path) -> tuple[bool, float | None]:
    path = run_dir / "codex_requests/call_0000.jsonl"
    if not path.exists():
        return False, None
    try:
        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception:
        return False, None

    import re

    rate_limit_events = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("type", "")).lower() not in {"error", "turn.failed", "response.failed"}:
            continue
        error_payload = event.get("error", event)
        error_text = json.dumps(error_payload, ensure_ascii=False, sort_keys=True).lower()
        status_429 = any(
            key.lower().replace("_", "-") in {"status", "status-code", "http-status", "code"}
            and str(value).strip().split(maxsplit=1)[0] == "429"
            for key, value in _nested_items(error_payload)
            if value is not None and str(value).strip()
        )
        if (
            status_429
            or re.search(r"(?<!\d)429(?!\d)", error_text)
            or "rate_limit" in error_text
            or "rate limit" in error_text
            or "too many requests" in error_text
        ):
            rate_limit_events.append(event)

    if not rate_limit_events:
        return False, None

    delays = []
    for event in rate_limit_events:
        error_payload = event.get("error", event)
        for key, value in _nested_items(event):
            if key.lower().replace("_", "-") in {"retry-after", "retry-after-seconds"}:
                delay = _retry_after_value(value)
                if delay is not None:
                    delays.append(delay)
        for message in _nested_strings(error_payload):
            match = re.search(r"(?i)\bretry[-_ ]after\b\s*[:=]\s*([^\r\n]+)", message)
            if match:
                delay = _retry_after_value(match.group(1).strip().strip("\"' },;"))
                if delay is not None:
                    delays.append(delay)

    return True, max(delays) if delays else None


def execute(manifest: dict[str, Any]) -> None:
    manifest["status"] = "running"
    manifest["updated_at_utc"] = now()
    atomic_json(MANIFEST_PATH, manifest)
    for task in manifest["tasks"]:
        if task["status"] in {"completed_pass", "completed_route_or_gate_failure", "failed_no_usable_route", "outcome_unknown", "preflight_blocked", "failed_preflight_or_missing_manifest"}:
            continue
        order_id = task["order_id"]
        run_dir = Path(task["run_dir"])
        preflight_already_passed = task.get("status") == "ready_for_single_call"
        if task["status"] == "active":
            outcome, details = classify_child(run_dir, 1)
            if outcome == "preflight_complete_no_model_call":
                task["status"] = "ready_for_single_call"
                task["recovery_note"] = "recovered_preflight_only_state; no model request had been made"
                preflight_already_passed = True
            else:
                task["status"] = "outcome_unknown"
                task["recovery_note"] = "prior active state is never replayed"
                task.update(details)
                continue
        if run_dir.exists() and task["status"] == "pending":
            task["status"] = "failed_preflight_or_missing_manifest"
            task["recovery_note"] = "untracked output directory exists; refusing to overwrite or replay"
            continue
        task["status"] = "active"
        task["started_at_utc"] = now()
        task["attempts"] = 1
        manifest["current_order_id"] = order_id
        manifest["updated_at_utc"] = now()
        atomic_json(MANIFEST_PATH, manifest)

        if not preflight_already_passed:
            preflight_rc = run_cmd("preflight", order_id, run_dir, BATCH_DIR / "logs" / f"{order_id}.log")
            preflight_status = None
            child_manifest = run_dir / "manifest.json"
            if child_manifest.exists():
                preflight_status = load_json(child_manifest).get("status")
            task["preflight_status"] = preflight_status
            task["preflight_return_code"] = preflight_rc
            if preflight_rc != 0 or preflight_status != "preflight_only_complete":
                task["status"] = "preflight_blocked"
                task["completed_at_utc"] = now()
                task["model_calls"] = 0
                task["reason"] = "zero_call_preflight_did_not_pass"
                manifest["events"].append({"at_utc": now(), "order_id": order_id, "event": "preflight_blocked_no_model_call"})
                manifest["updated_at_utc"] = now()
                atomic_json(MANIFEST_PATH, manifest)
                print(f"{order_id}: preflight_blocked", flush=True)
                continue
        child_manifest = run_dir / "manifest.json"

        # Persist before the live request so an interrupted run is quarantined on resume.
        task["status"] = "active"
        task["model_call_started_at_utc"] = now()
        manifest["updated_at_utc"] = now()
        atomic_json(MANIFEST_PATH, manifest)
        run_rc = run_cmd("run", order_id, run_dir, BATCH_DIR / "logs" / f"{order_id}.log")
        outcome, details = classify_child(run_dir, run_rc)
        task.update(details)
        task["model_calls"] = details.get("model_calls", 0)
        manifest["model_calls_used"] = sum(int(item.get("model_calls", 0) or 0) for item in manifest["tasks"])
        task["status"] = outcome
        task["run_return_code"] = run_rc
        task["completed_at_utc"] = now()
        task["child_manifest_sha256"] = sha256_file(child_manifest) if child_manifest.exists() else None
        task["run_output_sha256"] = sha256_file(run_dir / "decision_v5_candidate_gate.json") if (run_dir / "decision_v5_candidate_gate.json").exists() else None
        manifest["events"].append({"at_utc": now(), "order_id": order_id, "event": outcome, "child_manifest_sha256": task["child_manifest_sha256"]})
        manifest["current_order_id"] = None
        manifest["updated_at_utc"] = now()
        atomic_json(MANIFEST_PATH, manifest)
        print(f"{order_id}: {outcome}", flush=True)
        is_provider_429, cooldown = retry_after_seconds(run_dir)
        if is_provider_429:
            if cooldown is None:
                manifest["status"] = "paused_provider_429_without_retry_after"
                manifest["updated_at_utc"] = now()
                atomic_json(MANIFEST_PATH, manifest)
                print("provider 429 without Retry-After; pausing batch", flush=True)
                return
            task["retry_after_respected_seconds"] = cooldown
            atomic_json(MANIFEST_PATH, manifest)
            print(f"provider 429; waiting Retry-After {cooldown:g}s before next independent order", flush=True)
            if cooldown > 0:
                time.sleep(cooldown)

    manifest["current_order_id"] = None
    manifest["status"] = "completed" if all(t["status"] in {"completed_pass", "completed_route_or_gate_failure", "failed_no_usable_route", "outcome_unknown", "preflight_blocked", "failed_preflight_or_missing_manifest"} for t in manifest["tasks"]) else "running"
    manifest["updated_at_utc"] = now()
    atomic_json(MANIFEST_PATH, manifest)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.plan_only == args.execute:
        raise SystemExit("choose exactly one of --plan-only or --execute")
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another Porto R4 formal runner holds the lock")

    pilot, _runner, source_order_ids = load_source()
    if not MANIFEST_PATH.exists():
        manifest = initial_manifest(pilot, source_order_ids)
        atomic_json(MANIFEST_PATH, manifest)
    else:
        manifest = load_json(MANIFEST_PATH)
        if manifest.get("cohort_order_ids") != [x for x in source_order_ids if x != manifest.get("excluded_pilot", {}).get("order_id")]:
            raise SystemExit("formal cohort source changed; refusing to resume")
        if manifest.get("source_provenance", {}).get("pilot_runner_sha256") != sha256_file(PILOT_SCRIPT):
            raise SystemExit("runner SHA changed; refusing to resume frozen batch")
    if args.plan_only:
        manifest["status"] = "planned"
        manifest["updated_at_utc"] = now()
        atomic_json(MANIFEST_PATH, manifest)
        print(json.dumps({"status": "planned", "formal_denominator": manifest["formal_denominator"], "first_order": manifest["cohort_order_ids"][0], "last_order": manifest["cohort_order_ids"][-1], "pilot_excluded": manifest["excluded_pilot"]["order_id"], "runner_sha256": manifest["source_provenance"]["pilot_runner_sha256"]}, ensure_ascii=False))
        return 0
    execute(manifest)
    print(json.dumps({"status": manifest["status"], "model_calls_used": manifest["model_calls_used"], "formal_denominator": manifest["formal_denominator"], "manifest": str(MANIFEST_PATH)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
