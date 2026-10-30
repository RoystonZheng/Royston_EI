#!/usr/bin/env python3
"""Run the frozen 20-cell R1 v3 matrix without overwriting prior outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
OUTPUT = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/formal_v3_fixed_delay"
RUNNER = PACKAGE / "scripts/run_porto_r1_dynamic_sumo_v3.py"
PROTOCOL = PACKAGE / "derived/experiments/protocols/r1_dynamic_sumo_protocol_v3.json"
TRACE_MANIFEST = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/planning_latency_traces_v1/manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_statuses(path: Path) -> dict[tuple[int, str], str]:
    result: dict[tuple[int, str], str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            seed, objective, status = parts
            result[(int(seed), objective)] = status
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="Continue cells already recorded in batch_status.tsv; never retry recorded failures.")
    options = parser.parse_args()

    status_path = OUTPUT / "batch_status.tsv"
    existing = read_statuses(status_path)
    if OUTPUT.exists() and any(OUTPUT.iterdir()) and not options.resume and existing:
        raise SystemExit("formal_v3_fixed_delay already has a status ledger; use --resume to continue without retrying recorded cells")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "logs").mkdir(exist_ok=True)
    manifest_path = OUTPUT / "run_manifest.json"
    manifest = {
        "experiment_id": "porto_r1_dynamic_sumo_v3_fixed_delay",
        "protocol": str(PROTOCOL.relative_to(PACKAGE)),
        "protocol_sha256": sha256(PROTOCOL),
        "runner": str(RUNNER.relative_to(PACKAGE)),
        "runner_sha256": sha256(RUNNER),
        "latency_trace_manifest": str(TRACE_MANIFEST.relative_to(PACKAGE)),
        "latency_trace_manifest_sha256": sha256(TRACE_MANIFEST),
        "planned_cells": 20,
        "seeds": list(range(10)),
        "objectives": ["FASTEST", "LOWEST_ENERGY"],
        "no_retries": True,
        "outputs": "Each seed/objective is written once below seed_<n>/<objective>; failed runs remain in the denominator.",
    }
    if manifest_path.exists():
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        if prior != manifest:
            raise SystemExit("existing v3 run manifest does not match current frozen protocol/runner; refusing to continue")
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    for seed in range(10):
        for objective in ("FASTEST", "LOWEST_ENERGY"):
            key = (seed, objective)
            if key in existing:
                continue
            trace = PACKAGE / f"derived/experiments/r1_dynamic_sumo_v1/planning_latency_traces_v1/seed_{seed}/{objective}.json"
            run_dir = OUTPUT / f"seed_{seed}" / objective
            if run_dir.exists() and any(run_dir.iterdir()):
                raise SystemExit(f"refusing to overwrite an unledgered run directory: {run_dir}")
            run_dir.mkdir(parents=True, exist_ok=True)
            log_path = OUTPUT / "logs" / f"seed_{seed}_{objective}.log"
            command = [
                sys.executable,
                str(RUNNER),
                "--seed", str(seed),
                "--objective", objective,
                "--traffic-rate-per-hour", "90",
                "--traffic-hours", "2",
                "--planning-latency-trace", str(trace.relative_to(PACKAGE)),
                "--output-dir", str(run_dir.relative_to(PACKAGE)),
            ]
            start = time.time()
            with log_path.open("w", encoding="utf-8") as log:
                completed = subprocess.run(command, cwd=PACKAGE, stdout=log, stderr=subprocess.STDOUT, check=False)
            elapsed = time.time() - start
            status = "COMPLETED" if completed.returncode == 0 and (run_dir / "summary.json").exists() else f"FAILED_EXIT_{completed.returncode}"
            with status_path.open("a", encoding="utf-8") as ledger:
                ledger.write(f"{seed}\t{objective}\t{status}\n")
            with (OUTPUT / "run_timing.tsv").open("a", encoding="utf-8") as timing:
                timing.write(f"{seed}\t{objective}\t{status}\t{elapsed:.3f}\t{sha256(trace)}\n")
            existing[key] = status
            print(f"seed={seed} objective={objective} status={status} wall_s={elapsed:.1f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
