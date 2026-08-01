"""Persistence port and deterministic in-memory implementation."""
from __future__ import annotations

from collections.abc import Collection
from copy import deepcopy
from datetime import datetime, timedelta
import secrets
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .projection import ProjectionRecord

from .events import (
    AuditEvent,
    InvalidOutboxClaimError,
    OutboxClaim,
    OutboxEvent,
    OutboxRecord,
    OutboxStatus,
)
from .model import WorkflowInstance


class InstanceNotFoundError(KeyError):
    pass


class InstanceAlreadyExistsError(RuntimeError):
    pass


class ConcurrentUpdateError(RuntimeError):
    pass


class WorkflowRepository(Protocol):
    def add(
        self,
        instance: WorkflowInstance,
        *,
        audit_events: tuple[AuditEvent, ...] = (),
        outbox_events: tuple[OutboxEvent, ...] = (),
    ) -> None:
        ...

    def runnable_instance_ids(
        self,
        tenant_id: str,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[str, ...]:
        ...

    def get(self, tenant_id: str, instance_id: str) -> WorkflowInstance:
        ...

    def save(
        self,
        instance: WorkflowInstance,
        *,
        expected_version: int,
        audit_events: tuple[AuditEvent, ...] = (),
        outbox_events: tuple[OutboxEvent, ...] = (),
    ) -> None:
        ...


class OutboxStore(Protocol):
    def claim_outbox(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 100,
        claim_ttl: timedelta = timedelta(minutes=5),
        event_types: Collection[str] | None = None,
    ) -> tuple[OutboxClaim, ...]:
        ...

    def mark_outbox_published(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        now: datetime,
    ) -> None:
        ...

    def mark_outbox_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...


class ProjectionStore(Protocol):
    def get_projection(
        self,
        tenant_id: str,
        node_instance_id: str,
        attempt_no: int,
        kind: str,
    ) -> ProjectionRecord | None:
        ...

    def save_projection(self, projection: ProjectionRecord) -> None:
        ...


class InMemoryWorkflowRepository:
    """Copy-on-read repository that exercises optimistic concurrency in tests."""

    def __init__(self) -> None:
        self._instances: dict[tuple[str, str], WorkflowInstance] = {}
        self._audit_events: list[AuditEvent] = []
        self._audit_ids: set[tuple[str, str]] = set()
        self._outbox: dict[tuple[str, str], OutboxRecord] = {}
        self._outbox_dedupe: set[tuple[str, tuple[str, str, str, int]]] = set()
        self._projections: dict[tuple[str, str, int, str], ProjectionRecord] = {}

    def add(
        self,
        instance: WorkflowInstance,
        *,
        audit_events: tuple[AuditEvent, ...] = (),
        outbox_events: tuple[OutboxEvent, ...] = (),
    ) -> None:
        key = (instance.tenant_id, instance.id)
        if key in self._instances:
            raise InstanceAlreadyExistsError(instance.id)
        self._validate_events(instance, audit_events, outbox_events)
        self._instances[key] = deepcopy(instance)
        self._append_events(audit_events, outbox_events)

    def runnable_instance_ids(
        self,
        tenant_id: str,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        candidates: list[tuple[datetime, str]] = []
        for (instance_tenant, instance_id), instance in self._instances.items():
            if instance_tenant != tenant_id or instance.status.value != "running":
                continue
            due_times: list[datetime] = []
            for node_key, node in instance.nodes.items():
                if node.status.value == "ready" and node.ready_at is not None:
                    due_times.append(node.ready_at)
                    continue
                if node.executor.value == "human" or node.status.value != "running":
                    continue
                attempt = instance.current_attempt(node_key)
                if (
                    attempt.status.value == "running"
                    and attempt.claim_expires_at is not None
                    and attempt.claim_expires_at <= now
                ):
                    due_times.append(attempt.claim_expires_at)
            if due_times:
                candidates.append((min(due_times), instance_id))
        candidates.sort()
        return tuple(instance_id for _, instance_id in candidates[:limit])

    def get(self, tenant_id: str, instance_id: str) -> WorkflowInstance:
        try:
            return deepcopy(self._instances[(tenant_id, instance_id)])
        except KeyError as exc:
            raise InstanceNotFoundError((tenant_id, instance_id)) from exc

    def save(
        self,
        instance: WorkflowInstance,
        *,
        expected_version: int,
        audit_events: tuple[AuditEvent, ...] = (),
        outbox_events: tuple[OutboxEvent, ...] = (),
    ) -> None:
        key = (instance.tenant_id, instance.id)
        current = self._instances.get(key)
        if current is None:
            raise InstanceNotFoundError(key)
        if current.version != expected_version:
            raise ConcurrentUpdateError(
                f"instance {instance.id} expected version {expected_version}, "
                f"found {current.version}"
            )
        self._validate_events(instance, audit_events, outbox_events)
        instance.version = expected_version + 1
        self._instances[key] = deepcopy(instance)
        self._append_events(audit_events, outbox_events)

    def audit_log(self, tenant_id: str, instance_id: str) -> tuple[AuditEvent, ...]:
        return tuple(
            event
            for event in self._audit_events
            if event.tenant_id == tenant_id and event.instance_id == instance_id
        )

    def outbox_records(self, tenant_id: str) -> tuple[OutboxRecord, ...]:
        return tuple(
            deepcopy(record)
            for (record_tenant, _), record in self._outbox.items()
            if record_tenant == tenant_id
        )

    def get_projection(
        self,
        tenant_id: str,
        node_instance_id: str,
        attempt_no: int,
        kind: str,
    ) -> ProjectionRecord | None:
        record = self._projections.get(
            (tenant_id, node_instance_id, attempt_no, kind)
        )
        return deepcopy(record) if record is not None else None

    def get_projection_by_external_id(
        self,
        tenant_id: str,
        kind: str,
        external_id: str,
    ) -> ProjectionRecord | None:
        matches = [
            record
            for (record_tenant, _, _, record_kind), record in self._projections.items()
            if record_tenant == tenant_id
            and record_kind == kind
            and record.external_id == external_id
        ]
        if len(matches) > 1:
            raise ValueError("projection external identity is not unique")
        return deepcopy(matches[0]) if matches else None

    def save_projection(self, projection: ProjectionRecord) -> None:
        key = (
            projection.tenant_id,
            projection.node_instance_id,
            projection.attempt_no,
            projection.kind,
        )
        existing = self._projections.get(key)
        if existing is not None:
            if existing.idempotency_key != projection.idempotency_key:
                raise ValueError("projection idempotency key cannot change")
            if (
                existing.external_id is not None
                and existing.external_id != projection.external_id
            ):
                raise ValueError("projection external id cannot change")
            if projection.sync_version < existing.sync_version:
                raise ValueError("projection sync version cannot move backwards")
        self._projections[key] = deepcopy(projection)

    def projection_records(self, tenant_id: str) -> tuple[ProjectionRecord, ...]:
        return tuple(
            deepcopy(record)
            for (record_tenant, _, _, _), record in self._projections.items()
            if record_tenant == tenant_id
        )

    def claim_outbox(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 100,
        claim_ttl: timedelta = timedelta(minutes=5),
        event_types: Collection[str] | None = None,
    ) -> tuple[OutboxClaim, ...]:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if limit < 1:
            raise ValueError("limit must be positive")
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        accepted_types = set(event_types) if event_types is not None else None
        if accepted_types == set():
            return ()
        candidates = sorted(
            (
                record
                for (record_tenant, _), record in self._outbox.items()
                if record_tenant == tenant_id
                and self._claimable(record, now)
                and (
                    accepted_types is None
                    or record.event.event_type in accepted_types
                )
            ),
            key=lambda record: (
                record.event.available_at,
                record.event.created_at,
                record.event.id,
            ),
        )[:limit]
        claims: list[OutboxClaim] = []
        for record in candidates:
            claim_token = secrets.token_urlsafe(24)
            claim_expires_at = now + claim_ttl
            record.status = OutboxStatus.PROCESSING
            record.attempt_count += 1
            record.claimed_by = worker_id
            record.claim_token = claim_token
            record.claim_expires_at = claim_expires_at
            claims.append(
                OutboxClaim(
                    event=record.event,
                    claim_token=claim_token,
                    claimed_by=worker_id,
                    claim_expires_at=claim_expires_at,
                    attempt_count=record.attempt_count,
                )
            )
        return tuple(claims)

    def mark_outbox_published(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        now: datetime,
    ) -> None:
        record = self._claimed_record(tenant_id, event_id, claim_token)
        record.status = OutboxStatus.PUBLISHED
        record.published_at = now
        record.claimed_by = None
        record.claim_token = None
        record.claim_expires_at = None
        record.last_error = None

    def mark_outbox_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        record = self._claimed_record(tenant_id, event_id, claim_token)
        record.status = OutboxStatus.FAILED
        record.event = OutboxEvent(
            id=record.event.id,
            tenant_id=record.event.tenant_id,
            aggregate_type=record.event.aggregate_type,
            aggregate_id=record.event.aggregate_id,
            aggregate_version=record.event.aggregate_version,
            event_type=record.event.event_type,
            payload=record.event.payload,
            created_at=record.event.created_at,
            available_at=retry_at,
        )
        record.claimed_by = None
        record.claim_token = None
        record.claim_expires_at = None
        record.last_error = error

    @staticmethod
    def _claimable(record: OutboxRecord, now: datetime) -> bool:
        if record.status in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
            return record.event.available_at <= now
        return (
            record.status == OutboxStatus.PROCESSING
            and record.claim_expires_at is not None
            and record.claim_expires_at <= now
        )

    def _claimed_record(
        self,
        tenant_id: str,
        event_id: str,
        claim_token: str,
    ) -> OutboxRecord:
        record = self._outbox.get((tenant_id, event_id))
        if (
            record is None
            or record.status != OutboxStatus.PROCESSING
            or record.claim_token is None
            or not secrets.compare_digest(record.claim_token, claim_token)
        ):
            raise InvalidOutboxClaimError(event_id)
        return record

    @staticmethod
    def _validate_events(
        instance: WorkflowInstance,
        audit_events: tuple[AuditEvent, ...],
        outbox_events: tuple[OutboxEvent, ...],
    ) -> None:
        for event in audit_events:
            if event.tenant_id != instance.tenant_id or event.instance_id != instance.id:
                raise ValueError("audit event does not belong to the aggregate")
        for event in outbox_events:
            if event.tenant_id != instance.tenant_id:
                raise ValueError("outbox event does not belong to the aggregate tenant")

    def _append_events(
        self,
        audit_events: tuple[AuditEvent, ...],
        outbox_events: tuple[OutboxEvent, ...],
    ) -> None:
        for event in audit_events:
            key = (event.tenant_id, event.id)
            if key in self._audit_ids:
                continue
            self._audit_ids.add(key)
            self._audit_events.append(event)
        for event in outbox_events:
            dedupe = (event.tenant_id, event.dedupe_key)
            if dedupe in self._outbox_dedupe:
                continue
            self._outbox_dedupe.add(dedupe)
            self._outbox[(event.tenant_id, event.id)] = OutboxRecord(event=event)
