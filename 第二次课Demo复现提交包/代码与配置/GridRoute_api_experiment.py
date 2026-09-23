from __future__ import annotations

import argparse
import ast
import os
import re
import time
from pathlib import Path

import pandas as pd
from openai import OpenAI

from prompt import (
    Dijkstra_algorithm_prompt,
    Few_shot_learning_prompt,
    Vanilla_prompt,
    algorithm_integration_CoT,
    independent_path_CoT,
)
from src.repro_utils import PROJECT_ROOT, load_table


PROMPT_TEMPLATES = {
    "vanilla": Vanilla_prompt,
    "independent": independent_path_CoT,
    "few_shot": Few_shot_learning_prompt,
    "algorithm": algorithm_integration_CoT,
    "dijkstra": Dijkstra_algorithm_prompt,
}


def obstacle_prompt(obstacles: object) -> str:
    rectangles = ast.literal_eval(obstacles) if isinstance(obstacles, str) else obstacles
    return "[" + ", ".join(
        f"(({item['top_left'][0]}, {item['top_left'][1]}), "
        f"({item['bottom_right'][0]}, {item['bottom_right'][1]}))"
        for item in rectangles
    ) + "]"


def build_prompt(
    obstacles: str,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    template: str,
) -> str:
    try:
        selected = PROMPT_TEMPLATES[template]
    except KeyError as exc:
        raise ValueError(f"Unknown template {template!r}; choose from {sorted(PROMPT_TEMPLATES)}") from exc
    return selected.format(
        prompt=obstacles,
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
    )


def extract_path_text(text: str) -> str:
    text = text or ""
    tuple_candidates = re.findall(
        r"\[(?:\s*\(\s*-?\d+\s*,\s*-?\d+\s*\)\s*,?)+\]",
        text,
    )
    list_candidates = re.findall(
        r"\[(?:\s*\[\s*-?\d+\s*,\s*-?\d+\s*\]\s*,?)+\]",
        text,
    )
    candidates = tuple_candidates + list_candidates
    return max(candidates, key=len) if candidates else "[]"


def make_client() -> tuple[OpenAI, str]:
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    api_key = openrouter_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing OPENROUTER_API_KEY (or OPENAI_API_KEY/DEEPSEEK_API_KEY). "
            "The data/reference/evaluation stages do not require an API key."
        )

    base_url = os.environ.get("OPENAI_BASE_URL")
    if openrouter_key:
        base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    elif not base_url:
        if os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
            base_url = "https://api.deepseek.com"
        else:
            base_url = "https://api.openai.com/v1"

    kwargs = {
        "api_key": api_key,
        "timeout": float(os.environ.get("OPENROUTER_TIMEOUT", "45")),
    }
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    if openrouter_key:
        model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")
    else:
        model = os.environ.get("OPENAI_MODEL") or (
            "deepseek-chat" if os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENAI_API_KEY")
            else "gpt-4o-mini"
        )
    return client, model


def planning(
    client: OpenAI,
    model: str,
    prompt_text: str,
    map_info: str,
) -> str:
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt_text}],
                max_tokens=int(os.environ.get("OPENROUTER_MAX_TOKENS", "512")),
                temperature=0.1,
            )
            reply = response.choices[0].message.content or ""
            path_text = extract_path_text(reply)
            if path_text != "[]":
                print(f"{map_info} attempt {attempt + 1}: {path_text}")
                return path_text
        except Exception as exc:
            print(f"{map_info} attempt {attempt + 1} failed: {exc}")
    return "[]"


def run_experiment(
    model_name: str,
    template: str,
    dataset_path: str | Path = PROJECT_ROOT / "data" / "dataset.csv",
    output_path: str | Path | None = None,
    limit: int | None = None,
) -> Path:
    client, configured_model = make_client()
    model_name = model_name or configured_model
    maps_df = load_table(dataset_path)
    if limit is not None:
        maps_df = maps_df.head(limit)

    rows = []
    for idx, row in maps_df.iterrows():
        matrix_size = int(row["matrix_size"])
        start_x, start_y = int(row["start_x"]), int(row["start_y"])
        end_x, end_y = int(row["end_x"]), int(row["end_y"])
        obstacles = obstacle_prompt(row["obstacles"])
        prompt_text = build_prompt(
            obstacles,
            start_x,
            start_y,
            end_x,
            end_y,
            template,
        )
        map_info = f"Map {idx + 1} ({matrix_size}x{matrix_size})"
        started = time.perf_counter()
        path_text = planning(client, model_name, prompt_text, map_info)
        run_time = time.perf_counter() - started
        rows.append(
            {
                "Matrix_Size": matrix_size,
                "Start_X": start_x,
                "Start_Y": start_y,
                "End_X": end_x,
                "End_Y": end_y,
                "Obstacles": obstacles,
                "Path": path_text,
                "Run_Time": run_time,
            }
        )

    if output_path is None:
        output_path = PROJECT_ROOT / "results" / "output_dir" / f"{model_name}_{template}.csv"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"Saved {len(rows)} planned paths: {output_path}")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GridRoute LLM path-planning experiments.")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", ""))
    parser.add_argument("--template", choices=sorted(PROMPT_TEMPLATES), default="independent")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "data" / "dataset.csv")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    run_experiment(args.model, args.template, args.input, args.output, args.limit)


if __name__ == "__main__":
    main()
