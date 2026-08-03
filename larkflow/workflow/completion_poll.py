"""Reconcile completed Feishu Tasks into the durable Target Inbox."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib

from .inbound import (
    TASK_COMPLETED_UPDATE,
    TaskCompletionSignal,
    TaskStateReader,
    WorkflowInboxStore,
)
from .model import NodeStatus
from .projection import FEISHU_TASK_KIND
from .repository import ProjectionStore, WorkflowRepository


TASK_POLL_SOURCE = "feishu_task_poll"
TASK_POLL_EVENT = "larkflow.task.completion_reconciled_v1"


@dataclass(frozen=True)
class CompletionPollReport:
    instances_scanned: int = 0
    nodes_scanned: int = 0
    tasks_read: int = 0
    pending: int = 0
    completions_observed: int = 0
    signals_appended: int = 0
    duplicates: int = 0
    missing_projections: int = 0
    failed: int = 0
    interrupted: bool = False
    errors: tuple[str, ...] = field(default_factory=tuple)


class TaskCompletionPoller:
    """Poll current Human projections and enqueue only observed completions."""

    def __init__(
        self,
        repository: WorkflowRepository,
        projections: ProjectionStore,
        inbox: WorkflowInboxStore,
        task_reader: TaskStateReader,
        *,
        tenant_id: str,
        batch_size: int = 100,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        if batch_size < 1:
            raise ValueError("completion poll batch_size must be positive")
        self.repository = repository
        self.projections = projections
        self.inbox = inbox
        self.task_reader = task_reader
        self.tenant_id = tenant_id
        self.batch_size = batch_size
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run_once(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> CompletionPollReport:
        should_stop = stop_requested or (lambda: False)
        totals = {
            "instances_scanned": 0,
            "nodes_scanned": 0,
            "tasks_read": 0,
            "pending": 0,
            "completions_observed": 0,
            "signals_appended": 0,
            "duplicates": 0,
            "missing_projections": 0,
            "failed": 0,
        }
        errors = []
        interrupted = False
        after_id = None
        while True:
            if should_stop():
                interrupted = True
                break
            instance_ids = self.repository.projection_instance_ids(
                self.tenant_id,
                after_id=after_id,
                limit=self.batch_size,
            )
            if not instance_ids:
                break
            for instance_id in instance_ids:
                after_id = instance_id
                if should_stop():
                    interrupted = True
                    break
                totals["instances_scanned"] += 1
                try:
                    instance = self.repository.get(self.tenant_id, instance_id)
                except Exception as exc:
                    totals["failed"] += 1
                    errors.append(
                        f"{instance_id}: {type(exc).__name__}: {exc}"
                    )
                    continue
                for node_key in sorted(instance.nodes):
                    node = instance.nodes[node_key]
                    if node.status != NodeStatus.WAITING_HUMAN:
                        continue
                    if should_stop():
                        interrupted = True
                        break
                    totals["nodes_scanned"] += 1
                    projection = self.projections.get_projection(
                        self.tenant_id,
                        node.id,
                        node.current_attempt_no,
                        FEISHU_TASK_KIND,
                    )
                    if projection is None or not projection.external_id:
                        totals["missing_projections"] += 1
                        continue
                    try:
                        task = self.task_reader.get_task(projection.external_id)
                        totals["tasks_read"] += 1
                        if task.guid != projection.external_id:
                            raise ValueError(
                                "Task read returned a different guid"
                            )
                        if task.status != "done" or not task.completed_at:
                            totals["pending"] += 1
                            continue
                        totals["completions_observed"] += 1
                        now = self.clock()
                        signal = TaskCompletionSignal(
                            id=_signal_id(
                                self.tenant_id,
                                projection.id,
                                projection.external_id,
                                task.completed_at,
                            ),
                            tenant_id=self.tenant_id,
                            task_guid=projection.external_id,
                            event_types=(TASK_COMPLETED_UPDATE,),
                            occurred_at=_completed_time(
                                task.completed_at,
                                fallback=now,
                            ),
                            received_at=now,
                            source=TASK_POLL_SOURCE,
                            event_type=TASK_POLL_EVENT,
                        )
                        if self.inbox.append_inbox(signal):
                            totals["signals_appended"] += 1
                        else:
                            totals["duplicates"] += 1
                    except Exception as exc:
                        totals["failed"] += 1
                        errors.append(
                            f"{instance_id}/{node_key}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                if interrupted:
                    break
            if interrupted or len(instance_ids) < self.batch_size:
                break
        return CompletionPollReport(
            **totals,
            interrupted=interrupted,
            errors=tuple(errors),
        )


def _signal_id(
    tenant_id: str,
    projection_id: str,
    task_guid: str,
    completed_at: str,
) -> str:
    identity = "\0".join(
        (tenant_id, projection_id, task_guid, completed_at)
    )
    return "task-poll-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _completed_time(value: str, *, fallback: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return fallback
