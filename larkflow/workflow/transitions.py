"""Explicit state transition guards for workflow aggregates."""
from __future__ import annotations

from datetime import datetime

from .model import (
    AttemptStatus,
    InstanceStatus,
    NodeAttempt,
    NodeInstance,
    NodeStatus,
    WorkflowInstance,
)


class TransitionError(RuntimeError):
    """A command does not apply to the aggregate's current state."""


INSTANCE_TRANSITIONS = {
    InstanceStatus.DRAFT: {InstanceStatus.RUNNING, InstanceStatus.DISCARDED},
    InstanceStatus.RUNNING: {
        InstanceStatus.PAUSED,
        InstanceStatus.DONE,
        InstanceStatus.FAILED,
        InstanceStatus.CANCELED,
    },
    InstanceStatus.PAUSED: {
        InstanceStatus.RUNNING,
        InstanceStatus.DONE,
        InstanceStatus.FAILED,
        InstanceStatus.CANCELED,
    },
}

NODE_TRANSITIONS = {
    NodeStatus.PENDING: {NodeStatus.READY, NodeStatus.CANCELED},
    NodeStatus.READY: {
        NodeStatus.RUNNING,
        NodeStatus.WAITING_HUMAN,
        NodeStatus.CANCELED,
    },
    NodeStatus.RUNNING: {
        NodeStatus.DONE,
        NodeStatus.FAILED,
        NodeStatus.CANCELED,
    },
    NodeStatus.WAITING_HUMAN: {
        NodeStatus.DONE,
        NodeStatus.FAILED,
        NodeStatus.CANCELED,
    },
}

ATTEMPT_TRANSITIONS = {
    AttemptStatus.PENDING: {
        AttemptStatus.RUNNING,
        AttemptStatus.WAITING_HUMAN,
        AttemptStatus.CANCELED,
    },
    AttemptStatus.RUNNING: {
        AttemptStatus.DONE,
        AttemptStatus.FAILED,
        AttemptStatus.CANCELED,
    },
    AttemptStatus.WAITING_HUMAN: {
        AttemptStatus.DONE,
        AttemptStatus.FAILED,
        AttemptStatus.CANCELED,
    },
}


def transition_instance(
    instance: WorkflowInstance,
    target: InstanceStatus,
    *,
    now: datetime,
) -> None:
    target = InstanceStatus(target)
    if target not in INSTANCE_TRANSITIONS.get(instance.status, set()):
        raise TransitionError(
            f"illegal instance transition: {instance.status.value} -> {target.value}"
        )
    instance.status = target
    if target == InstanceStatus.RUNNING and instance.confirmed_at is None:
        instance.confirmed_at = now
    if target in {InstanceStatus.DONE, InstanceStatus.FAILED, InstanceStatus.CANCELED}:
        instance.completed_at = now


def transition_node(node: NodeInstance, target: NodeStatus, *, now: datetime) -> None:
    target = NodeStatus(target)
    if target not in NODE_TRANSITIONS.get(node.status, set()):
        raise TransitionError(
            f"illegal node transition for {node.node_key}: "
            f"{node.status.value} -> {target.value}"
        )
    node.status = target
    node.version += 1
    if target == NodeStatus.READY:
        node.ready_at = now
    if target in {NodeStatus.RUNNING, NodeStatus.WAITING_HUMAN}:
        node.started_at = now
    if target in {NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.CANCELED}:
        node.completed_at = now


def transition_attempt(
    attempt: NodeAttempt,
    target: AttemptStatus,
    *,
    now: datetime,
) -> None:
    target = AttemptStatus(target)
    if target not in ATTEMPT_TRANSITIONS.get(attempt.status, set()):
        raise TransitionError(
            f"illegal attempt transition for {attempt.id}: "
            f"{attempt.status.value} -> {target.value}"
        )
    attempt.status = target
    if target in {AttemptStatus.RUNNING, AttemptStatus.WAITING_HUMAN}:
        attempt.started_at = now
    if target in {AttemptStatus.DONE, AttemptStatus.FAILED, AttemptStatus.CANCELED}:
        attempt.completed_at = now
