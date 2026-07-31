"""DAG scheduling rules that mutate only the in-memory aggregate."""
from __future__ import annotations

from datetime import datetime

from .graph import direct_successors, ready_node_keys, validate_snapshot
from .model import (
    AttemptStatus,
    FrozenDict,
    InstanceStatus,
    NodeAttempt,
    NodeInstance,
    NodeStatus,
    WorkflowInstance,
)
from .transitions import transition_instance, transition_node


class Scheduler:
    def confirm(self, instance: WorkflowInstance, *, now: datetime) -> None:
        validate_snapshot(instance.snapshot)
        transition_instance(instance, InstanceStatus.RUNNING, now=now)

        for spec in instance.snapshot.nodes:
            node_id = f"{instance.id}:{spec.key}"
            is_root = not spec.deps
            instance.nodes[spec.key] = NodeInstance(
                id=node_id,
                instance_id=instance.id,
                node_key=spec.key,
                owner_person_id=spec.owner_person_id,
                executor=spec.executor,
                status=NodeStatus.READY if is_root else NodeStatus.PENDING,
                ready_at=now if is_root else None,
            )
            instance.attempts[(spec.key, 1)] = NodeAttempt(
                id=f"{node_id}:attempt:1",
                node_instance_id=node_id,
                attempt_no=1,
                status=AttemptStatus.PENDING,
                input_snapshot=FrozenDict({"deps": spec.deps, "work": spec.work}),
            )

    def unlock_after(self, instance: WorkflowInstance, node_key: str, *, now: datetime) -> None:
        statuses = {key: node.status for key, node in instance.nodes.items()}
        ready = set(ready_node_keys(instance.snapshot, statuses))
        for successor in direct_successors(instance.snapshot, node_key):
            if successor in ready:
                transition_node(instance.nodes[successor], NodeStatus.READY, now=now)

        if instance.nodes and all(
            node.status == NodeStatus.DONE for node in instance.nodes.values()
        ):
            transition_instance(instance, InstanceStatus.DONE, now=now)

    def fail_instance(self, instance: WorkflowInstance, *, now: datetime) -> None:
        transition_instance(instance, InstanceStatus.FAILED, now=now)
