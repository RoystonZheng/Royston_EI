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
TRAINING_TECHNIQUES = [
    "Advantage Normalization",
    "State Normalization",
    "Reward Normalization",
    "Reward Scaling",
    "Policy Entropy",
    "Learning Rate Decay",
    "Gradient Clip",
    "Orthogonal Initialization",
    "Adam Optimizer Epsilon Parameter",
    "Tanh Activation Function",
]
TRICK_CONFIG = {
    "reward_scale": 0.6,
    "advantage_weight": 0.35,
    "entropy_coef_classifier": 0.006,
    "entropy_coef_regressor": 0.002,
    "max_grad_norm": 1.0,
    "adam_eps": 1e-5,
}


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


def tensor_normalize(values: torch.Tensor, eps: float = 1e-8) -> tuple[torch.Tensor, dict]:
    mean = values.mean(dim=0, keepdim=True)
    std = values.std(dim=0, keepdim=True).clamp_min(eps)
    normalized = (values - mean) / std
    return normalized, {
        "mean": mean.squeeze(0).detach().cpu().tolist(),
        "std": std.squeeze(0).detach().cpu().tolist(),
        "eps": eps,
    }


def normalize_vector(values: torch.Tensor, eps: float = 1e-8) -> torch.Tensor:
    return (values - values.mean()) / values.std().clamp_min(eps)


def grid_sample_rewards(task: str, xs: list[list[float]], ys: list[int]) -> list[float]:
    rewards: list[float] = []
    for features, action in zip(xs, ys):
        distance_score = 1.0 - features[6]
        open_space = sum(features[7:11]) / 4
        move_bonus = 0.12 if action != 0 else 0.0
        wait_bonus = 0.22 if task == "astar_dynamic" and action == 0 else 0.0
        rewards.append(0.55 + distance_score * 0.7 + open_space * 0.25 + move_bonus + wait_bonus)
    return rewards


def regression_sample_rewards(task: str, xs: list[list[float]], ys: list[list[float]]) -> list[float]:
    rewards: list[float] = []
    if task == "rrt_narrow":
        for features in xs:
            gate_dist = math.hypot(features[6], features[7])
            gate_focus = 1.0 / (1.0 + gate_dist * 6.0)
            rewards.append(0.7 + gate_focus)
    elif task == "car_control":
        for target in ys:
            steer, throttle = target
            smooth_turn = 1.0 - min(1.0, abs(steer))
            rewards.append(0.45 + smooth_turn * 0.45 + throttle * 0.35)
    else:
        rewards = [1.0 for _ in ys]
    return rewards


def advantage_weights(raw_rewards: list[float]) -> tuple[torch.Tensor, dict]:
    rewards = torch.tensor(raw_rewards, dtype=torch.float32)
    scaled_rewards = rewards * TRICK_CONFIG["reward_scale"]
    normalized_rewards = normalize_vector(scaled_rewards)
    advantages = normalize_vector(normalized_rewards)
    weights = 1.0 + TRICK_CONFIG["advantage_weight"] * advantages
    weights = weights.clamp(0.25, 2.5)
    return weights, {
        "raw_reward_mean": float(rewards.mean()),
        "raw_reward_std": float(rewards.std()),
        "reward_scale": TRICK_CONFIG["reward_scale"],
        "advantage_mean": float(advantages.mean()),
        "advantage_std": float(advantages.std()),
        "weight_min": float(weights.min()),
        "weight_max": float(weights.max()),
    }


def training_metadata(normalizer: dict, reward_stats: dict) -> dict:
    return {
        "techniques": TRAINING_TECHNIQUES,
        "config": TRICK_CONFIG,
        "state_normalizer": normalizer,
        "reward_and_advantage": reward_stats,
    }


