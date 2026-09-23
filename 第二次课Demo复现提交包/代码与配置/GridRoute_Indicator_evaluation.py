from __future__ import annotations

import argparse
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from .repro_utils import PROJECT_ROOT, as_int, load_table, obstacle_coords_from_row, parse_path


def validate_path(
    path: list[tuple[int, int]],
    start: tuple[int, int],
    end: tuple[int, int],
    obstacles: set[tuple[int, int]],
    matrix_size: int,
) -> tuple[bool, int, str]:
    if not path:
        return False, 0, "Empty Path"
    if path[0] != start or path[-1] != end:
        return False, len(path), "Start/End Mismatch"
    if start in obstacles or end in obstacles:
        return False, len(path), "Start/End Obstacle"

    for previous, current in zip(path, path[1:]):
        if not (0 <= current[0] < matrix_size and 0 <= current[1] < matrix_size):
            return False, len(path), "Out of Bounds"
        if current in obstacles:
            return False, len(path), "Path Through Obstacle"
        if abs(current[0] - previous[0]) + abs(current[1] - previous[1]) != 1:
            return False, len(path), "Invalid Step Distance"
    return True, len(path), "Valid Path"


def _canonical_row(row: pd.Series) -> dict[str, object]:
    def get(*names: str, default=None):
        for name in names:
            if name in row.index:
                return row[name]
        return default

    return {
        "matrix_size": as_int(get("Matrix_Size", "matrix_size")),
        "start": (as_int(get("Start_X", "start_x")), as_int(get("Start_Y", "start_y"))),
        "end": (as_int(get("End_X", "end_x")), as_int(get("End_Y", "end_y"))),
        "path": parse_path(get("Path", default="[]")),
        "run_time": get("Run_Time", "run_time", default=np.nan),
    }


def evaluate_paths(
    api_path: str | Path,
    reference_path: str | Path = PROJECT_ROOT / "data" / "reference.csv",
    dataset_path: str | Path = PROJECT_ROOT / "data" / "dataset.csv",
    output_path: str | Path = PROJECT_ROOT / "results" / "overall.csv",
) -> Path:
    api_df = load_table(api_path)
    reference_df = load_table(reference_path)
    dataset_df = load_table(dataset_path)

    if len(api_df) != len(dataset_df):
        print(
            f"Warning: API rows ({len(api_df)}) and dataset rows ({len(dataset_df)}) differ; "
            "evaluation uses matching row indices."
        )

    records: list[dict[str, object]] = []
    failure_counts: dict[int, Counter[str]] = defaultdict(Counter)

    for idx, row in api_df.iterrows():
        api = _canonical_row(row)
        if idx >= len(dataset_df):
            records.append(
                {
                    "Path_Index": idx,
                    "Matrix_Size": api["matrix_size"],
                    "Valid": False,
                    "Path_Length": len(api["path"]),
                    "Failure_Reason": "No Matching Dataset Row",
                    "Reference_Valid": False,
                    "Reference_Length": 0,
                    "Run_Time": api["run_time"],
                }
            )
            continue

        dataset_row = dataset_df.iloc[idx]
        obstacles = obstacle_coords_from_row(dataset_row)
        valid, path_length, reason = validate_path(
            api["path"],
            api["start"],
            api["end"],
            obstacles,
            api["matrix_size"],
        )

        reference_path = []
        reference_valid = False
        reference_length = 0
        if idx < len(reference_df):
            reference = _canonical_row(reference_df.iloc[idx])
            reference_path = reference["path"]
            reference_valid, reference_length, _ = validate_path(
                reference_path,
                api["start"],
                api["end"],
                obstacles,
                api["matrix_size"],
            )

        if not valid:
            failure_counts[int(api["matrix_size"])][reason] += 1

        records.append(
            {
                "Path_Index": idx,
                "Matrix_Size": api["matrix_size"],
                "Valid": valid,
                "Path_Length": path_length,
                "Failure_Reason": reason,
                "Reference_Valid": reference_valid,
                "Reference_Length": reference_length,
                "Run_Time": api["run_time"],
            }
        )

    result_df = pd.DataFrame(records)
    total = len(result_df)
    generated = result_df["Path_Length"] > 0
    valid = result_df["Valid"]
    comparable = valid & result_df["Reference_Valid"] & (result_df["Reference_Length"] > 0)
    ratios = result_df.loc[comparable, "Path_Length"] / result_df.loc[comparable, "Reference_Length"]
    optimal = comparable & (result_df["Path_Length"] == result_df["Reference_Length"])

    if len(ratios):
        geometric_mean = float(math.exp(np.log(ratios).mean()))
        mse = float(((result_df.loc[comparable, "Path_Length"] - result_df.loc[comparable, "Reference_Length"]) ** 2).mean())
    else:
        geometric_mean = float("nan")
        mse = float("nan")

    rows = []
    for size, group in result_df.groupby("Matrix_Size", sort=True):
        group_comparable = group["Valid"] & group["Reference_Valid"] & (group["Reference_Length"] > 0)
        group_ratios = group.loc[group_comparable, "Path_Length"] / group.loc[group_comparable, "Reference_Length"]
        group_optimal = group_comparable & (group["Path_Length"] == group["Reference_Length"])
        rows.append(
            {
                "Model": Path(api_path).stem.rsplit("_", 1)[0],
                "Prompt": Path(api_path).stem.rsplit("_", 1)[-1],
                "Size": str(size),
                "CR": float((group["Path_Length"] > 0).mean()),
                "FR": float(group["Valid"].mean()),
                "OR": float(group_optimal.sum() / group["Valid"].sum()) if group["Valid"].sum() else 0.0,
                "GM": float(math.exp(np.log(group_ratios).mean())) if len(group_ratios) else float("nan"),
                "MSE": float(((group.loc[group_comparable, "Path_Length"] - group.loc[group_comparable, "Reference_Length"]) ** 2).mean())
                if group_comparable.any()
                else float("nan"),
                "Average Time": float(group.loc[group["Path_Length"] > 0, "Run_Time"].mean()),
                "Source_File": Path(api_path).name,
            }
        )

    rows.append(
        {
            "Model": Path(api_path).stem.rsplit("_", 1)[0],
            "Prompt": Path(api_path).stem.rsplit("_", 1)[-1],
            "Size": "Overall",
            "CR": float(generated.mean()) if total else 0.0,
            "FR": float(valid.mean()) if total else 0.0,
            "OR": float(optimal.sum() / valid.sum()) if valid.sum() else 0.0,
            "GM": geometric_mean,
            "MSE": mse,
            "Average Time": float(result_df.loc[generated, "Run_Time"].mean()) if generated.any() else float("nan"),
            "Source_File": Path(api_path).name,
        }
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"Total cases: {total}")
    print(f"Generated paths: {int(generated.sum())} ({generated.mean():.2%})")
    print(f"Valid paths: {int(valid.sum())} ({valid.mean():.2%})")
    print(f"Optimal valid paths: {int(optimal.sum())}")
    print(f"Saved metrics: {output_path}")
    for size, reasons in failure_counts.items():
        print(f"Failures for map size {size}: {dict(reasons)}")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate GridRoute paths against Dijkstra references.")
    parser.add_argument("--api", required=True, type=Path)
    parser.add_argument("--reference", type=Path, default=PROJECT_ROOT / "data" / "reference.csv")
    parser.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "data" / "dataset.csv")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "results" / "overall.csv")
    args = parser.parse_args()
    evaluate_paths(args.api, args.reference, args.dataset, args.output)


if __name__ == "__main__":
    main()
