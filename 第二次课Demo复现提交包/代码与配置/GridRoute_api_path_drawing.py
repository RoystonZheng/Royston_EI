from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .repro_utils import PROJECT_ROOT, as_int, load_table, obstacle_coords_from_row, parse_path


def draw_paths(
    api_path: str | Path,
    dataset_path: str | Path = PROJECT_ROOT / "data" / "dataset.csv",
    output_dir: str | Path = PROJECT_ROOT / "results" / "api_planned_paths_figure",
    limit: int | None = None,
) -> Path:
    api_df = load_table(api_path)
    dataset_df = load_table(dataset_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if limit is not None:
        api_df = api_df.head(limit)

    written = 0
    for idx, row in api_df.iterrows():
        if idx >= len(dataset_df):
            break
        path = parse_path(row.get("Path"))
        if not path:
            continue

        dataset_row = dataset_df.iloc[idx]
        matrix_size = as_int(row.get("Matrix_Size", row.get("matrix_size")))
        obstacles = obstacle_coords_from_row(dataset_row)
        x_obstacles = [point[0] for point in obstacles]
        y_obstacles = [point[1] for point in obstacles]
        x_path = [point[0] for point in path]
        y_path = [point[1] for point in path]

        plt.figure(figsize=(8, 8))
        plt.scatter(x_obstacles, y_obstacles, c="black", marker="s", label="Obstacles")
        plt.plot(x_path, y_path, c="red", marker="o", label="Path")
        plt.scatter([path[0][0]], [path[0][1]], c="green", s=70, label="Start")
        plt.scatter([path[-1][0]], [path[-1][1]], c="blue", s=70, label="End")
        plt.title(f"GridRoute path - map {idx + 1}")
        plt.xlabel("X")
        plt.ylabel("Y")
        plt.grid(True)
        plt.xlim(-1, matrix_size)
        plt.ylim(-1, matrix_size)
        plt.legend()
        plt.savefig(output_dir / f"path_map_{idx + 1}.png", dpi=150, bbox_inches="tight")
        plt.close()
        written += 1

    print(f"Saved {written} path figures: {output_dir}")
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Draw GridRoute paths.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "data" / "dataset.csv")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "results" / "api_planned_paths_figure")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    draw_paths(args.input, args.dataset, args.output_dir, args.limit)


if __name__ == "__main__":
    main()
