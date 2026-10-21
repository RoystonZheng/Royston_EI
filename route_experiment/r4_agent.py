"""Bounded R4 route proposal, validation, and feedback loop.

The route validator is a deterministic gate. It sends only program-derived
route errors back to the route model and never substitutes a traditional
search result for the model's proposal.
"""

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

from .failure_audit import estimate_route_proposal_energy, recover_partial_edge_slot_sequence
from .llm_adapter import parse_route_output, replay_move_sequence, validate_llm_route
from .models import EvaluationConfig, RoadMap, RouteCandidate, VehicleOrderTask
from .r4_constraints import R4RouteOutputConstraint


_TRUTH_FIELDS = ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")


def _ensure_public_messages(messages: List[Dict[str, str]]) -> None:
    for message in messages:
        content = str(message.get("content") or "")
        if any(field in content for field in _TRUTH_FIELDS):
            raise ValueError("r4_route_input_contains_evaluator_truth")


def _feedback_for_attempt(
    attempt_number: int,
    route_validation_errors: List[str],
    move_violations: List[Dict[str, Any]],
    endpoint_ids_match: Optional[bool],
    parse_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Build feedback from observable output checks, excluding evaluator data."""

    public_violations = []
    for violation in move_violations:
        public_violations.append(
            {
                key: violation.get(key)
                for key in (
                    "step",
                    "move",
                    "kind",
                    "from_node_id",
                    "to_node_id",
                    "edge_id",
                )
                if key in violation
            }
        )
    feedback: Dict[str, Any] = {
        "schema_version": "r4_route_validation_feedback_v1",
        "failed_attempt": attempt_number,
        "route_validation_errors": route_validation_errors,
        "move_violations": public_violations,
        "endpoint_ids_match": endpoint_ids_match,
        "instruction": (
            "请重新从原始起点规划完整路线。逐步只使用输入中列出的有向出边，"
            "先经过指定上车点，再到指定下车点，并在首次抵达下车点时立即停止。"
            "不要沿用违规路段；仅按原输出契约返回一个 JSON 对象。"
        ),
    }
    if parse_error is not None:
        feedback["parse_error"] = parse_error
    return feedback


def _safe_error(error: Exception, client: Any) -> str:
    detail = str(error)
    api_key = getattr(client, "api_key", "")
    if api_key:
        detail = detail.replace(api_key, "[redacted]")
    detail = re.sub(r"(?i)\b(?:sk|ds)[-_][a-z0-9_-]{8,}\b", "[redacted]", detail)
    return (type(error).__name__ + ":" + detail.replace("\n", " "))[:300]


def _safe_response_parse_audit(error: Exception, client: Any) -> Optional[Dict[str, Any]]:
    excerpt = getattr(error, "response_body_excerpt", None)
    if not isinstance(excerpt, str):
        return None
    api_key = getattr(client, "api_key", "")
    if api_key:
        excerpt = excerpt.replace(api_key, "[redacted]")
    excerpt = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", excerpt)
    excerpt = re.sub(r"(?i)\b(?:sk|ds)[-_][a-z0-9_-]{8,}\b", "[redacted]", excerpt)
    return {
        "response_body_bytes": int(getattr(error, "response_body_bytes", 0) or 0),
        "response_body_sha256": getattr(error, "response_body_sha256", None),
        "response_body_excerpt": excerpt[:2048],
        "interpretation": "provider response JSON could not be parsed; excerpt is credential-scrubbed",
    }


def replay_directed_edge_sequence(
    road_map: RoadMap,
    start_node_id: str,
    edge_sequence: List[str],
    node_sequence: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Replay an edge proposal and mark discontinuities without adding roads."""

    edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
    if node_sequence is None:
        node_sequence = [start_node_id]
        for edge_id in edge_sequence:
            edge = edge_by_id.get(edge_id)
            node_sequence.append(edge.to_node if edge is not None else None)
    pairs = []
    violations = []
    prefix_edges = []
    prefix_nodes = [start_node_id]
    current_node = start_node_id
    prefix_open = True
    for step, edge_id in enumerate(edge_sequence, 1):
        edge = edge_by_id.get(edge_id)
        if edge is None:
            violations.append(
                {
                    "step": step,
                    "kind": "missing_directed_edge",
                    "from_node_id": current_node,
                    "to_node_id": None,
                    "edge_id": edge_id,
                }
            )
            prefix_open = False
            continue
        pairs.append(
            {
                "step": step,
                "from_node_id": edge.from_node,
                "to_node_id": edge.to_node,
                "edge_id": edge.edge_id,
            }
        )
        if edge.from_node != current_node:
            violations.append(
                {
                    "step": step,
                    "kind": "route_edge_node_discontinuity",
                    "from_node_id": current_node,
                    "to_node_id": edge.from_node,
                    "edge_id": edge.edge_id,
                }
            )
            prefix_open = False
        elif prefix_open:
            prefix_edges.append(edge.edge_id)
            prefix_nodes.append(edge.to_node)
        current_node = edge.to_node

    return {
        "schema_version": "r4_directed_edge_slot_replay_v1",
        "route_representation": "directed_edge_slots",
        "attempted_route_node_sequence": list(node_sequence),
        "attempted_route_edge_sequence": list(edge_sequence),
        "proposed_edge_pairs": pairs,
        "validated_prefix_node_sequence": prefix_nodes,
        "validated_prefix_edge_sequence": prefix_edges,
        "move_violations": violations,
        "move_count": len(edge_sequence),
        "first_violation_step": violations[0]["step"] if violations else None,
    }


def audit_partial_edge_slot_output(
    road_map: RoadMap,
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    raw_output: str,
    route_constraint: R4RouteOutputConstraint,
) -> Dict[str, Any]:
    """Audit recovered R4 edge slots without treating them as an accepted route."""

    sequence = recover_partial_edge_slot_sequence({"content": raw_output})
    raw_match = re.search(r'"edge_slot_sequence"\s*:\s*"([0-9A-Z]*)', raw_output)
    raw_sequence_length = len(raw_match.group(1)) if raw_match else 0
    codes = [sequence[index : index + 3] for index in range(0, len(sequence), 3)]
    check_match = re.search(r'"step_check_sequence"\s*:\s*"([01]*)', raw_output)
    model_check_sequence = check_match.group(1) if check_match else None
    model_step_check_count_matches_route = (
        model_check_sequence is not None and len(model_check_sequence) == len(codes)
    )
    try:
        parsed_response = json.loads(raw_output)
    except (TypeError, json.JSONDecodeError):
        parsed_response = None
    complete_response = (
        isinstance(parsed_response, dict)
        and isinstance(parsed_response.get("edge_slot_sequence"), str)
    )
    positions = {(node.x, node.y): node.node_id for node in road_map.nodes}
    min_x = min(node.x for node in road_map.nodes)
    max_x = max(node.x for node in road_map.nodes)
    min_y = min(node.y for node in road_map.nodes)
    max_y = max(node.y for node in road_map.nodes)
    width = max_x - min_x + 1
    direction_data = (
        ("E", 1, 0),
        ("W", -1, 0),
        ("N", 0, 1),
        ("S", 0, -1),
    )
    edge_by_pair = {
        (edge.from_node, edge.to_node): edge for edge in road_map.edges
    }
    current_node = start_node_id
    pickup_seen = current_node == pickup_node_id
    dropoff_seen = current_node == dropoff_node_id
    attempted_nodes = [start_node_id]
    attempted_edges: List[str] = []
    pairs: List[Dict[str, Any]] = []
    violations: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []
    prefix_nodes = [start_node_id]
    prefix_edges: List[str] = []
    prefix_open = True
    invalid_edge_slot_count = 0
    continuity_violation_count = 0
    endpoint_order_violation_count = 0

    for step, code in enumerate(codes, 1):
        slot = int(code, 36)
        cell_index, direction_index = divmod(slot, 4)
        x = min_x + cell_index % width
        y = min_y + cell_index // width
        direction, dx, dy = direction_data[direction_index]
        source_id = positions.get((x, y), "node_{0}_{1}".format(x, y))
        target_x, target_y = x + dx, y + dy
        target_id = "node_{0}_{1}".format(target_x, target_y)
        edge = route_constraint.edge_by_slot_code.get(code)
        if edge is not None:
            source_id, target_id = edge.from_node, edge.to_node
        target_exists = (target_x, target_y) in positions
        reverse_edge = edge_by_pair.get((target_id, source_id))
        if edge is not None:
            directed_check = True
            edge_failure = None
            edge_id = edge.edge_id
        else:
            directed_check = False
            invalid_edge_slot_count += 1
            edge_id = "invalid_edge_slot_{0}".format(code)
            if not target_exists:
                edge_failure = "outside_boundary"
            elif reverse_edge is not None and reverse_edge.one_way:
                edge_failure = "one_way_direction"
            else:
                edge_failure = "missing_directed_edge"

        continuity_check = source_id == current_node
        endpoint_order_check = not dropoff_seen and not (
            target_id == dropoff_node_id and not pickup_seen
        )
        if edge_failure:
            violations.append(
                {
                    "step": step,
                    "kind": edge_failure,
                    "from_node_id": source_id,
                    "to_node_id": target_id,
                    "edge_id": edge_id,
                    "edge_slot_code": code,
                }
            )
        if not continuity_check:
            continuity_violation_count += 1
            violations.append(
                {
                    "step": step,
                    "kind": "route_edge_node_discontinuity",
                    "from_node_id": current_node,
                    "to_node_id": source_id,
                    "edge_id": edge_id,
                    "edge_slot_code": code,
                }
            )
        if not endpoint_order_check:
            endpoint_order_violation_count += 1
            violations.append(
                {
                    "step": step,
                    "kind": "route_endpoint_order_invalid",
                    "from_node_id": source_id,
                    "to_node_id": target_id,
                    "edge_id": edge_id,
                    "edge_slot_code": code,
                }
            )

        pairs.append(
            {
                "step": step,
                "from_node_id": source_id,
                "to_node_id": target_id,
                "edge_id": None if edge_failure else edge.edge_id,
                "edge_slot_code": code,
            }
        )
        attempted_edges.append(edge_id)
        attempted_nodes.append(target_id)
        if model_check_sequence is None:
            model_edge_check = "NOT_RETURNED"
        elif not model_step_check_count_matches_route:
            model_edge_check = "UNALIGNABLE_COUNT_MISMATCH"
        else:
            model_edge_check = "PASS" if model_check_sequence[step - 1] == "1" else "FAIL"
        records.append(
            {
                "step": step,
                "from_node_id": source_id,
                "to_node_id": target_id,
                "edge_id": edge_id,
                "edge_slot_code": code,
                "model_edge_check": model_edge_check,
                "local_directed_edge_check": "PASS" if directed_check else "FAIL",
                "local_continuity_check": "PASS" if continuity_check else "FAIL",
                "local_endpoint_order_check": "PASS" if endpoint_order_check else "FAIL",
                "accepted_step": False,
            }
        )
        local_step_pass = directed_check and continuity_check and endpoint_order_check
        if prefix_open and local_step_pass:
            prefix_edges.append(edge_id)
            prefix_nodes.append(target_id)
        else:
            prefix_open = False
        current_node = target_id
        if target_id == pickup_node_id:
            pickup_seen = True
        if target_id == dropoff_node_id:
            dropoff_seen = True

    replay = {
        "schema_version": "r4_partial_edge_slot_replay_v1",
        "route_representation": "directed_edge_slots",
        "attempted_move_sequence": "",
        "attempted_route_node_sequence": attempted_nodes,
        "attempted_route_edge_sequence": attempted_edges,
        "proposed_edge_pairs": pairs,
        "validated_prefix_node_sequence": prefix_nodes,
        "validated_prefix_edge_sequence": prefix_edges,
        "move_violations": violations,
        "step_check_records": records,
        "move_count": len(codes),
        "first_violation_step": min(
            (item["step"] for item in violations), default=None
        ),
    }
    energy = estimate_route_proposal_energy(road_map, replay, EvaluationConfig())
    return {
        "complete_route": False,
        "complete_slot_count": len(codes),
        "trailing_incomplete_char_count": raw_sequence_length % 3,
        "invalid_edge_slot_count": invalid_edge_slot_count,
        "continuity_violation_count": continuity_violation_count,
        "endpoint_order_violation_count": endpoint_order_violation_count,
        "model_step_checks_available": model_check_sequence is not None,
        "model_step_check_count": (
            len(model_check_sequence) if model_check_sequence is not None else None
        ),
        "model_step_check_count_matches_route": model_step_check_count_matches_route,
        "model_step_check_sequence": model_check_sequence,
        "recovered_edge_slot_sequence": sequence,
        "interpretation": (
            "complete model response rejected by local validation; reconstructed slots are for audit only; "
            "not an accepted or executed route"
            if complete_response
            else "truncated or malformed model output; local checks are reconstructed from complete slot codes; "
            "not a complete or executed route"
        ),
        "replay": replay,
        "energy_estimate": energy,
    }


def needs_edge_slot_proposal_audit(status: str) -> bool:
    """Return whether a rejected tool response can contain auditable edge slots."""

    return status in {"route_output_parse_failed", "route_constraint_violation"}


def _parse_user_json_object(user_content: str) -> Dict[str, Any]:
    try:
        payload = json.loads(user_content)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("r4_route_input_user_message_must_be_json") from error
    if not isinstance(payload, dict):
        raise ValueError("r4_route_input_user_message_must_be_json_object")
    return payload


def _attach_feedback_to_user_json(
    user_content: str, feedback: Dict[str, Any]
) -> str:
    """Keep retry input valid JSON while adding validator feedback."""

    payload = _parse_user_json_object(user_content)
    payload["r4_validation_feedback"] = feedback
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def run_r4_route_search(
    client: Any,
    base_messages: List[Dict[str, str]],
    road_map: RoadMap,
    task: VehicleOrderTask,
    selected_pickup_node_id: str,
    selected_dropoff_node_id: str,
    max_attempts: int = 3,
    blocked_edge_ids: Iterable[str] = (),
    on_attempt: Optional[Callable[[Dict[str, Any]], None]] = None,
    route_constraint: Optional[R4RouteOutputConstraint] = None,
) -> Dict[str, Any]:
    """Run at most three route proposals, feeding back only local check errors.

    ``client.chat`` is called once per planning attempt. The client itself must
    be configured with its desired transport retry policy (R4 uses zero).
    Model-declared ``failed`` is preserved as an experiment outcome and ends
    the loop without an extra request.
    """

    if isinstance(max_attempts, bool) or not 1 <= int(max_attempts) <= 3:
        raise ValueError("r4_max_attempts_must_be_between_1_and_3")
    if not selected_pickup_node_id or not selected_dropoff_node_id:
        raise ValueError("r4_selected_endpoints_required")
    messages = [dict(message) for message in base_messages]
    _ensure_public_messages(messages)
    if not messages or messages[-1].get("role") != "user":
        raise ValueError("r4_route_input_must_end_with_user_message")
    _parse_user_json_object(messages[-1].get("content"))
    if route_constraint is None and hasattr(client, "route_constraint"):
        raise RuntimeError("r4_route_constraint_required")
    if route_constraint is not None:
        if (
            route_constraint.pickup_node_id != selected_pickup_node_id
            or route_constraint.dropoff_node_id != selected_dropoff_node_id
        ):
            raise ValueError("r4_route_constraint_endpoint_mismatch")
        client_constraint = getattr(client, "route_constraint", None)
        if (
            client_constraint is None
            or getattr(client_constraint, "schema_sha256", None)
            != route_constraint.schema_sha256
        ):
            raise RuntimeError("r4_route_constraint_client_schema_mismatch")
    attempts: List[Dict[str, Any]] = []
    feedback_for_next_request: Optional[Dict[str, Any]] = None
    blocked = tuple(blocked_edge_ids)
    request_count = 0
    final_status = "not_started"
    final_route = None
    final_replay = None
    final_validation = None

    def snapshot() -> Dict[str, Any]:
        status = final_status
        if (
            status in ("route_proposal_invalid", "route_output_parse_failed")
            and len(attempts) >= int(max_attempts)
        ):
            status = "planning_attempts_exhausted"
        return {
            "schema_version": "r4_route_search_result_v1",
            "status": status,
            "api_request_count": request_count,
            "attempt_limit": int(max_attempts),
            "attempts": attempts,
            "route": final_route,
            "replay": final_replay,
            "validation": final_validation,
            "usage": {
                "llm_calls": request_count,
                "input_tokens": sum(item.get("input_tokens", 0) for item in attempts),
                "output_tokens": sum(item.get("output_tokens", 0) for item in attempts),
                "llm_latency_ms": round(
                    sum(item.get("latency_ms", 0.0) for item in attempts), 3
                ),
                "client_retries": sum(item.get("retries", 0) for item in attempts),
            },
        }

    def notify_attempt() -> None:
        if on_attempt is not None:
            on_attempt(snapshot())

    for attempt_number in range(1, int(max_attempts) + 1):
        request_messages = [dict(message) for message in messages]
        if feedback_for_next_request is not None:
            request_messages[-1]["content"] = _attach_feedback_to_user_json(
                request_messages[-1]["content"], feedback_for_next_request
            )
        request_count += 1
        try:
            response = client.chat(request_messages)
        except Exception as error:
            failed_attempt = {
                "attempt": attempt_number,
                "status": "api_error",
                "error": _safe_error(error, client),
                "feedback": None,
            }
            response_audit = _safe_response_parse_audit(error, client)
            if response_audit is not None:
                failed_attempt["response_parse_audit"] = response_audit
            attempts.append(failed_attempt)
            final_status = "api_error"
            notify_attempt()
            break

        attempt: Dict[str, Any] = {
            "attempt": attempt_number,
            "status": "response_received",
            "model": str(getattr(response, "model", "")),
            "request_id": getattr(response, "request_id", None),
            "finish_reason": str(getattr(response, "finish_reason", "") or ""),
            "input_tokens": int(getattr(response, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(response, "output_tokens", 0) or 0),
            "latency_ms": float(getattr(response, "latency_ms", 0.0) or 0.0),
            "retries": int(getattr(response, "retries", 0) or 0),
            "output_constraint_mode": getattr(
                response, "output_constraint_mode", None
            ),
            "constraint_schema_sha256": getattr(
                response, "constraint_schema_sha256", None
            ),
            "constraint_schema_bytes": int(
                getattr(response, "constraint_schema_bytes", 0) or 0
            ),
            "raw_output": str(getattr(response, "content", "")),
            "feedback": None,
        }
        attempts.append(attempt)

        try:
            parsed = parse_route_output(attempt["raw_output"])
            if route_constraint is not None:
                parsed = route_constraint.normalize_payload(parsed)
            attempt["parsed_output"] = parsed
        except Exception as error:
            parse_error = _safe_error(error, client)
            if route_constraint is not None and str(error).startswith("r4_"):
                attempt.update(
                    {
                        "status": "route_constraint_violation",
                        "parse_error": parse_error,
                    }
                )
                final_status = "route_constraint_violation"
                notify_attempt()
                break
            attempt.update({"status": "route_output_parse_failed", "parse_error": parse_error})
            final_status = "route_output_parse_failed"
            if attempt_number < int(max_attempts):
                feedback = _feedback_for_attempt(
                    attempt_number,
                    ["llm_output_parse_failed"],
                    [],
                    None,
                    parse_error=parse_error,
                )
                attempt["feedback"] = feedback
                feedback_for_next_request = feedback
                notify_attempt()
                continue
            notify_attempt()
            break

        if parsed["status"] == "failed":
            attempt.update({"status": "model_returned_failed", "reason": parsed["reason"]})
            final_status = "model_returned_failed"
            notify_attempt()
            break

        replay = None
        if parsed["move_sequence"]:
            replay = replay_move_sequence(
                road_map,
                task.start_node_id,
                parsed["move_sequence"],
                blocked_edge_ids=blocked,
            )
            node_sequence = replay["attempted_route_node_sequence"]
            edge_sequence = replay["attempted_route_edge_sequence"]
            attempt["replay"] = replay
        else:
            node_sequence = parsed["node_sequence"]
            edge_sequence = parsed["edge_sequence"]
            if route_constraint is not None and parsed.get("edge_slot_sequence"):
                replay = replay_directed_edge_sequence(
                    road_map,
                    task.start_node_id,
                    edge_sequence,
                    node_sequence=node_sequence,
                )
                attempt["replay"] = replay

        candidate = RouteCandidate(
            route_id="r4-attempt-{0}".format(attempt_number),
            node_sequence=node_sequence,
            edge_sequence=edge_sequence,
            route_source="r4_deepseek_route_search",
        )
        validation = validate_llm_route(
            road_map,
            task,
            candidate,
            selected_pickup_node_id=selected_pickup_node_id,
            selected_dropoff_node_id=selected_dropoff_node_id,
            blocked_edge_ids=blocked,
        )
        endpoint_ids_match = (
            parsed.get("pickup_node_id") == selected_pickup_node_id
            and parsed.get("dropoff_node_id") == selected_dropoff_node_id
        )
        move_violations = list(replay["move_violations"]) if replay else []
        local_step_checks_pass = True
        if route_constraint is not None and replay is not None:
            model_checks = parsed["step_check_sequence"]
            pairs_by_step = {
                int(pair["step"]): pair
                for pair in replay["proposed_edge_pairs"]
            }
            edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
            current_node = task.start_node_id
            pickup_seen = current_node == selected_pickup_node_id
            dropoff_seen = current_node == selected_dropoff_node_id
            step_check_records = []
            for step, edge_id in enumerate(parsed["edge_sequence"], 1):
                pair = pairs_by_step.get(step)
                edge = edge_by_id.get(edge_id)
                edge_present = bool(
                    pair
                    and edge
                    and pair.get("from_node_id") == edge.from_node
                    and pair.get("to_node_id") == edge.to_node
                )
                continuity_pass = bool(
                    edge_present and pair.get("from_node_id") == current_node
                )
                endpoint_order_pass = edge_present and not dropoff_seen
                if edge_present:
                    target_node = pair["to_node_id"]
                    if target_node == selected_dropoff_node_id and not pickup_seen:
                        endpoint_order_pass = False
                    if target_node == selected_pickup_node_id:
                        pickup_seen = True
                    if target_node == selected_dropoff_node_id:
                        dropoff_seen = True
                    current_node = target_node
                record = {
                    "step": step,
                    "from_node_id": pair.get("from_node_id") if pair else None,
                    "to_node_id": pair.get("to_node_id") if pair else None,
                    "edge_id": edge_id,
                    "model_edge_check": "PASS" if model_checks[step - 1] == "1" else "FAIL",
                    "local_directed_edge_check": "PASS" if edge_present else "FAIL",
                    "local_continuity_check": "PASS" if continuity_pass else "FAIL",
                    "local_endpoint_order_check": "PASS" if endpoint_order_pass else "FAIL",
                }
                record["accepted_step"] = all(
                    record[key] == "PASS"
                    for key in (
                        "model_edge_check",
                        "local_directed_edge_check",
                        "local_continuity_check",
                        "local_endpoint_order_check",
                    )
                )
                step_check_records.append(record)
            local_step_checks_pass = bool(step_check_records) and all(
                record["accepted_step"] for record in step_check_records
            )
            replay["model_step_check_sequence"] = model_checks
            replay["step_check_records"] = step_check_records
            replay["local_step_checks_pass"] = local_step_checks_pass
            attempt["step_check_records"] = step_check_records
        valid = bool(
            endpoint_ids_match
            and validation.valid
            and not move_violations
            and local_step_checks_pass
        )
        attempt.update(
            {
                "candidate": candidate.to_dict(),
                "validation": validation.to_dict(),
                "endpoint_ids_match": endpoint_ids_match,
                "status": "route_validated" if valid else "route_proposal_invalid",
            }
        )
        final_route = candidate.to_dict()
        final_replay = replay
        final_validation = validation.to_dict()
        if valid:
            final_status = "route_validated"
            notify_attempt()
            break

        final_status = "route_proposal_invalid"
        if attempt_number < int(max_attempts):
            feedback = _feedback_for_attempt(
                attempt_number,
                list(validation.errors),
                move_violations,
                endpoint_ids_match,
            )
            attempt["feedback"] = feedback
            feedback_for_next_request = feedback
        notify_attempt()

    if (
        final_status in ("route_proposal_invalid", "route_output_parse_failed")
        and len(attempts) >= int(max_attempts)
    ):
        final_status = "planning_attempts_exhausted"

    return snapshot()
