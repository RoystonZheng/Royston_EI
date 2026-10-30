#!/usr/bin/env python3
"""Audit every completed R4 route segment against its frozen directed map.

This offline gate checks full start -> pickup -> dropoff flows for all
route_complete entries in the formal parent manifest. It validates every
accepted directed edge ID and does not change source routes or make model
requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parents[1]
SUPPORT_ROOT = Path("/Users/didi/Library/Application Support/CodexR4Formal20261027")
for item in (SUPPORT_ROOT, SUPPORT_ROOT / "scripts"):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from run_r4_codex_formal_checkpoint import load_map


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def final_audit_for(batch_dir: Path, task: dict[str, Any]) -> Path:
    output_dir = Path(task["output_dir"])
    if task.get("final_audit_path"):
        candidate = resolve_path(batch_dir, task["final_audit_path"])
        if candidate.is_file():
            return candidate
    candidate = output_dir / "final_audit.json"
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"final_audit_missing:{task.get('seed')}:{task.get('task_id')}")


def validate_completed_task(
    task: dict[str, Any], batch_dir: Path, project_root: Path
) -> dict[str, Any]:
    task_id = str(task["task_id"])
    seed = int(task["seed"])
    preflight_path = Path(task["preflight_path"])
    preflight_bytes = preflight_path.read_bytes()
    preflight_sha = sha256_bytes(preflight_bytes)
    if task.get("preflight_sha256") and preflight_sha != task["preflight_sha256"]:
        raise ValueError(f"preflight_sha256_mismatch:{seed}:{task_id}")
    preflight = json.loads(preflight_bytes)

    map_ref = preflight.get("map_ref") or {}
    map_path = resolve_path(project_root, str(map_ref["path"]))
    map_sha = sha256_file(map_path)
    if map_sha != map_ref.get("sha256"):
        raise ValueError(f"map_sha256_mismatch:{seed}:{task_id}")
    route_search = preflight.get("route_search") or {}
    map_equivalence = route_search.get("map_equivalence_audit") or {}
    if map_equivalence.get("passed") is not True:
        raise ValueError(f"map_equivalence_gate_failed:{seed}:{task_id}")

    final_path = final_audit_for(batch_dir, task)
    final_bytes = final_path.read_bytes()
    final_sha = sha256_bytes(final_bytes)
    final = json.loads(final_bytes)
    if final.get("status") != "completed_route":
        raise ValueError(f"final_route_not_complete:{seed}:{task_id}:{final.get('status')}")
    if final.get("source_preflight_sha256") != preflight_sha:
        raise ValueError(f"final_preflight_sha256_mismatch:{seed}:{task_id}")
    for field in ("pickup_reached", "dropoff_reached"):
        if final.get(field) is not True:
            raise ValueError(f"full_flow_endpoint_not_reached:{seed}:{task_id}:{field}")
    if final.get("repeated_nodes") is not False:
        raise ValueError(f"repeated_nodes_gate_failed:{seed}:{task_id}")
    if final.get("invalid_or_stopped_actions") not in ([], None):
        raise ValueError(f"invalid_or_stopped_actions:{seed}:{task_id}")
    if final.get("route_continuity_violations") not in ([], None):
        raise ValueError(f"route_continuity_violations:{seed}:{task_id}")

    start = str(final["start_node"])
    pickup = str(final["pickup_node"])
    dropoff = str(final["dropoff_node"])
    nodes = list(final.get("accepted_route_nodes") or [])
    edge_ids = list(final.get("accepted_route_edges") or [])
    if len(nodes) != len(edge_ids) + 1:
        raise ValueError(f"node_edge_length_mismatch:{seed}:{task_id}")
    if not nodes or nodes[0] != start or nodes[-1] != dropoff:
        raise ValueError(f"start_or_dropoff_mismatch:{seed}:{task_id}")
    if pickup not in nodes[:-1]:
        raise ValueError(f"pickup_not_before_dropoff:{seed}:{task_id}")
    if len(nodes) != len(set(nodes)):
        raise ValueError(f"repeated_node_sequence:{seed}:{task_id}")

    road_map = load_map(map_path)
    edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
    blocked = set(route_search.get("blocked_edge_ids") or [])
    for index, edge_id in enumerate(edge_ids):
        edge = edge_by_id.get(edge_id)
        if edge is None:
            raise ValueError(f"accepted_directed_edge_missing:{seed}:{task_id}:{index}:{edge_id}")
        if edge.from_node != nodes[index] or edge.to_node != nodes[index + 1]:
            raise ValueError(f"accepted_edge_direction_or_continuity_mismatch:{seed}:{task_id}:{index}:{edge_id}")
        if edge_id in blocked:
            raise ValueError(f"accepted_blocked_edge:{seed}:{task_id}:{index}:{edge_id}")

    pickup_index = nodes.index(pickup)
    return {
        "seed": seed,
        "task_id": task_id,
        "task_type": (preflight.get("task_public") or {}).get("task_type"),
        "map_id": road_map.map_id,
        "map_sha256": map_sha,
        "preflight_sha256": preflight_sha,
        "final_audit_path": str(final_path),
        "final_audit_sha256": final_sha,
        "start_node": start,
        "pickup_node": pickup,
        "dropoff_node": dropoff,
        "route_edge_count": len(edge_ids),
        "empty_leg_edge_count": pickup_index,
        "passenger_leg_edge_count": len(edge_ids) - pickup_index,
        "directed_edges_validated": len(edge_ids),
        "route_complete": True,
        "full_flow_valid": True,
        "no_repeated_nodes": True,
        "blocked_edges_used": 0,
    }


def audit(batch_dir: Path, project_root: Path, manifest_snapshot: Path) -> dict[str, Any]:
    manifest_path = batch_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest_sha = sha256_bytes(manifest_bytes)
    if manifest_snapshot.exists():
        raise FileExistsError(f"refusing_to_overwrite_manifest_snapshot:{manifest_snapshot}")
    manifest_snapshot.parent.mkdir(parents=True, exist_ok=True)
    manifest_snapshot.write_bytes(manifest_bytes)

    tasks = manifest.get("tasks", [])
    counts = Counter(task.get("status") for task in tasks)
    records = []
    failures = []
    for task in tasks:
        if task.get("status") != "route_complete":
            continue
        try:
            records.append(validate_completed_task(task, batch_dir, project_root))
        except Exception as error:
            failures.append({
                "seed": task.get("seed"),
                "task_id": task.get("task_id"),
                "error": f"{type(error).__name__}:{error}",
            })

    return {
        "schema_version": "r4_formal_all_completed_routes_directed_flow_audit_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_requests_made_for_audit": 0,
        "source_routes_modified": False,
        "parent_manifest_path": str(manifest_path),
        "parent_manifest_sha256": manifest_sha,
        "parent_manifest_snapshot_path": str(manifest_snapshot),
        "parent_task_status_counts": dict(counts),
        "route_complete_task_count": counts.get("route_complete", 0),
        "validated_completed_route_count": len(records),
        "directed_edge_count_validated": sum(row["directed_edges_validated"] for row in records),
        "full_flow_gate_passed": not failures and len(records) == counts.get("route_complete", 0),
        "failures": failures,
        "records": records,
        "interpretation": (
            "Each accepted edge ID was resolved on the same frozen directed map and checked against its exact "
            "from/to nodes. Every completed path was checked from start through pickup to dropoff. This is an "
            "offline integrity audit, not a model-performance or deployment claim."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-snapshot", type=Path)
    args = parser.parse_args()
    snapshot = args.manifest_snapshot or args.output.with_name(args.output.stem + "_parent_manifest_snapshot.json")
    result = audit(args.batch_dir.resolve(), args.project_root.resolve(), snapshot.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "audit": str(args.output.resolve()),
        "route_complete_task_count": result["route_complete_task_count"],
        "validated_completed_route_count": result["validated_completed_route_count"],
        "directed_edge_count_validated": result["directed_edge_count_validated"],
        "full_flow_gate_passed": result["full_flow_gate_passed"],
        "failures": result["failures"],
        "model_requests_made_for_audit": 0,
    }, ensure_ascii=False, indent=2))
    return 0 if result["full_flow_gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
