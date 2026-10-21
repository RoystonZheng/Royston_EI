"""Pure DeepSeek route planning adapter with an independent route validator.

The adapter keeps the live API boundary small and auditable.  The model sees a
route input package and the natural-language task, while endpoint truth fields
(``true_*``) stay out of the prompt and are used only by the post-hoc evaluator.
All API metadata is recorded as structured fields; credentials are never part
of a result payload.
"""

import json
import hashlib
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from dataclasses import replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

from .models import (
    Edge,
    Node,
    ResourceUsage,
    RoadMap,
    RouteCandidate,
    RouteInputPackage,
    RouteMethodResult,
    ValidationResult,
    VehicleOrderTask,
)
from .routing import (
    build_route_input_package,
    edge_travel_time_sec,
    route_chain_reachability,
)
from .r4_constraints import R4RouteOutputConstraint
from .semantic import resolve_graph_only_semantic_task


Transport = Callable[[str, Dict[str, str], Dict[str, Any], float], Dict[str, Any]]


class DeepSeekResponseParseError(ValueError):
    """Malformed provider JSON with a small, auditable response excerpt."""

    def __init__(self, response_body: bytes) -> None:
        super().__init__("deepseek_response_json_parse_failed")
        self.response_body_bytes = len(response_body)
        self.response_body_sha256 = hashlib.sha256(response_body).hexdigest()
        self.response_body_excerpt = response_body[:2048].decode(
            "utf-8", errors="replace"
        )


def _safe_http_error_detail(error: urllib.error.HTTPError, api_key: str) -> str:
    """Extract a small, credential-scrubbed detail from a provider error."""

    parts = ["deepseek_http_status:{0}".format(error.code)]
    try:
        body = error.read(16384)
        payload = json.loads(body.decode("utf-8", errors="replace"))
        details = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(details, dict):
            return ":".join(parts)
        for key in ("type", "code", "param", "message"):
            value = details.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.replace(api_key, "[redacted]") if api_key else value
            value = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", value)
            value = re.sub(r"(?i)\b(?:sk|ds)[-_][a-z0-9_-]{8,}\b", "[redacted]", value)
            value = re.sub(r"[\r\n\t]+", " ", value).strip()[:240]
            if value:
                parts.append("{0}={1}".format(key, value))
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return ":".join(parts)


@dataclass
class DeepSeekResponse:
    content: str
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    retries: int = 0
    request_id: Optional[str] = None
    finish_reason: str = ""
    output_constraint_mode: str = "json_object"
    constraint_schema_sha256: Optional[str] = None
    constraint_schema_bytes: int = 0


def _default_transport(
    url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float
) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response_body = response.read()
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DeepSeekResponseParseError(response_body) from error
        if not isinstance(payload, dict):
            raise ValueError("deepseek_response_not_object")
        request_id = response.headers.get("x-request-id")
        if request_id:
            payload["_request_id"] = request_id
        return payload


