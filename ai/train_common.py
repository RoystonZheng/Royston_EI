"""Shared training code for the AI comparison demos."""

from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

try:
    import torch
    from torch import nn
except ModuleNotFoundError as exc:
    raise SystemExit("没有找到 torch。请先运行 python3 setup_env.py，然后激活 .venv 再训练。") from exc

from envs import (
    astar,
    follow_path_samples,
    grid_features,
    make_car_map,
    make_dynamic_map,
    make_rrt_world,
    make_search_map,
)
from models import MLPPolicy, ModelSpec, save_checkpoint


ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_DIR = ROOT / "ai" / "checkpoints"
DIFFICULTIES = ["normal", "hard", "extreme"]


def open_cells(grid: list[int], w: int, h: int) -> list[tuple[int, int]]:
    return [(x, y) for y in range(h) for x in range(w) if grid[y * w + x] == 0]


def selected_difficulties(difficulty: str) -> list[str]:
    return DIFFICULTIES if difficulty == "all" else [difficulty]


def make_grid_dataset(task: str, difficulty: str, sample_limit: int = 320) -> tuple[list[list[float]], list[int]]:
    xs: list[list[float]] = []
    ys: list[int] = []

    for diff in selected_difficulties(difficulty):
        part_xs, part_ys = make_grid_dataset_for_difficulty(task, diff, sample_limit)
        xs.extend(part_xs)
        ys.extend(part_ys)
    return xs, ys


