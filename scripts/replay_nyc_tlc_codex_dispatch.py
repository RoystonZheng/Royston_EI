#!/usr/bin/env python3
"""Replay a D5 simulation from already captured Codex CLI final responses.

This script performs no model calls. It is intended to recover a compact,
auditable simulation result when a live run has saved all CLI traces but did
not finish writing its aggregate result file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dispatch_experiment.llm import (  # noqa: E402
    CODEX_DIRECT_ASSIGNMENT_INPUT_POLICY,
    CodexCliDirectAssignment,
    build_direct_assignment_messages,
)
from dispatch_experiment.models import DispatchConfig  # noqa: E402
from dispatch_experiment.nyc_tlc_scenario import (  # noqa: E402
    NYCTLCZoneCentroidPickupOracle,
    load_dispatch_scenario,
)
from dispatch_experiment.runner import DispatchSimulation  # noqa: E402


PROMPT_SUFFIX = (
    "\n\n仅依据下列当前快照和候选表做一次直接匹配。不要调用工具，不要读取或修改文件，"
    "不要执行命令。只返回符合 JSON Schema 的对象。\n\n"
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class CapturedCodexReplay:
    """Use saved final JSON/JSONL files in order, with no external calls."""

    backend = "codex_cli"

    def __init__(self, trace_dir: Path, model: str):
        self.trace_dir = trace_dir.resolve()
        self.model = model
        self.rows = []
        for trace_path in sorted(self.trace_dir.glob("dispatch_round_*.jsonl")):
            if trace_path.name.endswith(".final.jsonl"):
                continue
            final_path = trace_path.with_name(trace_path.stem + ".final.json")
            if not final_path.is_file():
                raise FileNotFoundError(final_path)
            events = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            audit = CodexCliDirectAssignment._audit_events(events)
            if audit["tool_usage_detected"]:
                raise ValueError("captured response includes a tool event: " + trace_path.name)
            final_bytes = final_path.read_bytes()
            final_payload = json.loads(final_bytes)
            if not isinstance(final_payload, dict):
                raise ValueError("captured final response is not a JSON object: " + final_path.name)
            self.rows.append(
                {
                    "trace_path": trace_path,
                    "final_path": final_path,
                    "trace_bytes": trace_path.read_bytes(),
                    "final_bytes": final_bytes,
                    "events": events,
                    "audit": audit,
                    "payload": final_payload,
                }
            )
        self.index = 0
        self.call_count = 0
        self.input_tokens = 0
        self.cached_input_tokens = 0
        self.output_tokens = 0
        self.usage_call_count = 0
        self.input_usage_call_count = 0
        self.cached_input_usage_call_count = 0
        self.output_usage_call_count = 0
        self.unknown_cost_count = 0
        self.api_cost_usd = None
        self.latency_ms = 0.0
        self.errors: List[str] = []
        self.last_record: Dict[str, Any] = {"status": "not_called"}

    def select(
        self,
        *,
        snapshot_time_sec: float,
        vehicles: Sequence[Any],
        orders: Sequence[Any],
        candidates: Sequence[Any],
        max_wait_sec: float,
    ) -> Tuple[List[Any], Dict[str, Any]]:
        feasible = {
            item.candidate_id: item
            for item in candidates
            if item.hard_constraint_ok
        }
        if not feasible:
            record = {
                "status": "no_feasible_candidates",
                "backend": self.backend,
                "model": self.model,
                "candidate_input_policy": CODEX_DIRECT_ASSIGNMENT_INPUT_POLICY,
                "raw_candidate_count": len(candidates),
                "prompt_candidate_count": 0,
                "invocation_made": False,
                "request_made": False,
                "account_cost_usd": None,
                "cost_status": "no_request",
            }
            self.last_record = record
            return [], record

        if self.index >= len(self.rows):
            raise RuntimeError("simulation requested more Codex responses than were captured")
        row = self.rows[self.index]
        self.index += 1
        self.call_count += 1
        payload = row["payload"]
        ids = payload.get("selected_candidate_ids")
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise ValueError("captured response has invalid selected_candidate_ids")
        if len(ids) != len(set(ids)):
            raise ValueError("captured response repeats a candidate ID")
        selected = []
        used_vehicles = set()
        used_orders = set()
        for candidate_id in ids:
            candidate = feasible.get(candidate_id)
            if candidate is None:
                raise ValueError("captured response selected a non-feasible candidate")
            if candidate.vehicle_id in used_vehicles or candidate.order_id in used_orders:
                raise ValueError("captured response violates one-to-one matching")
            used_vehicles.add(candidate.vehicle_id)
            used_orders.add(candidate.order_id)
            selected.append(candidate)

        messages = build_direct_assignment_messages(
            snapshot_time_sec=snapshot_time_sec,
            vehicles=vehicles,
            orders=orders,
            candidates=sorted(feasible.values(), key=lambda item: item.candidate_id),
            max_wait_sec=max_wait_sec,
        )
        prompt = messages[0]["content"] + PROMPT_SUFFIX + messages[1]["content"]
        usage = row["audit"]
        if usage["usage_available"]:
            self.usage_call_count += 1
            if usage["input_tokens"] is not None:
                self.input_tokens += usage["input_tokens"]
                self.input_usage_call_count += 1
            if usage["cached_input_tokens"] is not None:
                self.cached_input_tokens += usage["cached_input_tokens"]
                self.cached_input_usage_call_count += 1
            if usage["output_tokens"] is not None:
                self.output_tokens += usage["output_tokens"]
                self.output_usage_call_count += 1
        else:
            self.unknown_cost_count += 1
        record = {
            "status": "accepted",
            "backend": self.backend,
            "model": self.model,
            "candidate_input_policy": CODEX_DIRECT_ASSIGNMENT_INPUT_POLICY,
            "raw_candidate_count": len(candidates),
            "prompt_candidate_count": len(feasible),
            "codex_cli_version": "codex-cli 0.145.0",
            "reasoning_effort": "low",
            "invocation_made": True,
            "request_made": True,
            "retry_count": 0,
            "latency_ms": 0.0,
            "latency_status": "not_captured_before_original_run_interruption",
            "input_tokens": usage["input_tokens"],
            "cached_input_tokens": usage["cached_input_tokens"],
            "output_tokens": usage["output_tokens"],
            "reasoning_tokens": usage["reasoning_tokens"],
            "total_tokens": usage["total_tokens"],
            "usage_status": "available_best_effort" if usage["usage_available"] else "missing",
            "usage_source": "captured_codex_exec_json_turn_completed_event",
            "tool_usage_detected": usage["tool_usage_detected"],
            "tool_item_types": usage["tool_item_types"],
            "event_types": usage["event_types"],
            "account_cost_usd": None,
            "cost_status": "account_plan_or_credits_not_exposed_by_cli",
            "prompt_sha256_reconstructed": sha256_bytes(prompt.encode("utf-8")),
            "trace_jsonl_path": str(row["trace_path"]),
            "trace_jsonl_sha256": sha256_bytes(row["trace_bytes"]),
            "final_json_path": str(row["final_path"]),
            "final_json_sha256": sha256_bytes(row["final_bytes"]),
            "replay_source": "captured_live_codex_response",
        }
        self.last_record = record
        return selected, record


def scenario_fingerprint(road_map: Any, orders: Sequence[Any], vehicles: Sequence[Any]) -> str:
    payload = {
        "map": road_map.to_dict(),
        "orders": [order.to_dict() for order in orders],
        "vehicles": [vehicle.to_dict() for vehicle in vehicles],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sha256_bytes(serialized)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default="outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1")
    parser.add_argument("--case", required=True)
    parser.add_argument("--fleet-id", required=True)
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--model", default="gpt-5.6-luna")
    args = parser.parse_args()

    package = (ROOT / args.package).resolve()
    manifest = json.loads((package / "benchmark_manifest.json").read_text(encoding="utf-8"))
    pickup_policy = json.loads((package / "pickup_cost_policy.json").read_text(encoding="utf-8"))
    zone_payload = json.loads((package / "zone_centroids.json").read_text(encoding="utf-8"))
    zone_centroids = {
        int(zone_id): (float(point["lon"]), float(point["lat"]))
        for zone_id, point in zone_payload.items()
    }
    case = manifest["cases"][args.case]
    case_dir = package / case["path"]
    loaded = load_dispatch_scenario(
        case_dir,
        case_dir / "zone_od_cost_matrix.csv.gz",
        fleet_id=args.fleet_id,
    )
    controls = loaded["scenario"].get("dispatch_controls", {})
    config = DispatchConfig(
        map_size=loaded["road_map"].size,
        vehicle_count=len(loaded["vehicles"]),
        order_count=len(loaded["orders"]),
        horizon_sec=int(controls.get("horizon_sec", 3600)),
        max_pickup_wait_sec=float(controls.get("max_pickup_wait_sec", 300.0)),
        passenger_no_show_rate=float(controls.get("passenger_no_show_rate", 0.0)),
        method_name="D5_pure_llm_direct",
        feedback_mode="none",
        llm_backend="codex_cli",
        llm_model=args.model,
        seed=args.seed,
    )
    trace_dir = (ROOT / args.trace_dir).resolve()
    simulation = DispatchSimulation(
        config,
        road_map=loaded["road_map"],
        orders=loaded["orders"],
        vehicles=loaded["vehicles"],
        retain_round_records=False,
        route_oracle=NYCTLCZoneCentroidPickupOracle(
            loaded["road_map"],
            loaded["od_costs"],
            zone_centroids,
            speed_mps=float(pickup_policy["speed_mps"]),
            detour_factor=float(pickup_policy["detour_factor"]),
            minimum_intrazonal_distance_m=float(pickup_policy["minimum_intrazonal_distance_m"]),
        ),
        llm_trace_dir=trace_dir,
    )
    replay = CapturedCodexReplay(trace_dir, args.model)
    simulation.direct_llm = replay
    summary, _ = simulation.run()
    if replay.index != len(replay.rows):
        raise RuntimeError(
            "replay consumed {0} of {1} captured Codex response(s)".format(
                replay.index, len(replay.rows)
            )
        )

    round_audits = simulation.round_audits
    for audit in round_audits:
        record = audit.get("direct_llm_assignment")
        if isinstance(record, dict) and record.get("invocation_made"):
            record["latency_ms"] = None
    summary_payload = summary.to_dict()
    summary_payload["total_decision_time_ms"] = None
    payload = {
        "schema_version": config.schema_version,
        "experiment_type": "nyc_tlc_shared_input_method_run",
        "run_mode": "complete_simulation_replay_from_captured_codex_traces",
        "partial_run": False,
        "performance_claim": False,
        "recording_mode": "compact_round_audits_with_first_candidate_table",
        "execution_provenance": "offline deterministic replay; no model calls were made by this script",
        "source_live_run_status": "aggregate_result_writer_interrupted_after_all_captured_calls",
        "case_id": args.case,
        "scenario_id": loaded["scenario"]["scenario_id"],
        "fleet_id": loaded["fleet"]["fleet_id"],
        "method_name": "D5_pure_llm_direct",
        "llm_backend": "codex_cli",
        "llm_model": args.model,
        "llm_input_protocol": CODEX_DIRECT_ASSIGNMENT_INPUT_POLICY,
        "scenario_fingerprint_sha256": scenario_fingerprint(
            loaded["road_map"], loaded["orders"], loaded["vehicles"]
        ),
        "input_hashes": {
            "scenario_json": loaded["scenario_metadata_sha256"],
            "orders_json": loaded["orders_json_sha256"],
            "zone_od_cost_matrix": loaded["cost_matrix_sha256"],
        },
        "cost_semantics": {
            "pickup": pickup_policy,
            "ride": "direct observed training-period zone OD median proxy; not street routing",
        },
        "config": config.to_dict(),
        "summary": summary_payload,
        "rounds": [],
        "round_audits": round_audits,
        "round_validation_errors": simulation.round_validation_errors,
        "captured_cli_trace_count": len(replay.rows),
        "captured_cli_tokens": {
            "input": replay.input_tokens,
            "cached_input": replay.cached_input_tokens,
            "output": replay.output_tokens,
            "total": replay.input_tokens + replay.output_tokens,
        },
    }
    output_path = (ROOT / args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "result_path": str(output_path),
                "summary": summary_payload,
                "captured_cli_trace_count": len(replay.rows),
                "captured_cli_tokens": payload["captured_cli_tokens"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
