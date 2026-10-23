#!/usr/bin/env python3
"""Run paired local Qwen intent audits on the frozen ten-seed route set.

This script only evaluates natural-language endpoint resolution. It makes no
hosted model/API requests and does not generate or repair routes. Evaluator
truth is consulted only after the model response has been produced and
validated against the public candidate whitelist.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.models import Edge, Node, RoadMap, RoadsideFeature, VehicleOrderTask
from route_experiment.r4_intent import (
    build_intent_query,
    build_road_feature_protocol,
    extract_intent_json,
    validate_route_intent,
)


TRADITIONAL_ROOT = ROOT / "outputs/route_layer/traditional_experiment_v1/seeds"
PURE_LLM_ROOT = ROOT / "outputs/route_layer/pure_llm_experiment_v8/seeds"
DEFAULT_MODEL = Path(
    "/tmp/route-r4-mlx-cache/hub/models--mlx-community--Qwen3-4B-Instruct-2507-4bit/"
    "snapshots/50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b"
)
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_qwen_intent_local_batch_v4"
MODEL_NAME = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
MODEL_REVISION = "50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b"
MODES = ("road_feature_protocol_v1", "graph_only")
PROMPT_VERSION = "r4_intent_prompt_v5_directional_relations"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256(path.read_bytes())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _without_embedded_road_map(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_embedded_road_map(item)
            for key, item in value.items()
            if key != "road_map"
        }
    if isinstance(value, list):
        return [_without_embedded_road_map(item) for item in value]
    return value


def audit_shared_inputs(seed_count: int = 10) -> dict[str, Any]:
    """Verify frozen input equivalence before running any local inference."""

    seed_rows = []
    for seed in range(seed_count):
        traditional_dir = TRADITIONAL_ROOT / f"seed_{seed}"
        pure_dir = PURE_LLM_ROOT / f"seed_{seed}"
        names = {
            "map": f"map_100x100_seed_{seed}.json",
            "orders": f"order_tasks_seed_{seed}.json",
            "formal_scenarios": f"formal_scenarios_seed_{seed}.json",
        }
        files = {}
        for label, name in names.items():
            left = traditional_dir / name
            right = pure_dir / name
            if not left.is_file() or not right.is_file():
                raise FileNotFoundError(f"shared_input_missing:{left}:{right}")
            files[label] = {
                "traditional_sha256": _sha256_file(left),
                "pure_llm_v8_sha256": _sha256_file(right),
                "byte_identical": left.read_bytes() == right.read_bytes(),
            }

        traditional_scenarios = _without_embedded_road_map(
            _read_json(traditional_dir / names["formal_scenarios"])
        )
        pure_scenarios = _without_embedded_road_map(
            _read_json(pure_dir / names["formal_scenarios"])
        )
        files["formal_scenarios"]["semantically_identical_without_embedded_road_map"] = (
            traditional_scenarios == pure_scenarios
        )

        orders = _read_json(traditional_dir / names["orders"])
        tasks = orders.get("tasks", [])
        type_counts: dict[str, int] = {}
        for task in tasks:
            type_counts[task["task_type"]] = type_counts.get(task["task_type"], 0) + 1
        scenarios = _read_json(traditional_dir / names["formal_scenarios"])
        scenario_types = sorted(item["scenario_type"] for item in scenarios.get("scenarios", []))
        seed_rows.append({
            "seed": seed,
            "map_id": orders.get("map_id", f"grid-100x100-seed-{seed}"),
            "files": files,
            "order_task_count": len(tasks),
            "order_task_type_counts": type_counts,
            "formal_scenario_count": len(scenarios.get("scenarios", [])),
            "formal_scenario_types": scenario_types,
        })

    all_files_equal = all(
        row["files"][key]["byte_identical"]
        for row in seed_rows
        for key in ("map", "orders")
    )
    all_scenarios_equal = all(
        row["files"]["formal_scenarios"]["semantically_identical_without_embedded_road_map"]
        for row in seed_rows
    )
    all_workloads_match = all(
        row["order_task_count"] == 10
        and row["order_task_type_counts"].get("exact_node") == 7
        and sum(
            count for task_type, count in row["order_task_type_counts"].items()
            if task_type != "exact_node"
        ) == 3
        and row["formal_scenario_count"] == 5
        for row in seed_rows
    )
    return {
        "schema_version": "r4_shared_input_audit_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "traditional_root": str(TRADITIONAL_ROOT),
        "pure_llm_v8_root": str(PURE_LLM_ROOT),
        "seed_count": seed_count,
        "maps_and_orders_byte_identical": all_files_equal,
        "formal_scenarios_semantically_identical_without_embedded_road_map": all_scenarios_equal,
        "workload_counts_match_expected_10_orders_7_exact_3_semantic_5_formal": all_workloads_match,
        "passed": all_files_equal and all_scenarios_equal and all_workloads_match,
        "seeds": seed_rows,
    }


def _load_road_map(map_data: dict[str, Any]) -> RoadMap:
    return RoadMap(
        map_id=map_data["map_id"],
        version=map_data["version"],
        size=int(map_data["size"]),
        nodes=[Node(**item) for item in map_data["nodes"]],
        edges=[Edge(**item) for item in map_data["edges"]],
        boundary=dict(map_data["boundary"]),
        road_state_version=map_data.get("road_state_version", "road_state_v1"),
        roadside_features=[
            RoadsideFeature(**item) for item in map_data.get("roadside_features", [])
        ],
    )


def _public_task(task_data: dict[str, Any]) -> VehicleOrderTask:
    # Semantic tasks carry no concrete endpoint IDs in their public task rows.
    # Construct only from public request fields; evaluator truth is elsewhere.
    return VehicleOrderTask(
        task_id=task_data["task_id"],
        vehicle_id=task_data["vehicle_id"],
        order_id=task_data["order_id"],
        start_node_id=task_data["start_node_id"],
        pickup_node_id=None,
        dropoff_node_id=None,
        pickup_text=task_data.get("pickup_text"),
        dropoff_text=task_data.get("dropoff_text"),
        task_type=task_data["task_type"],
    )


def _build_batch_prompt(query: dict[str, Any]) -> str:
    """Build a model-facing query with opaque refs instead of copyable node IDs."""

    pickup_rows = query.get("pickup_candidates", [])
    dropoff_rows = query.get("dropoff_candidates", [])
    pickup_map = {f"P{index:02d}": row["node_id"] for index, row in enumerate(pickup_rows)}
    dropoff_map = {f"D{index:02d}": row["node_id"] for index, row in enumerate(dropoff_rows)}

    def candidate_rows(
        rows: list[dict[str, Any]],
        prefix: str,
        anchors: list[dict[str, Any]],
        source_key: str,
    ) -> list[dict[str, Any]]:
        visible = []
        for index, row in enumerate(rows):
            item = {
                "candidate_ref": f"{prefix}{index:02d}",
                "coordinates": row.get("coordinates"),
            }
            if "relation_to_feature" in row:
                item["relation_to_feature"] = row["relation_to_feature"]
            coordinates = row.get("coordinates") or {}
            item["manhattan_distance_to_anchors"] = [
                {
                    "anchor_ref": anchor["entry_id"],
                    "distance": abs(int(coordinates["x"]) - int(anchor["anchor_coordinates"]["x"]))
                    + abs(int(coordinates["y"]) - int(anchor["anchor_coordinates"]["y"])),
                }
                for anchor in anchors
                if anchor.get("entry_id") and anchor.get("anchor_coordinates")
            ]
            item["relative_direction_to_anchors"] = []
            for anchor in anchors:
                anchor_coordinates = anchor.get("anchor_coordinates")
                if not anchor.get("entry_id") or not anchor_coordinates:
                    continue
                dx = int(coordinates["x"]) - int(anchor_coordinates["x"])
                dy = int(coordinates["y"]) - int(anchor_coordinates["y"])
                if abs(dx) >= abs(dy) and dx:
                    direction = "east" if dx > 0 else "west"
                elif dy:
                    direction = "north" if dy > 0 else "south"
                else:
                    direction = "same"
                item["relative_direction_to_anchors"].append({
                    "anchor_ref": anchor["entry_id"],
                    "direction": direction,
                })
            node_id = row.get("node_id")
            links = []
            for anchor in anchors:
                linked = next(
                    (
                        candidate
                        for candidate in anchor.get(source_key, [])
                        if candidate.get("node_id") == node_id
                    ),
                    None,
                )
                if linked:
                    links.append({
                        "anchor_ref": anchor["entry_id"],
                        "entry_type": anchor.get("entry_type"),
                        "name": anchor.get("name"),
                        "side_of_road": anchor.get("side_of_road"),
                        "orientation": anchor.get("orientation"),
                        "relation_to_feature": linked.get("relation_to_feature"),
                    })
            item["candidate_anchor_links"] = links
            visible.append(item)
        return visible

    def safe_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        visible = []
        for row in rows:
            item = {
                key: row[key]
                for key in (
                    "entry_id", "entry_type", "name", "aliases", "side_of_road",
                    "orientation", "anchor_coordinates",
                )
                if key in row
            }
            visible.append(item)
        return visible

    model_query = {
        "schema_version": "route_intent_query_v2",
        "task_id": query.get("task_id"),
        "task_type": query.get("task_type"),
        "map_id": query.get("map_id"),
        "map_version": query.get("map_version"),
        "map_input_mode": query.get("map_input_mode"),
        "start_node_id": query.get("start_node_id"),
        "pickup_text": query.get("pickup_text"),
        "dropoff_text": query.get("dropoff_text"),
        "pickup_resolution_mode": query.get("pickup_resolution_mode"),
        "dropoff_resolution_mode": query.get("dropoff_resolution_mode"),
        "matched_pickup_entries": safe_entries(query.get("matched_pickup_entries", [])),
        "matched_dropoff_entries": safe_entries(query.get("matched_dropoff_entries", [])),
        "pickup_candidates": candidate_rows(
            pickup_rows, "P", query.get("matched_pickup_entries", []), "pickup_candidates"
        ),
        "dropoff_candidates": candidate_rows(
            dropoff_rows, "D", query.get("matched_dropoff_entries", []), "dropoff_candidates"
        ),
        "allowed_feature_refs": query.get("allowed_feature_refs", []),
        "allowed_landmark_refs": query.get("allowed_landmark_refs", []),
        "uses_evaluator_truth": False,
    }
    prompt = (
        "你是路线任务的地点意图解析器。根据自然语言、匹配到的地标/道路特征及候选坐标选端点。"
        "坐标 x 向东递增、y 向北递增；near 选择与对应锚点曼哈顿距离最小的合法候选，"
        "使用候选中的 manhattan_distance_to_anchors 数值；距离相同时选择候选列表中先出现者。"
        "directional 先将用户方位映射到 relative_direction_to_anchors，再只从该方位候选中按曼哈顿距离最小者选择；"
        "道路特征任务先按名称和用户描述的道路侧别选择对应特征锚点，"
        "再从该锚点 candidate_anchor_links 中选候选；‘路牌在道路西侧’表示 feature_side 候选，"
        "只有明确要求到特征/道路另一侧时才选 opposite_side。"
        "候选编号是临时引用，不是地图节点 ID；输出只能复制候选列表里的 candidate_ref。"
        "anchor_coordinates、start_node_id 和锚点引用都不是可选端点。禁止自行生成或推算节点 ID。"
        "如无足够依据，返回 unresolved 并把两个 candidate_ref 设为 null。"
        "只输出 JSON，schema_version=route_intent_candidate_v2。成功对象字段为："
        '{"schema_version":"route_intent_candidate_v2","status":"resolved",'
        '"pickup_candidate_ref":"P00","dropoff_candidate_ref":"D00",'
        '"pickup_anchor_ref":"允许引用或null","dropoff_anchor_ref":"允许引用或null",'
        '"relation":"near|directional|feature_side|opposite_side|unknown",'
        '"side":"north|south|east|west|none|unknown",'
        '"feature_evidence_refs":[],"confidence":0.0}。'
        "pickup_anchor_ref、dropoff_anchor_ref 和 feature_evidence_refs 必须引用输入白名单。\n输入："
        + json.dumps(model_query, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return prompt, model_query, pickup_map, dropoff_map


def _resolve_candidate_intent(
    parsed: Any,
    query: dict[str, Any],
    model_query: dict[str, Any],
    pickup_map: dict[str, str],
    dropoff_map: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Resolve opaque refs after generation, then apply the canonical whitelist validator."""

    if not isinstance(parsed, dict):
        return None, {"valid": False, "reason": "intent_output_not_object"}
    if parsed.get("schema_version") != "route_intent_candidate_v2":
        return None, {"valid": False, "reason": "intent_candidate_schema_version_invalid"}
    if parsed.get("status") != "resolved":
        return None, {"valid": False, "reason": "intent_unresolved", "model_status": parsed.get("status")}
    pickup_ref = parsed.get("pickup_candidate_ref")
    dropoff_ref = parsed.get("dropoff_candidate_ref")
    if pickup_ref not in pickup_map:
        return None, {"valid": False, "reason": "intent_pickup_candidate_ref_invalid"}
    if dropoff_ref not in dropoff_map:
        return None, {"valid": False, "reason": "intent_dropoff_candidate_ref_invalid"}
    pickup_row = next(row for row in model_query["pickup_candidates"] if row["candidate_ref"] == pickup_ref)
    dropoff_row = next(row for row in model_query["dropoff_candidates"] if row["candidate_ref"] == dropoff_ref)
    pickup_anchor = parsed.get("pickup_anchor_ref")
    dropoff_anchor = parsed.get("dropoff_anchor_ref")
    if pickup_anchor and pickup_anchor not in {
        row.get("anchor_ref") for row in pickup_row.get("candidate_anchor_links", [])
    }:
        return None, {"valid": False, "reason": "intent_pickup_candidate_anchor_mismatch"}
    if dropoff_anchor and dropoff_anchor not in {
        row.get("anchor_ref") for row in dropoff_row.get("candidate_anchor_links", [])
    }:
        return None, {"valid": False, "reason": "intent_dropoff_candidate_anchor_mismatch"}
    if (
        query.get("task_type") == "semantic_road_feature_position"
        and query.get("map_input_mode") == "road_feature_protocol_v1"
    ):
        selected_link = next(
            row for row in pickup_row.get("candidate_anchor_links", [])
            if row.get("anchor_ref") == pickup_anchor
        )
        if parsed.get("relation") != selected_link.get("relation_to_feature"):
            return None, {"valid": False, "reason": "intent_pickup_candidate_relation_mismatch"}
    canonical = {
        "schema_version": "route_intent_v1",
        "status": "resolved",
        "pickup_node_id": pickup_map[pickup_ref],
        "dropoff_node_id": dropoff_map[dropoff_ref],
        "pickup_anchor_ref": parsed.get("pickup_anchor_ref"),
        "dropoff_anchor_ref": parsed.get("dropoff_anchor_ref"),
        "relation": parsed.get("relation"),
        "side": parsed.get("side"),
        "feature_evidence_refs": parsed.get("feature_evidence_refs", []),
        "confidence": parsed.get("confidence"),
    }
    return canonical, validate_route_intent(canonical, query)


