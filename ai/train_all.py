"""Train all small policies and save PyTorch .pt checkpoints."""

from __future__ import annotations

import json
from pathlib import Path

from train_common import train_task


ROOT = Path(__file__).resolve().parents[1]
SUMMARY_PATH = ROOT / "ai" / "training_summary.json"
TASKS = [
    ("dijkstra", "all", 180),
    ("astar_dynamic", "all", 180),
    ("rrt_narrow", "all", 220),
    ("car_control", "all", 220),
]


def downsample(values: list[float], count: int = 48) -> list[float]:
    if len(values) <= count:
        return values
    step = (len(values) - 1) / (count - 1)
    return [values[round(i * step)] for i in range(count)]


def main() -> int:
    summary: dict[str, dict] = {}
    for task, difficulty, epochs in TASKS:
        print(f"\n== Training {task} ({difficulty}, {epochs} epochs) ==")
        result = train_task(task=task, difficulty=difficulty, epochs=epochs)
        losses = result.pop("losses", [])
        result["loss_curve"] = downsample(losses)
        result["final_loss"] = losses[-1] if losses else None
        summary[task] = result
        print(json.dumps(result, ensure_ascii=False, indent=2))

    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved training summary to {SUMMARY_PATH.relative_to(ROOT)}")
    print("Next: python ai/export_rollouts.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
