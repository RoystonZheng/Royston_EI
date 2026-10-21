"""Map-derived hard constraints for R4 route output.

The strict tool schema limits every emitted route action to a directed edge
present in the task's local ``RouteInputPackage``.  It does not constrain
continuity or endpoint order; those remain the independent validator's job.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import Edge, RouteInputPackage


_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DIRECTION_INDEX = {"E": 0, "W": 1, "N": 2, "S": 3}
_DIRECTION_DELTA = {
    "E": (1, 0),
    "W": (-1, 0),
    "N": (0, 1),
    "S": (0, -1),
}
_TOOL_NAME = "submit_r4_route"


def _base36_slot(slot: int) -> str:
    if slot < 0 or slot >= len(_BASE36) ** 3:
        raise ValueError("r4_edge_slot_index_out_of_range")
    chars = []
    value = slot
    for _ in range(3):
        chars.append(_BASE36[value % len(_BASE36)])
        value //= len(_BASE36)
    return "".join(reversed(chars))


def _slot_set_regex(
    codes: List[str], *, allow_empty: bool = True, max_items: Optional[int] = None
) -> str:
    """Build a compact, anchored regex for a fixed-width base36 code set."""

    if not codes:
        raise ValueError("r4_route_constraint_has_no_directed_edges")
    root: Dict[str, dict] = {}
    for code in sorted(set(codes)):
        if len(code) != 3 or any(char not in _BASE36 for char in code):
            raise ValueError("r4_edge_slot_code_invalid")
        cursor = root
        for char in code:
            cursor = cursor.setdefault(char, {})
        cursor[""] = {}

    def render(node: Dict[str, dict], depth: int) -> str:
        if depth == 3:
            return ""
        by_suffix: Dict[str, List[str]] = {}
        for char in _BASE36:
            child = node.get(char)
            if child is not None:
                by_suffix.setdefault(render(child, depth + 1), []).append(char)
        alternatives = []
        for suffix, chars in by_suffix.items():
            prefix = chars[0] if len(chars) == 1 else "[" + "".join(chars) + "]"
            alternatives.append(prefix + suffix)
        if len(alternatives) == 1:
            return alternatives[0]
        return "(?:" + "|".join(alternatives) + ")"

    token_pattern = render(root, 0)
    if max_items is None:
        repetition = "*" if allow_empty else "+"
    else:
        minimum = 0 if allow_empty else 1
        if max_items < minimum:
            raise ValueError("r4_route_constraint_max_items_invalid")
        repetition = "{{{0},{1}}}".format(minimum, max_items)
    return "^(?:" + token_pattern + ")" + repetition + "$"


@dataclass
class R4RouteOutputConstraint:
    pickup_node_id: str
    dropoff_node_id: str
    edge_id_by_slot_code: Dict[str, str]
    edge_by_slot_code: Dict[str, Edge]
    edge_slot_pattern: str
    max_route_edges: int
    allow_failed: bool = True

    def tool_definition(self) -> dict:
        """Return a DeepSeek strict-mode function tool definition."""

        parameters = {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["completed", "failed"] if self.allow_failed else ["completed"],
                },
                "pickup_node_id": {
                    "type": "string",
                    "enum": [self.pickup_node_id],
                },
                "dropoff_node_id": {
                    "type": "string",
                    "enum": [self.dropoff_node_id],
                },
                "edge_slot_sequence": {
                    "type": "string",
                    "pattern": self.edge_slot_pattern,
                },
                "step_check_sequence": {
                    "type": "string",
                    "pattern": "^1{{{0},{1}}}$".format(
                        0 if self.allow_failed else 1, self.max_route_edges
                    ),
                    "description": (
                        "One '1' per route edge in order. Each '1' records the "
                        "model's explicit per-step directed-edge self-check."
                    ),
                },
                "failure_reason_code": {
                    "type": "string",
                    "enum": (
                        ["none", "no_directed_path", "unable_to_construct_route"]
                        if self.allow_failed
                        else ["none"]
                    ),
                    "description": (
                        "Use none for a completed route; distinguish a proven lack of a directed path "
                        "from inability to construct a route."
                    ),
                },
            },
            "required": [
                "status",
                "pickup_node_id",
                "dropoff_node_id",
                "edge_slot_sequence",
                "step_check_sequence",
                "failure_reason_code",
            ],
            "additionalProperties": False,
        }
        return {
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "strict": True,
                "description": (
                    "Submit the route as concatenated three-character base36 "
                    "directed-edge slot codes. Only map-present directed edges "
                    "are permitted by the schema."
                ),
                "parameters": parameters,
            },
        }

    @property
    def tool_choice(self) -> dict:
        return {"type": "function", "function": {"name": _TOOL_NAME}}

    @property
    def schema_sha256(self) -> str:
        canonical = json.dumps(
            self.tool_definition(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def schema_bytes(self) -> int:
        canonical = json.dumps(
            self.tool_definition(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return len(canonical.encode("utf-8"))

    def normalize_payload(self, payload: dict) -> dict:
        """Convert constrained edge slots into the validator's normal route shape."""

        if payload.get("pickup_node_id") != self.pickup_node_id:
            raise ValueError("r4_endpoint_constraint_violated:pickup")
        if payload.get("dropoff_node_id") != self.dropoff_node_id:
            raise ValueError("r4_endpoint_constraint_violated:dropoff")
        status = payload.get("status")
        failure_reason_code = payload.get("failure_reason_code")
        if status == "failed" and not self.allow_failed:
            raise ValueError("r4_failed_response_not_allowed")
        if failure_reason_code not in {
            "none",
            "no_directed_path",
            "unable_to_construct_route",
        }:
            raise ValueError("r4_failure_reason_code_invalid")
        step_check_sequence = payload.get("step_check_sequence")
        if not isinstance(step_check_sequence, str) or re.fullmatch(
            r"1*", step_check_sequence
        ) is None:
            raise ValueError("r4_step_check_sequence_invalid")
        if status == "failed":
            if failure_reason_code == "none":
                raise ValueError("r4_failure_reason_code_status_mismatch")
            if payload.get("edge_slot_sequence", "") or step_check_sequence:
                raise ValueError("r4_failed_response_must_have_empty_route")
            return {
                "status": "failed",
                "reason": (
                    "model_confirmed_no_directed_path"
                    if failure_reason_code == "no_directed_path"
                    else "model_unable_to_construct_route"
                ),
                "pickup_node_id": self.pickup_node_id,
                "dropoff_node_id": self.dropoff_node_id,
                "move_sequence": "",
                "node_sequence": [],
                "edge_sequence": [],
                "step_check_sequence": "",
                "step_check_records": [],
            }
        if status != "completed":
            raise ValueError("r4_route_constraint_status_invalid")
        if failure_reason_code != "none":
            raise ValueError("r4_failure_reason_code_status_mismatch")

        sequence = payload.get("edge_slot_sequence")
        if not isinstance(sequence, str) or len(sequence) % 3 != 0:
            raise ValueError("r4_edge_slot_sequence_invalid")
        if not sequence:
            raise ValueError("r4_edge_slot_sequence_empty")
        if len(sequence) // 3 > self.max_route_edges:
            raise ValueError("r4_route_edge_sequence_too_long")
        if re.fullmatch(self.edge_slot_pattern, sequence) is None:
            raise ValueError("r4_edge_slot_constraint_violated")

        codes = [sequence[index : index + 3] for index in range(0, len(sequence), 3)]
        if len(step_check_sequence) != len(codes):
            raise ValueError("r4_step_check_sequence_count_mismatch")
        edges: List[Edge] = []
        for code in codes:
            edge = self.edge_by_slot_code.get(code)
            if edge is None:
                raise ValueError("r4_edge_slot_constraint_violated")
            edges.append(edge)

        return {
            "status": "completed",
            "reason": None,
            "pickup_node_id": self.pickup_node_id,
            "dropoff_node_id": self.dropoff_node_id,
            "move_sequence": "",
            "node_sequence": [edges[0].from_node] + [edge.to_node for edge in edges],
            "edge_sequence": [edge.edge_id for edge in edges],
            "edge_slot_sequence": sequence,
            "step_check_sequence": step_check_sequence,
            "step_check_records": [
                {
                    "step": index + 1,
                    "from_node_id": edge.from_node,
                    "to_node_id": edge.to_node,
                    "edge_id": edge.edge_id,
                    "model_edge_check": "PASS",
                }
                for index, edge in enumerate(edges)
            ],
        }


