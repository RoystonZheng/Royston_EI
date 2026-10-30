#!/usr/bin/env python3
"""Recalculate saved pure-LLM grid proposal energy by frozen pickup phase.

This is an offline accounting sidecar. It preserves the original experiment
and earlier energy audit, makes no model calls, and does not rerun planning.
"""

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dataclasses import replace  # noqa: E402
from route_experiment.energy_model import EnergyModelConfig, evaluate_route_edges, model_metadata  # noqa: E402
from route_experiment.failure_audit import recover_partial_move_sequence  # noqa: E402
from route_experiment.llm_adapter import replay_move_sequence  # noqa: E402
from route_experiment.models import Edge, EvaluationConfig  # noqa: E402
from scripts.audit_pure_llm_failures import _route_paths, _task_lookup  # noqa: E402
from scripts.recalculate_pure_llm_grid_energy_v2 import load_map, read_json, sha256  # noqa: E402

DEFAULT_SOURCE = ROOT / "outputs/route_layer/pure_llm_experiment_v7"
DEFAULT_PRIOR = ROOT / "outputs/route_layer/pure_llm_grid_energy_v2_recalculation_20261029T0630UTC/pure_llm_experiment_v7/failure_energy_audit_v2.json"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/pure_llm_grid_energy_v2_payload_phase_recalculation_20261030_v2"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def get_move_sequence(method: Dict[str, Any]) -> Tuple[str, bool]:
    metadata = method.get("metadata") or {}
    raw = method.get("raw_output") or {}
    seq = metadata.get("move_sequence") or raw.get("move_sequence") or ""
    partial = False
    if not seq and method.get("failure_reason") == "llm_output_parse_failed":
        seq = metadata.get("recovered_partial_move_sequence") or recover_partial_move_sequence(raw)
        partial = bool(seq)
    return str(seq), partial


def hypothetical_edges(road_map: Any, route_nodes: List[str]) -> Tuple[List[Edge], Dict[str, int]]:
    edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
    lengths = [edge.length_m for edge in road_map.edges]
    fallback_length = statistics.median(lengths) if lengths else 100.0
    fallback_congestion = statistics.median([edge.congestion_level for edge in road_map.edges]) if road_map.edges else 0
    fallback_speed = statistics.median([edge.base_speed_mps for edge in road_map.edges]) if road_map.edges else 10.0
    fallback_signal = statistics.median([edge.signal_delay_sec for edge in road_map.edges]) if road_map.edges else 0.0
    counts = {"exact": 0, "reverse_inferred": 0, "proxy": 0}
    result = []
    for source, target in zip(route_nodes, route_nodes[1:]):
        edge_id = "edge_{0}_{1}".format(source, target)
        edge = edge_by_id.get(edge_id)
        if edge is not None:
            result.append(edge)
            counts["exact"] += 1
            continue
        reverse = edge_by_id.get("edge_{0}_{1}".format(target, source))
        if reverse is not None:
            result.append(replace(reverse, edge_id="reverse_inferred_" + reverse.edge_id,
                                  from_node=reverse.to_node, to_node=reverse.from_node,
                                  slope_rad=-reverse.slope_rad if reverse.slope_rad is not None else None))
            counts["reverse_inferred"] += 1
            continue
        def xy(node_id: str) -> Optional[Tuple[int, int]]:
            try:
                _, x, y = node_id.split("_")
                return int(x), int(y)
            except (ValueError, AttributeError):
                return None
        a, b = xy(source), xy(target)
        distance = fallback_length * max(1, abs(a[0] - b[0]) + abs(a[1] - b[1])) if a and b else fallback_length
        result.append(Edge(edge_id="hypothetical_proxy_" + str(len(result)), from_node=source, to_node=target,
                           length_m=distance, base_speed_mps=fallback_speed,
                           congestion_level=int(fallback_congestion), signal_delay_sec=fallback_signal))
        counts["proxy"] += 1
    return result, counts


def pickup_boundary(nodes: List[str], pickup: Optional[str]) -> Optional[int]:
    if not pickup:
        return None
    try:
        return nodes.index(str(pickup))
    except ValueError:
        return None


