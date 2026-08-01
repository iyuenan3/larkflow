"""Durable Feishu Task completion intake for Target Human nodes."""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import secrets
from threading import Lock
from typing import Any, Protocol

from .model import ExecutorKind, NodeStatus
from .projection import FEISHU_TASK_KIND, ProjectionRecord
from .repository import ConcurrentUpdateError, WorkflowRepository
from .service import WorkflowService


TASK_UPDATE_EVENT = "task.task.update_user_access_v2"
TASK_COMPLETED_UPDATE = "task_completed_update"


class InboxStatus(str, Enum):
    PENDING = "pending"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class InvalidInboxClaimError(RuntimeError):
    pass


class RetryableInboundError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskCompletionSignal:
    id: str
    tenant_id: str
    task_guid: str
    event_types: tuple[str, ...]
    occurred_at: datetime
    received_at: datetime


@dataclass
class InboxRecord:
    event: TaskCompletionSignal
    status: InboxStatus = InboxStatus.PENDING
    attempt_count: int = 0
    available_at: datetime | None = None
    claimed_by: str | None = None
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    processed_at: datetime | None = None
    outcome: str | None = None
    last_error: str | None = None
    failure_stage: str | None = None
    task_state: ExternalTaskState | None = None

    def __post_init__(self) -> None:
        if self.available_at is None:
            self.available_at = self.event.received_at


@dataclass(frozen=True)
class InboxClaim:
    event: TaskCompletionSignal
    claim_token: str
    claimed_by: str
    claim_expires_at: datetime
    attempt_count: int
    task_state: ExternalTaskState | None = None


@dataclass(frozen=True)
class ExternalTaskState:
    guid: str
    status: str
    mode: int | None
    completed_at: str | None
    source: int | None
    extra: str | None
    assignee_ids: tuple[str, ...]
    completed_assignee_ids: tuple[str, ...]


def task_state_to_dict(state: ExternalTaskState) -> dict[str, Any]:
    return {
        "guid": state.guid,
        "status": state.status,
        "mode": state.mode,
        "completed_at": state.completed_at,
        "source": state.source,
        "extra": state.extra,
        "assignee_ids": list(state.assignee_ids),
        "completed_assignee_ids": list(state.completed_assignee_ids),
    }


def task_state_from_dict(data: Mapping[str, Any]) -> ExternalTaskState:
    return ExternalTaskState(
        guid=str(data.get("guid") or ""),
        status=str(data.get("status") or ""),
        mode=_optional_int(data.get("mode")),
        completed_at=_optional_text(data.get("completed_at")),
        source=_optional_int(data.get("source")),
        extra=_optional_text(data.get("extra")),
        assignee_ids=tuple(str(item) for item in data.get("assignee_ids") or ()),
        completed_assignee_ids=tuple(
            str(item) for item in data.get("completed_assignee_ids") or ()
        ),
    )


class TaskStateReader(Protocol):
    def get_task(self, task_guid: str) -> ExternalTaskState:
        ...


class ProjectionLookup(Protocol):
    def get_projection_by_external_id(
        self,
        tenant_id: str,
        kind: str,
        external_id: str,
    ) -> ProjectionRecord | None:
        ...


class WorkflowInboxStore(Protocol):
    def append_inbox(self, event: TaskCompletionSignal) -> bool:
        ...

    def claim_inbox(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
    ) -> tuple[InboxClaim, ...]:
        ...

    def claim_inbox_verification(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
    ) -> tuple[InboxClaim, ...]:
        ...

    def mark_inbox_verified(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        task_state: ExternalTaskState,
        now: datetime,
    ) -> None:
        ...

    def mark_inbox_verification_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...

    def mark_inbox_processed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        outcome: str,
        now: datetime,
    ) -> None:
        ...

    def mark_inbox_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...


