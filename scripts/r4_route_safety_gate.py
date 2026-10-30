"""Fail-closed admission checks for the current formal R4 route-safety gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional


GATE_SCHEMA_VERSION = "r4_route_safety_gate_v2"
ACCEPTANCE_SCHEMA_VERSION = "r4_route_safety_acceptance_v2"
REQUIRED_CHECKS = (
    "online_step_chain",
    "zero_call_recovery_replay",
    "full_flow_sumo",
    "paired_candidate_decision",
)
REQUIRED_PROFILES = {
    "time_priority_0_6_0_4",
    "money_priority_0_4_0_6",
}
REQUIRED_SOURCE_FILES = (
    "scripts/r4_route_safety_gate.py",
    "scripts/run_r4_codex_formal_batch.py",
    "scripts/run_r4_codex_pilot_continuation.py",
    "scripts/run_r4_formal_chunk_driver.py",
    "scripts/r4_parallel_formal_batch.py",
    "scripts/run_r4_grid_route_sumo_acceptance.py",
    "scripts/build_r4_shared_candidate_pool_v1.py",
    "route_experiment/r4_stepwise.py",
    "route_experiment/decision.py",
    "route_experiment/energy_model.py",
    "tests/test_r4_codex_formal_batch.py",
    "tests/test_r4_codex_pilot_acceptance.py",
    "tests/test_r4_parallel_formal_batch.py",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _artifact_path(value: Any, project_root: Path) -> Optional[Path]:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return (path if path.is_absolute() else project_root / path).resolve()


def _acceptance_block_reason(
    gate: Dict[str, Any], project_root: Path
) -> Optional[str]:
    if gate.get("schema_version") != GATE_SCHEMA_VERSION:
        return "r4_route_safety_gate_not_passed:current_acceptance_evidence_missing"
    if gate.get("status") != "passed" or gate.get("formal_continuation_permitted") is not True:
        return "r4_route_safety_gate_not_passed:" + str(gate.get("status") or "pending")

    artifact = gate.get("acceptance_artifact")
    if not isinstance(artifact, dict):
        return "r4_route_safety_gate_not_passed:acceptance_artifact_missing"
    artifact_path = _artifact_path(artifact.get("path"), project_root)
    expected_sha = artifact.get("sha256")
    if artifact_path is None or not artifact_path.is_file() or not _valid_sha(expected_sha):
        return "r4_route_safety_gate_not_passed:acceptance_artifact_unavailable"
    if sha256_file(artifact_path) != expected_sha:
        return "r4_route_safety_gate_not_passed:acceptance_artifact_hash_mismatch"
    try:
        evidence = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "r4_route_safety_gate_not_passed:acceptance_artifact_invalid_json"
    if evidence.get("schema_version") != ACCEPTANCE_SCHEMA_VERSION or evidence.get("status") != "passed":
        return "r4_route_safety_gate_not_passed:acceptance_artifact_not_passed"
    if evidence.get("model_calls") != 0:
        return "r4_route_safety_gate_not_passed:acceptance_must_be_zero_model_call"

    checks = evidence.get("checks")
    if not isinstance(checks, dict):
        return "r4_route_safety_gate_not_passed:acceptance_checks_missing"
    if any(not isinstance(checks.get(name), dict) or checks[name].get("status") != "passed"
           for name in REQUIRED_CHECKS):
        return "r4_route_safety_gate_not_passed:acceptance_checks_incomplete"

    online = checks["online_step_chain"]
    if any(online.get(key) is not True for key in (
        "formal_entrypoint_guarded", "direct_one_step_guarded", "local_edge_validator_enabled"
    )):
        return "r4_route_safety_gate_not_passed:online_step_chain_not_integrated"

    replay = checks["zero_call_recovery_replay"]
    if (
        replay.get("model_calls") != 0
        or replay.get("illegal_candidate_rejected") is not True
        or replay.get("rollback_count_nodes") != 1
        or replay.get("reroute_from_rollback_node") is not True
        or replay.get("completed_route") is not True
        or replay.get("one_way_violations") != 0
    ):
        return "r4_route_safety_gate_not_passed:zero_call_recovery_replay_incomplete"

    sumo = checks["full_flow_sumo"]
    if (
        sumo.get("vehicle_completed") is not True
        or sumo.get("route_edge_sequence_matched") is not True
        or sumo.get("one_way_violations") != 0
        or not sumo.get("sumo_log_sha256")
        or not sumo.get("validator_audit_sha256")
    ):
        return "r4_route_safety_gate_not_passed:full_flow_sumo_incomplete"

    paired = checks["paired_candidate_decision"]
    profiles = paired.get("profiles")
    if (
        not isinstance(profiles, dict)
        or set(profiles) != REQUIRED_PROFILES
        or not _valid_sha(paired.get("candidate_pool_sha256"))
    ):
        return "r4_route_safety_gate_not_passed:paired_candidate_profiles_missing"
    for profile_id, profile in profiles.items():
        if (
            profile.get("candidate_count", 0) < 1
            or not profile.get("selected_candidate_id")
            or not profile.get("selection_reason")
            or profile.get("ev2_status") != "passed"
            or profile.get("sumo_status") != "passed"
            or profile.get("sumo_one_way_violations") != 0
        ):
            return "r4_route_safety_gate_not_passed:paired_profile_result_incomplete:" + profile_id
        if profile.get("candidate_count") == 1 and not profile.get("single_candidate_no_weight_effect"):
            return "r4_route_safety_gate_not_passed:single_candidate_weight_effect_not_disclosed:" + profile_id

    source_hashes = evidence.get("source_sha256")
    if not isinstance(source_hashes, dict):
        return "r4_route_safety_gate_not_passed:source_hashes_missing"
    for relative in REQUIRED_SOURCE_FILES:
        current = project_root / relative
        recorded = source_hashes.get(relative)
        if not current.is_file() or not _valid_sha(recorded) or sha256_file(current) != recorded:
            return "r4_route_safety_gate_not_passed:source_hash_mismatch:" + relative

    config_hashes = evidence.get("config_sha256")
    if not isinstance(config_hashes, dict) or not config_hashes or any(
        not _valid_sha(value) for value in config_hashes.values()
    ):
        return "r4_route_safety_gate_not_passed:config_hashes_missing"
    return None


def route_safety_gate_block_reason(
    manifest: Dict[str, Any], project_root: Path
) -> Optional[str]:
    """Block stale or incomplete declared gates; leave unrelated legacy runs alone."""
    gate = manifest.get("route_safety_gate")
    if gate is None:
        return None
    if not isinstance(gate, dict):
        return "r4_route_safety_gate_not_passed:invalid_gate_record"
    return _acceptance_block_reason(gate, Path(project_root).resolve())


def route_safety_gate_block_reason_for_path(
    run_or_batch_path: Path, project_root: Path
) -> Optional[str]:
    """Find a declared R4 batch gate above a run directory and validate it."""
    path = Path(run_or_batch_path).resolve()
    candidates = (path, *path.parents)
    for directory in candidates:
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "route_safety_gate" in manifest:
            return route_safety_gate_block_reason(manifest, project_root)
    return None
