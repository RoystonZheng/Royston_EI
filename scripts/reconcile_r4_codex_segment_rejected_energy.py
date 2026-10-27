#!/usr/bin/env python3
"""Offline EV v2 estimates for rejected R4 task9 model proposals.

The estimates cover the proposal from its recorded segment start through the
first rejected action when that action is a directed edge in the frozen route
input. They are counterfactual estimates, never vehicle-consumed energy.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.costs import evaluate_route_candidate
from route_experiment.models import EvaluationConfig, RouteCandidate
from route_experiment.routing import _search_path, edge_travel_time_sec
from render_r4_segment_replay import render_replay
from run_r4_codex_segment_task9 import _load_gate


DEFAULT_RUN_DIR = ROOT / "outputs/route_layer/r4_codex_segment_task9_run_v7_20261027"
DEFAULT_PREFLIGHT_DIR = ROOT / "outputs/route_layer/r4_codex_segment_task9_preflight_v15"
DIRECTION_DELTA = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def evaluate_by_occupancy(
    road_map,
    node_sequence,
    edge_sequence,
    pickup_node_id,
    pickup_seen,
    config: Optional[EvaluationConfig] = None,
    route_id: str = "r4-occupancy-split",
) -> Dict[str, Any]:
    """Evaluate empty and occupied legs separately, stopping/restarting at pickup."""

    config = config or EvaluationConfig()
    profile = config.energy_profile
    if isinstance(profile, dict):
        passenger_load_kg = float(profile["passenger_load_kg"])
    else:
        passenger_load_kg = float(profile.passenger_load_kg)
    if not edge_sequence:
        return {
            "status": "not_estimable",
            "energy_kwh": None,
            "distance_m": None,
            "travel_time_sec": None,
            "legs": [],
            "note": "没有连续可估算的路线边；不填零。",
        }

    legs = []
    chunk_start = 0
    loaded = bool(pickup_seen) or bool(node_sequence and node_sequence[0] == pickup_node_id)
    for edge_index, _edge_id in enumerate(edge_sequence):
        reaches_pickup = not loaded and node_sequence[edge_index + 1] == pickup_node_id
        if reaches_pickup:
            chunk_end = edge_index + 1
        else:
            chunk_end = None
        if chunk_end is not None or edge_index == len(edge_sequence) - 1:
            end = chunk_end if chunk_end is not None else len(edge_sequence)
            leg_nodes = node_sequence[chunk_start : end + 1]
            leg_edges = edge_sequence[chunk_start:end]
            payload = passenger_load_kg if loaded else 0.0
            evaluation = evaluate_route_candidate(
                road_map,
                RouteCandidate(
                    route_id=f"{route_id}-leg-{len(legs) + 1}",
                    node_sequence=list(leg_nodes),
                    edge_sequence=list(leg_edges),
                    payload_mass_kg=payload,
                ),
                config,
            )
            missing_grade_steps = sum(
                int(row.get("missing_grade_steps", 0))
                for row in evaluation.energy_segments
            )
            legs.append(
                {
                    "leg_type": "occupied" if loaded else "empty",
                    "start_node_id": leg_nodes[0],
                    "end_node_id": leg_nodes[-1],
                    "route_edge_start_index": chunk_start,
                    "payload_mass_kg": payload,
                    "edge_count": len(leg_edges),
                    "energy_kwh": evaluation.energy_total_kwh,
                    "distance_m": evaluation.route_distance_m,
                    "travel_time_sec": evaluation.total_travel_time_sec,
                    "energy_model_version": evaluation.energy_model_version,
                    "energy_trace_fidelity": evaluation.energy_trace_fidelity,
                    "energy_profile_id": evaluation.energy_profile_id,
                    "energy_profile_sha256": evaluation.energy_profile_sha256,
                    "energy_grade_missing": evaluation.energy_grade_missing,
                    "missing_grade_step_count": missing_grade_steps,
                    "edge_energy_breakdown": [
                        {
                            "edge_id": row.get("edge_id"),
                            "energy_kwh": row.get("energy_kwh"),
                            "distance_m": row.get("distance_m"),
                            "missing_grade_steps": row.get("missing_grade_steps", 0),
                        }
                        for row in evaluation.energy_segments
                    ],
                }
            )
            chunk_start = end
            if reaches_pickup:
                loaded = True

    return {
        "status": "estimated",
        "energy_kwh": sum(float(leg["energy_kwh"]) for leg in legs),
        "distance_m": sum(float(leg["distance_m"]) for leg in legs),
        "travel_time_sec": sum(float(leg["travel_time_sec"]) for leg in legs),
        "energy_model_version": legs[0]["energy_model_version"],
        "energy_trace_fidelity": legs[0]["energy_trace_fidelity"],
        "energy_profile_id": legs[0]["energy_profile_id"],
        "energy_profile_sha256": legs[0]["energy_profile_sha256"],
        "energy_grade_missing": any(leg["energy_grade_missing"] for leg in legs),
        "missing_grade_step_count": sum(leg["missing_grade_step_count"] for leg in legs),
        "pickup_stop_and_restart_assumed": len(legs) > 1,
        "pickup_dwell_sec": None,
        "legs": legs,
    }


def rejected_proposal_energy(
    road_map,
    package_edges: Iterable[Any],
    blocked_edge_ids: Iterable[str],
    attempt: Dict[str, Any],
    pickup_node_id: Optional[str] = None,
    config: Optional[EvaluationConfig] = None,
) -> Dict[str, Any]:
    """Estimate a rejected proposal's route-edge energy through its first failure."""

    config = config or EvaluationConfig()
    audit = attempt.get("audit") or {}
    violations = audit.get("violations") or []
    violation = violations[0] if violations else {}
    first_step = audit.get("first_violation_step")
    if first_step is None:
        first_step = violation.get("step")
    if isinstance(first_step, bool) or not isinstance(first_step, int) or first_step < 1:
        return {
            "request_number": attempt.get("request_number"),
            "status": "not_estimable",
            "failure_reason": violation.get("kind", "first_violation_step_missing"),
            "energy_kwh": None,
            "distance_m": None,
            "travel_time_sec": None,
            "estimated_edge_count": 0,
            "includes_first_rejected_action": False,
            "first_rejected_action_energy_estimable": False,
            "note": "拒绝位置缺失，无法确定要估算的提议前缀；未填入零能耗。",
        }

    nodes_by_id = {node.node_id: node for node in road_map.nodes}
    nodes_by_xy = {(node.x, node.y): node for node in road_map.nodes}
    map_edges = {
        (edge.from_node, edge.to_node): edge.edge_id for edge in road_map.edges
    }
    package_edges = list(package_edges)
    route_edges = {
        (edge.from_node, edge.to_node): edge.edge_id for edge in package_edges
    }
    edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
    blocked = set(blocked_edge_ids)
    actions = (attempt.get("proposal") or {}).get("actions") or []
    start_node = attempt.get("current_node_id")
    if not start_node and audit.get("steps"):
        start_node = audit["steps"][0].get("from_node_id")

    current = start_node
    node_sequence = [current] if current in nodes_by_id else []
    edge_sequence = []
    first_rejected_action: Dict[str, Any] = {
        "step": first_step,
        "direction": actions[first_step - 1].get("direction")
        if len(actions) >= first_step and isinstance(actions[first_step - 1], dict)
        else None,
        "from_node_id": None,
        "to_node_id": None,
        "map_directed_edge_exists": False,
        "route_input_directed_edge_exists": False,
        "edge_id": None,
        "energy_estimable": False,
        "energy_kwh": None,
        "non_estimable_reason": None,
    }
    failure_path = None

    for index, action in enumerate(actions[:first_step], start=1):
        direction = action.get("direction") if isinstance(action, dict) else None
        source = nodes_by_id.get(current)
        delta = DIRECTION_DELTA.get(direction)
        target = nodes_by_xy.get((source.x + delta[0], source.y + delta[1])) if source and delta else None
        pair = (current, target.node_id) if target else None
        map_edge_id = map_edges.get(pair) if pair else None
        package_edge_id = route_edges.get(pair) if pair else None

        if index == first_step:
            first_rejected_action.update(
                {
                    "from_node_id": current,
                    "to_node_id": target.node_id if target else None,
                    "map_directed_edge_exists": map_edge_id is not None,
                    "route_input_directed_edge_exists": package_edge_id is not None,
                    "edge_id": package_edge_id,
                    "non_estimable_reason": (
                        None
                        if package_edge_id
                        else "no_directed_edge_in_map"
                        if not map_edge_id
                        else "directed_edge_not_in_frozen_route_input"
                    ),
                }
            )

        # Use only edges present in the frozen route package and map profile.
        if not package_edge_id or package_edge_id in blocked or package_edge_id not in edge_by_id:
            failure_path = {
                "step": index,
                "direction": direction,
                "from_node_id": current,
                "to_node_id": target.node_id if target else None,
                "reason": (
                    "route_input_directed_edge_missing"
                    if not package_edge_id
                    else "route_input_edge_blocked_or_missing_from_map"
                ),
            }
            break

        edge_sequence.append(package_edge_id)
        current = target.node_id
        node_sequence.append(current)
        if index == first_step:
            first_rejected_action["energy_estimable"] = True

    estimate = None
    evaluation_error = None
    if edge_sequence:
        try:
            estimate = evaluate_by_occupancy(
                road_map,
                node_sequence,
                edge_sequence,
                pickup_node_id,
                bool(attempt.get("pickup_seen", False)),
                config,
                route_id=f"rejected-proposal-{attempt.get('request_number')}",
            )
        except Exception as exc:  # preserve a missing estimate rather than fabricate zero
            evaluation_error = f"{type(exc).__name__}:{exc}"

    energy_kwh = estimate.get("energy_kwh") if estimate is not None else None
    status = "estimated" if energy_kwh is not None else "not_estimable"
    if estimate is not None and first_rejected_action["energy_estimable"]:
        edge_position = first_step - 1
        for leg in estimate.get("legs", []):
            local_position = edge_position - int(leg["route_edge_start_index"])
            breakdown = leg.get("edge_energy_breakdown", [])
            if 0 <= local_position < len(breakdown):
                first_rejected_action["energy_kwh"] = breakdown[local_position]["energy_kwh"]
                first_rejected_action["energy_model_version"] = leg["energy_model_version"]
                break
    if failure_path is None and len(edge_sequence) < first_step:
        failure_path = {"step": first_step, "reason": "proposal_prefix_truncated"}
    return {
        "request_number": attempt.get("request_number"),
        "status": status,
        "failure_reason": violation.get("kind", "unspecified_rejection"),
        "first_violation_step": first_step,
        "scope": "counterfactual_model_proposal_through_first_rejected_action_when_route_edge_exists",
        "energy_kwh": energy_kwh,
        "distance_m": estimate.get("distance_m") if estimate else None,
        "travel_time_sec": estimate.get("travel_time_sec") if estimate else None,
        "estimated_edge_count": len(edge_sequence),
        "attempted_legal_prefix_energy_kwh": (
            (attempt.get("attempted_prefix_energy") or {}).get("energy_kwh")
        ),
        "attempted_legal_prefix_edge_count": (
            (attempt.get("attempted_prefix_energy") or {}).get("legal_prefix_edge_count", 0)
        ),
        "includes_first_rejected_action": bool(first_rejected_action["energy_estimable"]),
        "first_rejected_action_energy_estimable": bool(first_rejected_action["energy_estimable"]),
        "first_rejected_action": first_rejected_action,
        "estimated_end_node_id": current if edge_sequence else None,
        "truncated_at": failure_path,
        "energy_model_version": estimate.get("energy_model_version") if estimate else config.energy_model_version,
        "energy_trace_fidelity": estimate.get("energy_trace_fidelity") if estimate else None,
        "energy_profile_id": estimate.get("energy_profile_id") if estimate else config.energy_profile.profile_id,
        "energy_profile_sha256": estimate.get("energy_profile_sha256") if estimate else config.energy_profile.fingerprint(),
        "energy_grade_missing": estimate.get("energy_grade_missing") if estimate else None,
        "missing_grade_step_count": estimate.get("missing_grade_step_count") if estimate else None,
        "pickup_stop_and_restart_assumed": estimate.get("pickup_stop_and_restart_assumed") if estimate else None,
        "pickup_dwell_sec": estimate.get("pickup_dwell_sec") if estimate else None,
        "occupancy_legs": estimate.get("legs", []) if estimate else [],
        "simulation_estimate": True,
        "note": (
            "这是模型被拒绝提议在冻结路线图有向边上的反事实 EV v2 估值，不是车辆实际行驶能耗；"
            "若首个拒绝动作不是路线输入中的可用有向边，估算只覆盖其前面的连续有向边。"
            if status == "estimated"
            else "没有可估算的连续路线输入有向边，保留空值而非填零。"
        ),
        "evaluation_error": evaluation_error,
    }


