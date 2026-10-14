#!/usr/bin/env python3
"""Generate semantic road-feature showcase scenarios for one map artifact."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.scenario import showcase_payload
from route_experiment.models import RoadsideFeature


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("map_json")
    parser.add_argument("output_json")
    args = parser.parse_args()

    payload = json.loads(Path(args.map_json).read_text(encoding="utf-8"))
    class RoadMapView:
        pass

    road_map = RoadMapView()
    road_map.map_id = payload["map_id"]
    road_map.nodes = [type("NodeView", (), node) for node in payload["nodes"]]
    road_map.edges = [type("EdgeView", (), edge) for edge in payload["edges"]]
    road_map.roadside_features = [
        RoadsideFeature(**feature) for feature in payload.get("roadside_features", [])
    ]

    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(showcase_payload(road_map), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print("scenario_path={0}".format(output))
    print("scenario_count={0}".format(len(showcase_payload(road_map)["scenarios"])))


if __name__ == "__main__":
    main()
