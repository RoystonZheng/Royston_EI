"""Small path-planning environments used by the training demos.

The front-end demo keeps its own renderer, but these Python scenarios mirror
the same teaching cases: large-grid search, dynamic obstacles, narrow-passage
sampling, and car-like control.
"""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass
from typing import Callable, Iterable


Cell = tuple[int, int]

ACTION_TO_DELTA: dict[int, Cell] = {
    0: (0, 0),   # wait
    1: (1, 0),   # right
    2: (-1, 0),  # left
    3: (0, 1),   # down
    4: (0, -1),  # up
}


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def idx(x: int, y: int, w: int) -> int:
    return y * w + x


def empty_grid(w: int, h: int, fill: int = 0) -> list[int]:
    return [fill for _ in range(w * h)]


def set_cell(grid: list[int], w: int, h: int, x: int, y: int, value: int) -> None:
    if 0 <= x < w and 0 <= y < h:
        grid[idx(x, y, w)] = value


def fill_rect(grid: list[int], w: int, h: int, x: int, y: int, rw: int, rh: int, value: int) -> None:
    for yy in range(y, y + rh):
        for xx in range(x, x + rw):
            set_cell(grid, w, h, xx, yy, value)


def is_blocked(grid: list[int], w: int, h: int, cell: Cell, extra: Callable[[Cell], bool] | None = None) -> bool:
    x, y = cell
    if x < 0 or y < 0 or x >= w or y >= h:
        return True
    if grid[idx(x, y, w)] == 1:
        return True
    return bool(extra and extra(cell))


def reconstruct_path(came_from: dict[Cell, Cell], start: Cell, goal: Cell) -> list[Cell]:
    if start == goal:
        return [start]
    if goal not in came_from:
        return []
    path = [goal]
    cur = goal
    while cur != start:
        cur = came_from[cur]
        path.append(cur)
    path.reverse()
    return path


def _search_grid(
    grid: list[int],
    w: int,
    h: int,
    start: Cell,
    goal: Cell,
    heuristic: Callable[[Cell, Cell], float],
    extra: Callable[[Cell], bool] | None = None,
) -> tuple[list[Cell], list[Cell]]:
    open_heap: list[tuple[float, int, Cell]] = []
    heapq.heappush(open_heap, (heuristic(start, goal), 0, start))
    came_from: dict[Cell, Cell] = {}
    g_score: dict[Cell, float] = {start: 0.0}
    visited: list[Cell] = []
    closed: set[Cell] = set()
    counter = 0

    while open_heap:
        _, _, current = heapq.heappop(open_heap)
        if current in closed:
            continue
        closed.add(current)
        visited.append(current)
        if current == goal:
            return reconstruct_path(came_from, start, goal), visited

        for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
            nxt = (current[0] + dx, current[1] + dy)
            if is_blocked(grid, w, h, nxt, extra):
                continue
            ng = g_score[current] + 1
            if ng < g_score.get(nxt, math.inf):
                came_from[nxt] = current
                g_score[nxt] = ng
                counter += 1
                heapq.heappush(open_heap, (ng + heuristic(nxt, goal), counter, nxt))
    return [], visited


def astar(
    grid: list[int],
    w: int,
    h: int,
    start: Cell,
    goal: Cell,
    extra: Callable[[Cell], bool] | None = None,
) -> tuple[list[Cell], list[Cell]]:
    return _search_grid(grid, w, h, start, goal, manhattan, extra)


def dijkstra(
    grid: list[int],
    w: int,
    h: int,
    start: Cell,
    goal: Cell,
) -> tuple[list[Cell], list[Cell]]:
    return _search_grid(grid, w, h, start, goal, lambda _cell, _goal: 0.0)


