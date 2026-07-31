"""Core domain types for the target collaboration DAG runtime."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class FrozenDict(Mapping[str, Any]):
    """A small immutable mapping used inside instance snapshots."""

    def __init__(self, values: Mapping[str, Any] | None = None):
        self._values = {
            str(key): _freeze(value) for key, value in (values or {}).items()
        }

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"FrozenDict({self._values!r})"

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        return self


def _freeze(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


class ExecutorKind(str, Enum):
    HUMAN = "human"
    AGENT = "agent"
    TOOL = "tool"


class InstanceStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"
    DISCARDED = "discarded"


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class AttemptStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class QualityVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class QualityResult:
    verdict: QualityVerdict
    evidence: str = ""
    suggestion: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", QualityVerdict(self.verdict))
        if self.verdict == QualityVerdict.FAIL and not self.evidence.strip():
            raise ValueError("failed quality result requires evidence")


@dataclass(frozen=True)
class NodeSpec:
    """Immutable node definition embedded in an instance snapshot."""

    key: str
    title: str
    owner_person_id: str
    executor: ExecutorKind
    deps: tuple[str, ...] = ()
    work: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "executor", ExecutorKind(self.executor))
        object.__setattr__(self, "deps", tuple(self.deps))
        object.__setattr__(self, "work", FrozenDict(self.work))


@dataclass(frozen=True)
class InstanceSnapshot:
    """Frozen graph used by the runtime, independent of how it was authored."""

    nodes: tuple[NodeSpec, ...]
    goal: str = ""
    template_version_id: str | None = None
    inputs: Mapping[str, Any] = field(default_factory=FrozenDict)
    schema_version: str = "0.2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "inputs", FrozenDict(self.inputs))

    def node(self, key: str) -> NodeSpec:
        for node in self.nodes:
            if node.key == key:
                return node
        raise KeyError(key)


@dataclass
class NodeInstance:
    id: str
    instance_id: str
    node_key: str
    owner_person_id: str
    executor: ExecutorKind
    status: NodeStatus
    current_attempt_no: int = 1
    version: int = 0
    ready_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class NodeAttempt:
    id: str
    node_instance_id: str
    attempt_no: int
    status: AttemptStatus = AttemptStatus.PENDING
    input_snapshot: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] | None = None
    quality_result: QualityResult | None = None
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    submitted_by_person_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class WorkflowInstance:
    id: str
    tenant_id: str
    owner_person_id: str
    snapshot: InstanceSnapshot
    status: InstanceStatus = InstanceStatus.DRAFT
    graph_revision: int = 1
    version: int = 0
    nodes: dict[str, NodeInstance] = field(default_factory=dict)
    attempts: dict[tuple[str, int], NodeAttempt] = field(default_factory=dict)
    created_at: datetime | None = None
    confirmed_at: datetime | None = None
    completed_at: datetime | None = None

    def current_attempt(self, node_key: str) -> NodeAttempt:
        node = self.nodes[node_key]
        return self.attempts[(node_key, node.current_attempt_no)]


@dataclass(frozen=True)
class NodeActivation:
    instance_id: str
    node_key: str
    node_instance_id: str
    attempt_id: str
    attempt_no: int
    owner_person_id: str
    executor: ExecutorKind
    status: NodeStatus
    expected_node_version: int
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
