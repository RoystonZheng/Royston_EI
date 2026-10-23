"""Deterministic grounding of explicit landmark-side phrases.

The resolver only maps a model-selected, query-whitelisted landmark and an
explicit cardinal direction in the user's text to one of the supplied legal
pickup candidates. It does not inspect evaluator truth or search the road graph.
"""

from __future__ import annotations

from typing import Any, Dict

_DIRECTION_WORDS = {
    "东": "east",
    "东侧": "east",
    "西": "west",
    "西侧": "west",
    "南": "south",
    "南侧": "south",
    "北": "north",
    "北侧": "north",
}


def _explicit_direction(text: Any) -> Any:
    if not isinstance(text, str):
        return None
    for label, direction in sorted(_DIRECTION_WORDS.items(), key=lambda item: -len(item[0])):
        if label in text:
            return direction
    return None


def resolve_landmark_near_candidate(
    parsed_intent: Any, model_query: Dict[str, Any], endpoint: str
) -> Dict[str, Any]:
    """Choose the nearest legal candidate for an explicit landmark-nearby request."""

    if endpoint not in {"pickup", "dropoff"}:
        return {"status": "unresolved", "reason": "endpoint_role_invalid"}
    text = model_query.get(endpoint + "_text")
    if not isinstance(text, str) or "附近" not in text:
        return {"status": "unresolved", "reason": "explicit_near_relation_missing"}
    if (
        not isinstance(parsed_intent, dict)
        or parsed_intent.get("schema_version") != "route_intent_candidate_v2"
        or parsed_intent.get("status") != "resolved"
    ):
        return {"status": "unresolved", "reason": "model_intent_unresolved"}

    anchor_ref = parsed_intent.get(endpoint + "_anchor_ref")
    anchor = next(
        (
            entry
            for entry in model_query.get("matched_" + endpoint + "_entries", [])
            if entry.get("entry_id") == anchor_ref
            and entry.get("entry_type") == "landmark"
        ),
        None,
    )
    coordinates = (anchor or {}).get("anchor_coordinates")
    if not isinstance(coordinates, dict) or not isinstance(anchor_ref, str):
        return {"status": "unresolved", "reason": endpoint + "_anchor_not_in_query"}
    try:
        anchor_x = int(coordinates["x"])
        anchor_y = int(coordinates["y"])
    except (KeyError, TypeError, ValueError):
        return {"status": "unresolved", "reason": endpoint + "_anchor_coordinates_invalid"}

    candidates = model_query.get(endpoint + "_candidates", [])
    eligible = []
    for index, candidate in enumerate(candidates):
        candidate_ref = candidate.get("candidate_ref")
        candidate_coordinates = candidate.get("coordinates")
        if not isinstance(candidate_ref, str) or not isinstance(candidate_coordinates, dict):
            continue
        try:
            x = int(candidate_coordinates["x"])
            y = int(candidate_coordinates["y"])
        except (KeyError, TypeError, ValueError):
            continue
        distance = abs(x - anchor_x) + abs(y - anchor_y)
        # The frozen task generator explicitly excludes the landmark's own
        # node for nearby pickups. Dropoffs may be located at the landmark.
        if endpoint == "pickup" and distance == 0:
            continue
        eligible.append((distance, index, candidate_ref))

    if not eligible:
        return {
            "status": "unresolved",
            "reason": "no_legal_candidate_after_endpoint_exclusion",
        }
    _, _, candidate_ref = min(eligible)
    return {
        "status": "resolved",
        "candidate_ref": candidate_ref,
        "anchor_ref": anchor_ref,
        "relation": "near",
        "grounding_method": "nearest_landmark_candidate_v1",
    }


def resolve_landmark_relative_candidate(
    parsed_intent: Any, model_query: Dict[str, Any]
) -> Dict[str, Any]:
    """Return the nearest public candidate on the explicitly requested side.

    Candidate ordering is already canonical in the materialized query. Ties
    are resolved by that order, which is stable for a frozen map/query.
    """

    if model_query.get("task_type") != "semantic_landmark_relative":
        return {"status": "unresolved", "reason": "not_relative_landmark_task"}
    if (
        not isinstance(parsed_intent, dict)
        or parsed_intent.get("schema_version") != "route_intent_candidate_v2"
        or parsed_intent.get("status") != "resolved"
    ):
        return {"status": "unresolved", "reason": "model_intent_unresolved"}

    direction = _explicit_direction(model_query.get("pickup_text"))
    if direction is None:
        return {"status": "unresolved", "reason": "explicit_direction_missing"}

    anchor_ref = parsed_intent.get("pickup_anchor_ref")
    anchor = next(
        (
            entry
            for entry in model_query.get("matched_pickup_entries", [])
            if entry.get("entry_id") == anchor_ref
            and entry.get("entry_type") == "landmark"
        ),
        None,
    )
    coordinates = (anchor or {}).get("anchor_coordinates")
    if not isinstance(coordinates, dict) or not isinstance(anchor_ref, str):
        return {"status": "unresolved", "reason": "pickup_anchor_not_in_query"}
    try:
        anchor_x = int(coordinates["x"])
        anchor_y = int(coordinates["y"])
    except (KeyError, TypeError, ValueError):
        return {"status": "unresolved", "reason": "pickup_anchor_coordinates_invalid"}

    eligible = []
    for index, candidate in enumerate(model_query.get("pickup_candidates", [])):
        candidate_ref = candidate.get("candidate_ref")
        candidate_coordinates = candidate.get("coordinates")
        if not isinstance(candidate_ref, str) or not isinstance(candidate_coordinates, dict):
            continue
        try:
            x = int(candidate_coordinates["x"])
            y = int(candidate_coordinates["y"])
        except (KeyError, TypeError, ValueError):
            continue

        dx = x - anchor_x
        dy = y - anchor_y
        on_requested_side = {
            "east": dx > 0,
            "west": dx < 0,
            "north": dy > 0,
            "south": dy < 0,
        }[direction]
        if on_requested_side:
            eligible.append((abs(dx) + abs(dy), index, candidate_ref))

    if not eligible:
        return {
            "status": "unresolved",
            "reason": "no_candidate_on_requested_side",
            "anchor_ref": anchor_ref,
            "direction": direction,
        }

    _, _, candidate_ref = min(eligible)
    return {
        "status": "resolved",
        "candidate_ref": candidate_ref,
        "anchor_ref": anchor_ref,
        "direction": direction,
        "grounding_method": "explicit_direction_and_candidate_geometry_v1",
    }