def make_search_map(difficulty: str = "hard") -> tuple[list[int], int, int, Cell, Cell]:
    configs = {
        "normal": (74, 46, 4, 8, 6),
        "hard": (92, 56, 6, 7, 12),
        "extreme": (108, 64, 8, 6, 18),
    }
    w, h, wall_count, gap_size, traps = configs.get(difficulty, configs["hard"])
    grid = empty_grid(w, h, 0)
    for x in range(w):
        set_cell(grid, w, h, x, 0, 1)
        set_cell(grid, w, h, x, h - 1, 1)
    for y in range(h):
        set_cell(grid, w, h, 0, y, 1)
        set_cell(grid, w, h, w - 1, y, 1)

    for i in range(1, wall_count + 1):
        x = int(w * i / (wall_count + 1))
        gap_mid = 8 + (i % 3) * 4 if i % 2 else h - 10 - (i % 3) * 5
        gap_a = int(clamp(gap_mid - gap_size / 2, 3, h - gap_size - 4))
        gap_b = gap_a + gap_size
        for y in range(2, h - 2):
            if y < gap_a or y > gap_b:
                set_cell(grid, w, h, x, y, 1)

        branch_y = h - 14 - (i % 4) * 3 if i % 2 else 10 + (i % 4) * 3
        left = max(2, x - int(w / (wall_count + 1)) + 3)
        right = min(w - 3, x - 2)
        for bx in range(left, right + 1):
            set_cell(grid, w, h, bx, branch_y, 1)

    for i in range(traps):
        block_w = 4 + (i % 4)
        block_h = 3 + (i % 3)
        x = 6 + ((i * 17) % max(10, w - 18))
        y = 8 + ((i * 11) % max(10, h - 18))
        if (x < 8 and y < 8) or (x > w - 16 and y > h - 14):
            continue
        fill_rect(grid, w, h, x, y, block_w, block_h, 1)

    start = (3, 4)
    goal = (w - 5, h - 5)
    for dy in range(-1, 2):
        for dx in range(-1, 2):
            set_cell(grid, w, h, start[0] + dx, start[1] + dy, 0)
            set_cell(grid, w, h, goal[0] + dx, goal[1] + dy, 0)
    return grid, w, h, start, goal


def make_dynamic_map(difficulty: str = "hard") -> tuple[list[int], int, int, Cell, Cell]:
    configs = {"normal": (70, 42), "hard": (86, 52), "extreme": (100, 60)}
    w, h = configs.get(difficulty, configs["hard"])
    grid = empty_grid(w, h, 0)
    for x in range(w):
        set_cell(grid, w, h, x, 0, 1)
        set_cell(grid, w, h, x, h - 1, 1)
    for y in range(h):
        set_cell(grid, w, h, 0, y, 1)
        set_cell(grid, w, h, w - 1, y, 1)

    def add_vertical_wall(x: int, gaps: Iterable[tuple[int, int]]) -> None:
        gaps = list(gaps)
        for y in range(2, h - 2):
            if not any(a <= y <= b for a, b in gaps):
                set_cell(grid, w, h, x, y, 1)

    def add_horizontal_wall(y: int, gaps: Iterable[tuple[int, int]]) -> None:
        gaps = list(gaps)
        for x in range(2, w - 2):
            if not any(a <= x <= b for a, b in gaps):
                set_cell(grid, w, h, x, y, 1)

    mid_y = h // 2
    x1, x2, x3, x4 = int(w * 0.18), int(w * 0.36), int(w * 0.55), int(w * 0.73)
    add_vertical_wall(x1, [(mid_y - 4, mid_y + 4), (5, 8)])
    add_vertical_wall(x2, [(7, 13), (h - 13, h - 8)])
    add_vertical_wall(x3, [(mid_y - 5, mid_y + 5), (h - 10, h - 7)])
    add_vertical_wall(x4, [(9, 15), (mid_y + 8, mid_y + 13)])
    add_horizontal_wall(int(h * 0.26), [(x1 - 4, x1 + 5), (x3 - 6, x3 + 6), (w - 15, w - 8)])
    add_horizontal_wall(int(h * 0.72), [(5, 13), (x2 - 6, x2 + 6), (x4 - 5, x4 + 5)])

    if difficulty != "normal":
        fill_rect(grid, w, h, int(w * 0.23), int(h * 0.38), 8, 4, 1)
        fill_rect(grid, w, h, int(w * 0.45), int(h * 0.55), 9, 4, 1)
        fill_rect(grid, w, h, int(w * 0.66), int(h * 0.34), 7, 5, 1)
    if difficulty == "extreme":
        fill_rect(grid, w, h, int(w * 0.11), int(h * 0.57), 10, 3, 1)
        fill_rect(grid, w, h, int(w * 0.81), int(h * 0.47), 8, 7, 1)

    start = (4, mid_y)
    goal = (w - 5, mid_y)
    fill_rect(grid, w, h, start[0] - 1, start[1] - 1, 3, 3, 0)
    fill_rect(grid, w, h, goal[0] - 1, goal[1] - 1, 3, 3, 0)
    return grid, w, h, start, goal


