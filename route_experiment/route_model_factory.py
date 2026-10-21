"""Construct explicitly selected live route-model clients."""

from pathlib import Path
from typing import Any, Optional

from .codex_cli_adapter import CodexCliRouteStepClient
from .llm_adapter import DeepSeekClient


def build_route_model_client(
    *,
    backend: str,
    generation_mode: str,
    model: str,
    trace_dir: Path,
    api_key: str = "",
    base_url: str = "https://api.deepseek.com",
    reasoning_effort: str = "low",
    timeout_sec: int = 90,
    max_output_tokens: int = 128,
    route_constraint: Optional[Any] = None,
):
    """Build the requested backend without silently substituting providers."""

    if backend == "codex_cli":
        if generation_mode != "stepwise":
            raise ValueError("codex_cli_requires_stepwise_mode")
        return CodexCliRouteStepClient(
            model=model,
            trace_dir=trace_dir,
            reasoning_effort=reasoning_effort,
            timeout_sec=timeout_sec,
            route_constraint=route_constraint,
        )
    if backend == "deepseek":
        if not api_key:
            raise ValueError("missing_deepseek_api_key")
        return DeepSeekClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout_sec,
            retries=0,
            max_tokens=max_output_tokens,
            reasoning_effort="none",
            route_constraint=route_constraint,
        )
    raise ValueError("route_model_backend_invalid")