def make_grid_dataset_for_difficulty(task: str, difficulty: str, sample_limit: int) -> tuple[list[list[float]], list[int]]:
    if task == "dijkstra":
        grid, w, h, start, goal = make_search_map(difficulty)
    elif task == "astar_dynamic":
        grid, w, h, start, goal = make_dynamic_map(difficulty)
    else:
        raise ValueError(f"unknown grid task: {task}")

    cells = open_cells(grid, w, h)
    rng = random.Random(13)
    starts = [start] + rng.sample(cells, min(sample_limit, len(cells)))
    xs: list[list[float]] = []
    ys: list[int] = []

    for candidate in starts:
        path, _ = astar(grid, w, h, candidate, goal)
        if len(path) < 2:
            continue
        samples = follow_path_samples(grid, w, h, path[: min(len(path), 40)], goal)
        for features, action in samples:
            xs.append(features)
            ys.append(action)

    if task == "astar_dynamic":
        base_path, _ = astar(grid, w, h, start, goal)
        for mark in [len(base_path) // 3, len(base_path) // 2, len(base_path) * 2 // 3]:
            if 0 <= mark < len(base_path):
                for _ in range(24):
                    xs.append(grid_features(grid, w, h, base_path[mark], goal))
                    ys.append(0)

    return xs, ys


def make_rrt_dataset(difficulty: str, n: int = 900) -> tuple[list[list[float]], list[list[float]]]:
    if difficulty == "all":
        xs: list[list[float]] = []
        ys: list[list[float]] = []
        for diff in DIFFICULTIES:
            part_xs, part_ys = make_rrt_dataset(diff, n)
            xs.extend(part_xs)
            ys.extend(part_ys)
        return xs, ys

    world = make_rrt_world(difficulty)
    rng = random.Random(23)
    xs: list[list[float]] = []
    ys: list[list[float]] = []
    gate_x, gate_y = world.gate
    goal_x, goal_y = world.goal

    for _ in range(n):
        x = rng.uniform(20, world.width - 20)
        y = rng.uniform(20, world.height - 20)
        target = (gate_x, gate_y) if x < gate_x + 20 else (goal_x, goal_y)
        dx = target[0] - x
        dy = target[1] - y
        norm = max(1e-6, math.hypot(dx, dy))
        xs.append([
            x / world.width,
            y / world.height,
            goal_x / world.width,
            goal_y / world.height,
            gate_x / world.width,
            gate_y / world.height,
            (gate_x - x) / world.width,
            (gate_y - y) / world.height,
        ])
        ys.append([dx / norm, dy / norm])
    return xs, ys


def make_car_dataset(difficulty: str) -> tuple[list[list[float]], list[list[float]]]:
    if difficulty == "all":
        xs: list[list[float]] = []
        ys: list[list[float]] = []
        for diff in DIFFICULTIES:
            part_xs, part_ys = make_car_dataset(diff)
            xs.extend(part_xs)
            ys.extend(part_ys)
        return xs, ys

    grid, w, h, start, goal, corridor_width = make_car_map(difficulty)
    path, _ = astar(grid, w, h, start, goal)
    xs: list[list[float]] = []
    ys: list[list[float]] = []
    heading = 0.0

    for cur, nxt in zip(path, path[1:]):
        desired = math.atan2(nxt[1] - cur[1], nxt[0] - cur[0])
        heading_error = math.atan2(math.sin(desired - heading), math.cos(desired - heading))
        steer = max(-1.0, min(1.0, heading_error * 1.8))
        throttle = 0.7 - min(0.35, abs(steer) * 0.25)
        xs.append([
            cur[0] / w,
            cur[1] / h,
            goal[0] / w,
            goal[1] / h,
            math.cos(heading),
            math.sin(heading),
            (goal[0] - cur[0]) / w,
            (goal[1] - cur[1]) / h,
            corridor_width / 8,
        ])
        ys.append([steer, throttle])
        heading += steer * 0.25
    return xs, ys


def train_classifier(task: str, difficulty: str, epochs: int) -> dict:
    xs, ys = make_grid_dataset(task, difficulty)
    if not xs:
        raise RuntimeError(f"no training samples for {task}")
    spec = ModelSpec(task=task, input_dim=11, output_dim=5, hidden_dim=96, continuous=False)
    model = MLPPolicy(spec.input_dim, spec.output_dim, spec.hidden_dim, spec.continuous)
    optimizer = torch.optim.Adam(model.parameters(), lr=2e-3)
    loss_fn = nn.CrossEntropyLoss()
    x_tensor = torch.tensor(xs, dtype=torch.float32)
    y_tensor = torch.tensor(ys, dtype=torch.long)
    losses: list[float] = []

    for _ in range(epochs):
        logits = model(x_tensor)
        loss = loss_fn(logits, y_tensor)
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        losses.append(float(loss.detach().cpu()))

    with torch.no_grad():
        pred = model(x_tensor).argmax(dim=1)
        accuracy = float((pred == y_tensor).float().mean().cpu())

    checkpoint = CHECKPOINT_DIR / f"{task}_ai.pt"
    save_checkpoint(checkpoint, spec, model, {"accuracy": accuracy, "loss": losses[-1], "epochs": epochs})
    return {"checkpoint": str(checkpoint.relative_to(ROOT)), "losses": losses, "accuracy": accuracy, "samples": len(xs)}


def train_regressor(task: str, difficulty: str, epochs: int) -> dict:
    if task == "rrt_narrow":
        xs, ys = make_rrt_dataset(difficulty)
        spec = ModelSpec(task=task, input_dim=8, output_dim=2, hidden_dim=96, continuous=True)
    elif task == "car_control":
        xs, ys = make_car_dataset(difficulty)
        spec = ModelSpec(task=task, input_dim=9, output_dim=2, hidden_dim=96, continuous=True)
    else:
        raise ValueError(f"unknown regression task: {task}")
    if not xs:
        raise RuntimeError(f"no training samples for {task}")

    model = MLPPolicy(spec.input_dim, spec.output_dim, spec.hidden_dim, spec.continuous)
    optimizer = torch.optim.Adam(model.parameters(), lr=1.5e-3)
    loss_fn = nn.MSELoss()
    x_tensor = torch.tensor(xs, dtype=torch.float32)
    y_tensor = torch.tensor(ys, dtype=torch.float32)
    losses: list[float] = []

    for _ in range(epochs):
        pred = model(x_tensor)
        loss = loss_fn(pred, y_tensor)
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        losses.append(float(loss.detach().cpu()))

    checkpoint = CHECKPOINT_DIR / f"{task}_ai.pt"
    save_checkpoint(checkpoint, spec, model, {"loss": losses[-1], "epochs": epochs})
    return {"checkpoint": str(checkpoint.relative_to(ROOT)), "losses": losses, "samples": len(xs)}


def train_task(task: str, difficulty: str = "all", epochs: int = 220) -> dict:
    torch.manual_seed(7)
    random.seed(7)
    if task in {"dijkstra", "astar_dynamic"}:
        result = train_classifier(task, difficulty, epochs)
    elif task in {"rrt_narrow", "car_control"}:
        result = train_regressor(task, difficulty, epochs)
    else:
        raise ValueError(f"unknown task: {task}")
    result["task"] = task
    result["difficulty"] = difficulty
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True, choices=["dijkstra", "astar_dynamic", "rrt_narrow", "car_control"])
    parser.add_argument("--difficulty", default="all", choices=["normal", "hard", "extreme", "all"])
    parser.add_argument("--epochs", type=int, default=220)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = train_task(args.task, args.difficulty, args.epochs)
    print(result)


if __name__ == "__main__":
    main()
