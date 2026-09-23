# -*- coding: utf-8 -*-
"""
LLM 规划器测试脚本:使用 OpenRouter API 运行 AIFP 主流程与 naive 基线,
输出图与日志保存到 results/repro/ (追加至 log.txt)。

用法 (conda 环境 uav_ppo):
    python run_llm_planners.py            # 运行 AIFP + naive
    python run_llm_planners.py aifp       # 只运行 AIFP
    python run_llm_planners.py naive      # 只运行 naive
"""
import sys
import os
import re
import json
import time
import traceback

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from shapely.geometry import LineString, Polygon
from openai import OpenAI

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "results", "repro")
LOG_PATH = os.path.join(OUT_DIR, "log.txt")

# ---------- OpenRouter 客户端 ----------
# Never keep a live credential in source. Export OPENROUTER_API_KEY before use.
openrouter_api_key = os.environ.get("OPENROUTER_API_KEY")
openai_api_key = os.environ.get("OPENAI_API_KEY")
api_key = openrouter_api_key or openai_api_key
if openrouter_api_key:
    api_base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    BASE_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")
else:
    api_base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    BASE_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
client = None


def get_client():
    global client
    if client is None:
        if not api_key:
            raise RuntimeError(
                "缺少 OPENROUTER_API_KEY。经典规划器无需 API；运行 LLM 规划前请先设置环境变量。"
            )
        kwargs = {
            "api_key": api_key,
            "base_url": api_base_url,
            "timeout": float(os.environ.get("OPENROUTER_TIMEOUT", "45")),
        }
        client = OpenAI(**kwargs)
    return client


