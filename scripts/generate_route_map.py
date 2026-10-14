#!/usr/bin/env python3
"""Generate and persist one reproducible route-layer road map."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Allow direct execution from the project checkout without installing the
# package first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.mapgen import generate_map
from route_experiment.models import MapConfig


def build_summary(road_map):
    return {
        "map_id": road_map.map_id,
        "map_version": road_map.version,
        "road_state_version": road_map.road_state_version,
        "size": road_map.size,
        "node_count": len(road_map.nodes),
        "edge_count": len(road_map.edges),
        "one_way_edge_count": sum(edge.one_way for edge in road_map.edges),
        "bidirectional_edge_count": sum(not edge.one_way for edge in road_map.edges),
        "congestion_level_counts": dict(
            sorted(Counter(edge.congestion_level for edge in road_map.edges).items())
        ),
        "signal_edge_count": sum(edge.signal_delay_sec > 0 for edge in road_map.edges),
        "legal_pickup_node_count": sum(node.legal_pickup for node in road_map.nodes),
        "legal_dropoff_node_count": sum(node.legal_dropoff for node in road_map.nodes),
        "landmark_node_count": sum(bool(node.landmark_name) for node in road_map.nodes),
        "roadside_feature_count": len(road_map.roadside_features),
        "roadside_feature_type_counts": dict(
            sorted(
                Counter(feature.feature_type for feature in road_map.roadside_features).items()
            )
        ),
        "boundary": road_map.boundary,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument(
        "--output-dir", default="outputs/route_layer/map_generation"
    )
    args = parser.parse_args()

    road_map = generate_map(args.seed, args.size, MapConfig(size=args.size))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "map_{0}x{0}_seed_{1}".format(args.size, args.seed)

    map_path = output_dir / (stem + ".json")
    summary_path = output_dir / (stem + "_summary.json")
    map_path.write_text(
        json.dumps(road_map.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(build_summary(road_map), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(build_summary(road_map), ensure_ascii=False, sort_keys=True))
    print("map_path={0}".format(map_path))
    print("summary_path={0}".format(summary_path))


if __name__ == "__main__":
    main()