def build_sidecar(
    loaded: Dict[str, Any],
    audit: Dict[str, Any],
    source_audit_sha256: str,
    preflight_sha256: str,
    run_dir: Path,
) -> Dict[str, Any]:
    item = loaded["item"]
    route = audit.get("route") or {}
    route_energy = evaluate_by_occupancy(
        item["road_map"],
        route.get("node_sequence") or [],
        route.get("edge_sequence") or [],
        item["task"].pickup_node_id,
        False,
        route_id="completed-route-by-occupancy",
    )
    original_energy = audit.get("energy") or {}
    original_kwh = original_energy.get("energy_kwh")
    route_energy["original_uniform_payload_energy_kwh"] = original_kwh
    route_energy["difference_from_original_kwh"] = (
        float(route_energy["energy_kwh"]) - float(original_kwh)
        if route_energy.get("energy_kwh") is not None and original_kwh is not None
        else None
    )
    route_energy["route_objective"] = "travel_time_then_distance"
    route_energy["electricity_cost_cny"] = None
    route_energy["electricity_cost_note"] = "本次单任务诊断未冻结适用电价，费用金额留空。"
    route_energy["note"] = (
        "按EV v2口径，start→pickup按空载0 kg，pickup→dropoff按参考载荷75 kg分别估算；"
        "pickup处假设停车并重新起步，未计未冻结的pickup dwell。均为模拟估值，不是实车测量。"
    )
    rejected = []
    for attempt in audit.get("attempts", []):
        if (attempt.get("audit") or {}).get("accepted") is False:
            rejected.append(
                rejected_proposal_energy(
                    item["road_map"],
                    item["package"].edges,
                    item["package"].blocked_edge_ids,
                    attempt,
                    pickup_node_id=item["task"].pickup_node_id,
                )
            )
    statuses = Counter(row["status"] for row in rejected)
    reasons = Counter(row["failure_reason"] for row in rejected)
    return {
        "schema_version": "r4_codex_rejected_proposal_energy_sidecar_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "offline_reconciliation_complete",
        "model_requests": 0,
        "run_dir": str(run_dir),
        "task_id": audit.get("task_id"),
        "map_id": audit.get("map_id"),
        "source_final_audit_sha256": source_audit_sha256,
        "preflight_sha256": preflight_sha256,
        "map_sha256": audit.get("map_sha256"),
        "orders_sha256": audit.get("orders_sha256"),
        "energy_model_version": "ev_longitudinal_v2",
        "energy_profile_id": EvaluationConfig().energy_profile.profile_id,
        "energy_profile_sha256": EvaluationConfig().energy_profile.fingerprint(),
        "completed_route_energy_by_occupancy": route_energy,
        "route_input_directed_edge_count": len(item["package"].edges),
        "rejected_proposal_count": len(rejected),
        "estimated_count": statuses["estimated"],
        "not_estimable_count": statuses["not_estimable"],
        "first_rejected_action_energy_estimable_count": sum(
            row["first_rejected_action_energy_estimable"] for row in rejected
        ),
        "first_rejected_action_energy_not_estimable_reasons": dict(
            sorted(
                Counter(
                    row["first_rejected_action"].get("non_estimable_reason")
                    or "unknown"
                    for row in rejected
                    if not row["first_rejected_action_energy_estimable"]
                ).items()
            )
        ),
        "reason_counts": dict(sorted(reasons.items())),
        "aggregation_warning": "各条是不同请求的反事实提议估值，不能累加为车辆能耗或单条路线总能耗。",
        "records": rejected,
    }


