"""Transactional outbox consumer for external workflow projections."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Protocol
from uuid import uuid4

from .model import ExecutorKind, FrozenDict, NodeStatus, WorkflowInstance
from .repository import OutboxStore, ProjectionStore, WorkflowRepository


FEISHU_TASK_KIND = "feishu_task"
PROJECTION_EVENTS = {
    "node.projection_create_requested",
    "node.projection_sync_requested",
}


@dataclass(frozen=True)
class ProjectionRecord:
    id: str
    tenant_id: str
    instance_id: str
    node_instance_id: str
    attempt_no: int
    kind: str
    idempotency_key: str
    sync_version: int
    state: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime
    external_id: str | None = None
    external_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", FrozenDict(self.state))


@dataclass(frozen=True)
class ExternalTask:
    guid: str
    url: str | None = None


@dataclass(frozen=True)
class TaskProjectionRequest:
    tenant_id: str
    instance_id: str
    node_key: str
    node_instance_id: str
    attempt_no: int
    owner_person_id: str
    summary: str
    description: str
    idempotency_key: str


class TaskProjectionAdapter(Protocol):
    def create_task(self, request: TaskProjectionRequest) -> ExternalTask:
        ...

    def complete_task(self, task_guid: str) -> None:
        ...


@dataclass(frozen=True)
class ProjectionWorkerReport:
    claimed: int = 0
    published: int = 0
    tasks_created: int = 0
    tasks_completed: int = 0
    noops: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class WorkflowProjectionWorker:
    """Claim outbox rows, perform external I/O, then publish or retry."""

    def __init__(
        self,
        repository: WorkflowRepository,
        outbox: OutboxStore,
        projections: ProjectionStore,
        task_adapter: TaskProjectionAdapter,
        *,
        tenant_id: str,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if claim_limit < 1:
            raise ValueError("claim_limit must be positive")
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("retry delays are invalid")
        self.repository = repository
        self.outbox = outbox
        self.projections = projections
        self.task_adapter = task_adapter
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.id_factory = id_factory or (lambda: str(uuid4()))

    def run_once(self) -> ProjectionWorkerReport:
        now = self.clock()
        claims = self.outbox.claim_outbox(
            self.tenant_id,
            worker_id=self.worker_id,
            now=now,
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
            event_types=PROJECTION_EVENTS,
        )
        published = 0
        tasks_created = 0
        tasks_completed = 0
        noops = 0
        failed = 0
        errors = []
        for claim in claims:
            try:
                outcome = self._project(claim.event, now=now)
            except Exception as exc:
                failed += 1
                error = f"{claim.event.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.outbox.mark_outbox_failed(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=now + self._retry_delay(claim.attempt_count),
                )
                continue
            self.outbox.mark_outbox_published(
                self.tenant_id,
                claim.event.id,
                claim_token=claim.claim_token,
                now=now,
            )
            published += 1
            tasks_created += int(outcome.created)
            tasks_completed += int(outcome.completed)
            noops += int(outcome.noop)
        return ProjectionWorkerReport(
            claimed=len(claims),
            published=published,
            tasks_created=tasks_created,
            tasks_completed=tasks_completed,
            noops=noops,
            failed=failed,
            errors=tuple(errors),
        )

    def _project(self, event: Any, *, now: datetime) -> _ProjectionOutcome:
        if event.aggregate_type != "node_instance" or event.event_type not in PROJECTION_EVENTS:
            raise ValueError(f"unsupported projection event: {event.event_type}")
        instance_id = _text(event.payload.get("instance_id"), "instance_id")
        node_key = _text(event.payload.get("node_key"), "node_key")
        attempt_no = _positive_int(event.payload.get("attempt_no"), "attempt_no")
        instance = self.repository.get(self.tenant_id, instance_id)
        node = instance.nodes[node_key]
        if node.id != event.aggregate_id:
            raise ValueError("projection event aggregate does not match the current node")
        if node.executor != ExecutorKind.HUMAN:
            return _ProjectionOutcome(noop=True)
        if node.status in {NodeStatus.PENDING, NodeStatus.READY}:
            return _ProjectionOutcome(noop=True)
        instance.attempts[(node_key, attempt_no)]

        record = self.projections.get_projection(
            self.tenant_id,
            node.id,
            attempt_no,
            FEISHU_TASK_KIND,
        )
        created = False
        if record is None:
            request = self._task_request(instance, node_key, attempt_no)
            external = self.task_adapter.create_task(request)
            if not external.guid.strip():
                raise ValueError("Feishu task create returned an empty guid")
            record = ProjectionRecord(
                id=self.id_factory(),
                tenant_id=self.tenant_id,
                instance_id=instance.id,
                node_instance_id=node.id,
                attempt_no=attempt_no,
                kind=FEISHU_TASK_KIND,
                external_id=external.guid,
                external_url=external.url,
                idempotency_key=request.idempotency_key,
                sync_version=node.version,
                state={"node_status": node.status.value, "completed": False},
                created_at=now,
                updated_at=now,
            )
            self.projections.save_projection(record)
            created = True

        if not record.external_id:
            raise ValueError("Feishu task projection has no external id")
        terminal = node.status in {
            NodeStatus.DONE,
            NodeStatus.FAILED,
            NodeStatus.CANCELED,
        }
        completed = False
        if terminal and not bool(record.state.get("completed")):
            self.task_adapter.complete_task(record.external_id)
            completed = True
        updated = ProjectionRecord(
            id=record.id,
            tenant_id=record.tenant_id,
            instance_id=record.instance_id,
            node_instance_id=record.node_instance_id,
            attempt_no=record.attempt_no,
            kind=record.kind,
            external_id=record.external_id,
            external_url=record.external_url,
            idempotency_key=record.idempotency_key,
            sync_version=max(record.sync_version, node.version),
            state={"node_status": node.status.value, "completed": terminal},
            created_at=record.created_at,
            updated_at=now,
        )
        self.projections.save_projection(updated)
        return _ProjectionOutcome(created=created, completed=completed)

    def _task_request(
        self,
        instance: WorkflowInstance,
        node_key: str,
        attempt_no: int,
    ) -> TaskProjectionRequest:
        node = instance.nodes[node_key]
        spec = instance.snapshot.node(node_key)
        identity = (
            f"{self.tenant_id}:{node.id}:{attempt_no}:{FEISHU_TASK_KIND}"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48]
        acceptance = "\n".join(
            f"- {item}" for item in spec.work.get("acceptance", ())
        )
        description = (
            f"目标：{spec.work.get('objective', '')}\n\n"
            f"验收条件：\n{acceptance}\n\n"
            f"流程：{instance.id}\n节点：{node_key}\nAttempt：{attempt_no}"
        )
        return TaskProjectionRequest(
            tenant_id=self.tenant_id,
            instance_id=instance.id,
            node_key=node_key,
            node_instance_id=node.id,
            attempt_no=attempt_no,
            owner_person_id=node.owner_person_id,
            summary=spec.title,
            description=description,
            idempotency_key=f"lf-{digest}",
        )

    def _retry_delay(self, attempt_count: int) -> timedelta:
        multiplier = 2 ** min(max(attempt_count - 1, 0), 10)
        return min(self.retry_max, self.retry_base * multiplier)


@dataclass(frozen=True)
class _ProjectionOutcome:
    created: bool = False
    completed: bool = False
    noop: bool = False


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"projection event requires {field_name}")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"projection event requires positive {field_name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"projection event requires positive {field_name}") from exc
    if parsed < 1:
        raise ValueError(f"projection event requires positive {field_name}")
    return parsed