def _result_path(output_dir: Path, seed: int, task_id: str, mode: str) -> Path:
    return output_dir / f"seed_{seed}" / task_id / mode / "result.json"


def _run_one(
    *, model: Any, tokenizer: Any, sampler: Any, seed: int, task_data: dict[str, Any],
    road_map: RoadMap, protocol: Any, map_path: Path, task_path: Path,
    output_dir: Path, mode: str, max_tokens: int,
) -> dict[str, Any]:
    public_task = _public_task(task_data)
    query = build_intent_query(road_map, public_task, protocol, map_input_mode=mode)
    query["task_type"] = public_task.task_type
    prompt, model_query, pickup_map, dropoff_map = _build_batch_prompt(query)
    prompt_lower = prompt.lower()
    if any(marker in prompt_lower for marker in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")):
        raise RuntimeError("evaluator_truth_leaked_to_qwen_prompt")

    chat_prompt = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}],
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )
    input_tokens = len(tokenizer.encode(chat_prompt))
    started = datetime.now(timezone.utc)
    raw = None
    parsed = None
    validation = None
    parse_error = None
    try:
        from mlx_lm import generate
        raw = generate(
            model,
            tokenizer,
            prompt=chat_prompt,
            max_tokens=max_tokens,
            sampler=sampler,
            verbose=False,
        )
        try:
            parsed = extract_intent_json(raw)
        except Exception as exc:  # preserve the raw response as a failed sample
            parse_error = type(exc).__name__ + ":" + str(exc)
            validation = {"valid": False, "reason": "intent_output_parse_failed"}
        else:
            try:
                resolved_payload, validation = _resolve_candidate_intent(
                    parsed, query, model_query, pickup_map, dropoff_map
                )
            except Exception as exc:  # preserve the raw response and classify validator faults distinctly
                parse_error = type(exc).__name__ + ":" + str(exc)
                validation = {"valid": False, "reason": "intent_postvalidation_error"}
    except Exception as exc:
        parse_error = type(exc).__name__ + ":" + str(exc)
        validation = {"valid": False, "reason": "local_inference_failed"}
    completed = datetime.now(timezone.utc)

    # Load evaluator truth only after inference, parsing, and whitelist checks.
    truth_rows = _read_json(task_path).get("ground_truth", [])
    truth = next((row for row in truth_rows if row.get("task_id") == task_data["task_id"]), {})
    pickup = validation.get("pickup_node_id") if validation else None
    dropoff = validation.get("dropoff_node_id") if validation else None
    valid = bool(validation and validation.get("valid"))
    exact_pickup = pickup == truth.get("true_pickup_node_id")
    exact_dropoff = dropoff == truth.get("true_dropoff_node_id")

    result = {
        "schema_version": "r4_qwen_local_intent_batch_result_v1",
        "created_at_utc": completed.isoformat(),
        "seed": seed,
        "map_id": road_map.map_id,
        "task_id": public_task.task_id,
        "task_type": public_task.task_type,
        "map_input_mode": mode,
        "prompt_version": PROMPT_VERSION,
        "model": MODEL_NAME,
        "model_revision": MODEL_REVISION,
        "prompt_version": PROMPT_VERSION,
        "decoding": {"temperature": 0.0, "max_tokens": max_tokens, "thinking": False},
        "status": "validated" if valid else "invalid_intent",
        "latency_ms": round((completed - started).total_seconds() * 1000, 3),
        "input_tokens_local_tokenizer": input_tokens,
        "input_artifacts": {
            "map_sha256": _sha256_file(map_path),
            "orders_sha256": _sha256_file(task_path),
            "protocol_sha256": _sha256(json.dumps(
                protocol.to_dict(), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")),
            "model_query_sha256": _sha256(json.dumps(
                model_query, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")),
            "prompt_sha256": _sha256(prompt.encode("utf-8")),
        },
        "model_query": model_query,
        "candidate_ref_resolution": {
            "pickup": pickup_map,
            "dropoff": dropoff_map,
        },
        "raw_response": raw,
        "parsed_intent": parsed,
        "resolved_intent": resolved_payload if "resolved_payload" in locals() else None,
        "validation": validation,
        "parse_or_inference_error": parse_error,
        "evaluation_after_inference": {
            "truth_loaded_after_generation": True,
            "pickup_exact_match": exact_pickup,
            "dropoff_exact_match": exact_dropoff,
            "both_endpoints_exact_match": exact_pickup and exact_dropoff,
            "true_pickup_node_id": truth.get("true_pickup_node_id"),
            "true_dropoff_node_id": truth.get("true_dropoff_node_id"),
        },
        "usage": {
            "input_tokens_local_tokenizer": input_tokens,
            "output_tokens": None,
            "billing": "local_inference_no_API_request",
            "actual_hardware_billing": "not_applicable_local_inference",
        },
    }
    result_path = _result_path(output_dir, seed, public_task.task_id, mode)
    _write_json(result_path, result)
    (result_path.parent / "query.json").write_text(
        json.dumps(model_query, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (result_path.parent / "prompt.txt").write_text(prompt, encoding="utf-8")
    return result


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode in MODES:
        rows = [row for row in results if row.get("map_input_mode") == mode]
        valid = [row for row in rows if row["status"] == "validated"]
        exact = [
            row for row in valid
            if row["evaluation_after_inference"]["both_endpoints_exact_match"]
        ]
        by_type = {}
        for row in rows:
            task_type = row["task_type"]
            group = by_type.setdefault(task_type, {"count": 0, "validated": 0, "both_endpoints_exact_match": 0})
            group["count"] += 1
            group["validated"] += row["status"] == "validated"
            group["both_endpoints_exact_match"] += row["evaluation_after_inference"]["both_endpoints_exact_match"]
        latencies = [row["latency_ms"] for row in rows if row.get("latency_ms") is not None]
        summary[mode] = {
            "task_count": len(rows),
            "validated_count": len(valid),
            "invalid_or_failed_count": len(rows) - len(valid),
            "both_endpoint_exact_match_count": len(exact),
            "both_endpoint_exact_match_rate_over_all_tasks": (len(exact) / len(rows)) if rows else None,
            "both_endpoint_exact_match_rate_over_validated": (len(exact) / len(valid)) if valid else None,
            "median_latency_ms": statistics.median(latencies) if latencies else None,
            "mean_latency_ms": statistics.mean(latencies) if latencies else None,
            "input_tokens_local_tokenizer_total": sum(row.get("input_tokens_local_tokenizer", 0) or 0 for row in rows),
            "by_task_type": by_type,
        }
    all_tasks = {
        (row["seed"], row["task_id"])
        for row in results
    }
    paired = []
    for seed, task_id in sorted(all_tasks):
        pair = {
            mode: next((row for row in results if row["seed"] == seed and row["task_id"] == task_id and row["map_input_mode"] == mode), None)
            for mode in MODES
        }
        if all(pair.values()):
            paired.append({
                "seed": seed,
                "task_id": task_id,
                "feature_mode_valid": pair[MODES[0]]["status"] == "validated",
                "graph_only_valid": pair[MODES[1]]["status"] == "validated",
                "feature_mode_exact": pair[MODES[0]]["evaluation_after_inference"]["both_endpoints_exact_match"],
                "graph_only_exact": pair[MODES[1]]["evaluation_after_inference"]["both_endpoints_exact_match"],
            })
    summary["paired_comparison"] = {
        "paired_task_count": len(paired),
        "road_feature_only_exact_match_count": sum(row["feature_mode_exact"] and not row["graph_only_exact"] for row in paired),
        "graph_only_only_exact_match_count": sum(row["graph_only_exact"] and not row["feature_mode_exact"] for row in paired),
        "both_exact_match_count": sum(row["feature_mode_exact"] and row["graph_only_exact"] for row in paired),
        "neither_exact_match_count": sum(not row["feature_mode_exact"] and not row["graph_only_exact"] for row in paired),
        "rows": paired,
    }
    return summary


def _write_index(path: Path, shared_audit: dict[str, Any], summary: dict[str, Any], results: list[dict[str, Any]]) -> None:
    table_rows = []
    for seed in range(shared_audit["seed_count"]):
        seed_results = [row for row in results if row["seed"] == seed]
        for task_id in sorted({row["task_id"] for row in seed_results}):
            by_mode = {row["map_input_mode"]: row for row in seed_results if row["task_id"] == task_id}
            feature = by_mode.get(MODES[0], {})
            graph = by_mode.get(MODES[1], {})
            table_rows.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    seed,
                    html.escape(task_id),
                    html.escape(feature.get("task_type", "")),
                    html.escape(feature.get("status", "pending")),
                    html.escape(str(feature.get("evaluation_after_inference", {}).get("both_endpoints_exact_match", "—"))),
                    html.escape(graph.get("status", "pending") + " / " + str(graph.get("evaluation_after_inference", {}).get("both_endpoints_exact_match", "—"))),
                )
            )
    mode_items = []
    for mode in MODES:
        metrics = summary[mode]
        mode_items.append(
            "<li><code>{}</code>：{}/{} 通过白名单；两端点完全匹配 {}/{}</li>".format(
                html.escape(mode),
                metrics["validated_count"],
                metrics["task_count"],
                metrics["both_endpoint_exact_match_count"],
                metrics["task_count"],
            )
        )
    page = f"""<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>R4 Qwen 本地意图解析对照</title>
<style>body{{font:15px/1.55 system-ui,sans-serif;margin:2rem auto;padding:0 1rem;max-width:1200px;color:#172033}}h1{{margin-bottom:.2rem}}.note{{background:#f0f4ff;padding:1rem;border-radius:8px}}table{{border-collapse:collapse;width:100%;margin-top:1rem}}th,td{{padding:.55rem;border-bottom:1px solid #d7deeb;text-align:left}}th{{background:#eef2ff;position:sticky;top:0}}code{{background:#f3f4f6;padding:.1rem .25rem}}</style>
<h1>R4 本地 Qwen 意图解析配对审计</h1><p class=\"note\">该页面只展示地点/道路特征解析，不含路线模型调用或路线结果。两个条件在相同 30 个语义订单上配对运行；准确率按 30 个任务为分母，白名单合法性单独统计。真值只在输出生成后用于评估。</p>
<p>共享输入通过：{shared_audit['passed']}；种子：{shared_audit['seed_count']}；配对任务：{summary['paired_comparison']['paired_task_count']}。</p>
<ul>{''.join(mode_items)}</ul>
<table><thead><tr><th>种子</th><th>任务</th><th>类型</th><th>道路特征协议：状态</th><th>道路特征协议：端点全匹配</th><th>graph-only：状态 / 全匹配</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></html>"""
    path.write_text(page, encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    shared_audit = audit_shared_inputs(args.seed_count)
    _write_json(output_dir / "shared_input_audit.json", shared_audit)
    if not shared_audit["passed"]:
        raise RuntimeError("shared_input_audit_failed_before_inference")

    from mlx_lm import load
    from mlx_lm.sample_utils import make_sampler

    model, tokenizer = load(str(args.model_dir))
    sampler = make_sampler(temp=0.0)
    results: list[dict[str, Any]] = []
    work_items = []
    for seed in range(args.seed_count):
        seed_dir = TRADITIONAL_ROOT / f"seed_{seed}"
        map_path = seed_dir / f"map_100x100_seed_{seed}.json"
        task_path = seed_dir / f"order_tasks_seed_{seed}.json"
        map_data = _read_json(map_path)
        road_map = _load_road_map(map_data)
        protocol = build_road_feature_protocol(road_map, seed)
        tasks_payload = _read_json(task_path)
        semantic_tasks = [
            task for task in tasks_payload.get("tasks", [])
            if task.get("task_type") != "exact_node"
            and (args.task_type is None or task.get("task_type") == args.task_type)
        ]
        for task_data in semantic_tasks:
            for mode in MODES:
                work_items.append((seed, task_data, road_map, protocol, map_path, task_path, mode))

    if args.max_samples is not None:
        work_items = work_items[:args.max_samples]
    total = len(work_items)
    for index, (seed, task_data, road_map, protocol, map_path, task_path, mode) in enumerate(work_items, 1):
        result_path = _result_path(output_dir, seed, task_data["task_id"], mode)
        if args.resume and result_path.is_file():
            result = _read_json(result_path)
            results.append(result)
            print(json.dumps({"progress": index, "total": total, "seed": seed, "task_id": task_data["task_id"], "mode": mode, "status": result.get("status"), "resumed": True}, ensure_ascii=False), flush=True)
            continue
        result = _run_one(
            model=model,
            tokenizer=tokenizer,
            sampler=sampler,
            seed=seed,
            task_data=task_data,
            road_map=road_map,
            protocol=protocol,
            map_path=map_path,
            task_path=task_path,
            output_dir=output_dir,
            mode=mode,
            max_tokens=args.max_tokens,
        )
        results.append(result)
        print(json.dumps({"progress": index, "total": total, "seed": seed, "task_id": task_data["task_id"], "mode": mode, "status": result["status"], "valid": result["validation"].get("valid"), "exact": result["evaluation_after_inference"]["both_endpoints_exact_match"], "latency_ms": result["latency_ms"]}, ensure_ascii=False), flush=True)
        _write_json(output_dir / "summary.partial.json", {
            "schema_version": "r4_qwen_local_intent_batch_summary_v1",
            "shared_input_audit": {"passed": shared_audit["passed"], "seed_count": shared_audit["seed_count"]},
            "completed_samples": len(results),
            "expected_samples": total,
            "results": _summarize(results),
            "model": MODEL_NAME,
            "model_revision": MODEL_REVISION,
            "api_requests": 0,
        })

    summary = {
        "schema_version": "r4_qwen_local_intent_batch_summary_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment_scope": "paired_local_endpoint_intent_audit_only_no_route_generation",
        "model": MODEL_NAME,
        "model_revision": MODEL_REVISION,
        "shared_input_audit": {"passed": shared_audit["passed"], "seed_count": shared_audit["seed_count"]},
        "completed_samples": len(results),
        "expected_samples": total,
        "api_requests": 0,
        "hosted_inference_requests": 0,
        "results": _summarize(results),
        "limitations": [
            "This paired audit measures endpoint intent resolution, not route construction or route quality.",
            "Exact endpoint match is reported separately from candidate-whitelist validity.",
            "Local tokenizer counts are not API tokens, billing, or a Codex/DeepSeek usage estimate.",
            "A 30-task semantic subset is a controlled task sample, not a broad language benchmark.",
        ],
    }
    _write_json(output_dir / "summary.json", summary)
    (output_dir / "summary.partial.json").unlink(missing_ok=True)
    _write_index(output_dir / "index.html", shared_audit, summary["results"], results)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed-count", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument(
        "--task-type",
        choices=("semantic_landmark_nearby", "semantic_landmark_relative", "semantic_road_feature_position"),
        help="optional focused semantic subset for a development run",
    )
    parser.add_argument("--max-samples", type=int, help="optional bounded local-inference prefix for a test run")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    summary = run(args)
    print(json.dumps({
        "status": "complete" if summary["completed_samples"] == summary["expected_samples"] else "partial",
        "completed_samples": summary["completed_samples"],
        "expected_samples": summary["expected_samples"],
        "api_requests": 0,
        "results": summary["results"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
