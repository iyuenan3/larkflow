"""Bridge the workflow Worker contract to a replaceable AgentRuntime."""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from larkflow.workflow.model import ExecutorKind
from larkflow.workflow.runtime import ExecutionRequest, ExecutionResult

from .contracts import AgentRunRequest, AgentRuntime


class AgentRuntimeExecutor:
    """Keep claims local while passing one sanitized Attempt to a runtime."""

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        policy: Mapping[str, Any] | None = None,
    ) -> None:
        self.runtime = runtime
        self.policy = MappingProxyType(dict(policy or {}))

    def accepts(
        self,
        *,
        executor: ExecutorKind,
        work: Mapping[str, Any],
    ) -> bool:
        return self.runtime.accepts(
            executor=ExecutorKind(executor).value,
            work_contract=work,
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        runtime_request = AgentRunRequest(
            tenant_id=request.tenant_id,
            instance_id=request.instance_id,
            node_key=request.node_key,
            attempt_id=request.attempt_id,
            attempt_no=request.attempt_no,
            owner_person_id=request.owner_person_id,
            executor=request.executor.value,
            work_contract=request.work,
            input_snapshot=request.input_snapshot,
            policy=self.policy,
        )
        result = self.runtime.run(runtime_request)
        return ExecutionResult(result=result.deliverables)


__all__ = ["AgentRuntimeExecutor"]