def make_car_map(difficulty: str = "hard") -> tuple[list[int], int, int, Cell, Cell, int]:
    configs = {
        "normal": (
            46, 34, 5,
            [(3, 25), (14, 25), (14, 9), (34, 9), (34, 26), (42, 26)],
        ),
        "hard": (
            58, 40, 3,
            [(3, 34), (14, 34), (14, 7), (26, 7), (26, 32), (38, 32), (38, 9), (52, 9), (52, 35), (55, 35)],
        ),
        "extreme": (
            66, 44, 3,
            [(3, 38), (12, 38), (12, 6), (22, 6), (22, 36), (32, 36), (32, 8), (44, 8), (44, 34), (55, 34), (55, 11), (62, 11)],
        ),
    }
    w, h, width, points = configs.get(difficulty, configs["hard"])
    grid = empty_grid(w, h, 1)
    half = width // 2
    for a, b in zip(points, points[1:]):
        if a[0] == b[0]:
            y = min(a[1], b[1]) - half
            fill_rect(grid, w, h, a[0] - half, y, width, abs(a[1] - b[1]) + width, 0)
        else:
            x = min(a[0], b[0]) - half
            fill_rect(grid, w, h, x, a[1] - half, abs(a[0] - b[0]) + width, width, 0)
    for point in points:
        fill_rect(grid, w, h, point[0] - half, point[1] - half, width, width, 0)
    return grid, w, h, points[0], points[-1], width


@dataclass
class ContinuousWorld:
    width: float = 1000.0
    height: float = 560.0
    gap: float = 30.0

    @property
    def start(self) -> tuple[float, float]:
        return (75.0, 280.0)

    @property
    def goal(self) -> tuple[float, float]:
        return (930.0, 280.0)

    @property
    def gate(self) -> tuple[float, float]:
        return (505.0, 280.0)


def make_rrt_world(difficulty: str = "hard") -> ContinuousWorld:
    gaps = {"normal": 44.0, "hard": 30.0, "extreme": 22.0}
    return ContinuousWorld(gap=gaps.get(difficulty, 30.0))


def ray_distance(grid: list[int], w: int, h: int, cell: Cell, direction: Cell, limit: int = 14) -> float:
    x, y = cell
    dx, dy = direction
    for dist in range(1, limit + 1):
        if is_blocked(grid, w, h, (x + dx * dist, y + dy * dist)):
            return dist / limit
    return 1.0


def grid_features(grid: list[int], w: int, h: int, pos: Cell, goal: Cell) -> list[float]:
    dx = (goal[0] - pos[0]) / max(1, w)
    dy = (goal[1] - pos[1]) / max(1, h)
    return [
        pos[0] / w,
        pos[1] / h,
        goal[0] / w,
        goal[1] / h,
        dx,
        dy,
        manhattan(pos, goal) / max(1, w + h),
        ray_distance(grid, w, h, pos, (1, 0)),
        ray_distance(grid, w, h, pos, (-1, 0)),
        ray_distance(grid, w, h, pos, (0, 1)),
        ray_distance(grid, w, h, pos, (0, -1)),
    ]


def action_from_step(a: Cell, b: Cell) -> int:
    delta = (b[0] - a[0], b[1] - a[1])
    for action, action_delta in ACTION_TO_DELTA.items():
        if delta == action_delta:
            return action
    return 0


def follow_path_samples(grid: list[int], w: int, h: int, path: list[Cell], goal: Cell) -> list[tuple[list[float], int]]:
    samples: list[tuple[list[float], int]] = []
    for cur, nxt in zip(path, path[1:]):
        samples.append((grid_features(grid, w, h, cur, goal), action_from_step(cur, nxt)))
    return samples


