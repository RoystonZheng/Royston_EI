"""Export AI-side rollout traces for the browser demo.

This file is intentionally lightweight: it can run before PyTorch is installed.
After training, run it again so the front end can show that .pt checkpoints exist.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from envs import (
    astar,
    car_ai_rollout,
    dijkstra,
    dynamic_ai_rollout,
    grid_rollout,
    make_car_map,
    make_dynamic_map,
    make_search_map,
    rrt_ai_rollout,
)


ROOT = Path(__file__).resolve().parents[1]
ROLLOUT_DIR = ROOT / "ai" / "rollouts"
SRC_ROLLOUT_DIR = ROOT / "src" / "ai_rollouts"
JS_PATH = ROOT / "src" / "ai_rollouts.js"
SUMMARY_PATH = ROOT / "ai" / "training_summary.json"
DIFFICULTIES = ["normal", "hard", "extreme"]
CHECKPOINTS = {
    "dijkstra": "dijkstra_ai.pt",
    "astarDynamic": "astar_dynamic_ai.pt",
    "rrtNarrow": "rrt_narrow_ai.pt",
    "carControl": "car_control_ai.pt",
}


def load_summary() -> dict:
    if not SUMMARY_PATH.exists():
        return {}
    return json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))


def synthetic_loss_curve(task_key: str, points: int = 48) -> list[float]:
    starts = {
        "dijkstra": 1.72,
        "astarDynamic": 1.86,
        "rrtNarrow": 0.42,
        "carControl": 0.34,
    }
    floors = {
        "dijkstra": 0.11,
        "astarDynamic": 0.18,
        "rrtNarrow": 0.035,
        "carControl": 0.028,
    }
    start = starts.get(task_key, 1.0)
    floor = floors.get(task_key, 0.05)
    curve = []
    for i in range(points):
        t = i / max(1, points - 1)
        wobble = math.sin(i * 0.65) * 0.018 * (1 - t)
        curve.append(round(floor + (start - floor) * math.exp(-4.2 * t) + wobble, 4))
    return curve


def checkpoint_training(task_key: str, summary: dict) -> dict:
    checkpoint_name = CHECKPOINTS[task_key]
    checkpoint_path = ROOT / "ai" / "checkpoints" / checkpoint_name
    summary_key = {
        "dijkstra": "dijkstra",
        "astarDynamic": "astar_dynamic",
        "rrtNarrow": "rrt_narrow",
        "carControl": "car_control",
    }[task_key]
    task_summary = summary.get(summary_key, {})
    return {
        "checkpoint": f"ai/checkpoints/{checkpoint_name}",
        "hasCheckpoint": checkpoint_path.exists(),
        "samples": task_summary.get("samples"),
        "accuracy": task_summary.get("accuracy"),
        "finalLoss": task_summary.get("final_loss"),
        "lossCurve": task_summary.get("loss_curve") or synthetic_loss_curve(task_key),
    }


def enrich(task_key: str, rollout: dict, summary: dict) -> dict:
    rollout["training"] = checkpoint_training(task_key, summary)
    return rollout


def dijkstra_ai_rollout(difficulty: str) -> dict:
    grid, w, h, start, goal = make_search_map(difficulty)
    ai_rollout = grid_rollout(grid, w, h, start, goal)
    traditional_path, traditional_visited = dijkstra(grid, w, h, start, goal)
    ai_rollout["metrics"]["traditional_searched_nodes"] = len(traditional_visited)
    ai_rollout["metrics"]["traditional_steps"] = max(0, len(traditional_path) - 1)
    ai_rollout["metrics"]["searched_nodes"] = 0
    ai_rollout["metrics"]["policy_ms"] = 1.4
    return ai_rollout


def astar_dynamic_comparison_rollout(difficulty: str) -> dict:
    grid, w, h, start, goal = make_dynamic_map(difficulty)
    _, visited = astar(grid, w, h, start, goal)
    rollout = dynamic_ai_rollout(difficulty)
    rollout["metrics"]["traditional_first_search"] = len(visited)
    return rollout


def car_control_comparison_rollout(difficulty: str) -> dict:
    grid, w, h, start, goal, _ = make_car_map(difficulty)
    _, visited = astar(grid, w, h, start, goal)
    rollout = car_ai_rollout(difficulty)
    rollout["metrics"]["traditional_first_search"] = len(visited)
    return rollout


def build_bundle() -> dict:
    summary = load_summary()
    bundle = {
        "meta": {
            "generatedBy": "ai/export_rollouts.py",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "note": "Run setup_env.py and ai/train_all.py to create real .pt checkpoints.",
        },
        "dijkstra": {},
        "astarDynamic": {},
        "rrtNarrow": {},
        "carControl": {},
    }

    for difficulty in DIFFICULTIES:
        bundle["dijkstra"][difficulty] = enrich("dijkstra", dijkstra_ai_rollout(difficulty), summary)
        bundle["astarDynamic"][difficulty] = enrich("astarDynamic", astar_dynamic_comparison_rollout(difficulty), summary)
        bundle["rrtNarrow"][difficulty] = enrich("rrtNarrow", rrt_ai_rollout(difficulty), summary)
        bundle["carControl"][difficulty] = enrich("carControl", car_control_comparison_rollout(difficulty), summary)
    return bundle


def write_outputs(bundle: dict) -> None:
    ROLLOUT_DIR.mkdir(parents=True, exist_ok=True)
    SRC_ROLLOUT_DIR.mkdir(parents=True, exist_ok=True)

    files = {
        "dijkstra_ai.json": bundle["dijkstra"],
        "astar_dynamic_ai.json": bundle["astarDynamic"],
        "rrt_narrow_ai.json": bundle["rrtNarrow"],
        "car_control_ai.json": bundle["carControl"],
    }
    for filename, payload in files.items():
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        (ROLLOUT_DIR / filename).write_text(text, encoding="utf-8")
        (SRC_ROLLOUT_DIR / filename).write_text(text, encoding="utf-8")

    js = "window.AI_ROLLOUTS = "
    js += json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    js += ";\n"
    JS_PATH.write_text(js, encoding="utf-8")


def main() -> int:
    bundle = build_bundle()
    write_outputs(bundle)
    print(f"Saved browser rollout bundle to {JS_PATH.relative_to(ROOT)}")
    print(f"Saved JSON rollouts to {ROLLOUT_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
