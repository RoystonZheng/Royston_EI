"""Shared Codex CLI trace reservation and official-rate estimate helpers."""

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


PRICING_CONFIG = Path(__file__).resolve().parent / "configs" / "codex_cli_pricing_2026-10-22.json"
CODEX_DISABLED_TOOL_FEATURES = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "chronicle",
    "code_mode",
    "code_mode_host",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "workspace_dependencies",
)


def reserve_indexed_artifacts(
    trace_dir: Path,
    stem: str,
    suffixes: Iterable[str],
    reserve_suffixes: Iterable[str],
    current_count: int = 0,
) -> Tuple[int, Dict[str, Path]]:
    """Reserve a never-before-used indexed artifact stem without overwriting.

    The first reserved file is opened with exclusive creation, making the
    index safe against both resumed clients and concurrent callers. A partially
    written request remains visible and is skipped on the next attempt.
    """
    root = Path(trace_dir)
    root.mkdir(parents=True, exist_ok=True)
    suffix_list = tuple(suffixes)
    reserve_list = tuple(reserve_suffixes)
    if not reserve_list or any(suffix not in suffix_list for suffix in reserve_list):
        raise ValueError("codex_trace_reservation_suffix_invalid")

    pattern = re.compile(r"^" + re.escape(stem) + r"_(\d{4,})\..+$")
    existing_indices = []
    for path in root.iterdir():
        match = pattern.match(path.name)
        if match:
            existing_indices.append(int(match.group(1)))
    next_index = max([int(current_count or 0), *existing_indices], default=0) + 1

    while True:
        prefix = "{0}_{1:04d}".format(stem, next_index)
        paths = {suffix: root / (prefix + suffix) for suffix in suffix_list}
        if any(path.exists() for path in paths.values()):
            next_index += 1
            continue

        created = []
        collision = False
        try:
            for suffix in reserve_list:
                with paths[suffix].open("xb"):
                    pass
                created.append(paths[suffix])
        except FileExistsError:
            collision = True
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise

        if collision:
            for path in created:
                path.unlink(missing_ok=True)
            next_index += 1
            continue
        return next_index, paths


