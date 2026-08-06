"""Safe process-level pause, resume, and cancellation semantics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .model import (
    AttemptStatus,
    ExecutorKind,
    InstanceStatus,
    NodeStatus,
    WorkflowInstance,
)
from .transitions import transition_attempt, transition_instance, transition_node


class CancellationError(RuntimeError):
    """Base class for an invalid or stale cancellation command."""


class CancellationNotAllowedError(CancellationError):
    """The aggregate cannot be canceled from its current state."""


class StaleCancellationError(CancellationError):
    """The aggregate changed after the cancellation preview was rendered."""


@dataclass(frozen=True)
class CancellationPreview:
    """Version-bound impact summary for an explicit cancel confirmation."""

    instance_id: str
    expected_instance_version: int
    affected_node_keys: tuple[str, ...]
    active_node_keys: tuple[str, ...]


@dataclass(frozen=True)
class CancellationConfirmation:
    """Result of applying or replaying one process cancellation."""

    instance: WorkflowInstance
    canceled_node_keys: tuple[str, ...]
    revoked_claim_node_keys: tuple[str, ...]
    already_applied: bool = False


CANCELABLE_INSTANCE_STATUSES = {
    InstanceStatus.RUNNING,
    InstanceStatus.PAUSED,
}

ACTIVE_NODE_STATUSES = {
    NodeStatus.RUNNING,
    NodeStatus.WAITING_HUMAN,
}

CANCELABLE_NODE_STATUSES = {
    NodeStatus.PENDING,
    NodeStatus.READY,
    NodeStatus.RUNNING,
    NodeStatus.WAITING_HUMAN,
}

CANCELABLE_ATTEMPT_STATUSES = {
    AttemptStatus.PENDING,
    AttemptStatus.RUNNING,
    AttemptStatus.WAITING_HUMAN,
}


def preview_cancellation(instance: WorkflowInstance) -> CancellationPreview:
    """Return the stable cancel impact without mutating the aggregate."""

    if instance.status not in CANCELABLE_INSTANCE_STATUSES:
        raise CancellationNotAllowedError(
            f"instance cannot be canceled from {instance.status.value}"
        )
    affected = tuple(
        spec.key
        for spec in instance.snapshot.nodes
        if instance.nodes[spec.key].status in CANCELABLE_NODE_STATUSES
    )
    active = tuple(
        spec.key
        for spec in instance.snapshot.nodes
        if instance.nodes[spec.key].status in ACTIVE_NODE_STATUSES
    )
    return CancellationPreview(
        instance_id=instance.id,
        expected_instance_version=instance.version,
        affected_node_keys=affected,
        active_node_keys=active,
    )


def apply_cancellation(
    instance: WorkflowInstance,
    *,
    expected_instance_version: int,
    now: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Cancel unfinished nodes and revoke claims without rewriting history."""

    if instance.status not in CANCELABLE_INSTANCE_STATUSES:
        raise CancellationNotAllowedError(
            f"instance cannot be canceled from {instance.status.value}"
        )
    if instance.version != expected_instance_version:
        raise StaleCancellationError(
            "instance changed after the cancellation preview"
        )

    canceled: list[str] = []
    revoked: list[str] = []
    for spec in instance.snapshot.nodes:
        node = instance.nodes[spec.key]
        if node.status not in CANCELABLE_NODE_STATUSES:
            continue
        attempt = instance.current_attempt(spec.key)
        if (
            spec.executor != ExecutorKind.HUMAN
            and (attempt.claimed_by or attempt.claim_token or attempt.claim_expires_at)
        ):
            revoked.append(spec.key)
        transition_node(node, NodeStatus.CANCELED, now=now)
        if attempt.status in CANCELABLE_ATTEMPT_STATUSES:
            transition_attempt(attempt, AttemptStatus.CANCELED, now=now)
        attempt.claimed_by = None
        attempt.claim_token = None
        attempt.claim_expires_at = None
        canceled.append(spec.key)

    transition_instance(instance, InstanceStatus.CANCELED, now=now)
    return tuple(canceled), tuple(revoked)