class InMemoryWorkflowInbox:
    """Thread-safe inbox used by contract tests."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], InboxRecord] = {}
        self._lock = Lock()

    def append_inbox(self, event: TaskCompletionSignal) -> bool:
        key = (event.tenant_id, event.id)
        with self._lock:
            if key in self._records:
                return False
            self._records[key] = InboxRecord(event=event)
            return True

    def claim_inbox(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
    ) -> tuple[InboxClaim, ...]:
        return self._claim(
            tenant_id,
            worker_id=worker_id,
            now=now,
            limit=limit,
            claim_ttl=claim_ttl,
            status=InboxStatus.PROCESSING,
            predicate=_processable,
        )

    def claim_inbox_verification(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
    ) -> tuple[InboxClaim, ...]:
        return self._claim(
            tenant_id,
            worker_id=worker_id,
            now=now,
            limit=limit,
            claim_ttl=claim_ttl,
            status=InboxStatus.VERIFYING,
            predicate=_verifiable,
        )

    def _claim(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
        status: InboxStatus,
        predicate: Callable[[InboxRecord, datetime], bool],
    ) -> tuple[InboxClaim, ...]:
        _validate_claim(worker_id, limit, claim_ttl)
        with self._lock:
            candidates = sorted(
                (
                    record
                    for (record_tenant, _), record in self._records.items()
                    if record_tenant == tenant_id and predicate(record, now)
                ),
                key=lambda record: (
                    record.available_at or record.event.received_at,
                    record.event.received_at,
                    record.event.id,
                ),
            )[:limit]
            claims = []
            for record in candidates:
                token = secrets.token_urlsafe(24)
                expires_at = now + claim_ttl
                record.status = status
                record.attempt_count += 1
                record.claimed_by = worker_id
                record.claim_token = token
                record.claim_expires_at = expires_at
                claims.append(
                    InboxClaim(
                        event=record.event,
                        claim_token=token,
                        claimed_by=worker_id,
                        claim_expires_at=expires_at,
                        attempt_count=record.attempt_count,
                        task_state=record.task_state,
                    )
                )
            return tuple(claims)

    def mark_inbox_verified(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        task_state: ExternalTaskState,
        now: datetime,
    ) -> None:
        with self._lock:
            record = self._claimed(
                tenant_id,
                event_id,
                claim_token,
                expected=InboxStatus.VERIFYING,
            )
            record.status = InboxStatus.VERIFIED
            record.available_at = now
            record.task_state = task_state
            record.claimed_by = None
            record.claim_token = None
            record.claim_expires_at = None
            record.last_error = None
            record.failure_stage = None

    def mark_inbox_verification_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        with self._lock:
            record = self._claimed(
                tenant_id,
                event_id,
                claim_token,
                expected=InboxStatus.VERIFYING,
            )
            record.status = InboxStatus.FAILED
            record.available_at = retry_at
            record.claimed_by = None
            record.claim_token = None
            record.claim_expires_at = None
            record.last_error = error
            record.failure_stage = "verification"

    def mark_inbox_processed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        outcome: str,
        now: datetime,
    ) -> None:
        with self._lock:
            record = self._claimed(
                tenant_id,
                event_id,
                claim_token,
                expected=InboxStatus.PROCESSING,
            )
            record.status = InboxStatus.PROCESSED
            record.processed_at = now
            record.outcome = outcome
            record.claimed_by = None
            record.claim_token = None
            record.claim_expires_at = None
            record.last_error = None
            record.failure_stage = None

    def mark_inbox_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        with self._lock:
            record = self._claimed(
                tenant_id,
                event_id,
                claim_token,
                expected=InboxStatus.PROCESSING,
            )
            record.status = InboxStatus.FAILED
            record.available_at = retry_at
            record.claimed_by = None
            record.claim_token = None
            record.claim_expires_at = None
            record.last_error = error
            record.failure_stage = "processing"

    def records(self, tenant_id: str) -> tuple[InboxRecord, ...]:
        with self._lock:
            return tuple(
                record
                for (record_tenant, _), record in self._records.items()
                if record_tenant == tenant_id
            )

    def _claimed(
        self,
        tenant_id: str,
        event_id: str,
        claim_token: str,
        *,
        expected: InboxStatus,
    ) -> InboxRecord:
        record = self._records.get((tenant_id, event_id))
        if (
            record is None
            or record.status != expected
            or record.claim_token is None
            or not secrets.compare_digest(record.claim_token, claim_token)
        ):
            raise InvalidInboxClaimError(event_id)
        return record


class TaskEventInboxBridge:
    """Copy the authenticated event envelope into PostgreSQL without domain writes."""

    def __init__(
        self,
        inbox: WorkflowInboxStore,
        *,
        tenant_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        self.inbox = inbox
        self.tenant_id = tenant_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, key: str, payload: Mapping[str, Any]) -> bool:
        if key != TASK_UPDATE_EVENT:
            return False
        header = payload.get("header")
        body = payload.get("event")
        if not isinstance(header, Mapping) or not isinstance(body, Mapping):
            raise ValueError("Task event requires V2 header and event objects")
        event_id = _required_text(header.get("event_id"), "event_id")
        task_guid = _required_text(body.get("task_guid"), "task_guid")
        raw_types = body.get("event_types")
        if not isinstance(raw_types, Collection) or isinstance(raw_types, (str, bytes)):
            raise ValueError("Task event requires event_types")
        event_types = tuple(
            sorted({_required_text(item, "event_type") for item in raw_types})
        )
        if TASK_COMPLETED_UPDATE not in event_types:
            return False
        now = self.clock()
        event = TaskCompletionSignal(
            id=event_id,
            tenant_id=self.tenant_id,
            task_guid=task_guid,
            event_types=event_types,
            occurred_at=_event_time(header.get("create_time"), fallback=now),
            received_at=now,
        )
        return self.inbox.append_inbox(event)


@dataclass(frozen=True)
class VerificationWorkerReport:
    claimed: int = 0
    verified: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class TaskVerificationWorker:
    """Read Task state using the credential-owning adapter identity."""

    def __init__(
        self,
        inbox: WorkflowInboxStore,
        task_reader: TaskStateReader,
        *,
        tenant_id: str,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
    ) -> None:
        _validate_claim(worker_id, claim_limit, claim_ttl)
        if not tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("verification retry delays are invalid")
        self.inbox = inbox
        self.task_reader = task_reader
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max

    def run_once(self) -> VerificationWorkerReport:
        now = self.clock()
        claims = self.inbox.claim_inbox_verification(
            self.tenant_id,
            worker_id=self.worker_id,
            now=now,
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
        )
        verified = 0
        failed = 0
        errors = []
        for claim in claims:
            try:
                task_state = self.task_reader.get_task(claim.event.task_guid)
                if task_state.status != "done" or not task_state.completed_at:
                    raise RetryableInboundError(
                        "Task completion is not visible yet"
                    )
                if task_state.mode == 1 and not task_state.completed_assignee_ids:
                    raise RetryableInboundError(
                        "Task assignee completion is not visible yet"
                    )
            except Exception as exc:
                failed += 1
                error = f"{claim.event.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.inbox.mark_inbox_verification_failed(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=now + self._retry_delay(claim.attempt_count),
                )
                continue
            self.inbox.mark_inbox_verified(
                self.tenant_id,
                claim.event.id,
                claim_token=claim.claim_token,
                task_state=task_state,
                now=now,
            )
            verified += 1
        return VerificationWorkerReport(
            claimed=len(claims),
            verified=verified,
            failed=failed,
            errors=tuple(errors),
        )

    def _retry_delay(self, attempt_count: int) -> timedelta:
        multiplier = 2 ** min(max(attempt_count - 1, 0), 10)
        return min(self.retry_max, self.retry_base * multiplier)


@dataclass(frozen=True)
class InboundWorkerReport:
    claimed: int = 0
    submitted: int = 0
    noops: int = 0
    rejected: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class WorkflowInboundWorker:
    """Verify durable completion signals and submit current Human attempts."""

    def __init__(
        self,
        service: WorkflowService,
        repository: WorkflowRepository,
        projections: ProjectionLookup,
        inbox: WorkflowInboxStore,
        *,
        tenant_id: str,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
    ) -> None:
        _validate_claim(worker_id, claim_limit, claim_ttl)
        if not tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("inbound retry delays are invalid")
        self.service = service
        self.repository = repository
        self.projections = projections
        self.inbox = inbox
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max

    def run_once(self) -> InboundWorkerReport:
        now = self.clock()
        claims = self.inbox.claim_inbox(
            self.tenant_id,
            worker_id=self.worker_id,
            now=now,
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
        )
        totals = {"submitted": 0, "noops": 0, "rejected": 0, "failed": 0}
        errors = []
        for claim in claims:
            try:
                if claim.task_state is None:
                    raise ValueError("verified Inbox event has no Task state")
                outcome = self._process(claim.event, claim.task_state)
            except Exception as exc:
                totals["failed"] += 1
                error = f"{claim.event.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.inbox.mark_inbox_failed(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=now + self._retry_delay(claim.attempt_count),
                )
                continue
            category = outcome.split(":", 1)[0]
            totals[category] += 1
            self.inbox.mark_inbox_processed(
                self.tenant_id,
                claim.event.id,
                claim_token=claim.claim_token,
                outcome=outcome,
                now=now,
            )
        return InboundWorkerReport(
            claimed=len(claims),
            errors=tuple(errors),
            **totals,
        )

    def _process(
        self,
        event: TaskCompletionSignal,
        task: ExternalTaskState,
    ) -> str:
        projection = self.projections.get_projection_by_external_id(
            self.tenant_id,
            FEISHU_TASK_KIND,
            event.task_guid,
        )
        if projection is None:
            return "noops:unbound_task"
        instance = self.repository.get(self.tenant_id, projection.instance_id)
        nodes = [
            node
            for node in instance.nodes.values()
            if node.id == projection.node_instance_id
        ]
        if len(nodes) != 1:
            return "rejected:projection_node_mismatch"
        node = nodes[0]
        if (
            node.current_attempt_no != projection.attempt_no
            or instance.current_attempt(node.node_key).attempt_no != projection.attempt_no
        ):
            return "noops:stale_attempt"
        if node.status in {NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.CANCELED}:
            return "noops:already_terminal"
        if node.executor != ExecutorKind.HUMAN or node.status != NodeStatus.WAITING_HUMAN:
            return "rejected:node_not_waiting_human"

        rejected = self._validate_task(task, projection, node.owner_person_id)
        if rejected is not None:
            return f"rejected:{rejected}"
        try:
            self.service.submit_human(
                self.tenant_id,
                instance.id,
                node.node_key,
                actor_person_id=node.owner_person_id,
                attempt_no=projection.attempt_no,
                expected_node_version=node.version,
                result={"confirmed": True},
                correlation_id=event.id,
            )
        except ConcurrentUpdateError:
            current = self.repository.get(self.tenant_id, instance.id)
            if current.nodes[node.node_key].status in {
                NodeStatus.DONE,
                NodeStatus.FAILED,
                NodeStatus.CANCELED,
            }:
                return "noops:concurrent_terminal"
            raise
        return "submitted:human_node"

    @staticmethod
    def _validate_task(
        task: ExternalTaskState,
        projection: ProjectionRecord,
        owner_person_id: str,
    ) -> str | None:
        if task.guid != projection.external_id:
            return "task_guid_mismatch"
        if task.extra != projection.idempotency_key:
            return "task_binding_mismatch"
        if task.source != 6:
            return "task_source_mismatch"
        if task.mode != 1:
            return "task_not_all_assignees_mode"
        if task.assignee_ids != (owner_person_id,):
            return "task_owner_mismatch"
        if task.status != "done" or not task.completed_at:
            raise RetryableInboundError("Task completion is not visible yet")
        if task.completed_assignee_ids == ():
            raise RetryableInboundError("Task assignee completion is not visible yet")
        if task.completed_assignee_ids != (owner_person_id,):
            return "task_completed_by_non_owner"
        return None

    def _retry_delay(self, attempt_count: int) -> timedelta:
        multiplier = 2 ** min(max(attempt_count - 1, 0), 10)
        return min(self.retry_max, self.retry_base * multiplier)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Task event requires {field_name}")
    return value


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_time(value: Any, *, fallback: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return fallback


def _validate_claim(worker_id: str, limit: int, claim_ttl: timedelta) -> None:
    if not worker_id.strip():
        raise ValueError("inbound worker_id is required")
    if limit < 1:
        raise ValueError("inbound claim_limit must be positive")
    if claim_ttl <= timedelta(0):
        raise ValueError("inbound claim_ttl must be positive")


def _verifiable(record: InboxRecord, now: datetime) -> bool:
    if record.status == InboxStatus.PENDING:
        return bool(record.available_at and record.available_at <= now)
    if record.status == InboxStatus.FAILED and record.failure_stage == "verification":
        return bool(record.available_at and record.available_at <= now)
    return bool(
        record.status == InboxStatus.VERIFYING
        and record.claim_expires_at
        and record.claim_expires_at <= now
    )


def _processable(record: InboxRecord, now: datetime) -> bool:
    if record.status == InboxStatus.VERIFIED:
        return bool(record.available_at and record.available_at <= now)
    if record.status == InboxStatus.FAILED and record.failure_stage == "processing":
        return bool(record.available_at and record.available_at <= now)
    return bool(
        record.status == InboxStatus.PROCESSING
        and record.claim_expires_at
        and record.claim_expires_at <= now
    )
