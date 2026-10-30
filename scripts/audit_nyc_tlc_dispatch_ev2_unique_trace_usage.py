#!/usr/bin/env python3
"""Audit unique Codex CLI trace usage referenced by an NYC EV2 result ledger.

The audit is read-only. It verifies source result hashes, resolves trace paths,
counts each JSONL trace once, and reports cross-method references such as D6/D7
replays of a D8 context trace. It does not infer account billing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected_json_object:{path}")
    return value


def resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def trace_origin_method(path: Path) -> str:
    parts = path.parts
    for marker in ("codex_cli_traces", "codex_context_policy_traces"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                return parts[index + 1]
    return "unknown"


def collect_trace_paths(value: Any, found: set[Path]) -> None:
    if isinstance(value, dict):
        trace_path = value.get("trace_jsonl_path")
        if isinstance(trace_path, str) and trace_path.strip():
            found.add(resolve_path(trace_path))
        for child in value.values():
            collect_trace_paths(child, found)
    elif isinstance(value, list):
        for child in value:
            collect_trace_paths(child, found)


def audit_trace(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    usage: Counter[str] = Counter()
    event_types: Counter[str] = Counter()
    malformed_lines: list[int] = []
    errors: list[dict[str, Any]] = []

    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            malformed_lines.append(line_number)
            continue
        if not isinstance(item, dict):
            malformed_lines.append(line_number)
            continue

        event_type = str(item.get("type", "<missing>"))
        event_types[event_type] += 1
        if event_type == "turn.started":
            usage["turn_started_count"] += 1
        elif event_type == "turn.completed":
            usage["turn_completed_count"] += 1
            turn_usage = item.get("usage")
            if isinstance(turn_usage, dict):
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                ):
                    amount = turn_usage.get(field)
                    if isinstance(amount, int) and not isinstance(amount, bool):
                        key = "reasoning_tokens" if field == "reasoning_output_tokens" else field
                        usage[key] += amount
                if (
                    "total_tokens" not in turn_usage
                    and isinstance(turn_usage.get("input_tokens"), int)
                    and isinstance(turn_usage.get("output_tokens"), int)
                ):
                    usage["total_tokens"] += (
                        turn_usage["input_tokens"] + turn_usage["output_tokens"]
                    )
        elif event_type in {"error", "turn.failed"}:
            if event_type == "error":
                usage["cli_error_event_count"] += 1
                message = str(item.get("message", ""))
                lower_message = message.lower()
                if "429" in lower_message or "too many requests" in lower_message:
                    usage["http_429_error_event_count"] += 1
                errors.append({"type": event_type, "message": message})
            else:
                usage["turn_failed_count"] += 1

    usage["malformed_line_count"] = len(malformed_lines)
    return {
        "trace_path": str(path),
        "trace_sha256": sha256_bytes(raw),
        "bytes": len(raw),
        "line_count": len(lines),
        "trace_origin_method": trace_origin_method(path),
        "usage": dict(usage),
        "event_type_counts": dict(event_types),
        "malformed_line_numbers": malformed_lines,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-audit", required=True, type=Path)
    parser.add_argument("--fingerprint-audit", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    ledger_path = resolve_path(args.ledger_audit)
    fingerprint_path = resolve_path(args.fingerprint_audit)
    output_path = resolve_path(args.output)
    ledger = read_json(ledger_path)
    fingerprint = read_json(fingerprint_path)

    errors: list[str] = []
    if ledger.get("schema_version") != "nyc-tlc-dispatch-ev2-segment-ledger-audit-v1":
        errors.append("unexpected_ledger_schema")
    if fingerprint.get("audited_result_count") != ledger.get("valid_method_runs"):
        errors.append("fingerprint_result_count_mismatch")
    if fingerprint.get("package_manifest_sha256") != ledger.get("package_manifest_sha256"):
        errors.append("package_manifest_hash_mismatch")
    if fingerprint.get("experiment_manifest_sha256") != ledger.get("experiment_manifest_sha256"):
        errors.append("experiment_manifest_hash_mismatch")
    if fingerprint.get("scenario_fingerprint_cell_mismatch_count") != 0:
        errors.append("scenario_fingerprint_cell_mismatch")
    if fingerprint.get("input_hash_cell_mismatch_count") != 0:
        errors.append("input_hash_cell_mismatch")
    if fingerprint.get("per_run_errors"):
        errors.append("fingerprint_per_run_errors_present")
    if fingerprint.get("all_present_results_match_frozen_inputs_and_contract") is not True:
        errors.append("frozen_input_contract_audit_not_passed")
    if ledger.get("valid_method_runs") != len(ledger.get("runs", [])):
        errors.append("ledger_valid_run_count_mismatch")
    if ledger.get("invalid_method_runs"):
        errors.append("invalid_method_runs_present")

    trace_references: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    declared_costs: dict[str, Counter[str]] = defaultdict(Counter)
    result_errors: list[str] = []
    for run in ledger.get("runs", []):
        result_path = resolve_path(run["path"])
        try:
            source_bytes = result_path.read_bytes()
        except OSError as error:
            result_errors.append(f"result_missing:{result_path}:{type(error).__name__}")
            continue
        source_sha = sha256_bytes(source_bytes)
        if source_sha != run.get("sha256"):
            result_errors.append(f"result_hash_mismatch:{result_path}")
            continue
        try:
            result = json.loads(source_bytes)
        except json.JSONDecodeError:
            result_errors.append(f"result_unreadable:{result_path}")
            continue
        method = str(result.get("method_name", run.get("method", "unknown")))
        if (
            result.get("case_id") != run.get("case_id")
            or result.get("fleet_id") != run.get("fleet_id")
            or method != run.get("method")
        ):
            result_errors.append(f"result_identity_mismatch:{result_path}")
            continue

        for key, value in (result.get("method_costs") or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                declared_costs[method][key] += value

        result_trace_paths: set[Path] = set()
        collect_trace_paths(result.get("round_audits") or [], result_trace_paths)
        for trace_path in result_trace_paths:
            trace_references[trace_path].append(
                {
                    "case_id": run.get("case_id"),
                    "fleet_id": run.get("fleet_id"),
                    "result_method": method,
                    "result_path": str(result_path),
                    "result_sha256": source_sha,
                }
            )

    if result_errors:
        errors.extend(result_errors)

    unique_traces: list[dict[str, Any]] = []
    missing_trace_paths: list[str] = []
    trace_errors: list[str] = []
    usage_by_origin: dict[str, Counter[str]] = defaultdict(Counter)
    for trace_path, references in sorted(trace_references.items(), key=lambda item: str(item[0])):
        if not trace_path.is_file():
            missing_trace_paths.append(str(trace_path))
            continue
        try:
            trace = audit_trace(trace_path)
        except (OSError, UnicodeDecodeError) as error:
            trace_errors.append(f"trace_unreadable:{trace_path}:{type(error).__name__}")
            continue
        trace["referencing_methods"] = sorted(
            {reference["result_method"] for reference in references}
        )
        trace["reference_count"] = len(references)
        trace["references"] = references
        unique_traces.append(trace)
        usage_by_origin[trace["trace_origin_method"]].update(trace["usage"])
        if trace["malformed_line_numbers"]:
            trace_errors.append(f"trace_malformed_jsonl:{trace_path}")
    errors.extend(f"trace_missing:{path}" for path in missing_trace_paths)
    errors.extend(trace_errors)

    shared_groups = [
        trace for trace in unique_traces if len(trace["referencing_methods"]) > 1
    ]
    ledger_sha = sha256_file(ledger_path)
    fingerprint_sha = sha256_file(fingerprint_path)
    fingerprint_ledger_path = fingerprint.get("source_official_audit_path")
    if fingerprint_ledger_path:
        try:
            if resolve_path(fingerprint_ledger_path) != ledger_path:
                errors.append("fingerprint_references_different_ledger_path")
            if fingerprint.get("source_official_audit_sha256") != ledger_sha:
                errors.append("fingerprint_ledger_hash_mismatch")
        except Exception as error:  # keep path provenance visible in the audit
            errors.append(f"fingerprint_ledger_path_invalid:{type(error).__name__}")
    else:
        errors.append("fingerprint_ledger_path_missing")

    payload = {
        "schema_version": "nyc-tlc-dispatch-ev2-v4-unique-trace-usage-audit-v1",
        "status": "passed" if not errors else "failed",
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_ledger_audit_path": str(ledger_path),
        "source_ledger_audit_sha256": ledger_sha,
        "source_fingerprint_audit_path": str(fingerprint_path),
        "source_fingerprint_audit_sha256": fingerprint_sha,
        "audited_valid_result_count": ledger.get("valid_method_runs"),
        "expected_method_run_count": ledger.get("expected_method_runs"),
        "all_expected_runs_complete": ledger.get("all_runs_complete_and_valid"),
        "result_trace_reference_count": sum(len(items) for items in trace_references.values()),
        "unique_trace_file_count": len(unique_traces),
        "cross_method_shared_trace_group_count": len(shared_groups),
        "duplicate_method_trace_references": sum(
            len(items) - 1 for items in trace_references.values()
        ),
        "raw_declared_method_costs_by_method": {
            method: dict(costs) for method, costs in sorted(declared_costs.items())
        },
        "unique_trace_usage_by_trace_origin_method": {
            method: dict(totals) for method, totals in sorted(usage_by_origin.items())
        },
        "method_cost_fields_are_self_reported_per_result": True,
        "usage_deduplicated_by_resolved_trace_path": True,
        "actual_account_charges_available": False,
        "missing_trace_paths": missing_trace_paths,
        "trace_errors": trace_errors,
        "result_errors": result_errors,
        "new_model_calls": 0,
        "sumo_reruns": 0,
        "source_result_files_modified": False,
        "interpretation": (
            "Unique usage is counted once per resolved trace path. D6/D7 may reference a D8 "
            "context trace; their per-result method_costs can duplicate usage and must not be "
            "summed as unique calls. Failed turns without usage remain unknown, not zero-cost. "
            "Actual account charges are unavailable. NYC TLC demand is mapped to zone-level OD "
            "cost proxies and centroid pickup proxies, not street routing. Frozen dispatch "
            "objectives remain separate from the offline time-money v5 sidecar."
        ),
        "shared_trace_groups": shared_groups,
        "unique_traces": unique_traces,
        "errors": errors,
        "generator_path": str(Path(__file__).resolve()),
        "generator_sha256": sha256_file(Path(__file__).resolve()),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "audit": str(output_path),
                "status": payload["status"],
                "audited_valid_result_count": payload["audited_valid_result_count"],
                "result_trace_reference_count": payload["result_trace_reference_count"],
                "unique_trace_file_count": payload["unique_trace_file_count"],
                "cross_method_shared_trace_group_count": payload[
                    "cross_method_shared_trace_group_count"
                ],
                "missing_trace_count": len(missing_trace_paths),
                "trace_error_count": len(trace_errors),
                "result_error_count": len(result_errors),
                "errors": errors[:12],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
