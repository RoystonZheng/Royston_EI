from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .repro_utils import PROJECT_ROOT, as_int, dijkstra_path, load_table, obstacle_coords_from_row, parse_literal


def generate_reference_paths(
    dataset_path: str | Path = PROJECT_ROOT / "data" / "dataset.csv",
    output_path: str | Path = PROJECT_ROOT / "data" / "reference.csv",
) -> Path:
    dataset_path = Path(dataset_path)
    output_path = Path(output_path)
    maps_df = load_table(dataset_path)
    records = []

    for _, row in maps_df.iterrows():
        matrix_size = as_int(row["matrix_size"])
        start = (as_int(row["start_x"]), as_int(row["start_y"]))
        end = (as_int(row["end_x"]), as_int(row["end_y"]))
        obstacle_set = obstacle_coords_from_row(row)
        path = dijkstra_path(matrix_size, obstacle_set, start, end)

        records.append(
            {
                "Matrix_Size": matrix_size,
                "Start_X": start[0],
                "Start_Y": start[1],
                "End_X": end[0],
                "End_Y": end[1],
                "Path": str(path),
                "obstacles": str(parse_literal(row.get("obstacles"), [])),
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(records).to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"Generated {len(records)} reference paths: {output_path}")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate GridRoute Dijkstra reference paths.")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "data" / "dataset.csv")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "reference.csv")
    args = parser.parse_args()
    generate_reference_paths(args.input, args.output)


if __name__ == "__main__":
    main()
