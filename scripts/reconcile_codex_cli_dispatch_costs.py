#!/usr/bin/env python3
"""Reprice archived Codex dispatch usage from recorded tokens and the rate snapshot."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_cli_support import estimate_codex_usage_cost  # noqa: E402


def _sum_calls(calls, model):
    estimates = []
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    cache_complete = True
    usage_complete = True
    for call in calls:
        input_tokens = call.get("input_tokens")
        output_tokens = call.get("output_tokens")
        cached = call.get("cached_input_tokens")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            usage_complete = False
            continue
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        if isinstance(cached, int):
            totals["cached_input_tokens"] += cached
        else:
            cache_complete = False
        estimates.append(
            estimate_codex_usage_cost(model, input_tokens, cached, output_tokens)
        )

    valid = [
        item for item in estimates
        if isinstance(item.get("api_equivalent_usd_max"), (int, float))
        and isinstance(item.get("codex_standard_credit_equivalent_max"), (int, float))
    ]
    totals["cached_input_tokens"] = totals["cached_input_tokens"] if cache_complete else None
    complete = usage_complete and len(valid) == len(calls) and len(calls) > 0
    result = {
        **totals,
        "call_count": len(calls),
        "usage_complete": complete,
        "codex_standard_credit_equivalent": None,
        "codex_standard_credit_equivalent_min": None,
        "codex_standard_credit_equivalent_max": None,
        "api_equivalent_usd": None,
        "api_equivalent_usd_min": None,
        "api_equivalent_usd_max": None,
        "account_invoice_amount": None,
        "account_invoice_currency": None,
        "account_invoice_status": "not_reported_by_codex_cli",
        "api_equivalent_is_invoice": False,
        "calculation_status": "usage_or_cost_estimate_incomplete",
        "call_estimates": estimates,
    }
    if valid:
        for output_key, estimate_key in (
            ("codex_standard_credit_equivalent_min", "codex_standard_credit_equivalent_min"),
            ("codex_standard_credit_equivalent_max", "codex_standard_credit_equivalent_max"),
            ("api_equivalent_usd_min", "api_equivalent_usd_min"),
            ("api_equivalent_usd_max", "api_equivalent_usd_max"),
        ):
            result[output_key] = sum(item[estimate_key] for item in valid)
        if complete:
            exact = all(
                item.get("calculation_status") == "standard_rate_equivalent"
                for item in valid
            )
            result["codex_standard_credit_equivalent"] = result[
                "codex_standard_credit_equivalent_max"
            ]
            result["api_equivalent_usd"] = result["api_equivalent_usd_max"]
            result["calculation_status"] = (
                "summed_official_standard_rate_equivalent"
                if exact
                else "summed_official_standard_rate_range"
            )
        else:
            result["calculation_status"] = "partial_rate_equivalent_only"
    return result


def _live_calls_from_result(data):
    calls = []
    for round_row in data.get("rounds", []):
        output = round_row.get("output", {})
        audit = output.get("audit", {})
        assignment = audit.get("direct_llm_assignment", {})
        if assignment.get("request_made"):
            calls.append(assignment)
    return calls


def _usage_from_trace_directory(directory):
    calls = []
    if not directory.is_dir():
        return calls
    for path in sorted(directory.glob("*.jsonl")):
        usages = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "turn.completed" or not isinstance(event.get("usage"), dict):
                continue
            usage = event["usage"]
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            cached = usage.get("cached_input_tokens")
            if not isinstance(cached, int):
                details = usage.get("input_tokens_details")
                cached = details.get("cached_tokens") if isinstance(details, dict) else None
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                usages.append(
                    {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached,
                        "output_tokens": output_tokens,
                        "trace_file": str(path),
                    }
                )
        if len(usages) == 1:
            calls.extend(usages)
        elif usages:
            # Ambiguous/multi-turn traces are retained as individual usage rows;
            # the caller will expose the invocation count mismatch.
            calls.extend(usages)
    return calls


def build_reconciliation(root: Path):
    runs = []
    represented_partial_keys = set()
    result_paths = sorted(root.rglob("D5_pure_llm_direct_seed_*.json"))
    for path in result_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("method_name") != "D5_pure_llm_direct":
            continue
        model = data.get("llm_model") or data.get("summary", {}).get("audit", {}).get("direct_llm_assignment", {}).get("model") or "gpt-5.6-luna"
        summary = data.get("summary", {})
        trace_calls = _usage_from_trace_directory(path.parent / "codex_cli_traces")
        round_calls = _live_calls_from_result(data)
        calls = trace_calls or round_calls
        usage_source = "codex_cli_traces" if trace_calls else "embedded_round_audits"
        if calls:
            cost = _sum_calls(calls, model)
            trace_match = all(
                summary.get(summary_key) == cost.get(cost_key)
                for summary_key, cost_key in (
                    ("total_llm_input_tokens", "input_tokens"),
                    ("total_llm_cached_input_tokens", "cached_input_tokens"),
                    ("total_llm_output_tokens", "output_tokens"),
                    ("total_codex_cli_invocations", "call_count"),
                )
                if summary.get(summary_key) is not None
            )
        else:
            cost = _sum_calls(
                [{
                    "input_tokens": summary.get("total_llm_input_tokens"),
                    "cached_input_tokens": summary.get("total_llm_cached_input_tokens"),
                    "output_tokens": summary.get("total_llm_output_tokens"),
                }],
                model,
            )
            cost["call_count"] = summary.get("total_codex_cli_invocations", summary.get("total_llm_calls"))
            usage_source = "saved_run_summary_totals"
            trace_match = None
        cost["usage_source"] = usage_source
        cost["summary_matches_call_traces"] = trace_match
        recorded = {
            "input_tokens": summary.get("total_llm_input_tokens"),
            "cached_input_tokens": summary.get("total_llm_cached_input_tokens"),
            "output_tokens": summary.get("total_llm_output_tokens"),
            "call_count": summary.get("total_codex_cli_invocations"),
        }
        cost["recorded_summary_usage"] = recorded
        logical_key = (
            data.get("case_id"),
            data.get("fleet_id"),
            summary.get("seed", data.get("config", {}).get("seed")),
        )
        represented_partial_keys.add(logical_key)
        runs.append(
            {
                "source_file": str(path.relative_to(root)),
                "scenario_id": data.get("scenario_id"),
                "fleet_id": data.get("fleet_id"),
                "seed": summary.get("seed", data.get("config", {}).get("seed")),
                "partial_run": bool(data.get("partial_run")),
                "execution_provenance": data.get("execution_provenance", "live_run"),
                "model": model,
                "cost": cost,
            }
        )

    partial_paths = sorted(root.rglob("partial_run_audit.json"))
    for path in partial_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        logical_key = (data.get("case_id"), data.get("fleet_id"), data.get("seed"))
        if logical_key in represented_partial_keys:
            continue
        calls = data.get("calls", [])
        model = data.get("model", "gpt-5.6-luna")
        cost = _sum_calls(calls, model)
        cost["recorded_summary_usage"] = {
            "input_tokens": data.get("input_tokens"),
            "cached_input_tokens": data.get("cached_input_tokens"),
            "output_tokens": data.get("output_tokens"),
            "call_count": data.get("codex_invocations"),
        }
        cost["summary_matches_call_traces"] = all(
            cost.get(field) == data.get(source)
            for field, source in (
                ("input_tokens", "input_tokens"),
                ("cached_input_tokens", "cached_input_tokens"),
                ("output_tokens", "output_tokens"),
                ("call_count", "codex_invocations"),
            )
        )
        runs.append(
            {
                "source_file": str(path.relative_to(root)),
                "scenario_id": data.get("case_id"),
                "fleet_id": data.get("fleet_id"),
                "seed": data.get("seed"),
                "partial_run": True,
                "partial_status": data.get("status"),
                "simulation_result_complete": data.get("simulation_result_complete"),
                "model": model,
                "cost": cost,
            }
        )

    aggregate = {
        "run_count": len(runs),
        "complete_usage_run_count": sum(run["cost"]["usage_complete"] for run in runs),
        "input_tokens": (
            sum(run["cost"]["input_tokens"] for run in runs)
            if all(run["cost"]["usage_complete"] for run in runs)
            else None
        ),
        "output_tokens": (
            sum(run["cost"]["output_tokens"] for run in runs)
            if all(run["cost"]["usage_complete"] for run in runs)
            else None
        ),
        "cached_input_tokens": (
            sum(run["cost"]["cached_input_tokens"] for run in runs)
            if runs and all(run["cost"]["usage_complete"] and run["cost"]["cached_input_tokens"] is not None for run in runs)
            else None
        ),
        "codex_standard_credit_equivalent": (
            sum(run["cost"]["codex_standard_credit_equivalent"] for run in runs)
            if runs and all(run["cost"]["codex_standard_credit_equivalent"] is not None for run in runs)
            else None
        ),
        "api_equivalent_usd": (
            sum(run["cost"]["api_equivalent_usd"] for run in runs)
            if runs and all(run["cost"]["api_equivalent_usd"] is not None for run in runs)
            else None
        ),
        "account_invoice_amount": None,
        "account_invoice_status": "not_reported_by_codex_cli",
        "api_equivalent_is_invoice": False,
    }
    return {
        "schema_version": "codex_cli_dispatch_cost_reconciliation_v1",
        "source_root": str(root),
        "pricing_config": "configs/codex_cli_pricing_2026-10-21.json",
        "formula_note": "API-rate equivalent=(uncached input*0.20 + cached input*0.02 + output*1.20)/1e6 USD; Codex Standard credits=(uncached input*5 + cached input*0.5 + output*30)/1e6. Reasoning tokens are included in output tokens.",
        "interpretation": "Rate-card equivalents are not an account invoice or a direct measure of included subscription allowance consumption; original run files are left unchanged.",
        "aggregate": aggregate,
        "runs": runs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = build_reconciliation(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"runs": report["aggregate"]["run_count"], "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
