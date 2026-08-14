"""Completion AgentRuntime adapter around the current baseline executor."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from larkflow.workflow.executors import LLMAgentExecutor
from larkflow.workflow.model import ExecutorKind

from .contracts import AgentRunRequest, AgentRunResult


@dataclass(frozen=True)
class _CompletionExecutionRequest:
    """Only the fields the legacy completion adapter is allowed to observe."""

    tenant_id: str
    instance_id: str
    node_key: str
    attempt_id: str
    attempt_no: int
    owner_person_id: str
    executor: ExecutorKind
    work: Mapping[str, Any]
    input_snapshot: Mapping[str, Any]

    @property
    def idempotency_key(self) -> str:
        return f"{self.tenant_id}:{self.attempt_id}"


class CompletionAgentRuntime:
    """Keep the current one-completion behavior behind the new port."""

    NAME = "completion"

    def __init__(self, executor: LLMAgentExecutor) -> None:
        self.executor = executor

    def accepts(
        self,
        *,
        executor: str,
        work_contract: Mapping[str, Any],
    ) -> bool:
        try:
            executor_kind = ExecutorKind(executor)
        except ValueError:
            return False
        return self.executor.accepts(executor=executor_kind, work=work_contract)

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        result = self.executor.execute(
            _CompletionExecutionRequest(
                tenant_id=request.tenant_id,
                instance_id=request.instance_id,
                node_key=request.node_key,
                attempt_id=request.attempt_id,
                attempt_no=request.attempt_no,
                owner_person_id=request.owner_person_id,
                executor=ExecutorKind(request.executor),
                work=request.work_contract,
                input_snapshot=request.input_snapshot,
            )
        )
        return AgentRunResult(
            deliverables=result.result,
            runtime_metadata={
                "runtime": self.NAME,
                "adapter": "llm_agent_executor",
                "adapter_version": "1",
            },
        )


__all__ = ["CompletionAgentRuntime"]