def train_classifier(task: str, difficulty: str, epochs: int) -> dict:
    xs, ys = make_grid_dataset(task, difficulty)
    if not xs:
        raise RuntimeError(f"no training samples for {task}")
    spec = ModelSpec(task=task, input_dim=11, output_dim=5, hidden_dim=96, continuous=False)
    model = MLPPolicy(spec.input_dim, spec.output_dim, spec.hidden_dim, spec.continuous, orthogonal=True)
    optimizer = torch.optim.Adam(model.parameters(), lr=2e-3, eps=TRICK_CONFIG["adam_eps"])
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs, eta_min=2e-4)
    loss_fn = nn.CrossEntropyLoss(reduction="none")
    x_tensor_raw = torch.tensor(xs, dtype=torch.float32)
    x_tensor, normalizer = tensor_normalize(x_tensor_raw)
    y_tensor = torch.tensor(ys, dtype=torch.long)
    weights, reward_stats = advantage_weights(grid_sample_rewards(task, xs, ys))
    losses: list[float] = []
    supervised_losses: list[float] = []
    entropies: list[float] = []
    learning_rates: list[float] = []

    for _ in range(epochs):
        logits = model(x_tensor)
        per_sample_loss = loss_fn(logits, y_tensor)
        entropy = model.distribution(x_tensor).entropy().mean()
        supervised_loss = (per_sample_loss * weights).mean()
        loss = supervised_loss - TRICK_CONFIG["entropy_coef_classifier"] * entropy
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=TRICK_CONFIG["max_grad_norm"])
        optimizer.step()
        scheduler.step()
        losses.append(float(loss.detach().cpu()))
        supervised_losses.append(float(supervised_loss.detach().cpu()))
        entropies.append(float(entropy.detach().cpu()))
        learning_rates.append(float(scheduler.get_last_lr()[0]))

    with torch.no_grad():
        pred = model(x_tensor).argmax(dim=1)
        accuracy = float((pred == y_tensor).float().mean().cpu())

    checkpoint = CHECKPOINT_DIR / f"{task}_ai.pt"
    metadata = training_metadata(normalizer, reward_stats)
    metrics = {
        "accuracy": accuracy,
        "loss": losses[-1],
        "supervised_loss": supervised_losses[-1],
        "entropy": entropies[-1],
        "final_lr": learning_rates[-1],
        "epochs": epochs,
        **metadata,
    }
    save_checkpoint(checkpoint, spec, model, metrics, normalizer=normalizer, techniques=TRAINING_TECHNIQUES)
    return {
        "checkpoint": str(checkpoint.relative_to(ROOT)),
        "losses": losses,
        "supervised_losses": supervised_losses,
        "entropies": entropies,
        "learning_rates": learning_rates,
        "accuracy": accuracy,
        "samples": len(xs),
        **metadata,
    }


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

    model = MLPPolicy(spec.input_dim, spec.output_dim, spec.hidden_dim, spec.continuous, orthogonal=True)
    optimizer = torch.optim.Adam(model.parameters(), lr=1.5e-3, eps=TRICK_CONFIG["adam_eps"])
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs, eta_min=1.5e-4)
    x_tensor_raw = torch.tensor(xs, dtype=torch.float32)
    x_tensor, normalizer = tensor_normalize(x_tensor_raw)
    y_tensor = torch.tensor(ys, dtype=torch.float32)
    weights, reward_stats = advantage_weights(regression_sample_rewards(task, xs, ys))
    losses: list[float] = []
    supervised_losses: list[float] = []
    entropies: list[float] = []
    learning_rates: list[float] = []

    for _ in range(epochs):
        pred = model(x_tensor)
        dist = model.distribution(x_tensor)
        per_sample_loss = -dist.log_prob(y_tensor).sum(dim=1)
        entropy = dist.entropy().sum(dim=1).mean()
        supervised_loss = (per_sample_loss * weights).mean()
        loss = supervised_loss - TRICK_CONFIG["entropy_coef_regressor"] * entropy
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=TRICK_CONFIG["max_grad_norm"])
        optimizer.step()
        scheduler.step()
        losses.append(float(loss.detach().cpu()))
        supervised_losses.append(float(supervised_loss.detach().cpu()))
        entropies.append(float(entropy.detach().cpu()))
        learning_rates.append(float(scheduler.get_last_lr()[0]))

    checkpoint = CHECKPOINT_DIR / f"{task}_ai.pt"
    with torch.no_grad():
        mse = float(nn.functional.mse_loss(model(x_tensor), y_tensor).detach().cpu())
    metadata = training_metadata(normalizer, reward_stats)
    metrics = {
        "loss": losses[-1],
        "supervised_loss": supervised_losses[-1],
        "entropy": entropies[-1],
        "mse": mse,
        "final_lr": learning_rates[-1],
        "epochs": epochs,
        **metadata,
    }
    save_checkpoint(checkpoint, spec, model, metrics, normalizer=normalizer, techniques=TRAINING_TECHNIQUES)
    return {
        "checkpoint": str(checkpoint.relative_to(ROOT)),
        "losses": losses,
        "supervised_losses": supervised_losses,
        "entropies": entropies,
        "learning_rates": learning_rates,
        "mse": mse,
        "samples": len(xs),
        **metadata,
    }


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


def brief_result(result: dict) -> dict:
    brief = {
        "task": result.get("task"),
        "difficulty": result.get("difficulty"),
        "checkpoint": result.get("checkpoint"),
        "samples": result.get("samples"),
        "final_loss": result.get("losses", [None])[-1],
        "techniques": TRAINING_TECHNIQUES,
    }
    if "accuracy" in result:
        brief["accuracy"] = result["accuracy"]
    if "mse" in result:
        brief["mse"] = result["mse"]
    return brief


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True, choices=["dijkstra", "astar_dynamic", "rrt_narrow", "car_control"])
    parser.add_argument("--difficulty", default="all", choices=["normal", "hard", "extreme", "all"])
    parser.add_argument("--epochs", type=int, default=220)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = train_task(args.task, args.difficulty, args.epochs)
    print(brief_result(result))


if __name__ == "__main__":
    main()