def account_edges(edges: List[Edge], boundary: Optional[int], config: EvaluationConfig) -> Dict[str, Any]:
    """Boundary is a node index, equal to the number of pre-pickup edges."""
    if boundary is None:
        return {"status": "pickup_boundary_unavailable", "empty_net_kwh": None,
                "passenger_net_kwh": None, "billable_kwh": None}
    split = max(0, min(int(boundary), len(edges)))
    empty = evaluate_route_edges(edges[:split], config.congestion_speed_multipliers,
                                 config.energy_profile, payload_mass_kg=0.0)
    passenger = evaluate_route_edges(edges[split:], config.congestion_speed_multipliers,
                                     config.energy_profile,
                                     payload_mass_kg=float(config.energy_profile.passenger_load_kg))
    return {
        "status": "phase_recalculated",
        "empty_edge_count": split,
        "passenger_edge_count": len(edges) - split,
        "empty_net_kwh": float(empty.net_kwh),
        "passenger_net_kwh": float(passenger.net_kwh),
        "total_signed_net_kwh": float(empty.net_kwh + passenger.net_kwh),
        "empty_billable_kwh": float(max(empty.net_kwh, 0.0)),
        "passenger_billable_kwh": float(max(passenger.net_kwh, 0.0)),
        "billable_kwh": float(max(empty.net_kwh, 0.0) + max(passenger.net_kwh, 0.0)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--prior-audit", type=Path, default=DEFAULT_PRIOR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    source, prior, output = args.source.resolve(), args.prior_audit.resolve(), args.output.resolve()
    if output.exists():
        raise SystemExit("refusing_to_overwrite_existing_output:" + str(output))
    source_manifest_path = source / "manifest.json"
    source_manifest = read_json(source_manifest_path)
    prior_payload = read_json(prior)
    prior_by_key = {(r.get("seed"), r.get("scope"), r.get("source_task_id")): r for r in prior_payload.get("records", [])}
    config = EvaluationConfig(energy_profile=EnergyModelConfig())
    counters = Counter()
    records = []
    source_files = [
        {"path": str(source_manifest_path), "sha256": sha256(source_manifest_path)},
        {"path": str(prior), "sha256": sha256(prior)},
        {"path": str(ROOT / "route_experiment/energy_model.py"), "sha256": sha256(ROOT / "route_experiment/energy_model.py")},
        {"path": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__).resolve())},
    ]
    for seed_dir in sorted((source / "seeds").glob("seed_*"), key=lambda p: int(p.name.split("_")[1])):
        seed = int(seed_dir.name.split("_")[1])
        map_path = seed_dir / ("map_100x100_seed_{0}.json".format(seed))
        road_map = load_map(map_path)
        task_lookup = _task_lookup(seed_dir)
        order_path = next(seed_dir.glob("order_tasks_seed_*.json"), None)
        if order_path:
            order_data = read_json(order_path)
            truth = {str(row.get("task_id")): row for row in order_data.get("ground_truth", [])}
            for task_id, task in task_lookup.items():
                if task_id in truth:
                    task.update(truth[task_id])
        source_files.append({"path": str(map_path), "sha256": sha256(map_path)})
        for task_path in list(seed_dir.glob("*_tasks_seed_*.json")) + list(seed_dir.glob("formal_scenarios_seed_*.json")):
            source_files.append({"path": str(task_path), "sha256": sha256(task_path)})
        for scope, routes_path in _route_paths(seed_dir):
            if routes_path is None:
                continue
            source_files.append({"path": str(routes_path), "sha256": sha256(routes_path)})
            payload = read_json(routes_path)
            for item in payload.get("results", []):
                task_id = str(item.get("task_id"))
                task = task_lookup.get(task_id, {})
                pickup = task.get("true_pickup_node_id") or task.get("pickup_node_id")
                methods = item.get("methods", [])
                for method in methods:
                    if method.get("method_name") != "pure_llm_deepseek":
                        continue
                    counters["records"] += 1
                    seq, partial = get_move_sequence(method)
                    prior_rec = prior_by_key.get((seed, scope, task_id), {})
                    prior_audit = prior_rec.get("proposal_energy_audit_v2") or {}
                    base = {
                        "seed": seed, "scope": scope, "task_id": task_id,
                        "failure_reason": method.get("failure_reason"),
                        "method_status": method.get("status"),
                        "response_received": bool((method.get("metadata") or {}).get("llm_response_received", method.get("raw_output") is not None)),
                        "frozen_pickup_node_id": pickup,
                        "model_declared_pickup_node_id": ((method.get("metadata") or {}).get("semantic_resolution") or {}).get("pickup_node_id") or (method.get("raw_output") or {}).get("pickup_node_id"),
                        "proposal_move_count": len(seq), "partial_recovery": partial,
                        "prior_fixed_load_proposal_energy_kwh": prior_audit.get("route_energy_estimate_kwh"),
                        "prior_fixed_load_legal_prefix_energy_kwh": prior_audit.get("validated_prefix_energy_estimate_kwh"),
                        "hypothetical_only": True,
                    }
                    if not seq or not task.get("start_node_id"):
                        base.update({"status": "no_recoverable_route", "proposal": None, "legal_prefix": None,
                                     "energy_not_estimable_reason": "no_recoverable_move_sequence_or_task_start"})
                        counters["not_estimable"] += 1
                        records.append(base)
                        continue
                    replay = replay_move_sequence(road_map, task["start_node_id"], seq,
                                                  blocked_edge_ids=task.get("blocked_edge_ids", []))
                    proposal_nodes = list(replay.get("attempted_route_node_sequence", []))
                    proposal_edges, edge_counts = hypothetical_edges(road_map, proposal_nodes)
                    prefix_nodes = list(replay.get("validated_prefix_node_sequence", []))
                    edge_by_id = {e.edge_id: e for e in road_map.edges}
                    prefix_edges = [edge_by_id[eid] for eid in replay.get("validated_prefix_edge_sequence", []) if eid in edge_by_id]
                    proposal_boundary = pickup_boundary(proposal_nodes, pickup)
                    prefix_boundary = pickup_boundary(prefix_nodes, pickup)
                    proposal_reaches_pickup = proposal_boundary is not None
                    prefix_reaches_pickup = prefix_boundary is not None
                    # If a returned path ends before frozen pickup, every edge
                    # it did traverse is still the empty deadhead phase.
                    if pickup and proposal_boundary is None:
                        proposal_boundary = max(0, len(proposal_nodes) - 1)
                    if pickup and prefix_boundary is None:
                        prefix_boundary = max(0, len(prefix_nodes) - 1)
                    proposal_metrics = account_edges(proposal_edges, proposal_boundary, config)
                    prefix_metrics = account_edges(prefix_edges, prefix_boundary, config)
                    model_pickup = base["model_declared_pickup_node_id"]
                    base.update({
                        "status": "phase_recalculated" if pickup else "pickup_truth_unavailable",
                        "pickup_source": "frozen_order_ground_truth_or_formal_task" if pickup else None,
                        "proposal_reaches_frozen_pickup": proposal_reaches_pickup,
                        "proposal_pickup_node_index": pickup_boundary(proposal_nodes, pickup),
                        "legal_prefix_reaches_frozen_pickup": prefix_reaches_pickup,
                        "legal_prefix_pickup_node_index": pickup_boundary(prefix_nodes, pickup),
                        "model_pickup_matches_frozen_truth": (model_pickup == pickup) if model_pickup and pickup else None,
                        "proposal_edge_provenance_counts": edge_counts,
                        "proposal": proposal_metrics,
                        "legal_prefix": prefix_metrics,
                        "interpretation": (
                            "Proposal values are hypothetical and not vehicle consumption. Exact map edges, reverse-edge proxies, and median-map proxies are counted separately. "
                            "When the proposal does not reach frozen pickup, its traversed segment is treated as empty only; no passenger-phase traversal is imputed."
                        ),
                    })
                    counters["phase_estimable"] += bool(pickup and proposal_metrics["status"] == "phase_recalculated")
                    counters["pickup_reached"] += bool(pickup and proposal_reaches_pickup)
                    counters["prefix_pickup_reached"] += bool(pickup and prefix_reaches_pickup)
                    counters["pickup_truth_missing"] += not bool(pickup)
                    counters["not_estimable"] += not bool(pickup)
                    counters["exact_edge_moves"] += edge_counts["exact"]
                    counters["reverse_edge_inferred_moves"] += edge_counts["reverse_inferred"]
                    counters["proxy_edge_moves"] += edge_counts["proxy"]
                    records.append(base)
    output.mkdir(parents=True)
    index = output / "phase_energy_records.json"
    write_json(index, records)
    manifest = {
        "schema_version": "pure_llm_grid_ev_v2_payload_phase_recalculation_v1",
        "status": "completed_offline_recalculation",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_experiment": str(source), "source_manifest_sha256": sha256(source_manifest_path),
        "prior_fixed_load_audit": str(prior), "prior_audit_sha256": sha256(prior),
        "source_files": source_files,
        "model_calls": 0, "route_planner_reruns": 0,
        "energy_model": model_metadata(config.energy_profile),
        "energy_model_module_sha256": sha256(ROOT / "route_experiment/energy_model.py"),
        "payload_phases": {"empty_kg": 0.0, "passenger_kg": float(config.energy_profile.passenger_load_kg)},
        "billing_rule": "max(segment_signed_net_kwh, 0) independently for empty and passenger phases",
        "record_count": len(records), "coverage": dict(counters),
        "records_path": str(index), "records_sha256": sha256(index),
        "limitations": [
            "All proposal and legal-prefix energy values are offline EV v2 simulations, not measured vehicle telemetry.",
            "Frozen task pickup is the phase boundary; model-declared pickup is retained for discrepancy audit only.",
            "Illegal moves may use reverse-edge or median-map proxies; these are hypothetical values, never executable route costs.",
            "Missing pickup truth or unrecoverable route leaves phase energy null, not zero.",
            "This sidecar does not alter historic route choices or claim the original experiment selected routes using these energy values.",
        ],
    }
    manifest_path = output / "manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps({"manifest": str(manifest_path), "manifest_sha256": sha256(manifest_path),
                      "records": len(records), "coverage": dict(counters)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
