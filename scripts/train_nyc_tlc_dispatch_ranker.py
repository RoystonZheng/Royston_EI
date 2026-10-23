#!/usr/bin/env python3
"""Train the dispatch MLP from D1 pairwise labels on frozen training windows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dispatch_experiment.neural_ranker import (
    collect_pairwise_training_data,
    save_ranker_checkpoint,
    train_pairwise_ranker,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default="outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1")
    parser.add_argument("--output", required=True, help="JSON model checkpoint path")
    parser.add_argument("--report", required=True, help="training audit report path")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--epochs", type=int, default=120)
    parser.add_argument("--max-negatives-per-positive", type=int, default=64)
    parser.add_argument("d1_results", nargs="+", help="full-round D1 result JSON files")
    args = parser.parse_args()

    package = (ROOT / args.package).resolve()
    pairs, data_summary = collect_pairwise_training_data(
        args.d1_results,
        max_negatives_per_positive=args.max_negatives_per_positive,
    )
    for item in data_summary["training_runs"]:
        scenario_path = package / "cases" / str(item["case_id"]) / "scenario.json"
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        item["source_request_window"] = scenario.get("source_request_window")

    checkpoint, train_summary = train_pairwise_ranker(
        pairs,
        seed=args.seed,
        epochs=args.epochs,
    )
    checkpoint["training"].update(data_summary)
    checkpoint["training"].update({
        "max_negatives_per_positive": args.max_negatives_per_positive,
        "source_request_windows": [
            item["source_request_window"] for item in data_summary["training_runs"]
        ],
    })
    checkpoint_sha256 = save_ranker_checkpoint(args.output, checkpoint)
    report = {
        "schema_version": "dispatch-ranker-training-report-v1",
        "model_id": checkpoint["model_id"],
        "model_path": str(Path(args.output).resolve()),
        "model_sha256": checkpoint_sha256,
        "training_data": data_summary,
        "training": train_summary,
        "interpretation": (
            "In-sample pair ranking against D1 assignment labels only; "
            "not an independent performance or real-world decision-quality result."
        ),
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "model_path": str(Path(args.output).resolve()),
        "model_sha256": checkpoint_sha256,
        "training_report_path": str(report_path.resolve()),
        "training_windows": data_summary["training_window_count"],
        "training_orders": data_summary["training_order_id_count"],
        "pairwise_samples": len(pairs),
        "in_sample_pair_accuracy": train_summary["in_sample_pair_accuracy"],
        "epochs": train_summary["epochs"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
