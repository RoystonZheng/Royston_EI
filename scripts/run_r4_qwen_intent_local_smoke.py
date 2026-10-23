#!/usr/bin/env python3
"""Run one offline Qwen intent parse against a frozen R4 task.

No network API is called. Evaluator truth is loaded only after generation for
the result audit and is never included in the prompt or candidate query.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.models import Edge, Node, RoadMap, RoadsideFeature, VehicleOrderTask
from route_experiment.r4_intent import (
    build_intent_prompt,
    build_intent_query,
    build_road_feature_protocol,
    extract_intent_json,
    validate_route_intent,
)


DEFAULT_DATA = ROOT / "outputs/route_layer/traditional_experiment_v1/seeds/seed_0"
DEFAULT_MODEL = Path(
    "/tmp/route-r4-mlx-cache/hub/models--mlx-community--Qwen3-4B-Instruct-2507-4bit/"
    "snapshots/50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b"
)
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_qwen_intent_local_smoke_v1"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(seed_dir: Path, task_id: str, model_dir: Path, output_dir: Path, map_input_mode: str):
    from mlx_lm import generate, load
    from mlx_lm.sample_utils import make_sampler

    seed_dir = Path(seed_dir)
    map_path = next(seed_dir.glob("map_*_seed_*.json"))
    task_path = next(seed_dir.glob("order_tasks_seed_*.json"))
    map_data = read_json(map_path)
    task_data = next(
        item for item in read_json(task_path)["tasks"] if item["task_id"] == task_id
    )
    if task_data["task_type"] == "exact_node":
        raise ValueError("local_intent_smoke_requires_semantic_task")

    road_map = RoadMap(
        map_id=map_data["map_id"],
        version=map_data["version"],
        size=int(map_data["size"]),
        nodes=[Node(**item) for item in map_data["nodes"]],
        edges=[Edge(**item) for item in map_data["edges"]],
        boundary=dict(map_data["boundary"]),
        road_state_version=map_data.get("road_state_version", "road_state_v1"),
        roadside_features=[RoadsideFeature(**item) for item in map_data.get("roadside_features", [])],
    )
    # Construct the model-facing task from public fields only.
    public_task = VehicleOrderTask(
        task_id=task_data["task_id"],
        vehicle_id=task_data["vehicle_id"],
        order_id=task_data["order_id"],
        start_node_id=task_data["start_node_id"],
        pickup_node_id=task_data.get("pickup_node_id"),
        dropoff_node_id=task_data.get("dropoff_node_id"),
        pickup_text=task_data.get("pickup_text"),
        dropoff_text=task_data.get("dropoff_text"),
        task_type=task_data["task_type"],
    )
    protocol = build_road_feature_protocol(road_map, int(map_data["map_id"].rsplit("-", 1)[-1]))
    query = build_intent_query(
        road_map, public_task, protocol, map_input_mode=map_input_mode
    )
    prompt = build_intent_prompt(query)
    prompt_lower = prompt.lower()
    if "true_pickup_node_id" in prompt_lower or "true_dropoff_node_id" in prompt_lower:
        raise RuntimeError("evaluator_truth_leaked_to_qwen_prompt")

    model, tokenizer = load(str(model_dir))
    messages = [{"role": "user", "content": prompt}]
    chat_prompt = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )
    started = datetime.now(timezone.utc)
    raw = generate(
        model,
        tokenizer,
        prompt=chat_prompt,
        max_tokens=256,
        sampler=make_sampler(temp=0.0),
        verbose=False,
    )
    completed = datetime.now(timezone.utc)
    parsed = extract_intent_json(raw)
    validation = validate_route_intent(parsed, query)

    # Ground truth is consulted only after model output and deterministic
    # whitelist validation have been finalized.
    ground_truth_rows = read_json(task_path).get("ground_truth", [])
    truth = next((row for row in ground_truth_rows if row.get("task_id") == task_id), {})
    result = {
        "schema_version": "r4_qwen_local_intent_smoke_v1",
        "status": "validated" if validation.get("valid") else "invalid_intent",
        "created_at_utc": completed.isoformat(),
        "map_id": road_map.map_id,
        "task_id": task_id,
        "task_type": public_task.task_type,
        "map_input_mode": map_input_mode,
        "model": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
        "model_snapshot": "50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b",
        "decoding": {"temperature": 0.0, "max_tokens": 256, "thinking": False},
        "started_at_utc": started.isoformat(),
        "latency_ms": round((completed - started).total_seconds() * 1000, 3),
        "input_artifacts": {
            "map_sha256": _sha256(map_path.read_bytes()),
            "tasks_sha256": _sha256(task_path.read_bytes()),
            "protocol_sha256": _sha256(json.dumps(protocol.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")),
            "query_sha256": _sha256(json.dumps(query, ensure_ascii=False, sort_keys=True).encode("utf-8")),
            "prompt_sha256": _sha256(prompt.encode("utf-8")),
        },
        "query": query,
        "raw_response": raw,
        "parsed_intent": parsed,
        "validation": validation,
        "evaluation_after_inference": {
            "truth_loaded_after_generation": True,
            "pickup_exact_match": validation.get("pickup_node_id") == truth.get("true_pickup_node_id"),
            "dropoff_exact_match": validation.get("dropoff_node_id") == truth.get("true_dropoff_node_id"),
            "true_pickup_node_id": truth.get("true_pickup_node_id"),
            "true_dropoff_node_id": truth.get("true_dropoff_node_id"),
        },
        "billing": "local_inference_no_API_request",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (output_dir / "query.json").write_text(json.dumps(query, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--task-id", default="task-0000-0009")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--map-input-mode", choices=["road_feature_protocol_v1", "graph_only"], default="road_feature_protocol_v1")
    args = parser.parse_args()
    result = run(args.seed_dir, args.task_id, args.model_dir, args.output_dir, args.map_input_mode)
    print(json.dumps({key: result.get(key) for key in ["status", "map_id", "task_id", "task_type", "latency_ms", "parsed_intent", "validation", "evaluation_after_inference", "billing"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