def estimate_codex_usage_cost(
    model: str,
    input_tokens: Optional[int],
    cached_input_tokens: Optional[int],
    output_tokens: Optional[int],
    pricing_config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return rate-card equivalents; these are not a ChatGPT account invoice."""
    config_path = Path(pricing_config_path) if pricing_config_path else PRICING_CONFIG
    config = json.loads(config_path.read_text(encoding="utf-8"))
    model_rates = config.get("models", {}).get(model)
    result: Dict[str, Any] = {
        "pricing_config": str(config_path),
        "pricing_checked_date": config.get("checked_date"),
        "pricing_mode": "standard",
        "model": model,
        "account_billing_amount": None,
        "account_billing_currency": None,
        "account_billing_status": "not_reported_by_codex_cli",
        "codex_standard_credit_equivalent": None,
        "api_equivalent_usd": None,
        "api_equivalent_is_invoice": False,
        "calculation_status": "usage_missing_or_model_unpriced",
        "cached_input_assumption": None,
    }
    if not isinstance(model_rates, dict):
        result["calculation_status"] = "model_not_in_pricing_config"
        return result
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or input_tokens < 0
        or output_tokens < 0
    ):
        return result

    cached_known = (
        isinstance(cached_input_tokens, int)
        and not isinstance(cached_input_tokens, bool)
    )
    if cached_known and (cached_input_tokens < 0 or cached_input_tokens > input_tokens):
        result["calculation_status"] = "invalid_usage_cached_input_out_of_range"
        return result
    cached = cached_input_tokens if cached_known else 0
    uncached = input_tokens - cached
    result["cached_input_assumption"] = (
        "reported_by_cli" if cached_known else "assumed_zero_cached_for_upper_bound"
    )

    credit_rates = model_rates["codex_credits_per_1m_tokens"]
    api_rates = model_rates["api_usd_per_1m_tokens"]
    credits = (
        uncached * credit_rates["input"]
        + cached * credit_rates["cached_input"]
        + max(0, output_tokens) * credit_rates["output"]
    ) / 1_000_000.0
    api_usd = (
        uncached * api_rates["input"]
        + cached * api_rates["cached_input"]
        + max(0, output_tokens) * api_rates["output"]
    ) / 1_000_000.0
    api_usd_min = (
        (input_tokens * api_rates["cached_input"])
        + output_tokens * api_rates["output"]
    ) / 1_000_000.0
    credits_min = (
        (input_tokens * credit_rates["cached_input"])
        + output_tokens * credit_rates["output"]
    ) / 1_000_000.0
    result.update(
        {
            "codex_standard_credit_equivalent": credits,
            "codex_standard_credit_equivalent_min": credits if cached_known else credits_min,
            "codex_standard_credit_equivalent_max": credits,
            "api_equivalent_usd": api_usd,
            "api_equivalent_usd_min": api_usd if cached_known else api_usd_min,
            "api_equivalent_usd_max": api_usd,
            "calculation_status": (
                "standard_rate_equivalent"
                if cached_known
                else "upper_bound_assuming_no_cached_input"
            ),
            "reasoning_tokens_included_in_output_tokens": True,
            "account_invoice_amount": None,
            "account_invoice_status": "not_reported_by_codex_cli",
        }
    )
    return result


def aggregate_codex_usage_cost(attempts, model: str) -> Dict[str, Any]:
    """Sum per-call Codex rate-card estimates without presenting them as a bill."""
    rows = list(attempts or [])
    codex_rows = []
    for attempt in rows:
        provider = attempt.get("provider_audit") if isinstance(attempt, dict) else None
        if isinstance(provider, dict) and provider.get("backend") == "codex_cli":
            codex_rows.append(provider)

    estimates = [row.get("cost_estimate") for row in codex_rows]
    numeric = [
        item for item in estimates
        if isinstance(item, dict)
        and isinstance(item.get("api_equivalent_usd"), (int, float))
        and isinstance(item.get("codex_standard_credit_equivalent"), (int, float))
        and isinstance(item.get("api_equivalent_usd_min"), (int, float))
        and isinstance(item.get("api_equivalent_usd_max"), (int, float))
        and isinstance(item.get("codex_standard_credit_equivalent_min"), (int, float))
        and isinstance(item.get("codex_standard_credit_equivalent_max"), (int, float))
    ]
    usage_rows = [
        row for row in codex_rows
        if row.get("usage_available")
        and isinstance(row.get("input_tokens"), int)
        and isinstance(row.get("output_tokens"), int)
    ]
    total = {
        "model": model,
        "pricing_mode": "standard",
        "attempt_count": len(rows),
        "codex_cli_invocation_count": len(codex_rows),
        "usage_available_count": len(usage_rows),
        "cost_estimate_available_count": len(numeric),
        "usage_complete": bool(codex_rows) and len(usage_rows) == len(codex_rows),
        "input_tokens": sum(row["input_tokens"] for row in usage_rows),
        "cached_input_tokens": (
            sum(row["cached_input_tokens"] for row in usage_rows)
            if usage_rows and all(isinstance(row.get("cached_input_tokens"), int) for row in usage_rows)
            else None
        ),
        "output_tokens": sum(row["output_tokens"] for row in usage_rows),
        "api_equivalent_usd": None,
        "api_equivalent_usd_min": None,
        "api_equivalent_usd_max": None,
        "codex_standard_credit_equivalent": None,
        "codex_standard_credit_equivalent_min": None,
        "codex_standard_credit_equivalent_max": None,
        "account_invoice_amount": None,
        "account_invoice_currency": None,
        "account_invoice_status": "not_reported_by_codex_cli",
        "api_equivalent_is_invoice": False,
        "calculation_status": "usage_or_cost_estimate_incomplete",
    }
    if not numeric:
        return total

    total["api_equivalent_usd_max"] = sum(item["api_equivalent_usd_max"] for item in numeric)
    total["api_equivalent_usd_min"] = sum(item["api_equivalent_usd_min"] for item in numeric)
    total["codex_standard_credit_equivalent_max"] = sum(
        item["codex_standard_credit_equivalent_max"] for item in numeric
    )
    total["codex_standard_credit_equivalent_min"] = sum(
        item["codex_standard_credit_equivalent_min"] for item in numeric
    )
    complete = len(numeric) == len(codex_rows) and len(usage_rows) == len(codex_rows)
    if complete:
        total["api_equivalent_usd"] = total["api_equivalent_usd_max"]
        total["codex_standard_credit_equivalent"] = total[
            "codex_standard_credit_equivalent_max"
        ]
        total["calculation_status"] = (
            "summed_official_standard_rate_equivalent"
            if total["api_equivalent_usd_min"] == total["api_equivalent_usd_max"]
            else "summed_official_standard_rate_range"
        )
    else:
        total["calculation_status"] = "partial_rate_equivalent_only"
    return total

