"""Codex CLI adapter for one-step R4 route proposals.

Each call is isolated in an empty read-only workspace, carries the current
step's JSON Schema, disables shell execution, and retains a local JSONL audit.
This backend uses the logged-in Codex account; it does not use a model API key.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


_TOOL_ITEM_TYPES = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search_call",
    "computer_call",
    "function_call",
    "tool_call",
    "image_view",
}
_API_ENV_KEYS = (
    "DEEPSEEK_API_KEY",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT",
    "CODEX_API_KEY",
)


@dataclass
class CodexCliRouteResponse:
    content: str
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    retries: int = 0
    request_id: Optional[str] = None
    finish_reason: str = ""
    output_constraint_mode: str = "codex_cli_json_schema"
    constraint_schema_sha256: Optional[str] = None
    constraint_schema_bytes: int = 0
    provider_audit: Dict[str, Any] = field(default_factory=dict)


class CodexCliRouteStepClient:
    """One Codex CLI invocation per constrained R4 route action."""

    TOOL_ITEM_TYPES = _TOOL_ITEM_TYPES

    def __init__(
        self,
        *,
        model: str,
        trace_dir: Path,
        reasoning_effort: str = "low",
        codex_executable: str = "codex",
        timeout_sec: int = 300,
        route_constraint: Optional[Any] = None,
    ) -> None:
        if reasoning_effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("codex_cli_reasoning_effort_invalid")
        self.model = model
        self.trace_dir = Path(trace_dir)
        self.reasoning_effort = reasoning_effort
        self.codex_executable = codex_executable
        self.timeout_sec = max(1, int(timeout_sec))
        self.route_constraint = route_constraint
        self.retries = 0
        self.call_count = 0
        self.last_record: Dict[str, Any] = {"status": "not_called", "backend": "codex_cli"}
        self.cli_version = self._read_cli_version()

    def _read_cli_version(self) -> Optional[str]:
        if not shutil.which(self.codex_executable):
            return None
        try:
            result = subprocess.run(
                [self.codex_executable, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            return None
        return (result.stdout or result.stderr).strip() or None

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @classmethod
    def _audit_events(cls, events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        turn_started = sum(event.get("type") == "turn.started" for event in events)
        usage_events = []
        tool_item_types = set()
        item_types = set()
        event_types = []
        for event in events:
            event_type = event.get("type")
            if isinstance(event_type, str):
                event_types.append(event_type)
            if event_type == "turn.completed" and isinstance(event.get("usage"), dict):
                usage_events.append(event["usage"])
            item = event.get("item")
            if isinstance(item, dict) and isinstance(item.get("type"), str):
                item_type = item["type"]
                item_types.add(item_type)
                if (
                    item_type in cls.TOOL_ITEM_TYPES
                    or item_type.endswith("_call")
                    or item_type.endswith("_execution")
                ):
                    tool_item_types.add(item_type)

        def sum_usage(key: str) -> Optional[int]:
            values = [row[key] for row in usage_events if isinstance(row.get(key), int)]
            return sum(values) if values else None

        cached = []
        reasoning = []
        for row in usage_events:
            if isinstance(row.get("cached_input_tokens"), int):
                cached.append(row["cached_input_tokens"])
            if isinstance(row.get("reasoning_output_tokens"), int):
                reasoning.append(row["reasoning_output_tokens"])
            details = row.get("input_tokens_details")
            if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
                cached.append(details["cached_tokens"])
            details = row.get("output_tokens_details")
            if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
                reasoning.append(details["reasoning_tokens"])
        return {
            "event_types": event_types,
            "item_types": sorted(item_types),
            "turn_started_count": turn_started,
            "usage_event_count": len(usage_events),
            "input_tokens": sum_usage("input_tokens"),
            "cached_input_tokens": sum(cached) if cached else None,
            "output_tokens": sum_usage("output_tokens"),
            "reasoning_output_tokens": sum(reasoning) if reasoning else None,
            "tool_usage_detected": bool(tool_item_types),
            "tool_item_types": sorted(tool_item_types),
        }

    @staticmethod
    def _render_prompt(messages: Sequence[Dict[str, str]]) -> str:
        content = []
        for message in messages:
            role = str(message.get("role", "user")).upper()
            content.append(role + ":\n" + str(message.get("content", "")))
        content.append(
            "Do not call tools, run commands, search, or access files. "
            "Return only the JSON object required by the supplied schema."
        )
        return "\n\n".join(content)

    def chat(self, messages: List[Dict[str, str]]) -> CodexCliRouteResponse:
        constraint = self.route_constraint
        if constraint is None or not isinstance(getattr(constraint, "schema", None), dict):
            raise RuntimeError("codex_cli_route_step_schema_required")
        if self.retries != 0:
            raise RuntimeError("codex_cli_route_retries_must_be_zero")
        if not shutil.which(self.codex_executable):
            raise RuntimeError("codex_cli_not_found")

        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.call_count += 1
        prefix = "r4_step_{0:04d}".format(self.call_count)
        prompt_path = self.trace_dir / (prefix + ".prompt.txt")
        schema_path = self.trace_dir / (prefix + ".schema.json")
        trace_path = self.trace_dir / (prefix + ".jsonl")
        final_path = self.trace_dir / (prefix + ".final.json")
        for path in (prompt_path, schema_path, trace_path, final_path):
            if path.exists():
                raise FileExistsError("codex_cli_route_artifact_exists:" + path.name)

        prompt = self._render_prompt(messages)
        prompt_bytes = prompt.encode("utf-8")
        schema_bytes = json.dumps(
            constraint.schema, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        prompt_path.write_bytes(prompt_bytes)
        schema_path.write_bytes(schema_bytes + b"\n")

        if hasattr(os, "chmod"):
            try:
                os.chmod(prompt_path, 0o600)
                os.chmod(schema_path, 0o600)
            except OSError:
                pass

        command = [
            self.codex_executable,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--disable",
            "shell_tool",
            "--disable",
            "unified_exec",
            "--model",
            self.model,
            "--config",
            'model_reasoning_effort="{0}"'.format(self.reasoning_effort),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(final_path),
            "--cd",
            "{empty_cwd}",
            "-",
        ]
        child_env = os.environ.copy()
        for name in _API_ENV_KEYS:
            child_env.pop(name, None)

        started = time.perf_counter()
        stdout = ""
        stderr = ""
        exit_code = None
        timeout = False
        try:
            with tempfile.TemporaryDirectory(prefix="route-codex-empty-") as empty_cwd:
                command[command.index("{empty_cwd}")] = empty_cwd
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
                exit_code = process.returncode
        except subprocess.TimeoutExpired as error:
            timeout = True
            stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        trace_path.write_text(stdout, encoding="utf-8")
        events = []
        parse_errors = []
        for line_number, line in enumerate(stdout.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                parse_errors.append("line_{0}:invalid_json".format(line_number))
        audit = self._audit_events(events)
        final_text = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
        record = {
            "backend": "codex_cli",
            "status": "response_received",
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "codex_cli_version": self.cli_version,
            "cli_exit_code": exit_code,
            "timed_out": timeout,
            "latency_ms": elapsed_ms,
            "retry_count": 0,
            "tool_policy": {
                "shell_tool": "disabled",
                "unified_exec": "disabled",
                "user_config": "ignored",
                "sandbox": "read-only",
                "working_directory": "empty_temporary_directory",
            },
            "event_audit": audit,
            "jsonl_parse_errors": parse_errors,
            "input_tokens": audit["input_tokens"],
            "cached_input_tokens": audit["cached_input_tokens"],
            "output_tokens": audit["output_tokens"],
            "reasoning_output_tokens": audit["reasoning_output_tokens"],
            "usage_available": audit["usage_event_count"] > 0,
            "output_token_hard_limit": None,
            "account_cost": "not_reported_by_cli",
            "prompt_path": str(prompt_path),
            "prompt_sha256": self._sha256(prompt_bytes),
            "schema_path": str(schema_path),
            "schema_sha256": self._sha256(schema_bytes),
            "trace_jsonl_path": str(trace_path),
            "trace_jsonl_sha256": self._sha256(stdout.encode("utf-8")),
            "final_json_path": str(final_path) if final_path.exists() else None,
            "stderr_tail": stderr[-1500:],
        }
        self.last_record = record

        if timeout:
            record["status"] = "timeout"
            raise RuntimeError("codex_cli_timeout")
        if parse_errors:
            record["status"] = "event_parse_error"
            raise RuntimeError("codex_cli_event_parse_error")
        if audit["tool_usage_detected"]:
            record["status"] = "tool_use_invalidated_response"
            raise RuntimeError("codex_cli_tool_event_detected")
        if audit["turn_started_count"] != 1:
            record["status"] = "unexpected_model_turn_count"
            raise RuntimeError("codex_cli_expected_exactly_one_model_turn")
        if exit_code != 0:
            record["status"] = "cli_error"
            raise RuntimeError("codex_cli_exit_code:{0}".format(exit_code))
        if not final_text:
            record["status"] = "missing_final_response"
            raise RuntimeError("codex_cli_final_response_missing")
        try:
            payload = json.loads(final_text)
        except json.JSONDecodeError as error:
            record["status"] = "invalid_final_json"
            raise RuntimeError("codex_cli_final_json_invalid") from error
        if not isinstance(payload, dict):
            record["status"] = "invalid_final_shape"
            raise RuntimeError("codex_cli_final_response_not_object")
        if audit["usage_event_count"] != 1:
            record["status"] = "usage_missing_or_ambiguous"
            raise RuntimeError("codex_cli_usage_event_missing_or_ambiguous")

        record["status"] = "accepted"
        return CodexCliRouteResponse(
            content=final_text,
            model=self.model,
            input_tokens=int(audit["input_tokens"] or 0),
            cached_input_tokens=int(audit["cached_input_tokens"] or 0),
            output_tokens=int(audit["output_tokens"] or 0),
            latency_ms=elapsed_ms,
            retries=0,
            request_id=str(trace_path),
            finish_reason="turn.completed",
            output_constraint_mode="codex_cli_json_schema",
            constraint_schema_sha256=str(getattr(constraint, "schema_sha256", "")) or None,
            constraint_schema_bytes=int(getattr(constraint, "schema_bytes", len(schema_bytes))),
            provider_audit=record,
        )
