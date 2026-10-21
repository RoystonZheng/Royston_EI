#!/usr/bin/env python3
"""Run the offline dispatch-agent simulation.

Examples:
  python scripts/run_dispatch_experiment.py --method D4_agent_llm_feedback_optimizer
  python scripts/run_dispatch_experiment.py --method D1_optimizer --seed 3 --map-size 20
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispatch_experiment.runner import run_experiment


def main() -> None:
    parser = argparse.ArgumentParser(description="run dispatch-agent simulation")
    parser.add_argument(
        "--method",
        default="D4_agent_llm_feedback_optimizer",
        choices=[
            "D0_greedy",
            "D1_optimizer",
            "D2_agent_optimizer",
            "D3_agent_llm_context_optimizer",
            "D4_agent_llm_feedback_optimizer",
            "D5_pure_llm_direct",
        ],
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--map-size", type=int, default=20)
    parser.add_argument("--vehicle-count", type=int, default=10)
    parser.add_argument("--order-count", type=int, default=100)
    parser.add_argument(
        "--feedback-mode",
        choices=["offline_rule", "none", "deepseek"],
        default="offline_rule",
        help="D4 feedback mode; D5 direct dispatch uses Codex CLI",
    )
    parser.add_argument("--codex-model", default="gpt-5.6-luna", help="Codex CLI model used by D5")
    parser.add_argument("--output-dir", default="outputs/dispatch_layer")
    args = parser.parse_args()
    payload = run_experiment(
        seed=args.seed,
        method_name=args.method,
        map_size=args.map_size,
        vehicle_count=args.vehicle_count,
        order_count=args.order_count,
        feedback_mode=args.feedback_mode,
        llm_backend="codex_cli",
        llm_model=args.codex_model,
        output_dir=args.output_dir,
    )
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print("result_path=" + str(payload.get("result_path", "")))


if __name__ == "__main__":
    main()

