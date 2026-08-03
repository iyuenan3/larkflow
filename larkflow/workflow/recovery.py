"""Explicit recovery rules for failed automated node Attempts."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from .model import (
    AttemptStatus,
    ExecutorKind,
    FrozenDict,
    InstanceStatus,
    NodeAttempt,
    NodeStatus,
    WorkflowInstance,
)
from .restart import (
    RestartNotAllowedError,
    RestartPreview,
    RestartScope,
    affected_restart_node_keys,
    apply_restart,
)
from .runner import AuthorizationError


RECOVERY_ACTION_NAME = "workflow_recovery"


class RecoveryAction(str, Enum):
    RETRY = "retry"
    HUMAN_TAKEOVER = "human_takeover"


class RecoveryNotAllowedError(RuntimeError):
    """The current aggregate is not an eligible automated failure."""


class StaleRecoveryError(RecoveryNotAllowedError):
    """A recovery action targets an older aggregate or Attempt version."""


def apply_failed_node_recovery(
    instance: WorkflowInstance,
    node_key: str,
    action: RecoveryAction,
    *,
    actor_person_id: str,
    expected_instance_version: int,
    expected_node_version: int,
    expected_attempt_no: int,
    now: datetime,
) -> tuple[str, ...]:
    """Apply one server-authorized recovery without rewriting old Attempts."""

    action = RecoveryAction(action)
    node = _validate_recovery_target(
        instance,
        node_key,
        actor_person_id=actor_person_id,
        expected_instance_version=expected_instance_version,
        expected_node_version=expected_node_version,
        expected_attempt_no=expected_attempt_no,
    )
    try:
        affected = affected_restart_node_keys(instance, node_key)
    except RestartNotAllowedError as exc:
        raise RecoveryNotAllowedError(str(exc)) from exc
    if action == RecoveryAction.RETRY:
        preview = RestartPreview(
            id=f"recovery:{instance.id}:{node_key}:{expected_attempt_no}",
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            actor_person_id=actor_person_id,
            node_key=node_key,
            affected_node_keys=affected,
            expected_instance_version=instance.version,
            graph_revision=instance.graph_revision,
            created_at=now,
            expires_at=now,
            scope=RestartScope.NODE,
        )
        apply_restart(instance, preview, now=now)
        return affected

    spec = instance.snapshot.node(node_key)
    next_attempt_no = node.current_attempt_no + 1
    node.current_attempt_no = next_attempt_no
    node.version += 1
    node.status = NodeStatus.WAITING_HUMAN
    node.ready_at = None
    node.started_at = now
    node.completed_at = None
    instance.attempts[(node_key, next_attempt_no)] = NodeAttempt(
        id=f"{node.id}:attempt:{next_attempt_no}",
        node_instance_id=node.id,
        attempt_no=next_attempt_no,
        status=AttemptStatus.WAITING_HUMAN,
        input_snapshot=FrozenDict(
            {
                "instance_inputs": instance.snapshot.inputs,
                "dependencies": {
                    dependency: instance.current_attempt(dependency).result
                    for dependency in spec.deps
                },
                "work": spec.work,
            }
        ),
        started_at=now,
    )
    instance.status = InstanceStatus.RUNNING
    instance.completed_at = None
    return (node_key,)


def _validate_recovery_target(
    instance: WorkflowInstance,
    node_key: str,
    *,
    actor_person_id: str,
    expected_instance_version: int,
    expected_node_version: int,
    expected_attempt_no: int,
):
    if instance.version != expected_instance_version:
        raise StaleRecoveryError("instance changed after the failure card was sent")
    if instance.status != InstanceStatus.FAILED:
        raise RecoveryNotAllowedError("instance is not failed")
    try:
        node = instance.nodes[node_key]
        spec = instance.snapshot.node(node_key)
    except KeyError as exc:
        raise RecoveryNotAllowedError(f"unknown recovery node: {node_key}") from exc
    if node.owner_person_id != actor_person_id:
        raise AuthorizationError("only the failed node owner may recover it")
    if node.version != expected_node_version:
        raise StaleRecoveryError("node changed after the failure card was sent")
    if node.current_attempt_no != expected_attempt_no:
        raise StaleRecoveryError("node is now on another Attempt")
    if spec.executor == ExecutorKind.HUMAN or node.executor == ExecutorKind.HUMAN:
        raise RecoveryNotAllowedError("human nodes do not use automated recovery")
    attempt = instance.current_attempt(node_key)
    if node.status != NodeStatus.FAILED or attempt.status != AttemptStatus.FAILED:
        raise RecoveryNotAllowedError("node has no failed automated Attempt")
    if attempt.submitted_by_person_id is not None:
        raise RecoveryNotAllowedError("human-submitted Attempts cannot use this recovery")
    return node


__all__ = [
    "RECOVERY_ACTION_NAME",
    "RecoveryAction",
    "RecoveryNotAllowedError",
    "StaleRecoveryError",
    "apply_failed_node_recovery",
]
