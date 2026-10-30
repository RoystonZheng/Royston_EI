#!/usr/bin/env python3
"""Offline audit of the single completed R2 Codex pilot; never calls a model."""

import json
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
RUN = PACKAGE / "derived/experiments/r2_codex_cli_v1/pilot_seed0_order0001_fastest_v3"
OUTPUT = RUN / "offline_route_postmortem.json"


def main():
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite existing audit: {OUTPUT}")
    payload = json.loads((RUN / "request_payload.json").read_text(encoding="utf-8"))
    response = json.loads((RUN / "model_response.json").read_text(encoding="utf-8"))
    usage = json.loads((RUN / "codex_usage_audit.json").read_text(encoding="utf-8"))
    preflight = json.loads((RUN / "preflight.json").read_text(encoding="utf-8"))
    edge_ids = {row[0] for row in payload["network"]["directed_links"]}
    legal_turns = {(row[0], row[1]) for row in payload["network"]["legal_turns"]}
    inputs_by_role = {row["leg_role"]: row for row in payload["legs"]}

    rows = []
    for route in response["routes"]:
        role = route["leg_role"]
        path = route["edge_ids"]
        task = inputs_by_role[role]
        transition_errors = sum(
            1 for left, right in zip(path, path[1:]) if (left, right) not in legal_turns
        )
        unknown_aliases = sum(1 for edge_id in path if edge_id not in edge_ids)
        forbidden_matches = 0
        for rule in payload["network"]["forbidden_link_group_sequences"]:
            width = len(rule)
            for start in range(max(0, len(path) - width + 1)):
                if all(path[start + offset] in group for offset, group in enumerate(rule)):
                    forbidden_matches += 1
        rows.append({
            "leg_role": role,
            "route_link_count": len(path),
            "output_schema_max_links_per_leg": payload["task"]["max_route_links"],
            "hit_schema_max_links": len(path) == payload["task"]["max_route_links"],
            "expected_start_alias": task["from"]["edge_id"],
            "proposed_start_alias": path[0] if path else None,
            "start_endpoint_matches": bool(path and path[0] == task["from"]["edge_id"]),
            "expected_end_alias": task["to"]["edge_id"],
            "proposed_end_alias": path[-1] if path else None,
            "end_endpoint_matches": bool(path and path[-1] == task["to"]["edge_id"]),
            "unknown_link_alias_count": unknown_aliases,
            "illegal_turn_count": transition_errors,
            "forbidden_link_sequence_count": forbidden_matches,
        })
    report = {
        "audit_id": "porto_r2_codex_pilot_route_postmortem_v1",
        "source": "single existing Codex response; offline only; no new model requests",
        "pilot_status": "model_route_rejected_no_retry",
        "execution_status": "not_run_due_to_hard_validation_rejection",
        "model_call_count": 1,
        "retry_count": 0,
        "preflight_rough_input_estimate": preflight["rough_input_token_estimate_characters_div_3"],
        "actual_input_tokens": usage["input_tokens"],
        "actual_input_exceeded_rough_estimate_by_factor": round(
            usage["input_tokens"] / max(1, preflight["rough_input_token_estimate_characters_div_3"]), 4
        ),
        "actual_output_tokens": usage["output_tokens"],
        "planning_latency_ms": usage["latency_ms"],
        "no_tool_events": not usage["event_audit"]["tool_usage_detected"],
        "standard_rate_equivalent_usd": usage["cost_estimate"]["api_equivalent_usd"],
        "standard_rate_equivalent_is_invoice": False,
        "routes": rows,
        "conclusion": "Both schema-valid route arrays reached the 512-link output ceiling and failed the required destination-link endpoint check. The proposed routes were rejected; no SUMO route was executed. This is one diagnostic failure, not a formal route success-rate estimate.",
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