def build_r4_route_output_constraint(
    package: RouteInputPackage,
    pickup_node_id: str,
    dropoff_node_id: str,
    *,
    allow_failed: bool = True,
) -> R4RouteOutputConstraint:
    """Build a strict output schema from only the package's actual directed edges."""

    if not pickup_node_id or not dropoff_node_id or pickup_node_id == dropoff_node_id:
        raise ValueError("r4_route_constraint_endpoints_invalid")
    node_by_id = {node.node_id: node for node in package.nodes}
    required = {pickup_node_id, dropoff_node_id}
    if not required.issubset(node_by_id):
        raise ValueError("r4_route_constraint_endpoint_missing")
    if not node_by_id[pickup_node_id].legal_pickup:
        raise ValueError("r4_route_constraint_pickup_illegal")
    if not node_by_id[dropoff_node_id].legal_dropoff:
        raise ValueError("r4_route_constraint_dropoff_illegal")

    min_x = min(node.x for node in package.nodes)
    max_x = max(node.x for node in package.nodes)
    min_y = min(node.y for node in package.nodes)
    max_y = max(node.y for node in package.nodes)
    width = max_x - min_x + 1
    height = max_y - min_y + 1
    positions = {(node.x, node.y) for node in package.nodes}
    if len(positions) != width * height:
        raise ValueError("r4_route_constraint_package_not_rectangular")

    edge_by_slot_code: Dict[str, Edge] = {}
    edge_id_by_slot_code: Dict[str, str] = {}
    for edge in package.edges:
        source = node_by_id.get(edge.from_node)
        target = node_by_id.get(edge.to_node)
        if source is None or target is None:
            raise ValueError("r4_route_constraint_edge_endpoint_missing")
        direction = next(
            (
                name
                for name, delta in _DIRECTION_DELTA.items()
                if (target.x - source.x, target.y - source.y) == delta
            ),
            None,
        )
        if direction is None:
            raise ValueError("r4_route_constraint_edge_not_adjacent")
        slot = (
            ((source.y - min_y) * width + (source.x - min_x)) * 4
            + _DIRECTION_INDEX[direction]
        )
        code = _base36_slot(slot)
        if code in edge_by_slot_code:
            raise ValueError("r4_route_constraint_duplicate_slot")
        edge_by_slot_code[code] = edge
        edge_id_by_slot_code[code] = edge.edge_id

    return R4RouteOutputConstraint(
        pickup_node_id=pickup_node_id,
        dropoff_node_id=dropoff_node_id,
        edge_id_by_slot_code=edge_id_by_slot_code,
        edge_by_slot_code=edge_by_slot_code,
        edge_slot_pattern=_slot_set_regex(
            list(edge_by_slot_code),
            allow_empty=allow_failed,
            max_items=max(1, len(package.nodes) - 1),
        ),
        max_route_edges=max(1, len(package.nodes) - 1),
        allow_failed=allow_failed,
    )
