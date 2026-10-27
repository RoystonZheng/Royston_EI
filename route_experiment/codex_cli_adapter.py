"""Codex CLI adapter for one-step R4 route proposals.

Each call is isolated in an empty read-only workspace, carries the current
step's JSON Schema, disables shell execution, and retains a local JSONL audit.
This backend uses the logged-in Codex account; it does not use a model API key.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from codex_cli_support import (
    CODEX_DISABLED_TOOL_FEATURES,
    estimate_codex_usage_cost,
    reserve_indexed_artifacts,
)

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
    session_id: Optional[str] = None
    finish_reason: str = ""
    output_constraint_mode: str = "codex_cli_json_schema"
    constraint_schema_sha256: Optional[str] = None
    constraint_schema_bytes: int = 0
    provider_audit: Dict[str, Any] = field(default_factory=dict)


class CodexCliRouteStepClient:
    """One Codex CLI invocation per constrained R4 route action."""

    TOOL_ITEM_TYPES = _TOOL_ITEM_TYPES
    NON_TOOL_ITEM_TYPES = {"agent_message", "reasoning", "error"}
    DISABLED_TOOL_FEATURES = CODEX_DISABLED_TOOL_FEATURES

    def __init__(
        self,
        *,
        model: str,
        trace_dir: Path,
        reasoning_effort: str = "low",
        codex_executable: str = "codex",
        timeout_sec: int = 300,
        route_constraint: Optional[Any] = None,
        persist_session: bool = False,
        session_id: Optional[str] = None,
        working_directory: Optional[Path] = None,
        trace_stem: str = "r4_step",
    ) -> None:
        if reasoning_effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("codex_cli_reasoning_effort_invalid")
        self.model = model
        # Codex CLI runs with a different --cd directory, so schema/output
        # arguments must not depend on the parent process working directory.
        self.trace_dir = Path(trace_dir).expanduser().resolve()
        self.reasoning_effort = reasoning_effort
        self.codex_executable = codex_executable
        self.timeout_sec = max(1, int(timeout_sec))
        self.route_constraint = route_constraint
        # A thread id is returned for auditing even in ephemeral mode. It must
        # never implicitly switch a fresh request into a session resume.
        self.persist_session = bool(persist_session)
        self.session_id = session_id if self.persist_session else None
        self.working_directory = Path(working_directory) if working_directory else None
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", trace_stem):
            raise ValueError("codex_cli_trace_stem_invalid")
        self.trace_stem = trace_stem
        self.retries = 0
        self.call_count = 0
        self.trace_index = 0
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

    @staticmethod
    def _redact_diagnostics(text: str) -> str:
        safe = re.sub(
            r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+",
            r"\1[REDACTED]",
            text,
        )
        safe = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_API_KEY]", safe)
        safe = re.sub(
            r"(?i)(\b(?:api[_-]?key|authorization)\s*[:=]\s*)\S+",
            r"\1[REDACTED]",
            safe,
        )
        return safe

    @classmethod
    def _audit_events(cls, events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        turn_started = sum(event.get("type") == "turn.started" for event in events)
        usage_events = []
        tool_item_types = set()
        item_types = set()
        event_types = []
        thread_ids = []
        for event in events:
            event_type = event.get("type")
            if isinstance(event_type, str):
                event_types.append(event_type)
            if (
                event_type == "thread.started"
                and isinstance(event.get("thread_id"), str)
                and event["thread_id"]
            ):
                thread_ids.append(event["thread_id"])
            if event_type == "turn.completed" and isinstance(event.get("usage"), dict):
                usage_events.append(event["usage"])
            item = event.get("item")
            if isinstance(item, dict) and isinstance(item.get("type"), str):
                item_type = item["type"]
                item_types.add(item_type)
                if (
                    item_type in cls.TOOL_ITEM_TYPES
                    or item_type not in cls.NON_TOOL_ITEM_TYPES
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
            "thread_ids": thread_ids,
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
            message_content = str(message.get("content", ""))
            if role == "SYSTEM":
                # The shared DeepSeek route prompt is tool-call oriented. This
                # adapter disables tools and instead constrains the final JSON
                # object with Codex CLI's output schema, so remove the
                # contradictory function-call wording before sending it.
                message_content = message_content.replace(
                    "工具 schema 的 direction 候选",
                    "JSON Schema 的 direction 候选",
                ).replace(
                    "输出 submit_r4_route 工具参数：",
                    "仅返回符合 JSON Schema 的 JSON 对象：",
                )
            content.append(role + ":\n" + message_content)
        content.append(
            "Do not call tools, run commands, search, or access files. "
            "Return only the JSON object required by the supplied schema."
        )
        return "\n\n".join(content)

    def _render_resume_prompt(self, messages: Sequence[Dict[str, str]]) -> str:
        user_messages = [item for item in messages if item.get("role") == "user"]
        if not user_messages:
            raise RuntimeError("codex_cli_resume_state_missing_user_message")
        try:
            snapshot = json.loads(str(user_messages[-1].get("content", "")))
        except json.JSONDecodeError as error:
            raise RuntimeError("codex_cli_resume_state_invalid_json") from error
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("q"), dict) or not isinstance(snapshot.get("s"), dict):
            raise RuntimeError("codex_cli_resume_state_missing_task_or_state")
        schema = getattr(self.route_constraint, "schema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        direction_schema = properties.get("direction", {}) if isinstance(properties, dict) else {}
        directions = direction_schema.get("enum", []) if isinstance(direction_schema, dict) else []
        if isinstance(directions, list) and directions:
            update = {
                "q": snapshot["q"],
                "s": snapshot["s"],
                "allowed_directions": directions,
            }
            # Keep per-step quality and validator feedback in the persistent
            # conversation update. The static map and feature context already
            # exist in the initial session prompt and must not be resent here.
            for key in ("r", "f"):
                if key in snapshot:
                    update[key] = snapshot[key]
            return (
                "当前状态更新（覆盖前序状态快照；初始消息中的地图、目标顺序和约束规则保持不变）。"
                "只提出当前节点的一步移动，并逐项完成原格式自检。\n"
                + json.dumps(update, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )

        # Segment-v1 uses an array of actions, so its direction enum is nested
        # under actions.items. Preserve only the latest compact route state on
        # resumed turns; the full map and RoadFeatureInputProtocol context stay
        # in the initial task-scoped session message.
        actions_schema = properties.get("actions", {}) if isinstance(properties, dict) else {}
        items_schema = actions_schema.get("items", {}) if isinstance(actions_schema, dict) else {}
        item_properties = items_schema.get("properties", {}) if isinstance(items_schema, dict) else {}
        segment_direction_schema = item_properties.get("direction", {}) if isinstance(item_properties, dict) else {}
        segment_directions = segment_direction_schema.get("enum", []) if isinstance(segment_direction_schema, dict) else []
        if not isinstance(segment_directions, list) or not segment_directions:
            raise RuntimeError("codex_cli_resume_direction_schema_missing")
        if not isinstance(snapshot.get("seg"), dict) or not isinstance(snapshot.get("r"), dict):
            raise RuntimeError("codex_cli_resume_segment_state_missing")
        update = {
            key: snapshot[key]
            for key in ("v", "q", "s", "seg", "r", "f")
            if key in snapshot
        }
        return (
            "路线段状态更新：当前快照覆盖前一段后的当前位置、访问状态、动作预算和路线进度；"
            "首轮消息中的地图、道路特征、固定端点与约束继续有效。"
            "根据最新快照提出最多 seg.max_actions 个连续动作，每步填写方向和结构化自检；"
            "方向字段遵循 JSON Schema 枚举。不要输出长篇自由文本推理。\n"
            + json.dumps(update, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    def render_request_prompt(self, messages: Sequence[Dict[str, str]]) -> str:
        """Return the exact stdin prompt that the next CLI invocation will receive."""
        return (
            self._render_resume_prompt(messages)
            if self.session_id
            else self._render_prompt(messages)
        )

    def chat(self, messages: List[Dict[str, str]]) -> CodexCliRouteResponse:
        constraint = self.route_constraint
        if constraint is None or not isinstance(getattr(constraint, "schema", None), dict):
            raise RuntimeError("codex_cli_route_step_schema_required")
        if self.retries != 0:
            raise RuntimeError("codex_cli_route_retries_must_be_zero")
        if not shutil.which(self.codex_executable):
            raise RuntimeError("codex_cli_not_found")
        if self.persist_session and self.working_directory:
            self.working_directory.mkdir(parents=True, exist_ok=True)
            if any(self.working_directory.iterdir()):
                self.last_record = {
                    "backend": "codex_cli",
                    "status": "not_called",
                    "stop_reason": "persistent_working_directory_not_empty",
                    "session_id": self.session_id,
                }
                raise RuntimeError("codex_cli_persistent_working_directory_not_empty")

        call_index, paths = reserve_indexed_artifacts(
            self.trace_dir,
            self.trace_stem,
            (".prompt.txt", ".schema.json", ".jsonl", ".final.json"),
            (".prompt.txt", ".schema.json"),
            self.trace_index,
        )
        prompt_path = paths[".prompt.txt"]
        schema_path = paths[".schema.json"]
        trace_path = paths[".jsonl"]
        final_path = paths[".final.json"]
        self.trace_index = call_index
        self.call_count += 1

        prompt = self.render_request_prompt(messages)
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

        if self.session_id:
            command = [
                self.codex_executable,
                "exec",
                "resume",
                self.session_id,
                "--json",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--model",
                self.model,
                "--config",
                'model_reasoning_effort="{0}"'.format(self.reasoning_effort),
                "--config",
                "skills.include_instructions=false",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(final_path),
                "-",
            ]
        else:
            command = [
                self.codex_executable,
                "exec",
                "--json",
                "--ignore-user-config",
                "--skip-git-repo-check",
            ]
            if not self.persist_session:
                command.append("--ephemeral")
            command.extend(
                [
                    "--sandbox",
                    "read-only",
                    "--model",
                    self.model,
                    "--config",
                    'model_reasoning_effort="{0}"'.format(self.reasoning_effort),
                    "--config",
                    "skills.include_instructions=false",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(final_path),
                    "--cd",
                    "{empty_cwd}",
                    "-",
                ]
            )
        disable_args = [
            argument
            for feature in self.DISABLED_TOOL_FEATURES
            for argument in ("--disable", feature)
        ]
        command[-1:-1] = disable_args
        child_env = os.environ.copy()
        for name in _API_ENV_KEYS:
            child_env.pop(name, None)

        started = time.perf_counter()
        stdout = ""
        stderr = ""
        exit_code = None
        timeout = False
        try:
            with trace_path.open("w", encoding="utf-8") as trace_stream:
                if self.persist_session and self.working_directory:
                    working_directory = str(self.working_directory.resolve())
                    if "{empty_cwd}" in command:
                        command[command.index("{empty_cwd}")] = working_directory
                    process = subprocess.run(
                        command,
                        input=prompt,
                        stdout=trace_stream,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=self.timeout_sec,
                        check=False,
                        cwd=working_directory,
                        env=child_env,
                    )
                else:
                    with tempfile.TemporaryDirectory(prefix="route-codex-empty-") as empty_cwd:
                        if "{empty_cwd}" in command:
                            command[command.index("{empty_cwd}")] = empty_cwd
                        process = subprocess.run(
                            command,
                            input=prompt,
                            stdout=trace_stream,
                            stderr=subprocess.PIPE,
                            text=True,
                            timeout=self.timeout_sec,
                            check=False,
                            cwd=empty_cwd,
                            env=child_env,
                        )
                trace_stream.flush()
                os.fsync(trace_stream.fileno())
                stderr = process.stderr or ""
                exit_code = process.returncode
        except subprocess.TimeoutExpired as error:
            timeout = True
            stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
            if trace_path.exists():
                with trace_path.open("a", encoding="utf-8") as trace_stream:
                    trace_stream.flush()
                    os.fsync(trace_stream.fileno())
        if trace_path.exists():
            stdout = trace_path.read_text(encoding="utf-8", errors="replace")

        elapsed_ms = (time.perf_counter() - started) * 1000.0
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
        reported_thread_ids = sorted(set(audit["thread_ids"]))
        if len(reported_thread_ids) > 1:
            record_thread_id = None
        else:
            record_thread_id = reported_thread_ids[0] if reported_thread_ids else self.session_id
        cost_estimate = estimate_codex_usage_cost(
            self.model,
            audit["input_tokens"],
            audit["cached_input_tokens"],
            audit["output_tokens"],
        )
        final_text = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
        agent_messages = [
            event["item"]["text"]
            for event in events
            if event.get("type") == "item.completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "agent_message"
            and isinstance(event["item"].get("text"), str)
        ]
        error_items = [
            item
            for event in events
            if event.get("type") == "item.completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "error"
            for item in [event["item"]]
        ]
        completed_turn_count = sum(event.get("type") == "turn.completed" for event in events)
        recovered_agent_message_path = None
        response_recovered_from_jsonl = False
        if not final_text and len(agent_messages) == 1:
            only_known_disabled_code_mode_error = all(
                "code mode is unavailable because code-mode host is disabled" in
                str(item.get("message", "")).lower()
                and "code mode will fail closed" in str(item.get("message", "")).lower()
                for item in error_items
            )
            try:
                recovered_payload = json.loads(agent_messages[0])
            except json.JSONDecodeError:
                recovered_payload = None
            if (
                not timeout
                and not parse_errors
                and exit_code == 0
                and audit["tool_usage_detected"] is False
                and audit["turn_started_count"] == 1
                and completed_turn_count == 1
                and audit["usage_event_count"] == 1
                and only_known_disabled_code_mode_error
                and isinstance(recovered_payload, dict)
            ):
                # Codex CLI can emit a complete, usage-bearing agent_message
                # while omitting --output-last-message when its disabled
                # Code Mode host reports a fail-closed diagnostic. Retain
                # the original JSONL and write the recovered text separately;
                # never fabricate the canonical final artifact.
                final_text = agent_messages[0]
                recovered_agent_message_path = final_path.with_name(
                    final_path.name.replace(".final.json", ".recovered-agent-message.json")
                )
                recovered_agent_message_path.write_text(final_text, encoding="utf-8")
                response_recovered_from_jsonl = True
        record = {
            "backend": "codex_cli",
            "call_index": call_index,
            "status": "response_received",
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "codex_cli_version": self.cli_version,
            "session_mode": "persistent_per_task" if self.persist_session else "ephemeral_per_request",
            "session_id": record_thread_id,
            "cli_exit_code": exit_code,
            "timed_out": timeout,
            "latency_ms": elapsed_ms,
            "retry_count": 0,
            "tool_policy": {
                "shell_tool": "disabled",
                "unified_exec": "disabled",
                "disabled_features": list(self.DISABLED_TOOL_FEATURES),
                "user_config": "ignored",
                "skill_instructions": "disabled_via_cli_config",
                "sandbox": "read-only",
                "working_directory": (
                    "persistent_empty_directory"
                    if self.persist_session and self.working_directory
                    else "empty_temporary_directory"
                ),
                "api_key_environment_variables_removed": list(_API_ENV_KEYS),
            },
            "event_audit": audit,
            "jsonl_parse_errors": parse_errors,
            "input_tokens": audit["input_tokens"],
            "cached_input_tokens": audit["cached_input_tokens"],
            "output_tokens": audit["output_tokens"],
            "reasoning_output_tokens": audit["reasoning_output_tokens"],
            "usage_available": audit["usage_event_count"] > 0,
            "agent_message_count": len(agent_messages),
            "canonical_final_json_present": final_path.exists(),
            "response_recovered_from_jsonl_agent_message": response_recovered_from_jsonl,
            "recovered_agent_message_path": (
                str(recovered_agent_message_path) if recovered_agent_message_path else None
            ),
            "recovery_error_items": error_items if response_recovered_from_jsonl else [],
            "output_token_hard_limit": None,
            "account_cost": "not_reported_by_cli",
            "cost_estimate": cost_estimate,
            "prompt_path": str(prompt_path),
            "prompt_sha256": self._sha256(prompt_bytes),
            "schema_path": str(schema_path),
            "schema_sha256": self._sha256(schema_bytes),
            "trace_jsonl_path": str(trace_path),
            "trace_jsonl_sha256": self._sha256(stdout.encode("utf-8")),
            "final_json_path": str(final_path) if final_path.exists() else None,
            "stderr_tail": self._redact_diagnostics(stderr)[-1500:],
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
        if len(reported_thread_ids) > 1:
            record["status"] = "unexpected_cli_session_count"
            raise RuntimeError("codex_cli_unexpected_session_count")
        if self.session_id and record_thread_id != self.session_id:
            record["status"] = "cli_session_id_mismatch"
            raise RuntimeError("codex_cli_session_id_mismatch")
        if self.persist_session and not record_thread_id:
            record["status"] = "missing_cli_session_id"
            raise RuntimeError("codex_cli_session_id_missing")
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
        if self.persist_session:
            self.session_id = record_thread_id
        return CodexCliRouteResponse(
            content=final_text,
            model=self.model,
            input_tokens=int(audit["input_tokens"] or 0),
            cached_input_tokens=int(audit["cached_input_tokens"] or 0),
            output_tokens=int(audit["output_tokens"] or 0),
            latency_ms=elapsed_ms,
            retries=0,
            request_id=str(trace_path),
            session_id=record_thread_id,
            finish_reason="turn.completed",
            output_constraint_mode="codex_cli_json_schema",
            constraint_schema_sha256=str(getattr(constraint, "schema_sha256", "")) or None,
            constraint_schema_bytes=int(getattr(constraint, "schema_bytes", len(schema_bytes))),
            provider_audit=record,
        )
