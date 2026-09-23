# -*- coding: utf-8 -*-
"""
复现脚本:运行不依赖 LLM API 的经典规划器 (A* / RRT / MCTS / Q-learning),
将输出图保存到 results/repro/,并在同目录生成 log.txt 记录运行数据。

用法 (conda 环境 uav_ppo):
    python run_repro.py            # 运行全部
    python run_repro.py astar rrt  # 只运行指定规划器
"""
import sys
import os
import time
import traceback
import random

import matplotlib
matplotlib.use("Agg")  # 无界面后端,避免交互阻塞
import matplotlib.pyplot as plt
import numpy as np
import runpy

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("AIFP_OUT_DIR", os.path.join(ROOT, "results", "repro"))

SEED = int(os.environ.get("AIFP_SEED", "42"))
random.seed(SEED)
np.random.seed(SEED)

# The original planners call plt.pause() for live animation. In headless
# reproduction this can make A* take minutes, so animation is opt-in.
if os.environ.get("AIFP_INTERACTIVE", "0") != "1":
    plt.pause = lambda *_args, **_kwargs: None

PLANNERS = {
    "astar": "src/astar_planner.py",
    "rrt": "src/rrt_planner.py",
    "mcts": "src/mcts_planner.py",
    "q_learning": "src/q_learning_planner.py",
}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    selected = sys.argv[1:] or list(PLANNERS)
    log_lines = []
    for name in selected:
        script = os.path.join(ROOT, PLANNERS[name])
        t0 = time.time()
        try:
            plt.close("all")
            runpy.run_path(script, run_name="__main__")
            fig_path = os.path.join(OUT_DIR, f"{name}_planner.png")
            plt.savefig(fig_path, dpi=150, bbox_inches="tight")
            msg = f"[{name}] OK, 耗时 {time.time() - t0:.1f}s, 输出 {fig_path}"
            print(msg)
            log_lines.append(msg)
        except Exception:
            err = traceback.format_exc()
            msg = f"[{name}] FAILED, 耗时 {time.time() - t0:.1f}s\n{err}"
            print(msg)
            log_lines.append(msg)

    log_path = os.path.join(OUT_DIR, "log.txt")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    print(f"日志已写入 {log_path}")


if __name__ == "__main__":
    main()
