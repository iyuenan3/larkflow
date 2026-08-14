"""Provider-neutral contracts for one Agent node Attempt."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class AgentRunRequest:
    """The exact business input authorized for one runtime call."""

    tenant_id: str
    instance_id: str
    node_key: str
    attempt_id: str
    attempt_no: int
    owner_person_id: str
    executor: str
    work_contract: Mapping[str, Any]
    input_snapshot: Mapping[str, Any]
    context_bundle: Mapping[str, Any] = field(default_factory=dict)
    capability_envelope: Mapping[str, Any] = field(default_factory=dict)
    policy: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        executor = getattr(self.executor, "value", self.executor)
        object.__setattr__(self, "executor", str(executor))
        object.__setattr__(self, "work_contract", _freeze(self.work_contract))
        object.__setattr__(self, "input_snapshot", _freeze(self.input_snapshot))
        object.__setattr__(self, "context_bundle", _freeze(self.context_bundle))
        object.__setattr__(
            self,
            "capability_envelope",
            _freeze(self.capability_envelope),
        )
        object.__setattr__(self, "policy", _freeze(self.policy))

    @property
    def idempotency_key(self) -> str:
        return f"{self.tenant_id}:{self.attempt_id}"


@dataclass(frozen=True)
class AgentRunResult:
    """Candidate deliverables and non-authoritative runtime observations."""

    deliverables: Mapping[str, Any]
    quality_observations: tuple[str, ...] = ()
    tool_invocations: tuple[Mapping[str, Any], ...] = ()
    source_refs: tuple[str, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict)
    trace_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "deliverables", _freeze(self.deliverables))
        object.__setattr__(
            self,
            "quality_observations",
            tuple(self.quality_observations),
        )
        object.__setattr__(
            self,
            "tool_invocations",
            tuple(_freeze(item) for item in self.tool_invocations),
        )
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "usage", _freeze(self.usage))
        object.__setattr__(
            self,
            "runtime_metadata",
            _freeze(self.runtime_metadata),
        )


class AgentRuntime(Protocol):
    """Execute one node Attempt without changing workflow state."""

    def accepts(
        self,
        *,
        executor: str,
        work_contract: Mapping[str, Any],
    ) -> bool:
        ...

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        ...


__all__ = [
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRuntime",
]
