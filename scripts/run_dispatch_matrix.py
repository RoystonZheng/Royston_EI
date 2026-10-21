#!/usr/bin/env python3
"""Run the dispatch methods on shared seeds and write an aggregate manifest."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispatch_experiment.runner import run_experiment


METHODS = [
    "D0_greedy",
    "D1_optimizer",
    "D2_agent_optimizer",
    "D3_agent_llm_context_optimizer",
    "D4_agent_llm_feedback_optimizer",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="run a reproducible dispatch matrix")
    parser.add_argument("--seeds", type=int, nargs="+", default=list(range(10)))
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=METHODS)
    parser.add_argument("--map-size", type=int, default=100)
    parser.add_argument("--vehicle-count", type=int, default=10)
    parser.add_argument("--order-count", type=int, default=100)
    parser.add_argument("--feedback-mode", choices=["offline_rule", "none", "deepseek"], default="offline_rule")
    parser.add_argument("--output-dir", default="outputs/dispatch_layer")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    result_paths = []
    scenario_fingerprints = {}
    for method in args.methods:
        for seed in args.seeds:
            payload = run_experiment(
                seed=seed,
                method_name=method,
                map_size=args.map_size,
                vehicle_count=args.vehicle_count,
                order_count=args.order_count,
                feedback_mode=args.feedback_mode,
                output_dir=str(output_dir),
            )
            summaries.append(payload["summary"])
            result_paths.append(payload.get("result_path"))
            scenario_fingerprints["{0}/seed_{1}".format(method, seed)] = payload.get(
                "scenario_fingerprint_sha256"
            )
            print(json.dumps(payload["summary"], ensure_ascii=False))
    manifest = {
        "schema_version": "dispatch-matrix-v1",
        "methods": args.methods,
        "seeds": args.seeds,
        "map_size": args.map_size,
        "vehicle_count": args.vehicle_count,
        "order_count": args.order_count,
        "feedback_mode": args.feedback_mode,
        "scenario_fingerprints_sha256": scenario_fingerprints,
        "summaries": summaries,
        "result_paths": result_paths,
    }
    manifest_path = output_dir / "dispatch_matrix_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("manifest_path=" + str(manifest_path))


if __name__ == "__main__":
    main()