class DeepSeekClient:
    """Small stdlib-only client for DeepSeek chat completions.

    ``api_key`` is accepted at runtime and is never included in the returned
    object.  A transport injection keeps unit tests offline and deterministic.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout: float = 90.0,
        retries: int = 2,
        max_tokens: int = 4096,
        reasoning_effort: str = "none",
        transport: Optional[Transport] = None,
        route_constraint: Optional[Any] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = float(timeout)
        self.retries = 0 if route_constraint is not None else max(0, int(retries))
        self.max_tokens = max(1, int(max_tokens))
        self.reasoning_effort = reasoning_effort
        self.transport = transport or _default_transport
        self.route_constraint = route_constraint

    def _request_url(self) -> str:
        if self.route_constraint is None:
            return self.base_url + "/chat/completions"
        parsed = urlsplit(self.base_url)
        path = parsed.path.rstrip("/")
        if path in ("", "/v1"):
            path = "/beta"
        elif path != "/beta":
            raise ValueError("r4_strict_tool_calls_require_deepseek_beta_base_url")
        beta_base = urlunsplit(
            (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
        ).rstrip("/")
        return beta_base + "/chat/completions"

    def chat(self, messages: List[Dict[str, str]]) -> DeepSeekResponse:
        if not self.api_key:
            raise RuntimeError("missing_deepseek_api_key")
        endpoint = self._request_url()
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "stream": False,
            # Route planning only needs a short structured answer. Disable
            # V4 thinking mode so the provider does not spend output tokens on
            # a long reasoning trace before the JSON route.
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
        }
        if self.route_constraint is None:
            body["response_format"] = {"type": "json_object"}
        else:
            body["tools"] = [self.route_constraint.tool_definition()]
            body["tool_choice"] = self.route_constraint.tool_choice
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        started = time.perf_counter()
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                payload = self.transport(
                    endpoint,
                    headers,
                    body,
                    self.timeout,
                )
                content = (
                    _extract_content(payload)
                    if self.route_constraint is None
                    else _extract_tool_arguments(payload, "submit_r4_route")
                )
                choices = payload.get("choices") or []
                finish_reason = (
                    str(choices[0].get("finish_reason") or "")
                    if choices and isinstance(choices[0], dict)
                    else ""
                )
                usage = payload.get("usage") or {}
                return DeepSeekResponse(
                    content=content,
                    model=str(payload.get("model") or self.model),
                    input_tokens=int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
                    cached_input_tokens=int(
                        (usage.get("prompt_tokens_details") or {}).get(
                            "cached_tokens",
                            (usage.get("input_tokens_details") or {}).get(
                                "cached_tokens",
                                usage.get("prompt_cache_hit_tokens", 0),
                            ),
                        )
                        or 0
                    ),
                    output_tokens=int(
                        usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
                    ),
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    retries=attempt,
                    request_id=payload.get("_request_id"),
                    finish_reason=finish_reason,
                    output_constraint_mode=(
                        (
                            "strict_step_direction_schema"
                            if hasattr(self.route_constraint, "current_node_id")
                            else "strict_directed_edge_schema"
                        )
                        if self.route_constraint is not None
                        else "json_object"
                    ),
                    constraint_schema_sha256=(
                        self.route_constraint.schema_sha256
                        if self.route_constraint is not None
                        else None
                    ),
                    constraint_schema_bytes=(
                        self.route_constraint.schema_bytes
                        if self.route_constraint is not None
                        else 0
                    ),
                )
            except urllib.error.HTTPError as error:
                # Keep only allowlisted provider fields, scrub credentials, and
                # never persist the raw response body.
                last_error = RuntimeError(_safe_http_error_detail(error, self.api_key))
                # Retrying a client/credit error only burns time and quota.
                retryable = error.code == 429 or error.code >= 500
                if attempt < self.retries and retryable:
                    time.sleep(min(2.0, 0.5 * (2**attempt)))
                elif not retryable:
                    break
            except (urllib.error.URLError, TimeoutError, ValueError) as error:
                last_error = error
                if attempt < self.retries:
                    time.sleep(min(2.0, 0.5 * (2**attempt)))
        detail = str(last_error or "unknown_error")
        raise RuntimeError("deepseek_request_failed:" + detail)


def _extract_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("deepseek_choices_missing")
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise ValueError("deepseek_message_content_missing")
    return content


def _extract_tool_arguments(payload: Dict[str, Any], expected_name: str) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("deepseek_choices_missing")
    message = choices[0].get("message") or {}
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        raise ValueError("deepseek_route_tool_call_missing_or_ambiguous")
    function = tool_calls[0].get("function") or {}
    if function.get("name") != expected_name:
        raise ValueError("deepseek_route_tool_name_mismatch")
    arguments = function.get("arguments")
    if not isinstance(arguments, str) or not arguments.strip():
        raise ValueError("deepseek_route_tool_arguments_missing")
    # Leave JSON decoding to the route workflow.  It can then preserve the
    # malformed model output together with provider usage and latency instead
    # of misclassifying a received response as a transport/API failure.
    return arguments


def _json_from_text(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("llm_route_output_not_object")
    return payload


def _expand_move_runs(runs: Any, max_steps: int = 50000) -> str:
    """Expand model-selected direction/count runs into a verifiable path."""

    if not isinstance(runs, list) or not runs:
        raise ValueError("llm_move_runs_missing")
    moves = []
    total_steps = 0
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("llm_move_run_invalid")
        direction = str(run.get("direction", "")).strip().upper()
        steps = run.get("steps")
        if direction not in {"E", "W", "N", "S"}:
            raise ValueError("llm_move_run_direction_invalid")
        if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
            raise ValueError("llm_move_run_steps_invalid")
        total_steps += steps
        if total_steps > max_steps:
            raise ValueError("llm_move_runs_too_long")
        moves.append(direction * steps)
    return "".join(moves)


def parse_route_output(text: str) -> Dict[str, Any]:
    """Parse and minimally shape the fixed route output contract."""

    payload = _json_from_text(text)
    status = str(payload.get("status", "completed"))
    if status not in ("completed", "failed"):
        raise ValueError("llm_route_status_invalid")
    if status == "failed":
        result = {
            "status": "failed",
            "reason": str(payload.get("reason") or "llm_declined_route"),
            "pickup_node_id": payload.get("pickup_node_id"),
            "dropoff_node_id": payload.get("dropoff_node_id"),
            "move_sequence": "",
            "node_sequence": [],
            "edge_sequence": [],
        }
        if "step_check_sequence" in payload:
            result["step_check_sequence"] = payload["step_check_sequence"]
        if "failure_reason_code" in payload:
            result["failure_reason_code"] = payload["failure_reason_code"]
        return result
    edge_slot_sequence = payload.get("edge_slot_sequence")
    if edge_slot_sequence is not None:
        if not isinstance(edge_slot_sequence, str):
            raise ValueError("llm_edge_slot_sequence_invalid")
        result = {
            "status": "completed",
            "reason": payload.get("reason"),
            "pickup_node_id": payload.get("pickup_node_id"),
            "dropoff_node_id": payload.get("dropoff_node_id"),
            "move_sequence": "",
            "node_sequence": [],
            "edge_sequence": [],
            "edge_slot_sequence": edge_slot_sequence,
        }
        if "step_check_sequence" in payload:
            result["step_check_sequence"] = payload["step_check_sequence"]
        if "failure_reason_code" in payload:
            result["failure_reason_code"] = payload["failure_reason_code"]
        return result
    move_sequence = payload.get("move_sequence")
    if "move_runs" in payload:
        move_sequence = _expand_move_runs(payload.get("move_runs"))
    if isinstance(move_sequence, str):
        move_sequence = move_sequence.strip().upper()
        if not move_sequence:
            raise ValueError("llm_move_sequence_empty")
        invalid_moves = sorted(set(move_sequence) - set("EWNS"))
        if invalid_moves:
            raise ValueError("llm_move_sequence_invalid_symbols:" + "".join(invalid_moves))
        return {
            "status": "completed",
            "reason": payload.get("reason"),
            "pickup_node_id": payload.get("pickup_node_id"),
            "dropoff_node_id": payload.get("dropoff_node_id"),
            "move_sequence": move_sequence,
            "node_sequence": [],
            "edge_sequence": [],
        }
    node_sequence = payload.get("node_sequence")
    edge_sequence = payload.get("edge_sequence")
    if not isinstance(node_sequence, list) or not isinstance(edge_sequence, list):
        raise ValueError("llm_route_sequence_missing")
    if not node_sequence or not edge_sequence:
        raise ValueError("llm_route_sequence_empty")
    return {
        "status": "completed",
        "reason": payload.get("reason"),
        "pickup_node_id": payload.get("pickup_node_id"),
        "dropoff_node_id": payload.get("dropoff_node_id"),
        "move_sequence": "",
        "node_sequence": [str(item) for item in node_sequence],
        "edge_sequence": [str(item) for item in edge_sequence],
    }


def _task_public_payload(task: VehicleOrderTask) -> Dict[str, Any]:
    payload = {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "start_node_id": task.start_node_id,
        "pickup_node_id": task.pickup_node_id,
        "dropoff_node_id": task.dropoff_node_id,
        "pickup_text": task.pickup_text,
        "dropoff_text": task.dropoff_text,
    }
    return payload


def replay_move_sequence(
    road_map: RoadMap,
    start_node_id: str,
    move_sequence: str,
    blocked_edge_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    """Replay every proposed grid move and preserve each map-rule violation.

    The model has already emitted the complete direction string, so the
    returned proposed path continues geometrically after an invalid move.
    ``validated_prefix_*`` stops at the first violation and is the part that
    could actually be followed on the directed road graph.
    """

    node_by_id = {node.node_id: node for node in road_map.nodes}
    edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
    if start_node_id not in node_by_id:
        raise ValueError("llm_move_start_missing")
    blocked = set(blocked_edge_ids)
    deltas = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}
    current = node_by_id[start_node_id]
    attempted_nodes = [current.node_id]
    attempted_edges: List[str] = []
    validated_nodes = [current.node_id]
    validated_edges: List[str] = []
    violations: List[Dict[str, Any]] = []
    prefix_open = True

    for step, move in enumerate(move_sequence, start=1):
        dx, dy = deltas[move]
        next_x, next_y = current.x + dx, current.y + dy
        next_id = "node_{0}_{1}".format(next_x, next_y)
        edge_id = "edge_{0}_{1}".format(current.node_id, next_id)
        next_node = node_by_id.get(next_id)
        edge = edge_by_id.get(edge_id)
        reverse_edge = edge_by_id.get("edge_{0}_{1}".format(next_id, current.node_id))
        kind = None
        message = None
        if next_node is None:
            kind = "outside_boundary"
            message = "llm_move_leaves_map:" + next_id
        elif edge is None:
            kind = "one_way_direction" if reverse_edge and reverse_edge.one_way else "missing_directed_edge"
            message = "llm_move_missing_directed_edge:" + edge_id
        elif edge_id in blocked:
            kind = "blocked_edge"
            message = "llm_move_blocked_edge:" + edge_id

        attempted_edges.append(edge_id)
        attempted_nodes.append(next_id)
        if kind is not None:
            violations.append(
                {
                    "step": step,
                    "move": move,
                    "kind": kind,
                    "message": message,
                    "from_node_id": current.node_id,
                    "to_node_id": next_id,
                    "edge_id": edge_id,
                    "reverse_edge_id": reverse_edge.edge_id if reverse_edge else None,
                    "reverse_edge_one_way": bool(reverse_edge.one_way) if reverse_edge else False,
                    "blocked": edge_id in blocked,
                }
            )
            prefix_open = False
        elif prefix_open:
            validated_edges.append(edge_id)
            validated_nodes.append(next_id)

        # Continue following the model's coordinates after a violation so the
        # full proposal can be shown and later violations can also be marked.
        if next_node is not None:
            current = next_node
        else:
            current = Node(node_id=next_id, x=next_x, y=next_y)

    return {
        "attempted_move_sequence": move_sequence,
        "attempted_route_node_sequence": attempted_nodes,
        "attempted_route_edge_sequence": attempted_edges,
        "validated_prefix_node_sequence": validated_nodes,
        "validated_prefix_edge_sequence": validated_edges,
        "move_violations": violations,
        "first_violation_step": violations[0]["step"] if violations else None,
        "move_count": len(move_sequence),
    }


class InvalidMoveSequence(ValueError):
    """A failed move expansion together with its complete replay record."""

    def __init__(self, message: str, replay: Dict[str, Any]) -> None:
        super().__init__(message)
        self.replay = replay


def _route_from_moves(
    road_map: RoadMap,
    start_node_id: str,
    move_sequence: str,
    blocked_edge_ids: Iterable[str] = (),
) -> Tuple[List[str], List[str]]:
    """Expand moves only when every proposed move is legal on the map."""

    replay = replay_move_sequence(
        road_map, start_node_id, move_sequence, blocked_edge_ids=blocked_edge_ids
    )
    if replay["move_violations"]:
        first = replay["move_violations"][0]
        raise InvalidMoveSequence(first["message"], replay)
    return (
        replay["attempted_route_node_sequence"],
        replay["attempted_route_edge_sequence"],
    )


def _package_for_prompt(
    road_map: RoadMap,
    task: VehicleOrderTask,
    blocked_edge_ids: Iterable[str] = (),
) -> Tuple[RouteInputPackage, VehicleOrderTask, Optional[Dict[str, Any]]]:
    """Build the same neutral local package used by baselines.

    Semantic tasks use the graph-only selection only to establish the package
    window.  The selected IDs are removed from the public prompt and therefore
    remain a post-hoc comparison value rather than model input.
    """

    resolution: Optional[Dict[str, Any]] = None
    package_task = task
    if task.pickup_node_id is None or task.dropoff_node_id is None:
        pickup, dropoff, resolution = resolve_graph_only_semantic_task(road_map, task)
        if not resolution.get("resolved") or pickup is None or dropoff is None:
            raise ValueError("semantic_endpoint_unresolved")
        package_task = replace(task, pickup_node_id=pickup, dropoff_node_id=dropoff)
    package = build_route_input_package(
        road_map, package_task, blocked_edge_ids=blocked_edge_ids
    )
    return package, package_task, resolution


def _compact_grid_map_payload(package: RouteInputPackage) -> Optional[Dict[str, Any]]:
    """Encode graph-only topology as explicit per-node legal outgoing moves."""

    if not package.nodes:
        return None
    min_x = min(node.x for node in package.nodes)
    max_x = max(node.x for node in package.nodes)
    min_y = min(node.y for node in package.nodes)
    max_y = max(node.y for node in package.nodes)
    width = max_x - min_x + 1
    height = max_y - min_y + 1
    node_by_position = {(node.x, node.y): node for node in package.nodes}
    if len(node_by_position) != width * height:
        return None
    if any(
        (x, y) not in node_by_position
        for x in range(min_x, max_x + 1)
        for y in range(min_y, max_y + 1)
    ):
        return None

    node_id_to_position = {node.node_id: (node.x, node.y) for node in package.nodes}
    edge_positions: Dict[
        Tuple[Tuple[int, int], Tuple[int, int]], Dict[bool, Edge]
    ] = {}
    direction_bits = {(1, 0): 1, (-1, 0): 2, (0, 1): 4, (0, -1): 8}
    outgoing_masks: Dict[str, int] = {node.node_id: 0 for node in package.nodes}
    for edge in package.edges:
        source = node_id_to_position.get(edge.from_node)
        target = node_id_to_position.get(edge.to_node)
        if source is None or target is None:
            return None
        dx, dy = target[0] - source[0], target[1] - source[1]
        if abs(dx) + abs(dy) != 1:
            return None
        outgoing_masks[edge.from_node] |= direction_bits[(dx, dy)]
        positive_source = source if (dx > 0 or dy > 0) else target
        positive_target = target if positive_source == source else source
        forward = source == positive_source
        pair = (positive_source, positive_target)
        directions = edge_positions.setdefault(pair, {})
        if forward in directions:
            return None
        directions[forward] = edge

    speed_codes = {8.0: "0", 10.0: "1", 12.0: "2"}
    signal_codes = {0.0: "0", 10.0: "1", 15.0: "2", 20.0: "3"}
    edge_length_values = {edge.length_m for edge in package.edges}
    if len(edge_length_values) > 1:
        return None
    edge_length_m = next(iter(edge_length_values), 100.0)
    pair_cost_codes: Dict[
        Tuple[Tuple[int, int], Tuple[int, int]], str
    ] = {}
    for pair, directions in edge_positions.items():
        all_directions = list(directions.values())
        speed = all_directions[0].base_speed_mps
        congestion = all_directions[0].congestion_level
        signal_delay = all_directions[0].signal_delay_sec
        if (
            speed not in speed_codes
            or congestion not in (0, 1, 2)
            or signal_delay not in signal_codes
            or any(
                edge.length_m != edge_length_m
                or edge.base_speed_mps != speed
                or edge.congestion_level != congestion
                or edge.signal_delay_sec != signal_delay
                for edge in all_directions[1:]
            )
        ):
            return None
        pair_cost_codes[pair] = "{0}{1}{2}".format(
            speed_codes[speed], congestion, signal_codes[signal_delay]
        )

    def pair_cost_code(positive_source: Tuple[int, int], positive_target: Tuple[int, int]) -> str:
        return pair_cost_codes.get((positive_source, positive_target), "000")

    node_flags_by_y = []
    for y in range(min_y, max_y + 1):
        node_flags_by_y.append(
            "".join(
                str(
                    int(node_by_position[(x, y)].legal_pickup)
                    + 2 * int(node_by_position[(x, y)].legal_dropoff)
                )
                for x in range(min_x, max_x + 1)
            )
        )
    outgoing_mask_by_y = [
        "".join(
            "{0:x}".format(outgoing_masks[node_by_position[(x, y)].node_id])
            for x in range(min_x, max_x + 1)
        )
        for y in range(min_y, max_y + 1)
    ]
    horizontal_cost_codes_by_y = [
        "".join(
            pair_cost_code((x, y), (x + 1, y))
            for x in range(min_x, max_x)
        )
        for y in range(min_y, max_y + 1)
    ]
    vertical_cost_codes_by_x = [
        "".join(
            pair_cost_code((x, y), (x, y + 1))
            for y in range(min_y, max_y)
        )
        for x in range(min_x, max_x + 1)
    ]
    landmarks = [
        [node.x, node.y, node.landmark_name, list(node.landmark_aliases)]
        for node in package.nodes
        if node.landmark_name
    ]
    return {
        "encoding": "grid-directed-mask-v2",
        "input_mode": "graph_only",
        "map_id": package.map_id,
        "map_version": package.map_version,
        "road_state_version": package.road_state_version,
        "x_range": [min_x, max_x],
        "y_range": [min_y, max_y],
        "edge_length_m": edge_length_m,
        "grid_indexing": {
            "node_id": "node_{x}_{y}",
            "node_flags_by_y": "rows ordered by ascending y; each string scans x from x_range[0] to x_range[1]",
            "outgoing_mask_by_y": "rows ordered by ascending y; each hexadecimal character belongs to that node and scans x from x_range[0] to x_range[1]",
            "horizontal_cost_codes_by_y": "rows ordered by ascending y; each 3-digit cost code scans adjacent x pairs from x_range[0] to x_range[1]-1",
            "vertical_cost_codes_by_x": "columns ordered by ascending x; each 3-digit cost code scans adjacent y pairs from y_range[0] to y_range[1]-1",
        },
        "node_flag_legend": {
            "0": "not pickup-legal or dropoff-legal",
            "1": "pickup-legal only",
            "2": "dropoff-legal only",
            "3": "both pickup-legal and dropoff-legal",
        },
        "node_flags_by_y": node_flags_by_y,
        "outgoing_mask_legend": {
            "format": "each hexadecimal digit is the set of legal outgoing moves from that node",
            "bit_values": {
                "E": 1,
                "W": 2,
                "N": 4,
                "S": 8,
            },
            "example": "5 means E and N are allowed; 0 means no outgoing road",
        },
        "outgoing_mask_by_y": outgoing_mask_by_y,
        "edge_cost_code_legend": {
            "format": "each 3 digits encode speed-code, congestion-level, signal-delay-code; use only when the outgoing mask allows the move",
            "speed_mps": {"0": 8, "1": 10, "2": 12},
            "congestion_level": {"0": 0, "1": 1, "2": 2},
            "signal_delay_sec": {"0": 0, "1": 10, "2": 15, "3": 20},
        },
        "horizontal_cost_codes_by_y": horizontal_cost_codes_by_y,
        "vertical_cost_codes_by_x": vertical_cost_codes_by_x,
        "landmarks": landmarks,
        "blocked_edge_ids": list(package.blocked_edge_ids),
    }


def build_r4_route_search_messages(
    package: RouteInputPackage,
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    *,
    require_completed_route: bool = False,
) -> List[Dict[str, str]]:
    """Build an R4 route prompt using a compact, explicitly indexed edge grid."""

    node_by_id = {node.node_id: node for node in package.nodes}
    required = {start_node_id, pickup_node_id, dropoff_node_id}
    if not required.issubset(node_by_id):
        raise ValueError("r4_route_endpoint_missing_from_package")
    if not node_by_id[pickup_node_id].legal_pickup:
        raise ValueError("r4_pickup_not_legal")
    if not node_by_id[dropoff_node_id].legal_dropoff:
        raise ValueError("r4_dropoff_not_legal")

    min_x = min(node.x for node in package.nodes)
    max_x = max(node.x for node in package.nodes)
    min_y = min(node.y for node in package.nodes)
    max_y = max(node.y for node in package.nodes)
    width = max_x - min_x + 1
    height = max_y - min_y + 1
    node_by_position = {(node.x, node.y): node for node in package.nodes}
    if len(node_by_position) != width * height:
        raise ValueError("r4_route_package_not_rectangular")
    if any(
        (x, y) not in node_by_position
        for x in range(min_x, max_x + 1)
        for y in range(min_y, max_y + 1)
    ):
        raise ValueError("r4_route_package_has_missing_grid_node")
    if require_completed_route and not route_chain_reachability(
        package.edges, start_node_id, pickup_node_id, dropoff_node_id,
        package.blocked_edge_ids,
    )["chain_reachable"]:
        raise ValueError("r4_completed_route_requires_reachable_task")
    code_alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    edge_by_pair: Dict[Tuple[str, str], str] = {}
    edge_cost_legend: Dict[str, List[Any]] = {}
    code_by_cost: Dict[Tuple[float, float], str] = {}
    direction_by_delta = {
        (1, 0): "E",
        (-1, 0): "W",
        (0, 1): "N",
        (0, -1): "S",
    }
    for edge in package.edges:
        source = node_by_id.get(edge.from_node)
        target = node_by_id.get(edge.to_node)
        if source is None or target is None:
            raise ValueError("r4_route_edge_endpoint_missing_from_package")
        if (target.x - source.x, target.y - source.y) not in direction_by_delta:
            raise ValueError("r4_route_edge_not_adjacent")
        pair = (edge.from_node, edge.to_node)
        if pair in edge_by_pair:
            raise ValueError("r4_route_duplicate_directional_edge")
        distance_m = int(edge.length_m) if float(edge.length_m).is_integer() else edge.length_m
        cost = (round(edge_travel_time_sec(edge), 6), float(distance_m))
        code = code_by_cost.get(cost)
        if code is None:
            if len(code_by_cost) >= len(code_alphabet):
                raise ValueError("r4_route_cost_code_capacity_exceeded")
            code = code_alphabet[len(code_by_cost)]
            code_by_cost[cost] = code
            edge_cost_legend[code] = [cost[0], distance_m]
        edge_by_pair[pair] = code

    directional_cost_codes_by_y = []
    for y in range(min_y, max_y + 1):
        row_codes = []
        for x in range(min_x, max_x + 1):
            source_id = node_by_position[(x, y)].node_id
            for direction in "EWNS":
                dx, dy = {
                    "E": (1, 0),
                    "W": (-1, 0),
                    "N": (0, 1),
                    "S": (0, -1),
                }[direction]
                target = node_by_position.get((x + dx, y + dy))
                row_codes.append(
                    edge_by_pair.get((source_id, target.node_id), ".")
                    if target is not None
                    else "."
                )
        directional_cost_codes_by_y.append("".join(row_codes))
    map_payload = {
        "encoding": "directional-cost-grid-v1",
        "map_id": package.map_id,
        "map_version": package.map_version,
        "road_state_version": package.road_state_version,
        "boundary_inclusive": {
            "min_x": min_x,
            "max_x": max_x,
            "min_y": min_y,
            "max_y": max_y,
        },
        "width": width,
        "height": height,
        "coordinate_rule": {
            "node_id": "node_{x}_{y}",
            "E": "x + 1",
            "W": "x - 1",
            "N": "y + 1",
            "S": "y - 1",
        },
        "grid_indexing": "rows ordered by ascending y; each node occupies four consecutive characters in ascending x order; slots are E,W,N,S",
        "directional_cost_codes_by_y": directional_cost_codes_by_y,
        "edge_cost_legend": edge_cost_legend,
    }
    context = {
        "schema_version": "r4_route_search_input_v2",
        "map": map_payload,
        "route_query": {
            "start_node_id": start_node_id,
            "pickup_node_id": pickup_node_id,
            "dropoff_node_id": dropoff_node_id,
            "required_visit_order": [start_node_id, pickup_node_id, dropoff_node_id],
            "objective": "minimize_sum_travel_time_sec_then_sum_distance_m",
            "max_route_edges": max(1, len(package.nodes) - 1),
            "simple_path_rule": "do not revisit a node; stop at the first arrival at dropoff_node_id",
            "stepwise_self_check": (
                "每一步先核对当前节点的指定方向槽是否为实际有向出边，再核对它与前一步连续、"
                "不越界且不违反上车/下车顺序；只有全部检查通过才可标记该步通过。"
            ),
        },
        "map_reading_guide": {
            "map_type": "结构化自然语言说明的有向网格路网",
            "node_id": "node_{x}_{y}；x 向东递增，y 向北递增",
            "slot_order": "每节点的四个出边槽按 E,W,N,S 排列",
            "slot_meaning": "'.' 表示该方向禁止通行；其他编码表示从该节点出发的合法有向边",
            "one_way_rule": "单向道路只编码允许的出发方向；反向只有在反向槽也存在时才允许",
            "cost_rule": "每个非空槽的成本编码见 edge_cost_legend，单位为秒和米",
            "boundary_rule": "仅使用 boundary_inclusive 矩形范围内的节点",
        },
        "output_contract": {
            "completed": {
                "status": "completed",
                "pickup_node_id": pickup_node_id,
                "dropoff_node_id": dropoff_node_id,
                "edge_slot_sequence": "concatenated three-character base36 codes",
                "max_route_edges": max(1, len(package.nodes) - 1),
                "step_check_sequence": "one '1' per edge in route order; '1' means the model checked that step",
                "failure_reason_code": "none",
            },
            "failed": {
                "status": "failed",
                "pickup_node_id": pickup_node_id,
                "dropoff_node_id": dropoff_node_id,
                "edge_slot_sequence": "",
                "step_check_sequence": "",
                "failure_reason_code": ["no_directed_path", "unable_to_construct_route"],
            },
            "edge_slot_encoding": {
                "slot_order": "E,W,N,S",
                "direction_index": {"E": 0, "W": 1, "N": 2, "S": 3},
                "slot_index": "4*((y-min_y)*width+(x-min_x))+direction_index",
                "code": "uppercase base36 slot_index, never hexadecimal, padded to exactly 3 characters",
                "examples": {"35": "00Z", "36": "010", "255": "073"},
                "sequence": "one code per directed edge, concatenated without separators",
            },
        },
    }
    if require_completed_route:
        context["output_contract"].pop("failed")
    user = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    system = (
        "你只负责依据输入中的有向邻接表检索路线；上游已确定起点、上车点和下车点。"
        "map.directional_cost_codes_by_y 是紧凑有向邻接表。行索引为 y-min_y；每行按 x 从小到大排列节点，"
        "每个节点连续 4 个字符的槽位顺序固定为 E,W,N,S。字符 '.' 表示该方向没有出边；"
        "其他字符表示存在一条该方向的有向道路，其 travel_time_sec 和 distance_m 查 map.edge_cost_legend。"
        "未出现的方向禁止通行，绝不可根据相邻坐标推断反向道路。"
        "地图边界含 boundary_inclusive 的两个端点。"
        "路线必须从 start_node_id 出发，先到 pickup_node_id，再到 dropoff_node_id；"
        "首次抵达下车点后立即停止，路线最后一个节点必须恰好是 dropoff_node_id，不能多走一步；"
        "路线不得重复访问节点，边数不得超过 route_query.max_route_edges；"
        "槽码是大写 base36 而不是十六进制，例如槽位 35=00Z、36=010、255=073；"
        "只可使用输入中列出的节点和出边。按 travel_time_sec 总和最小规划，时间相同时按 distance_m 总和最小。"
        "travel_time_sec 是按项目统一道路速度、拥堵和信号延迟公式预先计算的单边时间，编码只描述从当前节点出发的方向。"
        "按 map_reading_guide 解释地图。先尝试构造一条完整路线，再尽量降低 travel_time_sec 总和、同时间时降低 distance_m 总和；"
        "completed 表示路线通过合法性校验，不代表已证明全局最优，不能证明全局最优不是无路可走。"
        "逐步检查每条候选路线的方向槽、边连续性、边界及 start→pickup→dropoff 顺序；自检失败只否决当前候选路线，继续搜索其他路线，"
        "不能把一次候选路线自检失败当作整项任务失败。只有检查完整个允许的有向图并确认 start→pickup 或 pickup→dropoff 至少一段不可达，"
        "才可返回 failed 且 failure_reason_code=no_directed_path；若未证明无路但无法构造完整路线，返回 failed 且 failure_reason_code=unable_to_construct_route。"
        "direction 含义为 E=x+1、W=x-1、N=y+1、S=y-1；节点坐标由 node_{x}_{y} 编码。"
        "若用户 JSON 顶层包含 r4_validation_feedback，它是本地校验器根据上一轮输出生成的结构化纠错信息，"
        "不是地图事实；请据此修正上一轮错误，并仍从 route_query 的原始起点重新规划完整路线。"
        "completed 时必须调用 submit_r4_route 工具，并用 edge_slot_sequence 返回路线：每条边一个 3 位 base36 槽码，不加分隔符。"
        "只提交实际路线所需的边，不要枚举或罗列地图中的其他合法边；"
        "每个槽码必须按当前节点的坐标和方向索引计算，不能按编号顺序遍历槽码；"
        "槽码先由局部行优先节点序号乘 4 再加方向序号得到："
        "4*((y-min_y)*width+(x-min_x))+方向序号，方向序号 E/W/N/S 分别为 0/1/2/3，"
        "再转大写 base36 并左侧补零到 3 位。每一步槽码必须对应当前所在节点的实际出边；"
        "工具结构约束仅允许图中存在的有向边，反向单行边和缺失边不会进入允许集合。"
        "边序列仍须连续，并按起点、上车点、下车点顺序到达；到达终点后停止。"
        "step_check_sequence 必须按路线顺序为每条边输出一个字符 '1'，逐步明确表示已完成该步自检；字符数必须与边数一致。"
        "completed 仅在每步自检全部通过时使用；failed 时路线和自检序列必须留空，并准确填写 failure_reason_code。"
        "不要输出长篇推理，只返回压缩的逐步自检码。"
    )
    if require_completed_route:
        system = system.replace(
            "completed 表示路线通过合法性校验，不代表已证明全局最优，不能证明全局最优不是无路可走。",
            "completed 表示路线通过合法性校验，不代表已证明全局最优。",
        )
        system = system.replace(
            "只有检查完整个允许的有向图并确认 start→pickup 或 pickup→dropoff 至少一段不可达，才可返回 failed 且 failure_reason_code=no_directed_path；若未证明无路但无法构造完整路线，返回 failed 且 failure_reason_code=unable_to_construct_route。",
            "本任务的 start→pickup 和 pickup→dropoff 两段已由本地可达性检查确认存在路径；该检查只确认可达，不提供路线。必须自行搜索并提交完整路线。候选路线自检失败后继续搜索其他路线，直至每步自检通过。",
        )
        system = system.replace(
            "completed 仅在每步自检全部通过时使用；failed 时路线和自检序列必须留空，并准确填写 failure_reason_code。",
            "只有每步自检全部通过时才提交路线。",
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_route_messages(
    road_map: RoadMap,
    task: VehicleOrderTask,
    package: Optional[RouteInputPackage] = None,
    blocked_edge_ids: Iterable[str] = (),
    encoded_map: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Render a fixed JSON input/output contract for the pure LLM."""

    if package is None:
        package, _, _ = _package_for_prompt(road_map, task, blocked_edge_ids)
    public_task = _task_public_payload(task)
    compact_grid = encoded_map
    if compact_grid is None:
        compact_grid = _compact_grid_map_payload(package)
    # Arrays keep the same package facts while avoiding repeating long JSON
    # field names for every one of the thousands of local edges.
    compact_nodes = [
        [
            node.node_id,
            node.x,
            node.y,
            1 if node.legal_pickup else 0,
            1 if node.legal_dropoff else 0,
            node.landmark_name,
            node.landmark_aliases,
        ]
        for node in package.nodes
    ]
    compact_edges = [
        [
            edge.edge_id,
            edge.from_node,
            edge.to_node,
            edge.length_m,
            edge.base_speed_mps,
            edge.congestion_level,
            edge.signal_delay_sec,
            1 if edge.one_way else 0,
        ]
        for edge in package.edges
    ]
    map_context = compact_grid or {
        "encoding": "explicit-edge-array-graph-only-v2",
        "input_mode": "graph_only",
        "map_id": package.map_id,
        "map_version": package.map_version,
        "road_state_version": package.road_state_version,
        "boundary": package.boundary,
        "node_fields": [
            "node_id",
            "x",
            "y",
            "legal_pickup_0_1",
            "legal_dropoff_0_1",
            "landmark_name",
            "landmark_aliases",
        ],
        "nodes": compact_nodes,
        "edge_fields": [
            "edge_id",
            "from_node",
            "to_node",
            "length_m",
            "base_speed_mps",
            "congestion_level",
            "signal_delay_sec",
            "one_way_0_1",
        ],
        "edges": compact_edges,
        "blocked_edge_ids": list(package.blocked_edge_ids),
    }
    context = {
        "schema_version": "pure_llm_route_input_v2",
        "map": map_context,
        "task": public_task,
        "instructions": {
            "goal": "只做路径搜索：解析地点并按最短总行驶时间规划 start -> pickup -> dropoff 的有向路线；时间相同时选距离较短路线",
            "map_input_mode": "graph_only; no roadside sign, utility pole, side-of-road, orientation, paired-feature, or agent-readable feature fields are provided",
            "must_use_only_listed_entities": True,
            "must_respect_legal_endpoint_flags_and_blocked_edges": True,
            "must_check_outgoing_mask_at_every_step": True,
            "must_stay_inside_inclusive_x_y_ranges": True,
            "must_return_compressed_move_runs": True,
            "unknown_or_unreachable_action": "status=failed",
            "response_rules": [
                "只输出一个 JSON 对象，不要解释、推理、Markdown 或额外文本",
                "不要输出 reasoning_content、路线说明、候选列表或搜索过程",
                "completed 时只填写 pickup_node_id、dropoff_node_id、move_runs，reason 必须为 null",
                "failed 时 move_runs 必须为空数组，reason 只能是简短错误码",
                "move_runs 是按行驶顺序排列的方向段数组，每项只含 direction 和 steps；方向只允许 E/W/N/S，steps 是正整数",
                "方向含义：E=x+1，W=x-1，N=y+1，S=y-1；从 start_node_id 逐段行驶，必须经过 pickup 后到达 dropoff",
                "每一步都先定位当前节点，再读取 outgoing_mask_by_y 对应十六进制位；只有对应方向位为 1 才能移动，缺少该位、没有道路或边被封闭都表示禁止通行",
                "E/W/N/S 位值分别为 1/2/4/8；例如 mask=5 只允许 E 和 N。x_range 和 y_range 为含边界范围，禁止越界",
                "只有在路线约束全部合法时才返回 completed；无法确认合法有向路径时返回 status=failed，不得臆造道路或继续输出违规路线",
                "语义地点只能依据任务文本、普通地标名称/别名、道路图和合法上下车标记解析；道路特征实体未提供",
                "连续相同方向必须合并为一个方向段，禁止逐步重复列出单字符路线",
            ],
        },
        "output_contract": {
            "status": "completed or failed",
            "pickup_node_id": "selected node id or null",
            "dropoff_node_id": "selected node id or null",
            "move_runs": [{"direction": "E", "steps": 2}, {"direction": "N", "steps": 2}],
            "reason": "null when completed, otherwise one short error code",
        },
        "output_example": {
            "status": "completed",
            "pickup_node_id": "node_x_y",
            "dropoff_node_id": "node_x2_y2",
            "move_runs": [
                {"direction": "E", "steps": 2},
                {"direction": "N", "steps": 2},
                {"direction": "S", "steps": 2},
            ],
            "reason": None,
        },
    }
    system = (
        "你是一个只执行路径搜索的路线规划器。只依据用户消息中的结构化地图和任务输入工作。"
        "必须输出 JSON 对象；禁止输出解释、推理过程、Markdown、候选路线或任何额外文本。"
        "每一步都必须按照当前节点的有向出口掩码核验，禁止单行逆行、越界或经过封闭边。"
        "completed 时必须用 move_runs 方向段返回可校验的路径；无法完成时返回 status=failed。"
    )
    user = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate_llm_route(
    road_map: RoadMap,
    task: VehicleOrderTask,
    route: RouteCandidate,
    selected_pickup_node_id: Optional[str] = None,
    selected_dropoff_node_id: Optional[str] = None,
    blocked_edge_ids: Iterable[str] = (),
) -> ValidationResult:
    """Validate model output against the shared map and endpoint contract."""

    errors: List[str] = []
    node_by_id = {node.node_id: node for node in road_map.nodes}
    edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
    blocked: Set[str] = set(blocked_edge_ids)
    nodes = route.node_sequence
    edges = route.edge_sequence
    if not nodes or not edges:
        errors.append("route_sequence_empty")
    if nodes and nodes[0] != task.start_node_id:
        errors.append("route_start_mismatch")
    if len(nodes) != len(edges) + 1:
        errors.append("route_edge_node_length_mismatch")
    for node_id in nodes:
        if node_id not in node_by_id:
            errors.append("node_missing:" + node_id)
    for edge_id in edges:
        if edge_id not in edge_by_id:
            errors.append("edge_missing:" + edge_id)
        elif edge_id in blocked:
            errors.append("blocked_edge_used:" + edge_id)
    direction_valid = True
    for index, edge_id in enumerate(edges):
        if edge_id not in edge_by_id or index + 1 >= len(nodes):
            direction_valid = False
            continue
        edge = edge_by_id[edge_id]
        if edge.from_node != nodes[index] or edge.to_node != nodes[index + 1]:
            direction_valid = False
            errors.append("route_edge_node_discontinuity")
    if not direction_valid and "route_edge_node_discontinuity" not in errors:
        errors.append("route_edge_node_discontinuity")

    selected_pickup_node_id = selected_pickup_node_id or (
        nodes[-1] if task.dropoff_node_id is None and nodes else None
    )
    selected_dropoff_node_id = selected_dropoff_node_id or (
        nodes[-1] if nodes else None
    )
    if selected_pickup_node_id not in node_by_id:
        errors.append("pickup_node_missing")
    if selected_dropoff_node_id not in node_by_id:
        errors.append("dropoff_node_missing")
    pickup_node = node_by_id.get(selected_pickup_node_id)
    dropoff_node = node_by_id.get(selected_dropoff_node_id)
    legal_pickup = bool(pickup_node and pickup_node.legal_pickup)
    legal_dropoff = bool(dropoff_node and dropoff_node.legal_dropoff)
    if not legal_pickup:
        errors.append("pickup_not_legal")
    if not legal_dropoff:
        errors.append("dropoff_not_legal")
    pickup_expected = task.pickup_node_id or task.true_pickup_node_id
    dropoff_expected = task.dropoff_node_id or task.true_dropoff_node_id
    pickup_exact = bool(pickup_expected and selected_pickup_node_id == pickup_expected)
    dropoff_exact = bool(dropoff_expected and selected_dropoff_node_id == dropoff_expected)
    if pickup_expected and not pickup_exact:
        errors.append("pickup_endpoint_mismatch")
    if dropoff_expected and not dropoff_exact:
        errors.append("dropoff_endpoint_mismatch")
    if selected_pickup_node_id and selected_pickup_node_id in nodes:
        pickup_index = nodes.index(selected_pickup_node_id)
    else:
        pickup_index = -1
    if selected_dropoff_node_id and selected_dropoff_node_id in nodes:
        dropoff_index = nodes.index(selected_dropoff_node_id)
    else:
        dropoff_index = -1
    if pickup_index < 0 or dropoff_index <= pickup_index:
        errors.append("route_endpoint_order_invalid")
    if selected_dropoff_node_id and nodes and nodes[-1] != selected_dropoff_node_id:
        errors.append("route_does_not_terminate_at_dropoff")
    valid = not errors
    return ValidationResult(
        valid=valid,
        status="valid" if valid else "invalid",
        errors=errors,
        pickup_exact_match=pickup_exact,
        dropoff_exact_match=dropoff_exact,
        legal_pickup=legal_pickup,
        legal_dropoff=legal_dropoff,
        boundary_violation=any(node_id not in node_by_id for node_id in nodes),
        direction_valid=direction_valid,
    )


