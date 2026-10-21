"""Optional live LLM adapter for structured candidate feedback.

The offline experiment never imports credentials or requires this adapter.  A
live run opts in with ``feedback_mode=deepseek`` and reuses the repository's
small DeepSeek client.  The adapter can only return the fixed feedback schema;
the hard-constraint validator and optimizer remain program controlled.
"""

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from route_experiment.llm_adapter import DeepSeekClient
from codex_cli_support import (
    CODEX_DISABLED_TOOL_FEATURES,
    estimate_codex_usage_cost,
    reserve_indexed_artifacts,
)

from .agent import CandidateFeedback
from .models import Candidate, Order, Vehicle


# DeepSeek's official peak rates checked on 2026-10-20. Peak rates are used
# conservatively so a run does not need to infer holiday/UTC billing windows.
DEEPSEEK_FLASH_PEAK_USD_PER_MILLION = {
    "cache_hit_input": 0.006,
    "cache_miss_input": 0.30,
    "output": 1.20,
}

CODEX_DIRECT_ASSIGNMENT_INPUT_POLICY = "hard_feasible_candidate_edges_v2"


def build_direct_assignment_messages(
    *,
    snapshot_time_sec: float,
    vehicles: Sequence[Vehicle],
    orders: Sequence[Order],
    candidates: Sequence[Candidate],
    max_wait_sec: float,
) -> List[Dict[str, str]]:
    """Serialize the complete current assignment options for a pure-LLM round."""

    def finite(value: float):
        return float(value) if math.isfinite(float(value)) else None

    waiting_orders = [
        {
            "order_id": order.order_id,
            "arrival_time_sec": order.arrival_time_sec,
            "waiting_time_sec": max(0.0, snapshot_time_sec - order.arrival_time_sec),
            "pickup_zone_or_node": order.pickup_node_id,
            "dropoff_zone_or_node": order.dropoff_node_id,
        }
        for order in orders
        if order.status == "waiting" and order.arrival_time_sec <= snapshot_time_sec
    ]
    vehicle_states = [
        {
            "vehicle_id": vehicle.vehicle_id,
            "state": vehicle.state,
            "position": vehicle.position_node_id,
            "available_time_sec": vehicle.available_time_sec,
            "current_order_id": vehicle.current_order_id,
            "reserved_order_id": vehicle.reserved_order_id,
            "current_dropoff": vehicle.current_dropoff_node_id,
        }
        for vehicle in vehicles
    ]
    candidate_rows = [
        {
            "candidate_id": item.candidate_id,
            "vehicle_id": item.vehicle_id,
            "order_id": item.order_id,
            "candidate_type": item.candidate_type,
            "route_reachability": item.route_reachability,
            "hard_constraint_ok": item.hard_constraint_ok,
            "rejection_reason": item.rejection_reason,
            "predicted_wait_sec": finite(item.predicted_wait_sec),
            "predicted_pickup_time_sec": finite(item.predicted_pickup_time_sec),
            "predicted_dropoff_time_sec": finite(item.predicted_dropoff_time_sec),
            "empty_distance_m": finite(item.empty_distance_m),
            "total_distance_m": finite(item.total_distance_m),
            "energy_kwh": finite(item.energy_kwh),
            "current_commitment_ok": item.current_commitment_ok,
            "one_to_one_ok": item.one_to_one_ok,
            "time_non_overlap_ok": item.time_non_overlap_ok,
            "max_wait_ok": item.max_wait_ok,
        }
        for item in sorted(candidates, key=lambda row: row.candidate_id)
    ]
    payload = {
        "task": "select_a_one_to_one_vehicle_order_matching_for_this_snapshot",
        "snapshot_time_sec": snapshot_time_sec,
        "max_pickup_wait_sec": max_wait_sec,
        "rules": [
            "Select only candidate IDs present in the input.",
            "Every vehicle and every order may appear at most once.",
            "Select only candidates with hard_constraint_ok=true.",
            "Do not change route, cost, availability, or hard-constraint facts.",
            "An empty assignment list is allowed when no assignment is suitable.",
            "Do not use future orders; only orders in this snapshot are visible.",
        ],
        "vehicles": vehicle_states,
        "waiting_orders": waiting_orders,
        "candidate_edges": candidate_rows,
        "output_schema": {"selected_candidate_ids": ["candidate_id"]},
    }
    return [
        {
            "role": "system",
            "content": (
                "你是无人网约车直接派单器。依据当前快照和候选边选择一对一车辆—订单匹配。"
                "只返回 JSON，不要解释；不得放宽硬约束或创造候选。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


class DeepSeekDirectAssignment:
    """Pure-LLM direct matching; no Agent context filter or optimizer is used."""

    def __init__(self, client: DeepSeekClient):
        self.client = client
        self.call_count = 0
        self.input_tokens = 0
        self.cached_input_tokens = 0
        self.output_tokens = 0
        self.api_cost_usd = 0.0
        self.unknown_cost_count = 0
        self.latency_ms = 0.0
        self.errors: List[str] = []
        self.last_record: Dict[str, Any] = {"status": "not_called"}

    def select(
        self,
        *,
        snapshot_time_sec: float,
        vehicles: Sequence[Vehicle],
        orders: Sequence[Order],
        candidates: Sequence[Candidate],
        max_wait_sec: float,
    ) -> Tuple[List[Candidate], Dict[str, Any]]:
        feasible = {item.candidate_id: item for item in candidates if item.hard_constraint_ok}
        if not feasible:
            record = {
                "status": "no_feasible_candidates",
                "model": self.client.model,
                "request_made": False,
                "estimated_api_cost_usd": 0.0,
                "cost_status": "no_request",
            }
            self.last_record = record
            return [], record

        self.call_count += 1
        started = time.perf_counter()
        response = None
        estimated_cost = None
        try:
            response = self.client.chat(
                build_direct_assignment_messages(
                    snapshot_time_sec=snapshot_time_sec,
                    vehicles=vehicles,
                    orders=orders,
                    candidates=candidates,
                    max_wait_sec=max_wait_sec,
                )
            )
            self.latency_ms += response.latency_ms
            self.input_tokens += response.input_tokens
            self.cached_input_tokens += response.cached_input_tokens
            self.output_tokens += response.output_tokens
            cache_hit = min(response.input_tokens, response.cached_input_tokens)
            cache_miss = max(0, response.input_tokens - cache_hit)
            estimated_cost = (
                cache_hit * DEEPSEEK_FLASH_PEAK_USD_PER_MILLION["cache_hit_input"]
                + cache_miss * DEEPSEEK_FLASH_PEAK_USD_PER_MILLION["cache_miss_input"]
                + response.output_tokens * DEEPSEEK_FLASH_PEAK_USD_PER_MILLION["output"]
            ) / 1_000_000.0
            self.api_cost_usd += estimated_cost
            payload = json.loads(response.content)
            ids = payload.get("selected_candidate_ids") if isinstance(payload, dict) else None
            if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
                raise ValueError("invalid_selected_candidate_ids")
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate_candidate_id")
            selected = []
            used_vehicles = set()
            used_orders = set()
            for candidate_id in ids:
                candidate = feasible.get(candidate_id)
                if candidate is None:
                    raise ValueError("selected_candidate_not_hard_feasible:" + candidate_id)
                if candidate.vehicle_id in used_vehicles:
                    raise ValueError("vehicle_selected_twice:" + candidate.vehicle_id)
                if candidate.order_id in used_orders:
                    raise ValueError("order_selected_twice:" + candidate.order_id)
                used_vehicles.add(candidate.vehicle_id)
                used_orders.add(candidate.order_id)
                selected.append(candidate)
            # Estimate with official peak rates to avoid undercounting when the
            # precise peak/off-peak billing window is unknown.
            record = {
                "status": "accepted",
                "request_made": True,
                "model": response.model,
                "request_id": response.request_id,
                "latency_ms": response.latency_ms,
                "input_tokens": response.input_tokens,
                "cached_input_tokens": response.cached_input_tokens,
                "output_tokens": response.output_tokens,
                "estimated_api_cost_usd": estimated_cost,
                "cost_status": "estimated_from_returned_usage",
                "pricing_basis": "DeepSeek official peak rates; conservative estimate",
                "pricing_checked_date": "2026-10-20",
                "pricing_rates_usd_per_million": dict(DEEPSEEK_FLASH_PEAK_USD_PER_MILLION),
                "retry_count": response.retries,
                "selected_candidate_ids": ids,
            }
            self.last_record = record
            return selected, record
        except Exception as error:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.latency_ms += elapsed_ms
            detail = str(error)
            self.errors.append(detail)
            if response is None:
                self.unknown_cost_count += 1
            record = {
                "status": "llm_error_or_invalid_response",
                "request_made": True,
                "error": detail,
                "latency_ms": elapsed_ms,
                "model": self.client.model,
                "input_tokens": response.input_tokens if response is not None else 0,
                "output_tokens": response.output_tokens if response is not None else 0,
                "estimated_api_cost_usd": estimated_cost,
                "cost_status": "unknown_request_failure" if response is None else "estimated_from_returned_usage_despite_invalid_output",
                "pricing_checked_date": "2026-10-20" if response is not None else None,
                "pricing_rates_usd_per_million": dict(DEEPSEEK_FLASH_PEAK_USD_PER_MILLION) if response is not None else None,
            }
            self.last_record = record
            return [], record


def build_feedback_messages(candidate: Candidate, max_wait_sec: float):
    payload = {
        "candidate_id": candidate.candidate_id,
        "candidate_type": candidate.candidate_type,
        "predicted_wait_sec": candidate.predicted_wait_sec,
        "max_wait_sec": max_wait_sec,
        "empty_distance_m": candidate.empty_distance_m,
        "total_distance_m": candidate.total_distance_m,
        "energy_kwh": candidate.energy_kwh,
        "current_commitment_ok": candidate.current_commitment_ok,
        "route_reachability": candidate.route_reachability,
        "feature_vector": candidate.feature_vector,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是无人网约车车辆候选审查器。只依据输入事实，输出 JSON。"
                "decision 只能是 retain、reject 或 review；不得修改硬约束、等待上限或路线事实。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "review_candidate",
                    "candidate": payload,
                    "output_schema": {
                        "decision": "retain|reject|review",
                        "risk": "number_0_to_1",
                        "reason": "short_string",
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


class DeepSeekCandidateFeedback(CandidateFeedback):
    def __init__(self, client: DeepSeekClient):
        super().__init__(mode="deepseek")
        self.is_live_llm = True
        self.client = client
        self.errors = []
        self.latency_ms = 0.0

    def evaluate(self, candidate: Candidate, max_wait_sec: float) -> Dict[str, Any]:
        self.call_count += 1
        try:
            response = self.client.chat(build_feedback_messages(candidate, max_wait_sec))
            payload = json.loads(response.content)
            decision = str(payload.get("decision", "review"))
            if decision not in ("retain", "reject", "review"):
                raise ValueError("invalid_feedback_decision")
            risk = float(payload.get("risk", 0.5))
            if not 0.0 <= risk <= 1.0:
                raise ValueError("invalid_feedback_risk")
            self.input_tokens += response.input_tokens
            self.output_tokens += response.output_tokens
            self.latency_ms += response.latency_ms
            return {
                "decision": decision,
                "risk": risk,
                "reason": str(payload.get("reason", "")),
            }
        except Exception as error:  # API/parse failure is a controlled fallback.
            self.errors.append(str(error))
            ratio = candidate.predicted_wait_sec / max_wait_sec if max_wait_sec else 1.0
            return {
                "decision": "review" if ratio > 0.95 else "retain",
                "risk": max(0.0, min(1.0, ratio * 0.75)),
                "reason": "llm_fallback:offline_structured_rule",
            }


def default_deepseek_feedback() -> DeepSeekCandidateFeedback:
    model = os.environ.get("DISPATCH_LLM_MODEL", "deepseek-flash")
    return DeepSeekCandidateFeedback(DeepSeekClient(model=model))


def default_deepseek_direct_assignment() -> DeepSeekDirectAssignment:
    model = os.environ.get("DISPATCH_LLM_MODEL", "deepseek-flash")
    client = DeepSeekClient(
        model=model,
        retries=0,
        max_tokens=512,
        reasoning_effort="none",
    )
    return DeepSeekDirectAssignment(client)


class CodexCliDirectAssignment:
    """Pure-LLM direct matching through one isolated Codex CLI invocation.

    Codex is run in an empty temporary directory with a read-only sandbox and
    user configuration disabled. The JSONL trace is retained for usage and
    tool-event auditing. Any observed tool invocation invalidates the model
    choice so this remains a direct matching condition.
    """

    TOOL_ITEM_TYPES = {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search_call",
        "computer_call",
        "function_call",
        "tool_call",
        "image_view",
    }
    NON_TOOL_ITEM_TYPES = {"agent_message", "reasoning", "plan", "error"}
    backend = "codex_cli"
    input_policy = CODEX_DIRECT_ASSIGNMENT_INPUT_POLICY

    @staticmethod
    def _tool_policy() -> Dict[str, Any]:
        return {
            "shell_tool": "disabled",
            "unified_exec": "disabled",
            "disabled_features": list(CODEX_DISABLED_TOOL_FEATURES),
            "user_config": "ignored",
            "sandbox": "read-only",
            "working_directory": "empty_temporary_directory",
            "api_key_environment_variables_removed": [
                "DEEPSEEK_API_KEY",
                "DISPATCH_LLM_MODEL",
                "DISPATCH_LLM_BASE_URL",
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE",
                "OPENAI_ORG_ID",
                "OPENAI_PROJECT",
                "CODEX_API_KEY",
            ],
        }

    def __init__(
        self,
        *,
        model: str,
        trace_dir: Path,
        codex_executable: str = "codex",
        reasoning_effort: str = "low",
        timeout_sec: int = 300,
    ):
        self.model = model
        self.trace_dir = Path(trace_dir)
        self.codex_executable = codex_executable
        self.reasoning_effort = reasoning_effort
        self.timeout_sec = timeout_sec
        self.call_count = 0
        self.trace_index = 0
        self.input_tokens = 0
        self.cached_input_tokens = 0
        self.output_tokens = 0
        self.usage_call_count = 0
        self.input_usage_call_count = 0
        self.cached_input_usage_call_count = 0
        self.output_usage_call_count = 0
        self.unknown_cost_count = 0
        self.codex_standard_credit_equivalent = 0.0
        self.api_equivalent_usd = 0.0
        self.cost_estimate_call_count = 0
        self.api_cost_usd = None
        self.latency_ms = 0.0
        self.errors: List[str] = []
        self.last_record: Dict[str, Any] = {"status": "not_called"}
        self.schema_path = (
            Path(__file__).resolve().parent
            / "schemas"
            / "codex_direct_assignment.schema.json"
        )
        try:
            version = subprocess.run(
                [self.codex_executable, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.cli_version = (version.stdout or version.stderr).strip() or None
        except Exception:
            self.cli_version = None

    @staticmethod
    def _event_usage(event: Dict[str, Any]) -> Dict[str, Any]:
        usage = event.get("usage")
        if not isinstance(usage, dict):
            turn = event.get("turn")
            usage = turn.get("usage") if isinstance(turn, dict) else None
        return usage if isinstance(usage, dict) else {}

    @classmethod
    def _audit_events(cls, events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        turn_usages = []
        turn_started_count = 0
        tool_item_types = set()
        non_message_item_types = set()
        event_types = []
        for event in events:
            event_type = event.get("type")
            if isinstance(event_type, str):
                event_types.append(event_type)
                if event_type == "turn.started":
                    turn_started_count += 1
                if event_type == "turn.completed":
                    usage = cls._event_usage(event)
                    if usage:
                        turn_usages.append(usage)
            item = event.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                if isinstance(item_type, str):
                    if item_type not in {"agent_message", "reasoning", "plan"}:
                        non_message_item_types.add(item_type)
                    if (
                        item_type in cls.TOOL_ITEM_TYPES
                        or item_type not in cls.NON_TOOL_ITEM_TYPES
                        or item_type.endswith("_call")
                        or item_type.endswith("_execution")
                    ):
                        tool_item_types.add(item_type)

        def summed(key: str):
            values = [row.get(key) for row in turn_usages if isinstance(row.get(key), int)]
            return sum(values) if values else None

        input_tokens = summed("input_tokens")
        output_tokens = summed("output_tokens")
        total_tokens = summed("total_tokens")
        cached_values = []
        reasoning_values = []
        for usage in turn_usages:
            input_details = usage.get("input_tokens_details")
            output_details = usage.get("output_tokens_details")
            cached = usage.get("cached_input_tokens")
            if not isinstance(cached, int):
                cached = input_details.get("cached_tokens") if isinstance(input_details, dict) else None
            reasoning = usage.get("reasoning_output_tokens")
            if not isinstance(reasoning, int):
                reasoning = output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None
            if isinstance(cached, int):
                cached_values.append(cached)
            if isinstance(reasoning, int):
                reasoning_values.append(reasoning)
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        return {
            "turn_started_count": turn_started_count,
            "turn_completed_usage_count": len(turn_usages),
            "input_tokens": input_tokens,
            "cached_input_tokens": sum(cached_values) if cached_values else None,
            "output_tokens": output_tokens,
            "reasoning_tokens": sum(reasoning_values) if reasoning_values else None,
            "total_tokens": total_tokens,
            "usage_available": bool(turn_usages),
            "tool_usage_detected": bool(tool_item_types),
            "tool_item_types": sorted(tool_item_types),
            "other_non_message_item_types": sorted(non_message_item_types),
            "event_types": event_types,
        }

    @classmethod
    def _audit_jsonl(cls, stdout: str):
        events = []
        parse_errors = []
        for line_number, line in enumerate(stdout.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except Exception as error:
                parse_errors.append("line_{0}:{1}".format(line_number, type(error).__name__))
        return events, cls._audit_events(events), parse_errors

    def _record_codex_usage(self, audit: Dict[str, Any]) -> Dict[str, Any]:
        cost_estimate = estimate_codex_usage_cost(
            self.model,
            audit.get("input_tokens"),
            audit.get("cached_input_tokens"),
            audit.get("output_tokens"),
        )
        if audit.get("usage_available"):
            self.usage_call_count += 1
            if isinstance(audit.get("input_tokens"), int):
                self.input_tokens += audit["input_tokens"]
                self.input_usage_call_count += 1
            if isinstance(audit.get("cached_input_tokens"), int):
                self.cached_input_tokens += audit["cached_input_tokens"]
                self.cached_input_usage_call_count += 1
            if isinstance(audit.get("output_tokens"), int):
                self.output_tokens += audit["output_tokens"]
                self.output_usage_call_count += 1
            if isinstance(cost_estimate.get("codex_standard_credit_equivalent"), (int, float)):
                self.codex_standard_credit_equivalent += cost_estimate["codex_standard_credit_equivalent"]
                self.api_equivalent_usd += cost_estimate["api_equivalent_usd"]
                self.cost_estimate_call_count += 1
            else:
                self.unknown_cost_count += 1
        else:
            self.unknown_cost_count += 1
        return cost_estimate

    @staticmethod
    def _sha256(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _safe_error(value: str) -> str:
        # Never copy bearer/API-key-shaped material into experiment artifacts.
        value = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{12,})", "[REDACTED]", value)
        return value[-3000:]

    def select(
        self,
        *,
        snapshot_time_sec: float,
        vehicles: Sequence[Vehicle],
        orders: Sequence[Order],
        candidates: Sequence[Candidate],
        max_wait_sec: float,
    ) -> Tuple[List[Candidate], Dict[str, Any]]:
        feasible = {item.candidate_id: item for item in candidates if item.hard_constraint_ok}
        if not feasible:
            record = {
                "status": "no_feasible_candidates",
                "backend": "codex_cli",
                "model": self.model,
                "candidate_input_policy": self.input_policy,
                "raw_candidate_count": len(candidates),
                "prompt_candidate_count": 0,
                "invocation_made": False,
                "request_made": False,
                "account_cost_usd": None,
                "cost_status": "not_available_from_cli",
            }
            self.last_record = record
            return [], record

        if not shutil.which(self.codex_executable):
            detail = "codex_cli_not_found"
            self.errors.append(detail)
            self.unknown_cost_count += 1
            record = {
                "status": "codex_cli_error",
                "backend": "codex_cli",
                "model": self.model,
                "candidate_input_policy": self.input_policy,
                "raw_candidate_count": len(candidates),
                "prompt_candidate_count": len(feasible),
                "invocation_made": False,
                "request_made": False,
                "error": detail,
                "account_cost_usd": None,
                "cost_status": "unknown_no_cli_invocation",
            }
            self.last_record = record
            return [], record

        feasible_candidates = sorted(feasible.values(), key=lambda item: item.candidate_id)
        messages = build_direct_assignment_messages(
            snapshot_time_sec=snapshot_time_sec,
            vehicles=vehicles,
            orders=orders,
            # Infeasible edges cannot be selected by the LLM or accepted by the
            # local validator. Omitting them preserves the legal action set and
            # keeps high-load prompts bounded.
            candidates=feasible_candidates,
            max_wait_sec=max_wait_sec,
        )
        prompt = (
            messages[0]["content"]
            + "\n\n仅依据下列当前快照和候选表做一次直接匹配。不要调用工具，不要读取或修改文件，"
            "不要执行命令。只返回符合 JSON Schema 的对象。\n\n"
            + messages[1]["content"]
        )
        prompt_bytes = prompt.encode("utf-8")
        schema_bytes = self.schema_path.read_bytes()
        call_index, trace_paths = reserve_indexed_artifacts(
            self.trace_dir,
            "dispatch_round",
            (".jsonl", ".final.json"),
            (".jsonl",),
            self.trace_index,
        )
        trace_path = trace_paths[".jsonl"]
        final_path = trace_paths[".final.json"]
        started = time.perf_counter()
        self.trace_index = call_index
        self.call_count += 1
        process = None
        stdout = ""
        stderr = ""
        try:
            with tempfile.TemporaryDirectory(prefix="dispatch-codex-empty-") as empty_cwd:
                command = [
                    self.codex_executable,
                    "exec",
                    "--json",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--model",
                    self.model,
                    "--config",
                    'model_reasoning_effort="{0}"'.format(self.reasoning_effort),
                    "--output-schema",
                    str(self.schema_path),
                    "--output-last-message",
                    str(final_path),
                    "--cd",
                    empty_cwd,
                    "-",
                ]
                disable_args = [
                    argument
                    for feature in CODEX_DISABLED_TOOL_FEATURES
                    for argument in ("--disable", feature)
                ]
                command[-1:-1] = disable_args
                child_env = os.environ.copy()
                for name in (
                    "DEEPSEEK_API_KEY",
                    "DISPATCH_LLM_MODEL",
                    "DISPATCH_LLM_BASE_URL",
                    "OPENAI_API_KEY",
                    "OPENAI_BASE_URL",
                    "OPENAI_API_BASE",
                    "OPENAI_ORG_ID",
                    "OPENAI_PROJECT",
                    "CODEX_API_KEY",
                ):
                    child_env.pop(name, None)
                process = subprocess.run(
                    command,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_sec,
                    check=False,
                    cwd=empty_cwd,
                    env=child_env,
                )
                stdout = process.stdout or ""
                stderr = process.stderr or ""
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.latency_ms += elapsed_ms
            self.errors.append("codex_cli_timeout")
            trace_path.write_text(stdout, encoding="utf-8")
            partial_events, partial_audit, parse_errors = self._audit_jsonl(stdout)
            cost_estimate = self._record_codex_usage(partial_audit)
            record = {
                "status": "codex_cli_timeout",
                "backend": "codex_cli",
                "call_index": call_index,
                "model": self.model,
                "candidate_input_policy": self.input_policy,
                "raw_candidate_count": len(candidates),
                "prompt_candidate_count": len(feasible_candidates),
                "codex_cli_version": self.cli_version,
                "invocation_made": True,
                "request_made": bool(partial_audit["turn_started_count"]),
                "latency_ms": elapsed_ms,
                "timeout_sec": self.timeout_sec,
                "retry_count": 0,
                "tool_policy": self._tool_policy(),
                "input_tokens": partial_audit["input_tokens"],
                "cached_input_tokens": partial_audit["cached_input_tokens"],
                "output_tokens": partial_audit["output_tokens"],
                "reasoning_tokens": partial_audit["reasoning_tokens"],
                "total_tokens": partial_audit["total_tokens"],
                "usage_status": "available_best_effort" if partial_audit["usage_available"] else "unknown_timeout",
                "account_cost_usd": None,
                "cost_status": "account_plan_or_credits_not_exposed_by_cli" if partial_audit["usage_available"] else "unknown_timeout",
                "cost_estimate": cost_estimate,
                "jsonl_parse_errors": parse_errors,
                "trace_jsonl_path": str(trace_path),
                "trace_jsonl_sha256": self._sha256(stdout.encode("utf-8")),
                "stderr_tail": self._safe_error(stderr),
            }
            self.last_record = record
            return [], record
        except Exception as error:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.latency_ms += elapsed_ms
            detail = self._safe_error(str(error))
            self.errors.append(detail)
            trace_path.write_text(stdout, encoding="utf-8")
            partial_events, partial_audit, parse_errors = self._audit_jsonl(stdout)
            cost_estimate = self._record_codex_usage(partial_audit)
            record = {
                "status": "codex_cli_error",
                "backend": "codex_cli",
                "call_index": call_index,
                "model": self.model,
                "candidate_input_policy": self.input_policy,
                "raw_candidate_count": len(candidates),
                "prompt_candidate_count": len(feasible_candidates),
                "codex_cli_version": self.cli_version,
                "invocation_made": True,
                "request_made": bool(partial_audit["turn_started_count"]),
                "latency_ms": elapsed_ms,
                "retry_count": 0,
                "tool_policy": self._tool_policy(),
                "error": detail,
                "input_tokens": partial_audit["input_tokens"],
                "cached_input_tokens": partial_audit["cached_input_tokens"],
                "output_tokens": partial_audit["output_tokens"],
                "reasoning_tokens": partial_audit["reasoning_tokens"],
                "total_tokens": partial_audit["total_tokens"],
                "usage_status": "available_best_effort" if partial_audit["usage_available"] else "unknown_cli_error",
                "account_cost_usd": None,
                "cost_status": "account_plan_or_credits_not_exposed_by_cli" if partial_audit["usage_available"] else "unknown_cli_error",
                "cost_estimate": cost_estimate,
                "jsonl_parse_errors": parse_errors,
                "trace_jsonl_path": str(trace_path),
                "trace_jsonl_sha256": self._sha256(stdout.encode("utf-8")),
                "stderr_tail": self._safe_error(stderr),
            }
            self.last_record = record
            return [], record

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.latency_ms += elapsed_ms
        trace_path.write_text(stdout, encoding="utf-8")
        events, audit, parse_errors = self._audit_jsonl(stdout)
        cli_error_messages = []
        for event in events:
            if event.get("type") != "error":
                continue
            message = event.get("message")
            if isinstance(message, dict):
                nested = message.get("error")
                if isinstance(nested, dict):
                    message = nested.get("message") or json.dumps(nested, ensure_ascii=False)
                else:
                    message = json.dumps(message, ensure_ascii=False)
            if isinstance(message, str) and message.lstrip().startswith("{"):
                try:
                    nested_message = json.loads(message)
                    nested_error = nested_message.get("error") if isinstance(nested_message, dict) else None
                    if isinstance(nested_error, dict) and isinstance(nested_error.get("message"), str):
                        message = nested_error["message"]
                except Exception:
                    pass
            if isinstance(message, str):
                cli_error_messages.append(self._safe_error(message))
        if audit["usage_available"]:
            cost_estimate = self._record_codex_usage(audit)
        else:
            cost_estimate = self._record_codex_usage(audit)

        final_text = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
        final_bytes = final_text.encode("utf-8")
        if final_path.exists():
            # Preserve the structured final response beside the JSONL stream.
            if final_path.resolve() != (self.trace_dir / final_path.name).resolve():
                final_path.write_bytes(final_bytes)
        parsed = None
        parse_error = None
        try:
            parsed = json.loads(final_text)
        except Exception as error:
            parse_error = type(error).__name__

        exit_code = process.returncode if process is not None else None
        tool_used = audit["tool_usage_detected"]
        status = "accepted"
        detail = None
        if tool_used:
            status = "tool_use_invalidated_direct_result"
            detail = "codex_cli_tool_event_detected"
        elif exit_code != 0:
            status = "codex_cli_error"
            detail = (
                cli_error_messages[-1]
                if cli_error_messages
                else "codex_cli_exit_code:{0}".format(exit_code)
            )
        elif parse_errors:
            status = "codex_cli_event_parse_error"
            detail = "jsonl_parse_error"
        elif parsed is None:
            status = "llm_error_or_invalid_response"
            detail = "final_json_parse_error:{0}".format(parse_error)
        else:
            ids = parsed.get("selected_candidate_ids") if isinstance(parsed, dict) else None
            if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
                status = "llm_error_or_invalid_response"
                detail = "invalid_selected_candidate_ids"
            elif len(ids) != len(set(ids)):
                status = "llm_error_or_invalid_response"
                detail = "duplicate_candidate_id"
            else:
                selected = []
                used_vehicles = set()
                used_orders = set()
                for candidate_id in ids:
                    candidate = feasible.get(candidate_id)
                    if candidate is None:
                        detail = "selected_candidate_not_hard_feasible:" + candidate_id
                        break
                    if candidate.vehicle_id in used_vehicles:
                        detail = "vehicle_selected_twice:" + candidate.vehicle_id
                        break
                    if candidate.order_id in used_orders:
                        detail = "order_selected_twice:" + candidate.order_id
                        break
                    used_vehicles.add(candidate.vehicle_id)
                    used_orders.add(candidate.order_id)
                    selected.append(candidate)
                if detail is not None:
                    status = "llm_error_or_invalid_response"
                    selected = []

        request_made = bool(audit["turn_started_count"])
        if status != "accepted":
            self.errors.append(detail or status)
        record = {
            "status": status,
            "backend": "codex_cli",
            "call_index": call_index,
            "model": self.model,
            "candidate_input_policy": self.input_policy,
            "raw_candidate_count": len(candidates),
            "prompt_candidate_count": len(feasible_candidates),
            "codex_cli_version": self.cli_version,
            "reasoning_effort": self.reasoning_effort,
            "invocation_made": True,
            "request_made": request_made,
            "cli_exit_code": exit_code,
            "retry_count": 0,
            "tool_policy": self._tool_policy(),
            "latency_ms": elapsed_ms,
            "input_tokens": audit["input_tokens"],
            "cached_input_tokens": audit["cached_input_tokens"],
            "output_tokens": audit["output_tokens"],
            "reasoning_tokens": audit["reasoning_tokens"],
            "total_tokens": audit["total_tokens"],
            "usage_status": "available_best_effort" if audit["usage_available"] else "missing",
            "usage_source": "codex_exec_json_turn_completed_event",
            "turn_started_count": audit["turn_started_count"],
            "turn_completed_usage_count": audit["turn_completed_usage_count"],
            "tool_usage_detected": tool_used,
            "tool_item_types": audit["tool_item_types"],
            "other_non_message_item_types": audit["other_non_message_item_types"],
            "event_types": audit["event_types"],
            "max_output_tokens": None,
            "account_cost_usd": None,
            "cost_estimate": cost_estimate,
            "cost_status": (
                "account_plan_or_credits_not_exposed_by_cli"
                if audit["usage_available"]
                else "unknown_request_or_error_without_usage"
            ),
            "cli_error_messages": cli_error_messages,
            "prompt_sha256": self._sha256(prompt_bytes),
            "schema_sha256": self._sha256(schema_bytes),
            "trace_jsonl_path": str(trace_path),
            "trace_jsonl_sha256": self._sha256(stdout.encode("utf-8")),
            "final_json_path": str(self.trace_dir / final_path.name) if final_path.exists() else None,
            "final_json_sha256": self._sha256(final_bytes) if final_path.exists() else None,
            "jsonl_parse_errors": parse_errors,
            "error": detail,
            "stderr_tail": self._safe_error(stderr),
        }
        self.last_record = record
        return selected if status == "accepted" else [], record


def default_codex_cli_direct_assignment(
    *, model: str, trace_dir: Path
) -> CodexCliDirectAssignment:
    return CodexCliDirectAssignment(model=model, trace_dir=trace_dir)

