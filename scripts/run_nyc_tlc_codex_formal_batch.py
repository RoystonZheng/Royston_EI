#!/usr/bin/env python3
"""Run the resumable Codex CLI D5 benchmark matrix on frozen NYC TLC inputs.

The scenario files are fixed; seeds 1-9 are repeated simulation/model runs on
those same five configurations, not independent NYC demand samples. Existing
seed-0 artifacts are referenced in the manifest and are never re-run here.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1")
RESULTS = PACKAGE / "results"
BASELINE_DIR = RESULTS / "pure_llm_codex_cli_formal_baselines_v1"
D5_DIR = RESULTS / "pure_llm_codex_cli_formal_v1"
SOURCE_BASELINES = RESULTS / "traditional_baselines_complete_v1"
SOURCE_D5 = RESULTS / "pure_llm_codex_cli_feasible_edges_v2"
RUNNER = ROOT / "scripts/run_nyc_tlc_dispatch_benchmark.py"

CONFIGS = [
    ("C2_four_same_second_orders_three_vehicles", None),
    ("C3_manhattan_multizone_44_order_burst", None),
    ("C4_manhattan_790_order_five_minute_load_sweep", "F1"),
    ("C4_manhattan_790_order_five_minute_load_sweep", "F2"),
    ("C4_manhattan_790_order_five_minute_load_sweep", "F4"),
]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def good_result(path: Path) -> bool:
    payload = load_json(path)
    return bool(payload and not payload.get("partial_run", True) and payload.get("summary"))


def result_path(out_dir: Path, case: str, fleet: str | None, method: str, seed: int) -> Path:
    return out_dir / case / (fleet or "single_fleet") / f"{method}_seed_{seed}.json"


def log_event(ledger: Path, event: dict[str, Any]) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp_utc": now(), **event}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    print(json.dumps({"timestamp_utc": now(), **event}, ensure_ascii=False), flush=True)


def make_command(args: argparse.Namespace, case: str, fleet: str | None, seed: int,
                 methods: list[str], output_dir: Path) -> list[str]:
    cmd = [
        sys.executable, str(RUNNER),
        "--package", str(PACKAGE),
        "--case", case,
        "--methods", *methods,
        "--seed", str(seed),
        "--compact-records",
        "--output-dir", str(output_dir),
        "--codex-model", args.model,
    ]
    if fleet:
        cmd += ["--fleet-id", fleet]
    if "D5_pure_llm_direct" in methods:
        cmd.append("--allow-live-api")
    return cmd


def trace_snapshot(case: str, fleet: str | None, seed: int) -> tuple[int, int]:
    folder = D5_DIR / f"seed_{seed}" / case / (fleet or "single_fleet") / "codex_cli_traces"
    files = list(folder.glob("*.jsonl")) if folder.exists() else []
    completed = 0
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") == "turn.completed":
                    completed += 1
        except OSError:
            continue
    return len(files), completed


def run_one(args: argparse.Namespace, ledger: Path, case: str, fleet: str | None,
            seed: int, methods: list[str], output_dir: Path, label: str) -> int:
    cmd = make_command(args, case, fleet, seed, methods, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = D5_DIR / "execution_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{case}__{fleet or 'single_fleet'}__seed{seed}__{label}.log"
    start = time.monotonic()
    log_event(ledger, {"event": "start", "case": case, "fleet": fleet or "single_fleet",
                       "seed": seed, "label": label, "command": cmd, "log": str(log_path)})
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                text=True, env=os.environ.copy())
        last_report = time.monotonic()
        while proc.poll() is None:
            time.sleep(min(5, args.progress_interval))
            if time.monotonic() - last_report >= args.progress_interval:
                traces, calls = trace_snapshot(case, fleet, seed) if "D5_pure_llm_direct" in methods else (0, 0)
                log_event(ledger, {"event": "progress", "case": case, "fleet": fleet or "single_fleet",
                                   "seed": seed, "label": label, "elapsed_seconds": round(time.monotonic()-start, 1),
                                   "trace_files": traces, "completed_turn_events": calls})
                last_report = time.monotonic()
        code = proc.returncode
    elapsed = round(time.monotonic() - start, 2)
    expected = [result_path(output_dir, case, fleet, method, seed) for method in methods]
    states = {p.name: {"exists": p.exists(), "sha256": sha256(p) if p.exists() else None,
                       "complete": good_result(p)} for p in expected}
    traces, calls = trace_snapshot(case, fleet, seed) if "D5_pure_llm_direct" in methods else (0, 0)
    log_event(ledger, {"event": "finish", "case": case, "fleet": fleet or "single_fleet",
                       "seed": seed, "label": label, "return_code": code,
                       "elapsed_seconds": elapsed, "results": states,
                       "trace_files": traces, "completed_turn_events": calls,
                       "log": str(log_path)})
    return code


def build_manifest(args: argparse.Namespace, seeds: list[int]) -> dict[str, Any]:
    package = ROOT / PACKAGE
    bm = load_json(package / "benchmark_manifest.json") or {}
    manifest = {
        "experiment_id": "nyc_tlc_d5_codex_cli_formal_v1",
        "created_at_utc": now(),
        "status": "running",
        "backend": "codex_cli",
        "model": args.model,
        "reasoning_effort": "low (CLI policy)",
        "input_protocol": "hard_feasible_candidate_edges_v2",
        "retry_policy": "zero model-request retries",
        "scenario_package": str(PACKAGE),
        "scenario_package_manifest_sha256": sha256(package / "benchmark_manifest.json"),
        "seeds_run_in_this_batch": seeds,
        "seed0_reused_from": {
            "d5": str(SOURCE_D5),
            "d0_d1": str(SOURCE_BASELINES),
            "seed0_pairing_audit": str(RESULTS / "three_method_codex_cli_seed0_matched_v1"),
        },
        "interpretation": "The five scenario/fleet inputs are frozen. Seed variation repeats simulation/model runs on the same inputs; it does not create independent NYC demand samples.",
        "configurations": [],
        "cost_accounting": "Reconcile raw Codex CLI turn usage after execution; report rate-card equivalents separately from unknown account billing.",
    }
    for case, fleet in CONFIGS:
        c = bm.get("cases", {}).get(case, {})
        files = c.get("files", {})
        scenario_dir = package / c.get("path", "")
        input_files = {name: {"sha256": entry.get("sha256")} for name, entry in files.items()
                       if name in {"scenario.json", "orders.json", "zone_od_cost_matrix.csv.gz"}}
        # Fleet selection is stored in the scenario JSON. The runner result also
        # records the complete runtime fingerprints for each method and seed.
        manifest["configurations"].append({
            "case": case, "fleet_id": fleet,
            "scenario_id": c.get("scenario_id"),
            "scenario_files": input_files,
            "scenario_dir": str(scenario_dir),
        })
    return manifest


def write_pairing(seeds: list[int]) -> None:
    rows: list[dict[str, Any]] = []
    for case, fleet in CONFIGS:
        for seed in [0, *seeds]:
            if seed == 0:
                bdir, ddir = SOURCE_BASELINES, SOURCE_D5
            else:
                bdir, ddir = BASELINE_DIR / f"seed_{seed}", D5_DIR / f"seed_{seed}"
            files = {m: result_path(bdir, case, fleet, m, seed) for m in ("D0_greedy", "D1_optimizer")}
            d5 = result_path(ddir, case, fleet, "D5_pure_llm_direct", seed)
            payloads = {m: load_json(p) for m, p in files.items()}
            payloads["D5_pure_llm_direct"] = load_json(d5)
            if seed == 0 and payloads["D5_pure_llm_direct"] is None and case == "C4_manhattan_790_order_five_minute_load_sweep" and fleet == "F1":
                replay = SOURCE_D5 / case / fleet / "D5_pure_llm_direct_seed_0_replay.json"
                if replay.is_file():
                    d5 = replay
                    payloads["D5_pure_llm_direct"] = load_json(d5)
            refs = {m: p for m, p in [("D0_greedy", files["D0_greedy"]), ("D1_optimizer", files["D1_optimizer"]), ("D5_pure_llm_direct", d5)]}
            fps = [p.get("scenario_fingerprint_sha256") if p else None for p in payloads.values()]
            hash_maps = [p.get("input_hashes") if p else None for p in payloads.values()]
            for method, p in payloads.items():
                summary = p.get("summary", {}) if p else {}
                rows.append({
                    "case": case, "fleet_id": fleet or "single_fleet", "seed": seed,
                    "method": method, "result_path": str(refs[method]),
                    "result_sha256": sha256(refs[method]) if refs[method].exists() else "",
                    "result_present": bool(p), "complete": bool(p and not p.get("partial_run", True)),
                    "execution_provenance": p.get("execution_provenance", "live_run") if p else "",
                    "scenario_fingerprint_sha256": fps[list(payloads).index(method)],
                    "inputs_match_all_methods": bool(all(fps) and len(set(fps)) == 1 and
                                                      all(h is not None for h in hash_maps) and len({json.dumps(h, sort_keys=True) for h in hash_maps}) == 1),
                    "served_orders": summary.get("served_orders"),
                    "unserved_orders": summary.get("unserved_orders"),
                    "service_rate": summary.get("service_rate"),
                    "average_pickup_wait_sec": summary.get("average_pickup_wait_sec"),
                    "p95_pickup_wait_sec": summary.get("p95_pickup_wait_sec"),
                    "total_empty_distance_m": summary.get("total_empty_distance_m"),
                    "total_energy_kwh": summary.get("total_energy_kwh"),
                    "dispatch_rounds": summary.get("total_dispatch_rounds"),
                    "llm_call_count": summary.get("total_llm_calls"),
                    "fallback_count": summary.get("fallback_count"),
                    "commitment_violations": summary.get("commitment_violations"),
                    "validator_errors": sum(len(v) if isinstance(v, list) else 1
                                             for v in (p.get("round_validation_errors", []) if p else [])),
                    "run_mode": p.get("run_mode") if p else "",
                })
    path = D5_DIR / "paired_results.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", nargs="+", type=int, default=list(range(1, 10)),
                        help="additional simulation seeds; seed 0 is reused from archived v2")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--progress-interval", type=int, default=60)
    args = parser.parse_args()
    seeds = sorted(set(args.seeds))
    if any(seed <= 0 for seed in seeds):
        parser.error("this batch only accepts seed >= 1; seed 0 is already archived")
    for d in (D5_DIR, BASELINE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    ledger = D5_DIR / "batch_progress.jsonl"
    manifest_path = D5_DIR / "experiment_manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps(build_manifest(args, seeds), ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
    manifest = load_json(manifest_path) or {}
    manifest["status"] = "running"
    manifest["seeds_run_in_this_batch"] = seeds
    manifest["updated_at_utc"] = now()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failures = 0
    for seed in seeds:
        baseline_out = BASELINE_DIR / f"seed_{seed}"
        d5_out = D5_DIR / f"seed_{seed}"
        for case, fleet in CONFIGS:
            d0 = result_path(baseline_out, case, fleet, "D0_greedy", seed)
            d1 = result_path(baseline_out, case, fleet, "D1_optimizer", seed)
            if not (good_result(d0) and good_result(d1)):
                code = run_one(args, ledger, case, fleet, seed,
                               ["D0_greedy", "D1_optimizer"], baseline_out, "baselines")
                failures += int(code != 0 or not (good_result(d0) and good_result(d1)))
            if not (good_result(d0) and good_result(d1)):
                log_event(ledger, {"event": "skip_unpaired_d5", "case": case,
                                   "fleet": fleet or "single_fleet", "seed": seed,
                                   "reason": "matched D0/D1 baseline is incomplete"})
                failures += 1
                continue

            d5 = result_path(d5_out, case, fleet, "D5_pure_llm_direct", seed)
            if good_result(d5):
                log_event(ledger, {"event": "skip_complete", "case": case,
                                   "fleet": fleet or "single_fleet", "seed": seed,
                                   "method": "D5_pure_llm_direct", "path": str(d5)})
                continue
            trace_dir = d5_out / case / (fleet or "single_fleet") / "codex_cli_traces"
            if trace_dir.exists() and any(trace_dir.glob("*.jsonl")):
                log_event(ledger, {"event": "skip_orphaned_trace_no_retry", "case": case,
                                   "fleet": fleet or "single_fleet", "seed": seed,
                                   "trace_dir": str(trace_dir),
                                   "reason": "model traces exist without a complete result; preserve zero-retry policy and require offline replay"})
                failures += 1
                continue
            code = run_one(args, ledger, case, fleet, seed,
                           ["D5_pure_llm_direct"], d5_out, "d5")
            failures += int(code != 0 or not good_result(d5))
            write_pairing(seeds)
    write_pairing(seeds)
    manifest = load_json(manifest_path) or {}
    manifest["status"] = "completed" if failures == 0 else "completed_with_gaps"
    manifest["updated_at_utc"] = now()
    manifest["failed_or_incomplete_cells"] = failures
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_event(ledger, {"event": "batch_finished", "status": manifest["status"],
                       "failed_or_incomplete_cells": failures,
                       "manifest": str(manifest_path), "paired_results": str(D5_DIR / "paired_results.csv")})
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
