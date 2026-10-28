#!/usr/bin/env python3
"""Run a disjoint subset of frozen NYC EV2 deterministic case/fleet cells.

Each shard keeps its own progress ledger and experiment manifest while writing
completed result files into the shared official evaluation directory. The
frozen scenario package and benchmark implementation are not modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path
from typing import List

from run_nyc_tlc_dispatch_multimonth_batch_ev2_proxy_v3 import (
    Batch,
    PACKAGE,
    ROOT,
    sha256,
)


OFFICIAL_SEED0_ROOT = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_ev2_segment_ledger_v4_20261027/seed_0"
)


def partition_hash(case_ids: List[str]) -> str:
    payload = {
        "case_ids": case_ids,
        "fleet_ids": ["F1", "F2", "F4"],
        "methods": ["D0_greedy", "D1_optimizer", "D2_agent_optimizer"],
        "seed": 0,
        "phase": "deterministic",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-ids", nargs="+", required=True)
    parser.add_argument("--output-dir", required=True,
                        help="unique shard directory for its manifest, logs, and ledger")
    parser.add_argument("--progress-interval", type=int, default=60)
    args = parser.parse_args()
    if args.progress_interval < 10:
        parser.error("--progress-interval must be at least 10 seconds")
    if len(set(args.case_ids)) != len(args.case_ids):
        parser.error("case IDs must be unique")

    batch_args = Namespace(
        phase="deterministic",
        package=str(PACKAGE),
        output_dir=args.output_dir,
        evaluation_dir=str((ROOT / OFFICIAL_SEED0_ROOT / "evaluation").relative_to(ROOT)),
        seed=0,
        model="gpt-5.6-luna",
        observed_fares_json=(
            "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/"
            "dispatch_ev2_proxy_v3_inputs/"
            "nyc_tlc_observed_fares_partial_from_audited_sidecar_v1.json"
        ),
        progress_interval=args.progress_interval,
    )
    batch = Batch(batch_args)
    all_case_ids = list(batch.package_manifest.get("evaluation_case_ids", []))
    unknown = [case_id for case_id in args.case_ids if case_id not in all_case_ids]
    if unknown:
        parser.error("case IDs are not in the frozen package: " + ", ".join(unknown))

    ranker_root = ROOT / OFFICIAL_SEED0_ROOT
    batch.model_path = ranker_root / "models/d1_pairwise_mlp.json"
    batch.training_report_path = ranker_root / "models/d1_pairwise_mlp_training_report.json"
    if not batch.model_path.is_file() or not batch.training_report_path.is_file():
        raise FileNotFoundError("frozen seed-0 ranker checkpoint or report is missing")

    batch.experiment_manifest["execution_partition"] = {
        "partition_type": "evaluation_case_subset",
        "partition_case_ids": list(args.case_ids),
        "full_frozen_case_count": len(all_case_ids),
        "full_frozen_case_ids_sha256": hashlib.sha256(
            json.dumps(all_case_ids, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "partition_sha256": partition_hash(list(args.case_ids)),
        "evaluation_fleets": ["F1", "F2", "F4"],
        "methods": ["D0_greedy", "D1_optimizer", "D2_agent_optimizer"],
        "seed": 0,
        "phase": "deterministic",
        "shared_official_evaluation_root": str(batch.evaluation_root.relative_to(ROOT)),
        "frozen_package_manifest_sha256": sha256(batch.package_manifest_path),
        "shard_runner_sha256": sha256(Path(__file__).resolve()),
        "model_calls_expected": 0,
    }
    batch.experiment_manifest["status"] = "running_partition"
    batch.checkpoint()
    batch.package_manifest["evaluation_case_ids"] = list(args.case_ids)

    ok = batch.evaluate()
    batch.experiment_manifest["execution_partition"]["status"] = (
        "completed" if ok else "blocked_or_failed"
    )
    batch.experiment_manifest["phases"]["deterministic_baselines"] = (
        "completed_partition" if ok else "blocked_or_failed_partition"
    )
    batch.experiment_manifest["status"] = (
        "completed_partition" if ok else "blocked_or_failed_partition"
    )
    batch.checkpoint()
    print(json.dumps({
        "output_dir": str(batch.output_root),
        "evaluation_dir": str(batch.evaluation_root),
        "case_ids": args.case_ids,
        "partition_sha256": batch.experiment_manifest["execution_partition"]["partition_sha256"],
        "status": batch.experiment_manifest["status"],
        "model_calls_expected": 0,
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