def LLM(messages, model=BASE_MODEL, stop=None, max_tokens=256, temperature=0.0):
    api_client = get_client()
    responses = api_client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens,
        temperature=temperature, stop=stop,
    )
    message = responses.choices[0].message
    content = message.content
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
            else:
                text = getattr(part, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
        if text_parts:
            return "\n".join(text_parts)

    # Some OpenRouter reasoning models put the generated text in `reasoning`
    # while leaving the standard `content` field empty.
    reasoning = getattr(message, "reasoning", None)
    if isinstance(reasoning, str):
        return reasoning
    return ""


# ---------- 公共环境 (与 notebook cell 3 一致) ----------
OBSTACLES = [
    Polygon([[0.0, -0.15], [0.5, 0.3], [0.3, 0.8], [-0.2, 0.4]]),
    Polygon([[-1.0, 0.0], [-0.6, 0.4], [-1.2, 0.7], [-1.4, 0.3]]),
]


def is_line_disjoint(line_start, line_end, obstacles):
    line = LineString([line_start, line_end])
    for obstacle in obstacles:
        if line.intersects(obstacle):
            return False
    return True


def plot_path(path_coordinates, goal, title, fig_path, obstacles=OBSTACLES):
    path_array = np.array(path_coordinates)
    plt.figure(figsize=(6, 6))
    plt.plot(path_array[:, 0], path_array[:, 1], marker="o", linestyle="-",
             color="tab:blue", linewidth=2)
    plt.scatter(path_array[0, 0], path_array[0, 1], color="green", s=100, label="Start")
    plt.scatter(goal[0], goal[1], color="red", s=100, label="Goal")
    for i, obstacle in enumerate(OBSTACLES):
        x, y = obstacle.exterior.xy
        plt.fill(x, y, alpha=0.5, label=f"Obstacle {i + 1}")
    plt.xlabel("X Coordinate")
    plt.ylabel("Y Coordinate")
    plt.title(title)
    plt.legend()
    plt.grid()
    plt.savefig(fig_path, dpi=150, bbox_inches="tight")
    plt.close()


# ---------- AIFP 主流程 (对应 aifp_planner.ipynb cell 3) ----------
def generate_prompt(current_x, current_y, goal_x, goal_y, obstacles, replanning_mode=False):
    obstacles_description = "\n".join(
        f"- Obstacle {i + 1}: Vertices {list(obstacle.exterior.coords)[:-1]}"
        for i, obstacle in enumerate(obstacles)
    )
    exploration_hint = (
        "\n\nReplanning Exploration Hints:\n"
        "- Your previous proposed positions were REJECTED: the straight line segment from the "
        "current position to each of them intersects an obstacle.\n"
        "- You may be trapped in a pocket near an obstacle. To escape, the FIRST waypoint must "
        "move AWAY from the obstacle (e.g., downward or rightward, even if it temporarily "
        "increases distance to the goal), then navigate around the obstacle's corner.\n"
        "- Consider curved segments or deviations from the straight path.\n"
        "- Use step-by-step reasoning to identify feasible exploratory paths."
        if replanning_mode else ""
    )
    return [
        {
            "role": "user",
            "content": (
                f"You are a reasoning agent tasked with planning a correct path for a robot.\n\n"
                f"Environment:\n"
                f"- Initial position (current): [{current_x}, {current_y}]\n"
                f"- Goal position: [{goal_x}, {goal_y}]\n"
                f"- Obstacles:\n{obstacles_description}\n\n"
                "Instructions:\n"
                "1. Plan the next 5 positions (m = 5) toward the goal.\n"
                "2. Ensure the straight line between each consecutive position is disjoint from all obstacles.\n"
                "3. If replanning, include exploration by considering curves or alternative directions.\n"
                "4. Provide step-by-step reasoning in your planning to ensure constraints are met."
                "5. Coordinates (Xi, Yi) for each position must have a precision greater than 0.1 (e.g., 0.05 or 0.01)."
                "6. If planning stuks in feedback loop, use smaller and finer points"
                f"{exploration_hint}\n\n"
                "Output format:\n"
                "{\n"
                "  \"Positions\": [\n"
                "    [X1, Y1],\n"
                "    [X2, Y2],\n"
                "    [X3, Y3],\n"
                "    [X4, Y4],\n"
                "    [X5, Y5]\n"
                "  ]\n"
                "}\n\n"
                "Important:\n"
                "- Only provide the JSON object with the list of positions.\n"
                "- Do not include explanations, repeated context, or extra formatting."
            )
        }
    ]


def run_aifp(trial=1, model=BASE_MODEL):
    t0 = time.time()
    current_x, current_y = 1, -0.5   # 初始位置 (I)
    goal_x, goal_y = -1.5, 1.2       # 目标位置 (G)
    goal = (goal_x, goal_y)

    path_coordinates = [(current_x, current_y)]
    iteration_count = 0
    max_iterations = int(os.environ.get("AIFP_MAX_ITERATIONS", "100"))
    m, q = 5, 2  # 预测 m 个位置, 每次执行 q 个
    replanning_mode = False
    parse_errors = 0
    replan_count = 0
    llm_calls = 0

    while (abs(current_x - goal_x) > 0.1 or abs(current_y - goal_y) > 0.1) and iteration_count < max_iterations:
        iteration_count += 1
        prompt = generate_prompt(current_x, current_y, goal_x, goal_y, OBSTACLES, replanning_mode)
        # 常规规划 temperature=0;重规划模式下提高温度引入多样性,
        # 避免 temperature=0 时模型重复相同提案导致死循环 (原代码注释标注作者默认 0.9)
        temperature = 0.7 if replanning_mode else 0.0
        positions_text = LLM(prompt, model=model, temperature=temperature)
        llm_calls += 1
        json_match = re.search(r"\{[\s\S]*\}", positions_text)
        if json_match:
            positions_text = json_match.group(0)

        try:
            positions = json.loads(positions_text)["Positions"]
            print(f"\nIteration {iteration_count} ({'Replanning' if replanning_mode else 'Planning'} Mode): Predicted Positions:")
            for idx, (pos_x, pos_y) in enumerate(positions):
                print(f"  Position {idx + 1}: [{pos_x}, {pos_y}]")

            executed_positions = []
            for pos_x, pos_y in positions:
                # 排除原地不动提案:贴边时否则会无限循环而不触发重规划
                if (pos_x, pos_y) != (current_x, current_y) and is_line_disjoint(
                    (current_x, current_y), (pos_x, pos_y), OBSTACLES
                ):
                    executed_positions.append((pos_x, pos_y))

            if not executed_positions:
                print(f"No valid positions found in iteration {iteration_count}. Entering replanning mode...")
                replanning_mode = True
                replan_count += 1
                continue

            print(f"  Executing {min(q, len(executed_positions))} valid positions:")
            for i in range(min(q, len(executed_positions))):
                current_x, current_y = executed_positions[i]
                path_coordinates.append((current_x, current_y))
                print(f"    Applied Position {i + 1}: [{current_x}, {current_y}]")

            replanning_mode = False
        except (json.JSONDecodeError, KeyError):
            print("Error parsing positions from LLM response.")
            parse_errors += 1
            continue

    reached = abs(current_x - goal_x) <= 0.1 and abs(current_y - goal_y) <= 0.1
    dist_final = float(np.linalg.norm(np.array([current_x, current_y]) - np.array(goal)))

    # 全路径碰撞校验
    collision_free = all(
        is_line_disjoint(path_coordinates[i], path_coordinates[i + 1], OBSTACLES)
        for i in range(len(path_coordinates) - 1)
    )

    trial_dir = os.path.join(OUT_DIR, "aifp_trials")
    os.makedirs(trial_dir, exist_ok=True)
    model_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
    fig_path = os.path.join(trial_dir, f"{model_tag}_trial_{trial}.png")
    plot_path(path_coordinates, goal, f"AIFP Trial {trial} ({model}, {'success' if reached else 'fail'})", fig_path)

    msg = (
        f"[aifp/{model} trial {trial}] {'OK - 到达目标' if reached else '未达目标(达到最大迭代)'}\n"
        f"  迭代次数={iteration_count}, LLM调用={llm_calls}, 重规划次数={replan_count}, JSON解析失败={parse_errors}\n"
        f"  路径点数={len(path_coordinates)}, 终点距目标={dist_final:.3f}, 全路径无碰撞={collision_free}\n"
        f"  耗时 {time.time() - t0:.1f}s, 输出 {fig_path}"
    )
    return reached, msg


# ---------- Naive 基线 (对应 naive_llm_planner.ipynb cell 2) ----------
NAIVE_PROMPT = [
    {
        "role": "user",
        "content": (
            "I need your assistance in planning a path based on specific instructions.\n\n"
            "Environment details:\n"
            "- Object 1: [name: red box, shape: square, color: red, center position: [-1.5, 1.2]]\n"
            "- Object 2: [name: yellow box, shape: square, color: yellow, center position: [1.0, -0.5]]\n"
            "- Object 3: [name: green box, shape: square, color: green, center position: [-1, 0]]\n"
            "- Obstacle: [name: blue area, shape: square, color: blue, square with corners at "
            "[[-0.5, 0.0], [0.5, 0.0], [-0.5, -1.0], [0.5, -1.0]]]\n\n"
            "**The blue area is a restricted zone and must be avoided completely during the path. "
            "No point in the path can intersect or enter this area.**\n\n"
            "Instructions:\n"
            "- Start at the yellow box, then navigate to the green box, and finally end at the red box.\n"
            "- Avoid entering the blue area (obstacle) at any point during the path.\n\n"
            "Output Format:\n"
            "Please generate the trajectory as a list of coordinates in the format [x, y], "
            "structured as a JSON object like this:\n"
            "{\n"
            "    \"Trajectory\": [\n"
            "        [x1, y1],\n"
            "        [x2, y2],\n"
            "        ...\n"
            "    ]\n"
            "}\n\n"
            "Additional Notes:\n"
            "- Ensure the JSON is properly formatted with correct syntax.\n"
            "- The path must consider the entire area of the blue square-shaped obstacle, avoiding it completely."
        )
    }
]


def run_naive():
    t0 = time.time()
    goal = (-1.5, 1.2)  # 终点为红盒
    text = LLM(NAIVE_PROMPT, max_tokens=2048)
    json_match = re.search(r"\{[\s\S]*\}", text)
    if not json_match:
        return f"[naive/{BASE_MODEL}] FAILED: 响应中未找到 JSON, 耗时 {time.time() - t0:.1f}s"
    try:
        trajectory = json.loads(json_match.group(0))["Trajectory"]
    except (json.JSONDecodeError, KeyError) as e:
        return f"[naive/{BASE_MODEL}] FAILED: JSON 解析失败 ({e}), 耗时 {time.time() - t0:.1f}s"

    print(f"Naive trajectory ({len(trajectory)} points):")
    for p in trajectory:
        print(f"  [{p[0]}, {p[1]}]")

    # 碰撞校验:相邻连线不得穿过 blue area 障碍 (naive 场景的障碍)
    blue_area = Polygon([(-0.5, 0.0), (0.5, 0.0), (0.5, -1.0), (-0.5, -1.0)])
    collision_free = all(
        not LineString([tuple(trajectory[i]), tuple(trajectory[i + 1])]).intersects(blue_area)
        for i in range(len(trajectory) - 1)
    )
    dist_final = float(np.linalg.norm(np.array(trajectory[-1]) - np.array(goal)))

    fig_path = os.path.join(OUT_DIR, "naive_llm_planner.png")
    plot_path(trajectory, goal, "Naive LLM Path Planning (OpenRouter)", fig_path, obstacles=[blue_area])

    msg = (
        f"[naive/{BASE_MODEL}] 单次生成完成\n"
        f"  轨迹点数={len(trajectory)}, 全路径无碰撞(避开blue area)={collision_free}, "
        f"终点距目标(红盒)={dist_final:.3f}\n"
        f"  耗时 {time.time() - t0:.1f}s, 输出 {fig_path}"
    )
    return msg


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    selected = sys.argv[1:] or ["aifp", "naive"]
    log_lines = []

    if not api_key:
        message = (
            "未运行 LLM 规划：未设置 OPENROUTER_API_KEY。"
            "请先设置环境变量，经典规划器请使用 run_repro.py。"
        )
        print(message)
        log_lines.append("[conn] SKIPPED: " + message)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(log_lines) + "\n")
        return 2

    # 连通性测试 (真实 key)
    try:
        hello = LLM([{"role": "user", "content": "hello world!"}])
        print(f"API 连通性测试通过, 模型 {BASE_MODEL} 回复: {hello[:50]}")
        log_lines.append(f"[conn] OpenRouter API 连通性测试通过 (model={BASE_MODEL})")
    except Exception:
        err = traceback.format_exc()
        print("API 连通性测试失败:\n" + err)
        log_lines.append(f"[conn] FAILED\n{err}")

    if "aifp" in selected:
        # 多试验统计成功率 (论文方法;重规划模式含碰撞反馈提示 + temperature=0.7)
        model = os.environ.get("AIFP_MODEL", BASE_MODEL)
        n_trials = int(os.environ.get("AIFP_TRIALS", "5"))
        successes = 0
        for trial in range(1, n_trials + 1):
            try:
                reached, msg = run_aifp(trial, model=model)
                successes += int(reached)
            except Exception:
                msg = f"[aifp/{model} trial {trial}] FAILED\n{traceback.format_exc()}"
            print("\n" + msg)
            log_lines.append(msg)
        stats = (
            f"[aifp 统计] model={model}, {n_trials} 次试验, 成功 {successes} 次, "
            f"成功率 {successes / n_trials * 100:.1f}% (论文 GPT-4 双障碍场景参考值 36.7%)"
        )
        print("\n" + stats)
        log_lines.append(stats)

    if "naive" in selected:
        try:
            msg = run_naive()
        except Exception:
            msg = f"[naive/{BASE_MODEL}] FAILED\n{traceback.format_exc()}"
        print("\n" + msg)
        log_lines.append(msg)

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write("\n" + "\n".join(log_lines) + "\n")
    print(f"\n日志已追加至 {LOG_PATH}")


if __name__ == "__main__":
    raise SystemExit(main())