def build_feasible_dijkstra_reference(
    loaded: Dict[str, Any], audit: Dict[str, Any], source_audit_sha256: str, preflight_sha256: str
) -> Dict[str, Any]:
    """Build a same-endpoint feasible local-Dijkstra reference without model input."""

    item = loaded["item"]
    road_map = item["road_map"]
    package = item["package"]
    task = item["task"]
    blocked = set(package.blocked_edge_ids)
    first, _ = _search_path(
        package.nodes,
        package.edges,
        task.start_node_id,
        task.pickup_node_id,
        "dijkstra",
        blocked,
    )
    if first is None:
        return {
            "schema_version": "r4_task9_feasible_dijkstra_reference_v1",
            "status": "reference_unreachable_start_to_pickup",
            "model_requests": 0,
            "source_final_audit_sha256": source_audit_sha256,
            "preflight_sha256": preflight_sha256,
            "route": None,
            "limitation": "No route returned; no value is imputed.",
        }

    # Keep the first shortest leg fixed and find the shortest second leg that
    # cannot revisit any pre-pickup node. This is feasible but not a joint
    # global optimum over every possible pair of legs.
    forbidden_before_pickup = set(first.node_sequence[:-1])
    second_edges = [
        edge
        for edge in package.edges
        if edge.from_node not in forbidden_before_pickup
        and edge.to_node not in forbidden_before_pickup
    ]
    second, _ = _search_path(
        package.nodes,
        second_edges,
        task.pickup_node_id,
        task.dropoff_node_id,
        "dijkstra",
        blocked,
    )
    if second is None:
        return {
            "schema_version": "r4_task9_feasible_dijkstra_reference_v1",
            "status": "no_simple_second_leg_given_shortest_first_leg",
            "model_requests": 0,
            "source_final_audit_sha256": source_audit_sha256,
            "preflight_sha256": preflight_sha256,
            "first_leg_move_count": len(first.edge_sequence),
            "route": None,
            "limitation": "No globally constrained route was found by this fixed-first-leg procedure; no value is imputed.",
        }

    node_sequence = list(first.node_sequence) + list(second.node_sequence[1:])
    edge_sequence = list(first.edge_sequence) + list(second.edge_sequence)
    if len(node_sequence) != len(set(node_sequence)):
        raise RuntimeError("constructed_local_dijkstra_reference_revisits_node")
    if (
        node_sequence[0] != task.start_node_id
        or node_sequence[-1] != task.dropoff_node_id
        or task.pickup_node_id not in node_sequence
    ):
        raise RuntimeError("constructed_local_dijkstra_reference_endpoint_mismatch")
    map_edges = {edge.edge_id: edge for edge in road_map.edges}
    if any(edge_id not in map_edges for edge_id in edge_sequence):
        raise RuntimeError("constructed_local_dijkstra_reference_edge_missing_from_map")
    objective_time = sum(edge_travel_time_sec(map_edges[edge_id]) for edge_id in edge_sequence)
    distance_m = sum(float(map_edges[edge_id].length_m) for edge_id in edge_sequence)
    energy = evaluate_by_occupancy(
        road_map,
        node_sequence,
        edge_sequence,
        task.pickup_node_id,
        False,
        route_id="task9-feasible-local-dijkstra-reference",
    )
    model_route = audit.get("route") or {}
    model_edges = list(model_route.get("edge_sequence") or [])
    model_time = sum(edge_travel_time_sec(map_edges[edge_id]) for edge_id in model_edges)
    model_distance_m = sum(float(map_edges[edge_id].length_m) for edge_id in model_edges)
    model_energy = evaluate_by_occupancy(
        road_map,
        list(model_route.get("node_sequence") or []),
        model_edges,
        task.pickup_node_id,
        False,
        route_id="task9-model-route-quality-reference",
    )
    directions = []
    nodes_by_id = {node.node_id: node for node in road_map.nodes}
    delta_to_direction = {value: key for key, value in DIRECTION_DELTA.items()}
    for source_id, target_id in zip(node_sequence, node_sequence[1:]):
        source, target = nodes_by_id[source_id], nodes_by_id[target_id]
        directions.append(delta_to_direction[(target.x - source.x, target.y - source.y)])
    traditional_path = (
        ROOT
        / "outputs/route_layer/traditional_experiment_v1/seeds/seed_0/visualizations/orders/task-0000-0009__local_dijkstra.json"
    )
    traditional_baseline: Dict[str, Any] = {
        "status": "not_found",
        "directly_comparable": False,
        "source_path": str(traditional_path.relative_to(ROOT)),
    }
    if traditional_path.is_file():
        traditional_bytes = traditional_path.read_bytes()
        traditional = json.loads(traditional_bytes)
        traditional_geometry = traditional.get("geometry") or {}
        traditional_nodes = (traditional.get("trace") or {}).get("route_node_sequence") or []
        traditional_map_path = traditional_path.parents[2] / "map_100x100_seed_0.json"
        traditional_map_sha256 = (
            _sha256(traditional_map_path.read_bytes()) if traditional_map_path.is_file() else None
        )
        current_endpoints = [task.start_node_id, task.pickup_node_id, task.dropoff_node_id]
        traditional_endpoints = traditional_geometry.get("endpoint_node_ids") or []
        map_matches = traditional_map_sha256 == audit.get("map_sha256")
        endpoint_match = traditional_endpoints == current_endpoints
        traditional_baseline = {
            "status": traditional.get("status"),
            "directly_comparable": bool(
                map_matches
                and traditional.get("task_id") == audit.get("task_id")
                and endpoint_match
                and traditional.get("validation", {}).get("valid")
                and traditional_nodes
                and traditional_nodes[0] == task.start_node_id
                and traditional_nodes[-1] == task.dropoff_node_id
                and task.pickup_node_id in traditional_nodes
            ),
            "source_path": str(traditional_path.relative_to(ROOT)),
            "source_sha256": _sha256(traditional_bytes),
            "map_sha256": traditional_map_sha256,
            "map_matches": map_matches,
            "task_id": traditional.get("task_id"),
            "endpoint_node_ids": traditional_endpoints,
            "current_r4_endpoint_node_ids": current_endpoints,
            "endpoint_match": endpoint_match,
            "route_node_count": len(traditional_nodes),
            "route_contains_requested_pickup": task.pickup_node_id in traditional_nodes,
            "route_edge_count": len((traditional.get("trace") or {}).get("route_edge_sequence") or []),
            "reason": None
            if map_matches and endpoint_match and task.pickup_node_id in traditional_nodes
            else "同一地图和任务编号的旧传统结果未经过 R4 请求的 pickup 节点，端点约束不一致，不能用于直接性能比较。",
        }
    return {
        "schema_version": "r4_task9_feasible_dijkstra_reference_v1",
        "status": "completed_feasible_reference",
        "model_requests": 0,
        "method": "local_dijkstra_first_shortest_leg_then_shortest_second_leg_with_first_leg_internal_nodes_forbidden",
        "map_id": audit.get("map_id"),
        "task_id": audit.get("task_id"),
        "start_node_id": task.start_node_id,
        "pickup_node_id": task.pickup_node_id,
        "dropoff_node_id": task.dropoff_node_id,
        "map_sha256": audit.get("map_sha256"),
        "orders_sha256": audit.get("orders_sha256"),
        "source_final_audit_sha256": source_audit_sha256,
        "preflight_sha256": preflight_sha256,
        "route_input_package_id": package.package_id,
        "route_input_directed_edge_count": len(package.edges),
        "route_is_simple": True,
        "route_complete": True,
        "global_optimum_claimed": False,
        "feasibility_rule": "The second leg avoids every internal node in the fixed first shortest leg; concatenated route has no repeated nodes.",
        "first_leg_move_count": len(first.edge_sequence),
        "second_leg_move_count": len(second.edge_sequence),
        "move_count": len(edge_sequence),
        "distance_m": distance_m,
        "objective_travel_time_sec": objective_time,
        "energy_by_occupancy": energy,
        "traditional_baseline_compatibility": traditional_baseline,
        "model_route_comparison": {
            "move_count": model_route.get("move_count"),
            "distance_m": model_distance_m,
            "objective_travel_time_sec": model_time,
            "energy_kwh": model_energy.get("energy_kwh"),
            "energy_uniform_75kg_original_kwh": (audit.get("energy") or {}).get("energy_kwh"),
            "additional_move_count": len(model_edges) - len(edge_sequence),
            "additional_distance_m": model_distance_m - distance_m,
            "additional_objective_time_sec": model_time - objective_time,
            "move_ratio": len(model_edges) / len(edge_sequence) if edge_sequence else None,
            "objective_time_ratio": model_time / objective_time if objective_time else None,
        },
        "node_sequence": node_sequence,
        "edge_sequence": edge_sequence,
        "direction_sequence": directions,
        "limitation": "Single-task offline reference only. It fixes the unconstrained shortest first leg and optimizes the second leg around its visited nodes, so it is feasible but not necessarily the joint shortest no-revisit route.",
    }


