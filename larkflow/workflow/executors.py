"""Explicitly scoped executor adapters for the Target runtime."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import time

from .model import ExecutorKind
from .runtime import ExecutionRequest, ExecutionResult
from .serde import to_json_value


class DevelopmentToolExecutor:
    """Deterministic dev-only Tool adapter used for persistence rehearsals."""

    KIND = "development.echo"

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_delay_seconds: float = 60.0,
    ) -> None:
        if max_delay_seconds <= 0:
            raise ValueError("max_delay_seconds must be positive")
        self.sleep = sleep
        self.max_delay_seconds = max_delay_seconds

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        tool = request.work.get("tool")
        if not self.accepts(executor=request.executor, work=request.work):
            raise ValueError(f"unsupported development tool kind: {tool!r}")
        args = tool.get("args") or {}
        if not isinstance(args, Mapping):
            raise ValueError("development tool args must be an object")

        delay = args.get("delay_seconds", 0)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)):
            raise ValueError("delay_seconds must be a number")
        if delay < 0 or delay > self.max_delay_seconds:
            raise ValueError(
                f"delay_seconds must be between 0 and {self.max_delay_seconds:g}"
            )
        if delay:
            self.sleep(float(delay))

        result = args.get("result", {"value": args.get("value")})
        if not isinstance(result, Mapping):
            raise ValueError("development tool result must be an object")
        return ExecutionResult(result=to_json_value(result))

    def accepts(self, *, executor: ExecutorKind, work: Mapping[str, object]) -> bool:
        tool = work.get("tool")
        return (
            executor == ExecutorKind.TOOL
            and isinstance(tool, Mapping)
            and tool.get("kind") == self.KIND
        )
