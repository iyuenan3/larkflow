"""Persistence port and deterministic in-memory implementation."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import secrets
from typing import Protocol

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


class InMemoryWorkflowRepository:
    """Copy-on-read repository that exercises optimistic concurrency in tests."""

    def __init__(self) -> None:
        self._instances: dict[tuple[str, str], WorkflowInstance] = {}
        self._audit_events: list[AuditEvent] = []
        self._audit_ids: set[tuple[str, str]] = set()
        self._outbox: dict[tuple[str, str], OutboxRecord] = {}
        self._outbox_dedupe: set[tuple[str, tuple[str, str, str, int]]] = set()

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

    def claim_outbox(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 100,
        claim_ttl: timedelta = timedelta(minutes=5),
    ) -> tuple[OutboxClaim, ...]:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if limit < 1:
            raise ValueError("limit must be positive")
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        candidates = sorted(
            (
                record
                for (record_tenant, _), record in self._outbox.items()
                if record_tenant == tenant_id
                and self._claimable(record, now)
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