def reconcile(run_dir: Path, preflight_dir: Path) -> Dict[str, Any]:
    loaded = _load_gate(preflight_dir)
    audit_path = run_dir / "final_audit.json"
    manifest_path = run_dir / "manifest.json"
    if not audit_path.is_file() or not manifest_path.is_file():
        raise RuntimeError("run_audit_or_manifest_missing")
    audit_bytes = audit_path.read_bytes()
    audit = json.loads(audit_bytes)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if audit.get("map_sha256") != loaded["observed_hashes"]["map_sha256"]:
        raise RuntimeError("run_map_hash_does_not_match_frozen_preflight")
    if audit.get("orders_sha256") != loaded["observed_hashes"]["orders_sha256"]:
        raise RuntimeError("run_orders_hash_does_not_match_frozen_preflight")
    expected_preflight_sha = manifest.get("preflight_sha256")
    preflight_path = preflight_dir / "preflight.json"
    actual_preflight_sha = _sha256(preflight_path.read_bytes())
    if expected_preflight_sha != actual_preflight_sha:
        raise RuntimeError("run_preflight_hash_does_not_match_requested_preflight")

    sidecar = build_sidecar(
        loaded, audit, _sha256(audit_bytes), actual_preflight_sha, run_dir
    )
    quality_reference = build_feasible_dijkstra_reference(
        loaded, audit, _sha256(audit_bytes), actual_preflight_sha
    )
    quality_path = run_dir / "route_quality_reference.json"
    _write_json(quality_path, quality_reference)
    sidecar["route_quality_reference_sha256"] = _sha256(quality_path.read_bytes())
    enriched = copy.deepcopy(audit)
    previous_energy = copy.deepcopy(enriched.get("energy"))
    enriched["energy_uniform_75kg_original"] = previous_energy
    route_energy = sidecar["completed_route_energy_by_occupancy"]
    enriched["energy_by_occupancy"] = route_energy
    enriched["energy"] = {
        "scope": "complete_model_route_split_by_occupancy",
        "energy_kwh": route_energy.get("energy_kwh"),
        "distance_m": route_energy.get("distance_m"),
        "travel_time_sec": route_energy.get("travel_time_sec"),
        "energy_model_version": route_energy.get("energy_model_version"),
        "trace_fidelity": route_energy.get("energy_trace_fidelity"),
        "energy_profile_id": route_energy.get("energy_profile_id"),
        "energy_profile_sha256": route_energy.get("energy_profile_sha256"),
        "energy_grade_missing": route_energy.get("energy_grade_missing"),
        "missing_grade_step_count": route_energy.get("missing_grade_step_count"),
        "simulation_estimate": True,
        "route_objective": "travel_time_then_distance",
        "electricity_cost_cny": None,
        "note": route_energy.get("note"),
    }
    enriched["route_quality_reference"] = {
        key: value
        for key, value in quality_reference.items()
        if key not in {"node_sequence", "edge_sequence", "direction_sequence", "energy_by_occupancy"}
    }
    enriched["route_quality_reference"]["energy_kwh"] = (
        quality_reference.get("energy_by_occupancy", {}).get("energy_kwh")
    )
    enriched["route_quality_reference"]["sidecar_sha256"] = _sha256(quality_path.read_bytes())
    enriched["route_quality_reference_route"] = {
        "node_sequence": quality_reference.get("node_sequence", []),
        "edge_sequence": quality_reference.get("edge_sequence", []),
        "direction_sequence": quality_reference.get("direction_sequence", []),
    }
    by_request = {row["request_number"]: row for row in sidecar["records"]}
    for attempt in enriched.get("attempts", []):
        record = by_request.get(attempt.get("request_number"))
        if record:
            attempt["rejected_proposal_energy"] = record

    sidecar_path = run_dir / "rejected_proposal_energy.json"
    enriched_path = run_dir / "final_audit_with_rejected_proposal_energy.json"
    _write_json(sidecar_path, sidecar)
    _write_json(enriched_path, enriched)
    task = {
        "task_id": enriched.get("task_id"),
        "start_node_id": enriched.get("start_node_id"),
        "pickup_node_id": enriched.get("pickup_node_id"),
        "dropoff_node_id": enriched.get("dropoff_node_id"),
    }
    render_replay(loaded["item"]["map_data"], task, enriched, run_dir / "replay.html")
    comparison = quality_reference.get("model_route_comparison") or {}
    old_baseline = quality_reference.get("traditional_baseline_compatibility") or {}
    old_baseline_note = old_baseline.get("reason") or "传统基线端点已匹配。"
    index = f"""<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>R4 task9 单任务诊断试跑</title><h1>R4 task9 单任务诊断试跑</h1><p>状态：{enriched.get('status')}。这是单任务诊断试跑，不是十种子正式比较。</p><ul><li><a href='replay.html'>打开地图与逐段回放及路线对照</a></li><li><a href='rejected_proposal_energy.json'>完成路线与被拒绝提议的 EV v2 能耗侧算</a></li><li><a href='route_quality_reference.json'>同端点无重复节点的离线 Dijkstra 参考</a></li><li><a href='final_audit_with_rejected_proposal_energy.json'>按载荷阶段修正的审计副本</a></li><li><a href='final_audit.json'>原始最终审计</a></li><li><a href='manifest.json'>输入/运行清单</a></li><li><a href='state.json'>运行状态</a></li></ul><p>完成路线EV v2分阶段估值：{route_energy.get('energy_kwh'):.6f} kWh（旧的全程75 kg值见审计副本字段 energy_uniform_75kg_original）。</p><p>模型路线 {comparison.get('move_count','—')} 步、{comparison.get('distance_m',0)/1000:.1f} km、目标时间 {comparison.get('objective_travel_time_sec',0):.1f} 秒；同端点无重复节点可行参考 {quality_reference.get('move_count','—')} 步、{quality_reference.get('distance_m',0)/1000:.1f} km、目标时间 {quality_reference.get('objective_travel_time_sec',0):.1f} 秒。参考固定第一段，不代表全局最优。</p><p>旧传统 task-0000-0009 的直接对照状态：{old_baseline.get('status','未知')}。{old_baseline_note}</p><p>被拒绝提议估值覆盖 {sidecar['estimated_count']}/{sidecar['rejected_proposal_count']} 条；这些数值是各请求提议的反事实估值，不是车辆实际消耗，也不能彼此相加。</p></html>"""
    (run_dir / "index.html").write_text(index, encoding="utf-8")
    return sidecar


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    args = parser.parse_args()
    result = reconcile(args.run_dir, args.preflight_dir)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "status",
                    "model_requests",
                    "rejected_proposal_count",
                    "estimated_count",
                    "not_estimable_count",
                    "first_rejected_action_energy_estimable_count",
                    "reason_counts",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
