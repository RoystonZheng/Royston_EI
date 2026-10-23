#!/usr/bin/env python3
"""Audit saved Codex CLI JSONL traces and estimate official rate equivalents."""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_cli_support import estimate_codex_usage_cost  # noqa: E402


def _usage(event: Dict[str, Any]) -> Dict[str, Any]:
    usage = event.get("usage")
    if not isinstance(usage, dict) and isinstance(event.get("turn"), dict):
        usage = event["turn"].get("usage")
    return usage if isinstance(usage, dict) else {}


def _sum_if_complete(rows: List[Dict[str, Any]], key: str) -> Optional[int]:
    values = [row.get(key) for row in rows]
    if not values or any(not isinstance(value, int) for value in values):
        return None
    return sum(values)


def audit(root: Path, model: str) -> Dict[str, Any]:
    root = Path(root).resolve()
    trace_paths = sorted(
        path
        for path in root.rglob("*.jsonl")
        if any("codex" in part.lower() for part in path.relative_to(root).parts[:-1])
    )
    seen_hashes: Dict[str, str] = {}
    records = []
    total_calls_with_usage = 0
    total_input = 0
    total_cached = 0
    cached_complete = True
    total_output = 0
    input_complete = True
    output_complete = True
    standard_credit_equivalent = 0.0
    api_equivalent_usd = 0.0
    estimate_count = 0
    unknown_usage_attempts = 0
    duplicate_files = 0
    non_trace_jsonl_files_skipped = 0

    for path in trace_paths:
        raw = path.read_bytes()
        events = []
        parse_errors = 0
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                parse_errors += 1
        turn_started = sum(event.get("type") == "turn.started" for event in events)
        usage_rows = [
            _usage(event)
            for event in events
            if event.get("type") == "turn.completed" and _usage(event)
        ]
        # Some experiment runners also write JSONL progress streams beneath a
        # directory whose name contains "codex". Keep only CLI traces that
        # actually show a model turn; progress/eventless JSONL is not usage.
        if not turn_started and not usage_rows:
            non_trace_jsonl_files_skipped += 1
            continue

        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen_hashes:
            duplicate_files += 1
            records.append(
                {
                    "path": str(path.relative_to(root)),
                    "sha256": digest,
                    "status": "duplicate_trace_not_double_counted",
                    "duplicate_of": seen_hashes[digest],
                }
            )
            continue
        seen_hashes[digest] = str(path.relative_to(root))

        input_tokens = _sum_if_complete(usage_rows, "input_tokens")
        output_tokens = _sum_if_complete(usage_rows, "output_tokens")
        cached_values = []
        for row in usage_rows:
            cached = row.get("cached_input_tokens")
            if not isinstance(cached, int):
                details = row.get("input_tokens_details")
                cached = details.get("cached_tokens") if isinstance(details, dict) else None
            cached_values.append(cached)
        cached_input_tokens = (
            sum(cached_values)
            if cached_values and all(isinstance(value, int) for value in cached_values)
            else None
        )
        cost_estimate = estimate_codex_usage_cost(
            model, input_tokens, cached_input_tokens, output_tokens
        )
        status = "usage_available" if usage_rows else (
            "started_without_usage" if turn_started else "no_turn_started_event"
        )
        record = {
            "path": str(path.relative_to(root)),
            "sha256": digest,
            "model_for_rate_card_assumption": model,
            "status": status,
            "turn_started_count": turn_started,
            "turn_completed_usage_count": len(usage_rows),
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "parse_error_count": parse_errors,
            "cost_estimate": cost_estimate,
        }
        records.append(record)

        if usage_rows:
            total_calls_with_usage += 1
            if input_tokens is None:
                input_complete = False
            else:
                total_input += input_tokens
            if cached_input_tokens is None:
                cached_complete = False
            else:
                total_cached += cached_input_tokens
            if output_tokens is None:
                output_complete = False
            else:
                total_output += output_tokens
            if isinstance(cost_estimate.get("codex_standard_credit_equivalent"), (int, float)):
                standard_credit_equivalent += cost_estimate["codex_standard_credit_equivalent"]
                api_equivalent_usd += cost_estimate["api_equivalent_usd"]
                estimate_count += 1
        elif turn_started:
            unknown_usage_attempts += 1

    grouped = defaultdict(
        lambda: {
            "trace_files": 0,
            "unique_attempts_with_usage": 0,
            "attempts_started_without_usage": 0,
            "input_tokens_including_cached": 0,
            "cached_input_tokens": 0,
            "output_tokens_including_reasoning": 0,
            "standard_credit_equivalent": 0.0,
            "api_equivalent_usd": 0.0,
        }
    )
    for record in records:
        path_text = record["path"]
        if "r4_codex_pilot_continuation_v2" in path_text:
            group_name = "route_codex_pilot_continuation_v2"
        elif "r4_codex_pilot_continuation_v1" in path_text:
            group_name = "route_codex_pilot_continuation_v1"
        elif "codex_cli_cost_smoke" in path_text:
            group_name = "dispatch_codex_cli_cost_smoke"
        elif path_text.startswith("dispatch_layer/"):
            group_name = "dispatch_layer_codex_cli"
        elif path_text.startswith("route_layer/"):
            group_name = "route_layer_codex_cli_other"
        else:
            group_name = "other"
        group = grouped[group_name]
        group["trace_files"] += 1
        if record.get("status") == "duplicate_trace_not_double_counted":
            continue
        if record.get("turn_completed_usage_count"):
            group["unique_attempts_with_usage"] += 1
            for source, destination in (
                ("input_tokens", "input_tokens_including_cached"),
                ("cached_input_tokens", "cached_input_tokens"),
                ("output_tokens", "output_tokens_including_reasoning"),
            ):
                if isinstance(record.get(source), int):
                    group[destination] += record[source]
            estimate = record.get("cost_estimate", {})
            credits = estimate.get("codex_standard_credit_equivalent")
            api_usd = estimate.get("api_equivalent_usd")
            if isinstance(credits, (int, float)):
                group["standard_credit_equivalent"] += credits
                group["api_equivalent_usd"] += api_usd
        elif record.get("turn_started_count"):
            group["attempts_started_without_usage"] += 1

    return {
        "schema_version": "codex_cli_cost_audit_v1",
        "model_for_rate_card": model,
        "root": str(root),
        "pricing_config": str(Path("configs/codex_cli_pricing_2026-10-21.json")),
        "files_scanned": len(trace_paths),
        "non_trace_jsonl_files_skipped": non_trace_jsonl_files_skipped,
        "unique_traces": len(seen_hashes),
        "duplicate_trace_files_not_double_counted": duplicate_files,
        "attempts_with_turn_completed_usage": total_calls_with_usage,
        "attempts_started_but_usage_missing": unknown_usage_attempts,
        "known_input_tokens_including_cached": total_input if input_complete else None,
        "known_cached_input_tokens": total_cached if cached_complete else None,
        "known_output_tokens_including_reasoning": total_output if output_complete else None,
        "known_total_tokens_from_reported_usage": (
            total_input + total_output if input_complete and output_complete else None
        ),
        "complete_total_tokens_across_scanned_traces": (
            total_input + total_output
            if input_complete and output_complete and unknown_usage_attempts == 0
            else None
        ),
        "cost_estimates_apply_only_to_traces_with_reported_usage": True,
        "model_assumption_source": (
            "experiment runner configuration; Codex CLI JSONL events do not include model slug"
        ),
        "sum_of_per_trace_codex_standard_credit_equivalents": standard_credit_equivalent if estimate_count else None,
        "sum_of_per_trace_api_equivalent_usd": api_equivalent_usd if estimate_count else None,
        "cost_estimate_trace_count": estimate_count,
        "by_experiment_group": dict(sorted(grouped.items())),
        "account_invoice_amount": None,
        "account_invoice_status": "not_reported_by_codex_cli; check account usage/billing dashboard",
        "records": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "outputs")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = audit(args.root, args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in payload.items() if key != "records"}, ensure_ascii=False, indent=2))
    print("report_path=" + str(args.output))


if __name__ == "__main__":
    main()
