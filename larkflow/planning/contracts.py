"""Provider-neutral contracts for one bounded planning request."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from .context import ContextBundle


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def to_mutable(value: Any) -> Any:
    """Return a detached JSON-like value for compatibility callers."""
    if isinstance(value, Mapping):
        return {key: to_mutable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_mutable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [to_mutable(item) for item in value]
    return value


@dataclass(frozen=True)
class PlannerRequest:
    """Inputs authorized for one candidate-DAG planning attempt."""

    tenant_id: str
    actor_person_id: str
    request_id: str
    brief: str
    context: str = ""
    constraints: Mapping[str, Any] = field(default_factory=dict)
    context_bundle: ContextBundle | None = None
    capability_envelope: Mapping[str, Any] = field(default_factory=dict)
    policy: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "actor_person_id", "request_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"planner request {field_name} is required")
        object.__setattr__(self, "constraints", _freeze(self.constraints))
        if self.context_bundle is not None:
            bundle = self.context_bundle
            if bundle.tenant_id != self.tenant_id:
                raise ValueError("planner context tenant does not match request")
            if bundle.actor_person_id != self.actor_person_id:
                raise ValueError("planner context actor does not match request")
            if bundle.scope_id != self.request_id:
                raise ValueError("planner context scope does not match request")
        object.__setattr__(
            self,
            "capability_envelope",
            _freeze(self.capability_envelope),
        )
        object.__setattr__(self, "policy", _freeze(self.policy))


@dataclass(frozen=True)
class PlannerResult:
    """An untrusted candidate plus runtime observations."""

    candidate: Mapping[str, Any]
    validation_report: tuple[str, ...] = ()
    planning_evidence: Mapping[str, Any] = field(default_factory=dict)
    usage: Mapping[str, Any] = field(default_factory=dict)
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict)
    trace_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate", _freeze(self.candidate))
        object.__setattr__(
            self,
            "validation_report",
            tuple(self.validation_report),
        )
        object.__setattr__(
            self,
            "planning_evidence",
            _freeze(self.planning_evidence),
        )
        object.__setattr__(self, "usage", _freeze(self.usage))
        object.__setattr__(
            self,
            "runtime_metadata",
            _freeze(self.runtime_metadata),
        )


class PlannerRuntime(Protocol):
    """Produce one candidate without writing workflow state."""

    def plan(
        self,
        request: PlannerRequest,
        *,
        on_repair: Callable[[], None] | None = None,
    ) -> PlannerResult:
        ...


class DraftGenerator(Protocol):
    """Compatibility facade consumed by existing durable draft workers."""

    def generate(
        self,
        *,
        tenant_id: str,
        actor_person_id: str,
        request_id: str,
        brief: str,
        context: str,
        context_bundle: ContextBundle | None = None,
        on_repair: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        ...


__all__ = [
    "DraftGenerator",
    "PlannerRequest",
    "PlannerResult",
    "PlannerRuntime",
]
