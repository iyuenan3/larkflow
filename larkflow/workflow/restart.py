"""Safe preview and confirmation rules for node restarts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .graph import reachable_downstream, topological_order
from .model import (
    AttemptStatus,
    FrozenDict,
    InstanceStatus,
    NodeAttempt,
    NodeStatus,
    WorkflowInstance,
)


class RestartError(RuntimeError):
    """Base class for an invalid or stale restart command."""


class RestartNotAllowedError(RestartError):
    """The current aggregate state cannot be restarted safely."""


class RestartPreviewNotFoundError(RestartError):
    """A durable restart preview does not exist in this tenant."""


class RestartPreviewExpiredError(RestartError):
    """A restart preview has passed its confirmation deadline."""


class StaleRestartPreviewError(RestartError):
    """The aggregate changed after the restart preview was created."""


class RestartScope(str, Enum):
    """Explicit impact boundary carried by a durable restart preview."""

    NODE = "node"
    INSTANCE = "instance"


@dataclass(frozen=True)
class RestartPreview:
    """Durable, bounded authority for one explicit restart confirmation."""

    id: str
    tenant_id: str
    instance_id: str
    actor_person_id: str
    node_key: str | None
    affected_node_keys: tuple[str, ...]
    expected_instance_version: int
    graph_revision: int
    created_at: datetime
    expires_at: datetime
    scope: RestartScope = RestartScope.NODE
    consumed_at: datetime | None = None
    applied_instance_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", RestartScope(self.scope))
        object.__setattr__(
            self,
            "affected_node_keys",
            tuple(self.affected_node_keys),
        )
        if self.scope == RestartScope.NODE and not self.node_key:
            raise ValueError("node restart preview requires node_key")
        if self.scope == RestartScope.INSTANCE and self.node_key is not None:
            raise ValueError("instance restart preview cannot have node_key")
        if not self.affected_node_keys:
            raise ValueError("restart preview requires affected nodes")
        if len(set(self.affected_node_keys)) != len(self.affected_node_keys):
            raise ValueError("restart preview affected nodes must be unique")


@dataclass(frozen=True)
class RestartConfirmation:
    """Result of consuming or replaying a restart preview."""

    instance: WorkflowInstance
    preview: RestartPreview
    already_applied: bool = False


RESTARTABLE_INSTANCE_STATUSES = {
    InstanceStatus.RUNNING,
    InstanceStatus.DONE,
    InstanceStatus.FAILED,
}
RESTARTABLE_TARGET_STATUSES = {
    NodeStatus.RUNNING,
    NodeStatus.WAITING_HUMAN,
    NodeStatus.DONE,
    NodeStatus.FAILED,
}


def affected_restart_node_keys(
    instance: WorkflowInstance,
    node_key: str,
) -> tuple[str, ...]:
    """Return the stable target-plus-downstream impact set."""

    if instance.status not in RESTARTABLE_INSTANCE_STATUSES:
        raise RestartNotAllowedError(
            f"instance cannot be restarted from {instance.status.value}"
        )
    try:
        target = instance.nodes[node_key]
        spec = instance.snapshot.node(node_key)
    except KeyError as exc:
        raise RestartNotAllowedError(f"unknown restart node: {node_key}") from exc
    if target.status not in RESTARTABLE_TARGET_STATUSES:
        raise RestartNotAllowedError(
            f"node cannot be restarted from {target.status.value}: {node_key}"
        )
    if any(
        instance.nodes[dependency].status != NodeStatus.DONE
        for dependency in spec.deps
    ):
        raise RestartNotAllowedError(
            f"restart target has incomplete dependencies: {node_key}"
        )
    affected = {node_key, *reachable_downstream(instance.snapshot, node_key)}
    blocking = sorted(
        key
        for key, node in instance.nodes.items()
        if key not in affected and node.status == NodeStatus.FAILED
    )
    if blocking:
        raise RestartNotAllowedError(
            "restart does not cover failed nodes: " + ", ".join(blocking)
        )
    return tuple(
        key for key in topological_order(instance.snapshot) if key in affected
    )


def affected_instance_restart_node_keys(
    instance: WorkflowInstance,
) -> tuple[str, ...]:
    """Return every node in stable topological order for a full restart."""

    if instance.status not in RESTARTABLE_INSTANCE_STATUSES:
        raise RestartNotAllowedError(
            f"instance cannot be restarted from {instance.status.value}"
        )
    affected = topological_order(instance.snapshot)
    if not affected:
        raise RestartNotAllowedError("instance has no nodes to restart")
    return affected


def apply_node_restart(
    instance: WorkflowInstance,
    preview: RestartPreview,
    *,
    now: datetime,
) -> None:
    """Create fresh Attempts without overwriting any historical attempt."""

    if preview.scope != RestartScope.NODE or preview.node_key is None:
        raise StaleRestartPreviewError("restart preview is not node-scoped")
    _apply_restart(instance, preview, now=now)


def apply_instance_restart(
    instance: WorkflowInstance,
    preview: RestartPreview,
    *,
    now: datetime,
) -> None:
    """Create fresh Attempts for the complete frozen graph."""

    if preview.scope != RestartScope.INSTANCE or preview.node_key is not None:
        raise StaleRestartPreviewError("restart preview is not instance-scoped")
    _apply_restart(instance, preview, now=now)


def apply_restart(
    instance: WorkflowInstance,
    preview: RestartPreview,
    *,
    now: datetime,
) -> None:
    """Apply either explicit restart scope using the same safety checks."""

    if preview.scope == RestartScope.NODE:
        apply_node_restart(instance, preview, now=now)
        return
    apply_instance_restart(instance, preview, now=now)


def _apply_restart(
    instance: WorkflowInstance,
    preview: RestartPreview,
    *,
    now: datetime,
) -> None:
    """Reset the preview impact set without overwriting historical Attempts."""

    if instance.id != preview.instance_id or instance.tenant_id != preview.tenant_id:
        raise StaleRestartPreviewError("restart preview targets another instance")
    if instance.version != preview.expected_instance_version:
        raise StaleRestartPreviewError("instance changed after restart preview")
    if instance.graph_revision != preview.graph_revision:
        raise StaleRestartPreviewError("graph changed after restart preview")
    if preview.scope == RestartScope.NODE:
        if preview.node_key is None:
            raise StaleRestartPreviewError("node restart preview has no target")
        affected = affected_restart_node_keys(instance, preview.node_key)
    else:
        affected = affected_instance_restart_node_keys(instance)
    if affected != preview.affected_node_keys:
        raise StaleRestartPreviewError("restart impact changed after preview")

    for node_key in affected:
        spec = instance.snapshot.node(node_key)
        node = instance.nodes[node_key]
        current_attempt = instance.current_attempt(node_key)
        if current_attempt.status in {
            AttemptStatus.PENDING,
            AttemptStatus.RUNNING,
            AttemptStatus.WAITING_HUMAN,
        }:
            current_attempt.status = AttemptStatus.CANCELED
            current_attempt.completed_at = now
        current_attempt.claimed_by = None
        current_attempt.claim_token = None
        current_attempt.claim_expires_at = None

        next_attempt_no = node.current_attempt_no + 1
        node.current_attempt_no = next_attempt_no
        node.version += 1
        node.status = NodeStatus.PENDING
        node.ready_at = None
        node.started_at = None
        node.completed_at = None
        instance.attempts[(node_key, next_attempt_no)] = NodeAttempt(
            id=f"{node.id}:attempt:{next_attempt_no}",
            node_instance_id=node.id,
            attempt_no=next_attempt_no,
            status=AttemptStatus.PENDING,
            input_snapshot=FrozenDict({"deps": spec.deps, "work": spec.work}),
        )

    if preview.scope == RestartScope.NODE:
        ready_keys = (preview.node_key,)
    else:
        ready_keys = tuple(
            spec.key for spec in instance.snapshot.nodes if not spec.deps
        )
    for node_key in ready_keys:
        if node_key is None:
            continue
        node = instance.nodes[node_key]
        node.status = NodeStatus.READY
        node.ready_at = now
    instance.status = InstanceStatus.RUNNING
    instance.completed_at = None