def _llm_route_trace(
    task: VehicleOrderTask,
    route: Optional[RouteCandidate],
    selected_pickup_node_id: Optional[str],
    selected_dropoff_node_id: Optional[str],
    status: str,
    move_replay: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Represent a model route as a replayable trace for the shared renderer."""

    node_sequence = list(route.node_sequence) if route is not None else []
    edge_sequence = list(route.edge_sequence) if route is not None else []
    legs = []
    if node_sequence:
        pickup = selected_pickup_node_id or task.pickup_node_id
        dropoff = selected_dropoff_node_id or task.dropoff_node_id
        legs = [
            {
                "schema_version": "llm_route_leg_trace_v1",
                "source_node_id": task.start_node_id,
                "target_node_id": pickup,
                "status": "completed" if pickup in node_sequence else "invalid",
                "expanded_node_ids": [],
                "expanded_events": [],
                "route_node_sequence": node_sequence,
                "route_edge_sequence": edge_sequence,
            },
            {
                "schema_version": "llm_route_leg_trace_v1",
                "source_node_id": pickup,
                "target_node_id": dropoff,
                "status": "completed" if dropoff in node_sequence else "invalid",
                "expanded_node_ids": [],
                "expanded_events": [],
                "route_node_sequence": node_sequence,
                "route_edge_sequence": edge_sequence,
            },
        ]
    trace = {
        "schema_version": "route_search_trace_v1",
        "method_name": LLMRoutePlanner.method_name,
        "algorithm": "llm",
        "status": status,
        "legs": legs,
        "expanded_nodes": 0,
        "route_node_sequence": node_sequence,
        "route_edge_sequence": edge_sequence,
    }
    if move_replay is not None:
        trace.update(move_replay)
        trace["legs"] = [
            {
                "schema_version": "llm_route_leg_trace_v1",
                "source_node_id": task.start_node_id,
                "target_node_id": selected_pickup_node_id or task.pickup_node_id,
                "status": "invalid" if move_replay["move_violations"] else "completed",
                "expanded_node_ids": [],
                "expanded_events": [],
            },
            {
                "schema_version": "llm_route_leg_trace_v1",
                "source_node_id": selected_pickup_node_id or task.pickup_node_id,
                "target_node_id": selected_dropoff_node_id or task.dropoff_node_id,
                "status": "not_started" if move_replay["move_violations"] else "completed",
                "expanded_node_ids": [],
                "expanded_events": [],
            },
        ]
    return trace


class LLMRoutePlanner:
    """Run one pure LLM route task and retain the raw trace metadata."""

    method_name = "pure_llm_deepseek"

    def __init__(self, client: DeepSeekClient) -> None:
        self.client = client

    def plan(
        self,
        road_map: RoadMap,
        task: VehicleOrderTask,
        blocked_edge_ids: Iterable[str] = (),
    ) -> RouteMethodResult:
        started = time.perf_counter()
        blocked = set(blocked_edge_ids)
        input_map_encoding = None
        prompt_char_count = None
        prompt_metadata = {
            "prompt_schema_version": "pure_llm_route_input_v2",
            "map_input_mode": "graph_only",
            "roadside_features_provided": False,
            "route_repair": "none",
        }
        try:
            package, package_task, resolution = _package_for_prompt(
                road_map, task, blocked
            )
            encoded_map = _compact_grid_map_payload(package)
            input_map_encoding = (
                encoded_map.get("encoding")
                if encoded_map is not None
                else "explicit-edge-array-v1"
            )
            messages = build_route_messages(
                road_map, task, package=package, blocked_edge_ids=blocked,
                encoded_map=encoded_map,
            )
            prompt_char_count = len(messages[-1]["content"])
            response = self.client.chat(messages)
        except Exception as error:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return RouteMethodResult(
                task_id=task.task_id,
                method_name=self.method_name,
                status="failed",
                failure_reason=str(error),
                resources=ResourceUsage(
                    llm_calls=1,
                    compute_time_ms=elapsed_ms,
                ),
                metadata={
                    **prompt_metadata,
                    "compute_time_ms": elapsed_ms,
                    "llm_status": "request_failed",
                    "llm_error": str(error),
                    "input_map_encoding": input_map_encoding,
                    "prompt_char_count": prompt_char_count,
                },
                raw_output={"status": "failed", "reason": str(error)},
            )

        try:
            output = parse_route_output(response.content)
        except Exception as error:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return RouteMethodResult(
                task_id=task.task_id,
                method_name=self.method_name,
                status="failed",
                failure_reason="llm_output_parse_failed",
                resources=ResourceUsage(
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    llm_calls=1,
                    llm_latency_ms=response.latency_ms,
                    compute_time_ms=elapsed_ms,
                ),
                metadata={
                    **prompt_metadata,
                    "compute_time_ms": elapsed_ms,
                    "llm_status": "output_parse_failed",
                    "input_map_encoding": input_map_encoding,
                    "prompt_char_count": prompt_char_count,
                    "llm_model": response.model,
                    "llm_request_id": response.request_id,
                    "llm_error": str(error),
                    "llm_retries": response.retries,
                },
                raw_output={"content": response.content, "error": str(error)},
            )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if output["status"] == "failed":
            return RouteMethodResult(
                task_id=task.task_id,
                method_name=self.method_name,
                status="failed",
                failure_reason=output["reason"],
                resources=ResourceUsage(
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    llm_calls=1,
                    llm_latency_ms=response.latency_ms,
                    compute_time_ms=elapsed_ms,
                ),
                metadata={
                    **prompt_metadata,
                    "compute_time_ms": elapsed_ms,
                    "llm_status": "model_failed",
                    "input_map_encoding": input_map_encoding,
                    "prompt_char_count": prompt_char_count,
                    "llm_model": response.model,
                    "llm_request_id": response.request_id,
                    "llm_retries": response.retries,
                    "semantic_resolution": resolution,
                    "search_trace": _llm_route_trace(
                        task, None, output.get("pickup_node_id"), output.get("dropoff_node_id"), "failed"
                    ),
                },
                raw_output=output,
            )

        node_sequence = output.get("node_sequence") or []
        edge_sequence = output.get("edge_sequence") or []
        if output.get("move_sequence"):
            try:
                node_sequence, edge_sequence = _route_from_moves(
                    road_map, task.start_node_id, output["move_sequence"],
                    blocked_edge_ids=blocked,
                )
            except ValueError as error:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                return RouteMethodResult(
                    task_id=task.task_id,
                    method_name=self.method_name,
                    status="failed",
                    failure_reason="llm_move_sequence_invalid",
                    resources=ResourceUsage(
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        llm_calls=1,
                        llm_latency_ms=response.latency_ms,
                        compute_time_ms=elapsed_ms,
                    ),
                    metadata={
                        **prompt_metadata,
                        "compute_time_ms": elapsed_ms,
                        "llm_status": "move_sequence_invalid",
                        "input_map_encoding": input_map_encoding,
                        "prompt_char_count": prompt_char_count,
                        "llm_model": response.model,
                        "llm_request_id": response.request_id,
                        "llm_retries": response.retries,
                        "semantic_resolution": resolution,
                        "move_sequence": output["move_sequence"],
                        "llm_error": str(error),
                        "package_expansion_count": package.expansion_count,
                        "package_node_count": len(package.nodes),
                        "package_edge_count": len(package.edges),
                        "package_token_estimate": package.serialized_token_estimate,
                        "search_trace": _llm_route_trace(
                            task,
                            None,
                            output.get("pickup_node_id"),
                            output.get("dropoff_node_id"),
                            "invalid",
                            move_replay=getattr(error, "replay", None),
                        ),
                    },
                    raw_output=output,
                )

        route = RouteCandidate(
            route_id=self.method_name + "-" + task.task_id,
            node_sequence=node_sequence,
            edge_sequence=edge_sequence,
            route_source=self.method_name,
            route_reachability=True,
        )
        validation = validate_llm_route(
            road_map,
            task,
            route,
            selected_pickup_node_id=output.get("pickup_node_id"),
            selected_dropoff_node_id=output.get("dropoff_node_id"),
            blocked_edge_ids=blocked,
        )
        route.validation_status = "valid" if validation.valid else "invalid"
        route.validation_errors = list(validation.errors)
        return RouteMethodResult(
            task_id=task.task_id,
            method_name=self.method_name,
            status="completed" if validation.valid else "failed",
            routes=[route],
            best_route_id=route.route_id if validation.valid else None,
            valid_route_count=1 if validation.valid else 0,
            invalid_route_count=0 if validation.valid else 1,
            failure_reason=None if validation.valid else "llm_route_invalid",
            validation=validation,
            resources=ResourceUsage(
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                llm_calls=1,
                llm_latency_ms=response.latency_ms,
                compute_time_ms=elapsed_ms,
            ),
            metadata={
                **prompt_metadata,
                "compute_time_ms": elapsed_ms,
                "llm_status": "validated",
                "input_map_encoding": input_map_encoding,
                "prompt_char_count": prompt_char_count,
                "llm_model": response.model,
                "llm_request_id": response.request_id,
                "llm_retries": response.retries,
                "package_expansion_count": package.expansion_count,
                "package_node_count": len(package.nodes),
                "package_edge_count": len(package.edges),
                "semantic_resolution": resolution,
                "search_trace": _llm_route_trace(
                    task,
                    route,
                    output.get("pickup_node_id"),
                    output.get("dropoff_node_id"),
                    "completed" if validation.valid else "invalid",
                ),
            },
            raw_output=output,
        )


__all__ = [
    "DeepSeekClient",
    "DeepSeekResponse",
    "LLMRoutePlanner",
    "build_route_messages",
    "parse_route_output",
    "validate_llm_route",
]
