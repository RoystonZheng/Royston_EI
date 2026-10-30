#!/usr/bin/env python3
"""Offline EV2 sidecar for rejected Porto OSM R4 model route proposals.

Reads only completed source orders. It decodes each original model response
against that request's exact saved prompt, estimates energy on the proposed
directed Links, and writes a separate counterfactual sidecar. It does not call
the model, edit source runs, rerun route selection, or invoke SUMO.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.energy_model import (  # noqa: E402
    EnergyModelConfig,
    evaluate_route_edges,
    model_metadata,
)
from route_experiment.models import EvaluationConfig  # noqa: E402

ENERGY_MODULE = ROOT / "route_experiment/energy_model.py"
DEFAULT_SOURCE = ROOT / "outputs/route_layer/porto_osm_r4_v5_formal_20261029T1310CST"
DEFAULT_NETWORK = ROOT / "data/real_map_dispatch/porto_hybrid_v1/derived/sumo/v1.28.0/porto.net.xml"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def base36(value: str) -> int:
    return int(value, 36)


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def load_network(path: Path) -> dict[str, dict[str, Any]]:
    network: dict[str, dict[str, Any]] = {}
    for edge in ET.parse(path).getroot().findall("edge"):
        if edge.get("function") == "internal":
            continue
        lanes = edge.findall("lane")
        if not lanes:
            continue
        length = float(edge.get("length", lanes[0].get("length", "0")))
        speed = max(float(lane.get("speed", "0")) for lane in lanes)
        network[str(edge.get("id"))] = {
            "edge_id": str(edge.get("id")),
            "length_m": length,
            "base_speed_mps": speed,
            "congestion_level": 0,
            "signal_delay_sec": 0.0,
            "slope_rad": None,
        }
    return network


def decode_response_leg(task: dict[str, Any], response: dict[str, Any], role: str) -> tuple[dict[str, Any], list[str], int]:
    task_key, prefix = {
        "vehicle_to_pickup": ("vp", "v"),
        "pickup_to_dropoff": ("pd", "p"),
    }[role]
    leg = task["g"][task_key]
    rows = leg["l"].split("~")
    edge_ids = list(map(str, leg["e"]))
    if len(rows) != len(edge_ids):
        raise ValueError("prompt_link_rows_and_edge_ids_length_mismatch")
    route_rows = [row for row in response.get("routes", []) if row.get("leg_role") == role]
    if len(route_rows) != 1:
        raise ValueError("response_route_role_missing_or_duplicated")
    aliases = route_rows[0].get("intermediate_edge_ids")
    if not isinstance(aliases, list):
        raise ValueError("intermediate_edge_ids_not_a_list")
    indices = [base36(str(leg["a"][0]))]
    for alias in aliases:
        alias = str(alias)
        if not alias.startswith(prefix) or len(alias) != 5:
            raise ValueError("route_alias_role_or_width_mismatch:" + alias)
        indices.append(base36(alias[1:]))
    indices.append(base36(str(leg["b"][0])))
    if any(index < 0 or index >= len(edge_ids) for index in indices):
        raise ValueError("route_alias_index_outside_exact_prompt_graph")
    path = [edge_ids[index] for index in indices]

    successors: dict[int, set[int]] = {}
    for index, row in enumerate(rows):
        _cost, sep, targets = row.partition("|")
        if not sep:
            raise ValueError("prompt_link_successor_separator_missing")
        valid = set()
        if targets:
            for token in targets.split(";"):
                target_text = token.partition(".")[0]
                valid.add(base36(target_text))
        successors[index] = valid
    link_index = {edge_id: i for i, edge_id in enumerate(edge_ids)}
    illegal_pairs = []
    for step, (source, target) in enumerate(zip(path, path[1:]), start=1):
        si, ti = link_index[source], link_index[target]
        if ti not in successors[si]:
            illegal_pairs.append({
                "segment_index": step,
                "from_link": source,
                "to_link": target,
                "reason": "not_a_listed_directed_successor_in_exact_model_prompt",
            })
    return leg, path, len(illegal_pairs)


def clip_link_lengths(path: list[str], leg: dict[str, Any], network: dict[str, dict[str, Any]]) -> list[float]:
    if not path:
        raise ValueError("proposal_path_empty")
    lengths = [float(network[edge_id]["length_m"]) for edge_id in path]
    start_pos, end_pos = float(leg["a"][1]), float(leg["b"][1])
    if len(path) == 1:
        distance = end_pos - start_pos
        if distance < -1e-6 or distance > lengths[0] + 1e-6:
            raise ValueError("same_link_endpoint_offsets_not_estimable")
        return [max(0.0, distance)]
    if start_pos < 0 or start_pos > lengths[0] + 1e-6:
        raise ValueError("start_offset_outside_start_link")
    if end_pos < -1e-6 or end_pos > lengths[-1] + 1e-6:
        raise ValueError("end_offset_outside_end_link")
    result = list(lengths)
    result[0] = max(0.0, lengths[0] - start_pos)
    result[-1] = end_pos
    if any(value <= 0 for value in result[1:-1]):
        raise ValueError("proposal_contains_zero_length_interior_link")
    return result


def evaluate_counterfactual_leg(
    role: str,
    path: list[str],
    prompt_leg: dict[str, Any],
    network: dict[str, dict[str, Any]],
    profile: EnergyModelConfig,
    evaluation: EvaluationConfig,
    response_sha: str,
    prompt_sha: str,
    network_sha: str,
    run_id: str,
    order_id: str,
    first_failure: dict[str, Any] | None,
    illegal_turn_count: int,
    illegal_turn_examples: list[dict[str, Any]],
) -> dict[str, Any]:
    unknown = [edge_id for edge_id in path if edge_id not in network]
    if unknown:
        raise ValueError("proposal_links_missing_from_frozen_sumo_network:" + ",".join(unknown[:5]))
    distances = clip_link_lengths(path, prompt_leg, network)
    payload_kg = 0.0 if role == "vehicle_to_pickup" else float(profile.passenger_load_kg)
    per_link = []
    for index, (edge_id, distance) in enumerate(zip(path, distances)):
        if distance <= 1e-9:
            per_link.append({
                "occurrence_index": index,
                "edge_id": edge_id,
                "distance_m": 0.0,
                "drive_time_sec": 0.0,
                "propulsion_draw_kwh": 0.0,
                "regenerated_kwh": 0.0,
                "auxiliary_kwh": 0.0,
                "signed_net_kwh": 0.0,
                "missing_grade_steps": 0,
                "status": "zero_distance_endpoint_no_motion",
            })
            continue
        edge = dict(network[edge_id])
        edge["length_m"] = distance
        # Give even very short Links at least three intervals. This lets the
        # common motion model accelerate and brake inside a Link when it is
        # evaluated independently from rest.
        pieces = max(3, int(math.ceil(distance / 5.0)))
        spatial_step = distance / pieces
        estimate = evaluate_route_edges(
            [edge], evaluation.congestion_speed_multipliers, profile,
            payload_mass_kg=payload_kg,
            initial_speed_mps=0.0,
            terminal_stop=True,
            spatial_step_m=spatial_step,
        )
        per_link.append({
            "occurrence_index": index,
            "edge_id": edge_id,
            "distance_m": distance,
            "drive_time_sec": estimate.drive_time_sec,
            "propulsion_draw_kwh": estimate.propulsion_draw_kwh,
            "regenerated_kwh": estimate.regenerated_kwh,
            "auxiliary_kwh": estimate.auxiliary_kwh,
            "signed_net_kwh": estimate.net_kwh,
            "missing_grade_steps": estimate.missing_grade_steps,
        })
    propulsion = sum(float(row["propulsion_draw_kwh"]) for row in per_link)
    regenerated = sum(float(row["regenerated_kwh"]) for row in per_link)
    auxiliary = sum(float(row["auxiliary_kwh"]) for row in per_link)
    signed_net = propulsion - regenerated + auxiliary
    return {
        "schema_version": "porto_r4_rejected_model_route_ev2_counterfactual_v1",
        "status": "counterfactual_candidate_estimated_from_mapped_model_route",
        "scope": "complete raw model proposal; rejected one-way turns were not executed; hypothetical energy is not SUMO or vehicle-consumed energy",
        "experiment_family": "R4_real_OSM_route_layer",
        "map_type": "real OSM-derived Porto SUMO network",
        "layer": "route planning before vehicle execution",
        "run_id": run_id,
        "order_id": order_id,
        "leg_role": role,
        "payload_mass_kg": payload_kg,
        "model_route_edge_count": len(path),
        "unique_link_count": len(set(path)),
        "repeated_edge_occurrences_in_full_path": len(path) - len(set(path)),
        "proposal_path_sha256": canonical_sha256(path),
        "first_rejected_directed_segment": first_failure,
        "illegal_directed_turn_count_in_full_proposal": illegal_turn_count,
        "illegal_directed_turn_examples": illegal_turn_examples,
        "executed": False,
        "route_replay_or_sumo_run": False,
        "model_calls": 0,
        "ev2": {
            "model_version": profile.model_version,
            "energy_module_sha256": sha256_file(ENERGY_MODULE),
            "profile_id": profile.profile_id,
            "profile_sha256": profile.fingerprint(),
            "trace_fidelity": "estimated_from_mapped_route_edges_not_observed_trace",
            "distance_m": sum(distances),
            "drive_time_sec": sum(float(row["drive_time_sec"]) for row in per_link),
            "propulsion_draw_kwh": propulsion,
            "regenerated_kwh": regenerated,
            "auxiliary_kwh": auxiliary,
            "signed_net_kwh": signed_net,
            "billable_kwh_for_this_journey_segment": max(signed_net, 0.0),
            "accounting": "sum signed per-Link EV2 within this journey segment, then max(segment_signed_net_kwh, 0); empty payload 0kg, passenger payload 75kg",
            "missing_grade_steps": sum(int(row["missing_grade_steps"]) for row in per_link),
            "per_link": per_link,
        },
        "assumptions": [
            "each Link occurrence independently starts from rest and stops",
            "free-flow SUMO speed cap; zero congestion",
            "zero traffic-signal dwell and no pickup dwell",
            "flat grade when SUMO network has no grade input",
            "illegal turn motion is not simulated; link energy is a counterfactual estimate",
            "fixed endpoint Link distances are clipped using the exact request offsets",
        ],
        "evidence": {
            "response_sha256": response_sha,
            "request_prompt_sha256": prompt_sha,
            "exact_prompt_leg_sha256": canonical_sha256(prompt_leg),
            "sumo_network_sha256": network_sha,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--network", type=Path, default=DEFAULT_NETWORK)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    network_path = args.network.resolve()
    output_dir = args.output_dir.resolve()
    source_manifest_path = source_root / "formal_manifest.json"
    parent_manifest = read_json(source_manifest_path)
    parent_manifest_sha = sha256_file(source_manifest_path)
    parent_tasks = [row for row in parent_manifest.get("tasks", []) if row.get("status") == "completed_pass"]
    network = load_network(network_path)
    network_sha = sha256_file(network_path)
    profile = EnergyModelConfig()
    evaluation = EvaluationConfig()

    results = []
    counters: Counter[str] = Counter()
    completed_orders = []
    for task_row in parent_tasks:
        order_id = str(task_row.get("order_id") or task_row.get("task_id") or "")
        run_dir = source_root / "tasks" / order_id
        child_manifest_path = run_dir / "manifest.json"
        audit_path = run_dir / "pre_sumo_oneway_segment_audit.json"
        response_path = run_dir / "codex_requests/call_0000.final.json"
        prompt_path = run_dir / "codex_requests/call_0000.prompt.txt"
        if not all(path.is_file() for path in (child_manifest_path, audit_path, response_path, prompt_path)):
            counters["completed_source_missing_required_artifact"] += 1
            continue
        child = read_json(child_manifest_path)
        if child.get("status") != "pilot_route_complete":
            counters["completed_source_child_status_mismatch"] += 1
            continue
        audit = read_json(audit_path)
        response = read_json(response_path)
        prompt = prompt_path.read_text(encoding="utf-8")
        try:
            request_task = json.loads(prompt[prompt.rfind("\n") + 1 :])
        except Exception:
            counters["actual_prompt_task_decode_failed"] += 1
            continue
        response_sha, prompt_sha = sha256_file(response_path), sha256_file(prompt_path)
        runtime_request_path = run_dir / "preflight/runtime_request.json"
        if runtime_request_path.is_file():
            runtime_request = read_json(runtime_request_path)
            if runtime_request.get("prompt_sha256") != prompt_sha:
                counters["actual_prompt_sha256_mismatch"] += 1
                continue
        if audit.get("source_network_sha256") != network_sha:
            counters["frozen_network_sha256_mismatch"] += 1
            continue
        preflight_summary_path = run_dir / "preflight/summary.json"
        if preflight_summary_path.is_file():
            preflight_summary = read_json(preflight_summary_path)
            energy_source = preflight_summary.get("energy_module", {})
            if energy_source.get("source_sha256") != sha256_file(ENERGY_MODULE):
                counters["energy_module_sha256_mismatch"] += 1
                continue
            if energy_source.get("profile_sha256") != profile.fingerprint():
                counters["energy_profile_sha256_mismatch"] += 1
                continue
        completed_orders.append(order_id)
        legs = audit.get("model_proposal_and_repair_checks", {})
        for role, audit_leg in legs.items():
            if audit_leg.get("proposal_status") != "rejected_at_directed_segment_and_researched":
                continue
            counters["rejected_model_route_legs"] += 1
            try:
                prompt_leg, path, illegal_count = decode_response_leg(request_task, response, role)
                failure = dict(audit_leg.get("rejected_segment") or {})
                all_illegal = []
                # Re-derive every illegal turn from the exact graph encoded in
                # the saved model prompt, independent of the repaired route.
                rows = prompt_leg["l"].split("~")
                edge_ids = list(map(str, prompt_leg["e"]))
                row_index = {edge_id: index for index, edge_id in enumerate(edge_ids)}
                successors = []
                for row in rows:
                    _cost, _sep, encoded = row.partition("|")
                    successors.append({base36(token.partition(".")[0]) for token in encoded.split(";") if token})
                for step, (source, target) in enumerate(zip(path, path[1:]), start=1):
                    if row_index[target] not in successors[row_index[source]]:
                        all_illegal.append({"segment_index": step, "from_link": source, "to_link": target})
                if illegal_count != len(all_illegal):
                    raise ValueError("decoded_illegal_turn_count_mismatch")
                if failure and all_illegal:
                    if (failure.get("from_link"), failure.get("to_link"), failure.get("segment_index")) != (
                        all_illegal[0]["from_link"], all_illegal[0]["to_link"], all_illegal[0]["segment_index"]
                    ):
                        raise ValueError("pre_sumo_first_rejection_does_not_match_saved_model_prompt")
                failure = {
                    key: failure.get(key)
                    for key in ("segment_index", "from_link", "to_link", "reason")
                }
                result = evaluate_counterfactual_leg(
                    role, path, prompt_leg, network, profile, evaluation,
                    response_sha, prompt_sha, network_sha,
                    str(child.get("run_id", run_dir.name)), order_id, failure,
                    len(all_illegal), all_illegal[:5],
                )
                result["source_child_manifest_sha256"] = sha256_file(child_manifest_path)
                result["source_pre_sumo_audit_sha256"] = sha256_file(audit_path)
                result["model_proposal_routes_repaired_count"] = child.get("model_proposal_routes_repaired_count")
                results.append(result)
                counters["counterfactual_energy_estimated"] += 1
                counters["illegal_directed_turns_rederived"] += len(all_illegal)
            except Exception as exc:
                counters["counterfactual_energy_not_estimable"] += 1
                results.append({
                    "schema_version": "porto_r4_rejected_model_route_ev2_counterfactual_v1",
                    "status": "not_estimable",
                    "experiment_family": "R4_real_OSM_route_layer",
                    "map_type": "real OSM-derived Porto SUMO network",
                    "layer": "route planning before vehicle execution",
                    "run_id": str(child.get("run_id", run_dir.name)),
                    "order_id": order_id,
                    "leg_role": role,
                    "executed": False,
                    "route_replay_or_sumo_run": False,
                    "model_calls": 0,
                    "failure_reason": f"{type(exc).__name__}:{exc}",
                    "energy_kwh": None,
                    "source_child_manifest_sha256": sha256_file(child_manifest_path),
                    "source_pre_sumo_audit_sha256": sha256_file(audit_path),
                    "response_sha256": response_sha,
                    "request_prompt_sha256": prompt_sha,
                })

    output_dir.mkdir(parents=True, exist_ok=True)
    rows_path = output_dir / "rejected_model_route_legs_ev2.jsonl"
    rows_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in results), encoding="utf-8")
    rows_sha = sha256_file(rows_path)
    summary = {
        "schema_version": "porto_r4_rejected_model_route_ev2_counterfactual_manifest_v1",
        "status": "completed_offline_counterfactual_recalculation" if counters["counterfactual_energy_not_estimable"] == 0 else "completed_with_not_estimable_rows",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "experiment_family": "R4_real_OSM_route_layer",
        "map_type": "real OSM-derived Porto SUMO network",
        "layer": "route planning before vehicle execution",
        "source_root": str(source_root),
        "source_parent_manifest_sha256": parent_manifest_sha,
        "source_parent_status": parent_manifest.get("status"),
        "source_completed_order_count": len(parent_tasks),
        "completed_orders_with_audit_and_model_response": len(completed_orders),
        "completed_order_ids_sha256": canonical_sha256(sorted(completed_orders)),
        "counters": dict(counters),
        "energy_model": model_metadata(profile),
        "energy_module_sha256": sha256_file(ENERGY_MODULE),
        "recalculation_script_path": str(Path(__file__).resolve()),
        "recalculation_script_sha256": sha256_file(Path(__file__).resolve()),
        "sumo_network_path": str(network_path),
        "sumo_network_sha256": network_sha,
        "row_file": rows_path.name,
        "row_file_sha256": rows_sha,
        "rows": len(results),
        "total_billable_kwh_is_not_aggregated": True,
        "method_limitations": [
            "counterfactual energy for rejected proposals is not executed-route energy or a feasible candidate score",
            "each Link occurrence is evaluated independently from rest; illegal turn motion is omitted",
            "free-flow speed caps, zero congestion and zero signal dwell are assumed",
            "grade is unavailable in this network file and is treated as flat by EV2 while missing-grade steps are counted",
            "pickup dwell and auxiliary energy outside the route edge sequence are not included",
        ],
        "model_calls": 0,
        "sumo_runs": 0,
        "source_runs_modified": False,
    }
    dump_json(output_dir / "manifest.json", summary)
    print(json.dumps({"output_dir": str(output_dir), "status": summary["status"], "source_completed": len(parent_tasks), "counters": dict(counters), "rows": len(results), "row_sha256": rows_sha}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
