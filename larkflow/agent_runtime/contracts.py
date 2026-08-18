"""Provider-neutral contracts for one Agent node Attempt."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType
from typing import Any, Protocol

from larkflow.planning.context import ContextBundle


PROJECT_ATTACHMENTS_INPUT = "instance_inputs.project_attachments"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class CapabilityEnvelope:
    """Server-issued, immutable capabilities for one Agent Attempt."""

    tenant_id: str
    actor_person_id: str
    instance_id: str
    node_key: str
    attempt_id: str
    attempt_no: int
    allowed_capabilities: tuple[str, ...]
    knowledge_scopes: tuple[str, ...]
    data_classification: str
    egress_decision: str
    max_context_chars: int
    issued_at: datetime
    expires_at: datetime
    envelope_id: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "actor_person_id",
            "instance_id",
            "node_key",
            "attempt_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"capability envelope {name} is required")
        if self.attempt_no < 1:
            raise ValueError("capability envelope attempt_no must be positive")
        if len(self.allowed_capabilities) != len(set(self.allowed_capabilities)):
            raise ValueError("capability envelope capabilities must be unique")
        if len(self.knowledge_scopes) != len(set(self.knowledge_scopes)):
            raise ValueError("capability envelope knowledge scopes must be unique")
        if self.data_classification not in {"none", "internal"}:
            raise ValueError("capability envelope classification is invalid")
        if self.egress_decision not in {"none", "allow", "deny"}:
            raise ValueError("capability envelope egress decision is invalid")
        if self.max_context_chars < 0:
            raise ValueError("capability envelope context budget is invalid")
        issued_at = _utc(self.issued_at)
        expires_at = _utc(self.expires_at)
        if expires_at <= issued_at:
            raise ValueError("capability envelope expiry is invalid")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        fingerprint = _capability_fingerprint(self)
        if self.fingerprint and self.fingerprint != fingerprint:
            raise ValueError("capability envelope fingerprint is invalid")
        object.__setattr__(self, "fingerprint", fingerprint)
        envelope_id = f"cap_{fingerprint[:32]}"
        if self.envelope_id and self.envelope_id != envelope_id:
            raise ValueError("capability envelope id is invalid")
        object.__setattr__(self, "envelope_id", envelope_id)

    def audit_value(self) -> dict[str, Any]:
        """Return safe evidence without credentials or source bodies."""

        return {
            "envelope_id": self.envelope_id,
            "fingerprint": self.fingerprint,
            "tenant_id": self.tenant_id,
            "actor_person_id": self.actor_person_id,
            "instance_id": self.instance_id,
            "node_key": self.node_key,
            "attempt_id": self.attempt_id,
            "attempt_no": self.attempt_no,
            "allowed_capabilities": list(self.allowed_capabilities),
            "knowledge_scopes": list(self.knowledge_scopes),
            "data_classification": self.data_classification,
            "egress_decision": self.egress_decision,
            "max_context_chars": self.max_context_chars,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


def _capability_fingerprint(envelope: CapabilityEnvelope) -> str:
    value = {
        "tenant_id": envelope.tenant_id,
        "actor_person_id": envelope.actor_person_id,
        "instance_id": envelope.instance_id,
        "node_key": envelope.node_key,
        "attempt_id": envelope.attempt_id,
        "attempt_no": envelope.attempt_no,
        "allowed_capabilities": list(envelope.allowed_capabilities),
        "knowledge_scopes": list(envelope.knowledge_scopes),
        "data_classification": envelope.data_classification,
        "egress_decision": envelope.egress_decision,
        "max_context_chars": envelope.max_context_chars,
        "issued_at": envelope.issued_at.isoformat(),
        "expires_at": envelope.expires_at.isoformat(),
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("capability envelope timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class AgentContextRequest:
    """Claim-free identity and snapshot used to authorize Agent context."""

    tenant_id: str
    instance_id: str
    node_key: str
    attempt_id: str
    attempt_no: int
    owner_person_id: str
    work_contract: Mapping[str, Any]
    input_snapshot: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "instance_id",
            "node_key",
            "attempt_id",
            "owner_person_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Agent context request {name} is required")
        if self.attempt_no < 1:
            raise ValueError("Agent context request attempt_no must be positive")
        object.__setattr__(self, "work_contract", _freeze(self.work_contract))
        object.__setattr__(self, "input_snapshot", _freeze(self.input_snapshot))


class AgentContextResolver(Protocol):
    """Resolve only the context explicitly authorized for one Attempt."""

    def resolve(self, request: AgentContextRequest) -> ContextBundle | None:
        ...


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
    context_bundle: ContextBundle | None = None
    capability_envelope: CapabilityEnvelope | None = None
    policy: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        executor = getattr(self.executor, "value", self.executor)
        object.__setattr__(self, "executor", str(executor))
        object.__setattr__(self, "work_contract", _freeze(self.work_contract))
        object.__setattr__(self, "input_snapshot", _freeze(self.input_snapshot))
        if self.context_bundle is not None:
            bundle = self.context_bundle
            if (
                bundle.tenant_id != self.tenant_id
                or bundle.scope_id != self.instance_id
                or bundle.actor_person_id != self.owner_person_id
                or bundle.node_key != self.node_key
                or bundle.attempt_id != self.attempt_id
            ):
                raise ValueError("Agent context bundle binding is invalid")
        if self.capability_envelope is not None:
            envelope = self.capability_envelope
            if (
                envelope.tenant_id != self.tenant_id
                or envelope.instance_id != self.instance_id
                or envelope.actor_person_id != self.owner_person_id
                or envelope.node_key != self.node_key
                or envelope.attempt_id != self.attempt_id
                or envelope.attempt_no != self.attempt_no
            ):
                raise ValueError("Agent capability envelope binding is invalid")
            context_capability = "context.read.project_attachments"
            if self.context_bundle is not None:
                if context_capability not in envelope.allowed_capabilities:
                    raise ValueError("Agent context capability is missing")
                context_chars = sum(
                    len(chunk.text) for chunk in self.context_bundle.chunks
                )
                if context_chars > envelope.max_context_chars:
                    raise ValueError("Agent context exceeds capability budget")
            elif context_capability in envelope.allowed_capabilities:
                raise ValueError("Agent context capability has no authorized bundle")
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
    "AgentContextRequest",
    "AgentContextResolver",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRuntime",
    "CapabilityEnvelope",
    "PROJECT_ATTACHMENTS_INPUT",
]