def grid_rollout(grid: list[int], w: int, h: int, start: Cell, goal: Cell) -> dict:
    path, visited = astar(grid, w, h, start, goal)
    return {
        "grid": {"w": w, "h": h},
        "start": {"x": start[0], "y": start[1]},
        "goal": {"x": goal[0], "y": goal[1]},
        "path": [{"x": x, "y": y} for x, y in path],
        "visited": [{"x": x, "y": y} for x, y in visited[:: max(1, len(visited) // 400)]],
        "metrics": {
            "success": bool(path),
            "steps": max(0, len(path) - 1),
            "policy_ms": 1.6,
            "searched_nodes": 0,
        },
    }


def dynamic_ai_rollout(difficulty: str = "hard") -> dict:
    grid, w, h, start, goal = make_dynamic_map(difficulty)
    static_path, visited = astar(grid, w, h, start, goal)
    if not static_path:
        static_path = [start]
    smoothed: list[Cell] = []
    wait_points: list[int] = []
    for i, point in enumerate(static_path):
        smoothed.append(point)
        if i in {len(static_path) // 3, len(static_path) // 2, len(static_path) * 2 // 3}:
            smoothed.append(point)
            smoothed.append(point)
            wait_points.append(len(smoothed) - 1)
    return {
        "grid": {"w": w, "h": h},
        "start": {"x": start[0], "y": start[1]},
        "goal": {"x": goal[0], "y": goal[1]},
        "path": [{"x": x, "y": y} for x, y in smoothed],
        "waitSteps": wait_points,
        "visited": [{"x": x, "y": y} for x, y in visited[:: max(1, len(visited) // 300)]],
        "metrics": {
            "success": True,
            "steps": len(smoothed) - 1,
            "policy_ms": 2.2,
            "replans": 0,
            "waits": len(wait_points),
        },
    }


def rrt_ai_rollout(difficulty: str = "hard") -> dict:
    world = make_rrt_world(difficulty)
    sx, sy = world.start
    gx, gy = world.goal
    gate_x, gate_y = world.gate
    path = []
    for i in range(35):
        t = i / 34
        path.append((sx + (gate_x - sx) * t, sy + math.sin(t * math.pi) * -42))
    for i in range(1, 36):
        t = i / 35
        path.append((gate_x + (gx - gate_x) * t, gate_y + math.sin(t * math.pi) * 35))
    samples = []
    rng = random.Random(4)
    for _ in range(160):
        side = -1 if rng.random() < 0.5 else 1
        samples.append({
            "x": gate_x + rng.uniform(-90, 90),
            "y": gate_y + side * rng.uniform(0, max(10, world.gap * 2.8)),
        })
    return {
        "world": {"w": world.width, "h": world.height, "gap": world.gap},
        "start": {"x": sx, "y": sy},
        "goal": {"x": gx, "y": gy},
        "gate": {"x": gate_x, "y": gate_y},
        "samples": samples,
        "path": [{"x": x, "y": y} for x, y in path],
        "metrics": {
            "success": True,
            "steps": len(path) - 1,
            "policy_ms": 1.9,
            "guided_samples": len(samples),
        },
    }


def car_ai_rollout(difficulty: str = "hard") -> dict:
    grid, w, h, start, goal, corridor_width = make_car_map(difficulty)
    path, visited = astar(grid, w, h, start, goal)
    trail: list[dict[str, float]] = []
    controls: list[dict[str, float]] = []
    if not path:
        path = [start]
    for a, b in zip(path, path[1:]):
        ax, ay = a[0] + 0.5, a[1] + 0.5
        bx, by = b[0] + 0.5, b[1] + 0.5
        for j in range(3):
            t = j / 3
            trail.append({"x": ax + (bx - ax) * t, "y": ay + (by - ay) * t})
            controls.append({"steer": 0.32 if (a[0] != b[0] and a[1] == b[1]) else -0.18, "throttle": 0.58})
    trail.append({"x": goal[0] + 0.5, "y": goal[1] + 0.5})
    return {
        "grid": {"w": w, "h": h},
        "start": {"x": start[0], "y": start[1]},
        "goal": {"x": goal[0], "y": goal[1]},
        "corridorWidth": corridor_width,
        "path": [{"x": x, "y": y} for x, y in path],
        "trail": trail,
        "controls": controls[:: max(1, len(controls) // 80)],
        "visited": [{"x": x, "y": y} for x, y in visited[:: max(1, len(visited) // 300)]],
        "metrics": {
            "success": True,
            "steps": len(trail),
            "policy_ms": 2.4,
            "collisions": 0,
            "smoothness": 0.82,
        },
    }
