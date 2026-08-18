"""Bridge the workflow Worker contract to a replaceable AgentRuntime."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

from larkflow.workflow.model import ExecutorKind
from larkflow.workflow.runtime import ExecutionRequest, ExecutionResult

from .contracts import (
    AgentContextRequest,
    AgentContextResolver,
    AgentRunRequest,
    AgentRuntime,
    CapabilityEnvelope,
    PROJECT_ATTACHMENTS_INPUT,
)


class AgentContextUnavailable(RuntimeError):
    """A node requested project context that the Worker cannot authorize."""

    error_code = "agent_context_unavailable"


class AgentRuntimeExecutor:
    """Keep claims local while passing one sanitized Attempt to a runtime."""

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        policy: Mapping[str, Any] | None = None,
        context_resolver: AgentContextResolver | None = None,
        clock: Any = None,
        capability_ttl: timedelta = timedelta(minutes=5),
        max_context_chars: int = 12_000,
    ) -> None:
        if capability_ttl <= timedelta(0):
            raise ValueError("Agent capability TTL must be positive")
        if max_context_chars < 1:
            raise ValueError("Agent context character budget must be positive")
        self.runtime = runtime
        self.policy = MappingProxyType(dict(policy or {}))
        self.context_resolver = context_resolver
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.capability_ttl = capability_ttl
        self.max_context_chars = max_context_chars

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
        context_request = AgentContextRequest(
            tenant_id=request.tenant_id,
            instance_id=request.instance_id,
            node_key=request.node_key,
            attempt_id=request.attempt_id,
            attempt_no=request.attempt_no,
            owner_person_id=request.owner_person_id,
            work_contract=request.work,
            input_snapshot=request.input_snapshot,
        )
        context_bundle = None
        if self.context_resolver is not None:
            context_bundle = self.context_resolver.resolve(context_request)
        elif _declares_project_attachments(request.work):
            raise AgentContextUnavailable(
                "Agent node requested project attachments, but context is unavailable"
            )
        if self.context_resolver is None:
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

        now = self.clock()
        capabilities = (
            ("context.read.project_attachments",)
            if context_bundle is not None
            else ()
        )
        envelope = CapabilityEnvelope(
            tenant_id=request.tenant_id,
            actor_person_id=request.owner_person_id,
            instance_id=request.instance_id,
            node_key=request.node_key,
            attempt_id=request.attempt_id,
            attempt_no=request.attempt_no,
            allowed_capabilities=capabilities,
            knowledge_scopes=("project_attachments",) if capabilities else (),
            data_classification=(
                context_bundle.data_classification
                if context_bundle is not None
                else "none"
            ),
            egress_decision=(
                context_bundle.egress_decision
                if context_bundle is not None
                else "none"
            ),
            max_context_chars=(self.max_context_chars if capabilities else 0),
            issued_at=now,
            expires_at=now + self.capability_ttl,
        )
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
            context_bundle=context_bundle,
            capability_envelope=envelope,
            policy=self.policy,
        )
        result = self.runtime.run(runtime_request)
        deliverables = dict(result.deliverables)
        runtime_evidence: dict[str, Any] = {
            "capability_envelope": envelope.audit_value(),
            "runtime_metadata": dict(result.runtime_metadata),
        }
        if context_bundle is not None:
            runtime_evidence["context_manifest"] = context_bundle.snapshot_manifest()
        deliverables["_runtime_evidence"] = runtime_evidence
        return ExecutionResult(result=deliverables)


def _declares_project_attachments(work: Mapping[str, Any]) -> bool:
    inputs = work.get("inputs") or ()
    if isinstance(inputs, (str, bytes)):
        return False
    for value in inputs:
        reference = value.get("ref") if isinstance(value, Mapping) else value
        if reference == PROJECT_ATTACHMENTS_INPUT:
            return True
    return False


__all__ = [
    "AgentContextUnavailable",
    "AgentRuntimeExecutor",
    "PROJECT_ATTACHMENTS_INPUT",
]
