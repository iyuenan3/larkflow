"""Persistence port and deterministic in-memory implementation."""
from __future__ import annotations

from collections.abc import Collection
from copy import deepcopy
from dataclasses import replace
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
from .model import (
    TemplateAuditEvent,
    TemplateStatus,
    WorkflowInstance,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)


class InstanceNotFoundError(KeyError):
    pass


class InstanceAlreadyExistsError(RuntimeError):
    pass


class ConcurrentUpdateError(RuntimeError):
    pass


class TemplateNotFoundError(KeyError):
    pass


class TemplateAlreadyExistsError(RuntimeError):
    pass


class ConcurrentTemplateUpdateError(RuntimeError):
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

    def projection_instance_ids(
        self,
        tenant_id: str,
        *,
        after_id: str | None = None,
        limit: int = 100,
    ) -> tuple[str, ...]:
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

    def replace_projection_external(
        self,
        projection: ProjectionRecord,
        *,
        expected_external_id: str,
        expected_idempotency_key: str,
    ) -> None:
        ...


class TemplateStore(Protocol):
    def add_template(
        self,
        template: WorkflowTemplate,
        initial_version: WorkflowTemplateVersion,
        event: TemplateAuditEvent,
    ) -> None:
        ...

    def get_template(self, tenant_id: str, template_id: str) -> WorkflowTemplate:
        ...

    def list_templates(self, tenant_id: str) -> tuple[WorkflowTemplate, ...]:
        ...

    def get_template_version(
        self,
        tenant_id: str,
        template_id: str,
        version: int | None = None,
    ) -> WorkflowTemplateVersion:
        ...

    def add_template_version(
        self,
        template_version: WorkflowTemplateVersion,
        *,
        expected_template_version: int,
        updated_at: datetime,
        event: TemplateAuditEvent,
    ) -> WorkflowTemplate:
        ...

    def set_template_status(
        self,
        tenant_id: str,
        template_id: str,
        *,
        expected_template_version: int,
        status: TemplateStatus,
        updated_at: datetime,
        deleted_at: datetime | None,
        event: TemplateAuditEvent,
    ) -> WorkflowTemplate:
        ...

    def template_audit_log(
        self,
        tenant_id: str,
        template_id: str,
    ) -> tuple[TemplateAuditEvent, ...]:
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

    def projection_instance_ids(
        self,
        tenant_id: str,
        *,
        after_id: str | None = None,
        limit: int = 100,
    ) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        candidates = sorted(
            instance_id
            for (instance_tenant, instance_id), instance in self._instances.items()
            if instance_tenant == tenant_id
            and (after_id is None or instance_id > after_id)
            and any(
                node.executor.value == "human"
                and (
                    node.status.value == "waiting_human"
                    or (
                        node.status.value in {"done", "failed", "canceled"}
                        and (
                            projection := self._projections.get(
                                (
                                    tenant_id,
                                    node.id,
                                    node.current_attempt_no,
                                    "feishu_task",
                                )
                            )
                        ) is not None
                        and not bool(projection.state.get("completed"))
                    )
                )
                for node in instance.nodes.values()
            )
        )
        return tuple(candidates[:limit])

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

    def replace_projection_external(
        self,
        projection: ProjectionRecord,
        *,
        expected_external_id: str,
        expected_idempotency_key: str,
    ) -> None:
        key = (
            projection.tenant_id,
            projection.node_instance_id,
            projection.attempt_no,
            projection.kind,
        )
        existing = self._projections.get(key)
        if (
            existing is None
            or existing.id != projection.id
            or existing.external_id != expected_external_id
            or existing.idempotency_key != expected_idempotency_key
            or projection.sync_version < existing.sync_version
        ):
            raise ValueError("projection replacement lost a concurrent update")
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


class InMemoryTemplateStore:
    """Copy-on-read template store with aggregate-version compare-and-swap."""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, str], WorkflowTemplate] = {}
        self._versions: dict[tuple[str, str, int], WorkflowTemplateVersion] = {}
        self._events: list[TemplateAuditEvent] = []
        self._event_ids: set[tuple[str, str]] = set()

    def add_template(
        self,
        template: WorkflowTemplate,
        initial_version: WorkflowTemplateVersion,
        event: TemplateAuditEvent,
    ) -> None:
        key = (template.tenant_id, template.id)
        if key in self._templates:
            raise TemplateAlreadyExistsError(template.id)
        self._validate_version(template, initial_version, expected_number=1)
        self._validate_event(template, event, aggregate_version=0)
        self._templates[key] = deepcopy(template)
        self._versions[(template.tenant_id, template.id, 1)] = deepcopy(
            initial_version
        )
        self._append_template_event(event)

    def get_template(self, tenant_id: str, template_id: str) -> WorkflowTemplate:
        try:
            return deepcopy(self._templates[(tenant_id, template_id)])
        except KeyError as exc:
            raise TemplateNotFoundError((tenant_id, template_id)) from exc

    def list_templates(self, tenant_id: str) -> tuple[WorkflowTemplate, ...]:
        templates = [
            deepcopy(template)
            for (item_tenant, _), template in self._templates.items()
            if item_tenant == tenant_id
        ]
        templates.sort(key=lambda item: (item.created_at, item.id))
        return tuple(templates)

    def get_template_version(
        self,
        tenant_id: str,
        template_id: str,
        version: int | None = None,
    ) -> WorkflowTemplateVersion:
        self.get_template(tenant_id, template_id)
        if version is None:
            numbers = [
                number
                for item_tenant, item_template, number in self._versions
                if item_tenant == tenant_id and item_template == template_id
            ]
            if not numbers:
                raise TemplateNotFoundError((tenant_id, template_id, "latest"))
            version = max(numbers)
        try:
            return deepcopy(self._versions[(tenant_id, template_id, version)])
        except KeyError as exc:
            raise TemplateNotFoundError((tenant_id, template_id, version)) from exc

    def add_template_version(
        self,
        template_version: WorkflowTemplateVersion,
        *,
        expected_template_version: int,
        updated_at: datetime,
        event: TemplateAuditEvent,
    ) -> WorkflowTemplate:
        template = self.get_template(
            template_version.tenant_id,
            template_version.template_id,
        )
        if template.version != expected_template_version:
            raise ConcurrentTemplateUpdateError(template.id)
        latest = self.get_template_version(template.tenant_id, template.id)
        self._validate_version(
            template,
            template_version,
            expected_number=latest.version + 1,
        )
        self._validate_event(
            template,
            event,
            aggregate_version=expected_template_version + 1,
        )
        updated = replace(
            template,
            version=expected_template_version + 1,
            updated_at=updated_at,
        )
        self._templates[(template.tenant_id, template.id)] = deepcopy(updated)
        self._versions[
            (template.tenant_id, template.id, template_version.version)
        ] = deepcopy(template_version)
        self._append_template_event(event)
        return deepcopy(updated)

    def set_template_status(
        self,
        tenant_id: str,
        template_id: str,
        *,
        expected_template_version: int,
        status: TemplateStatus,
        updated_at: datetime,
        deleted_at: datetime | None,
        event: TemplateAuditEvent,
    ) -> WorkflowTemplate:
        template = self.get_template(tenant_id, template_id)
        if template.version != expected_template_version:
            raise ConcurrentTemplateUpdateError(template.id)
        self._validate_event(
            template,
            event,
            aggregate_version=expected_template_version + 1,
        )
        updated = replace(
            template,
            status=TemplateStatus(status),
            version=expected_template_version + 1,
            updated_at=updated_at,
            deleted_at=deleted_at,
        )
        self._templates[(tenant_id, template_id)] = deepcopy(updated)
        self._append_template_event(event)
        return deepcopy(updated)

    def template_audit_log(
        self,
        tenant_id: str,
        template_id: str,
    ) -> tuple[TemplateAuditEvent, ...]:
        return tuple(
            deepcopy(event)
            for event in self._events
            if event.tenant_id == tenant_id and event.template_id == template_id
        )

    @staticmethod
    def _validate_version(
        template: WorkflowTemplate,
        version: WorkflowTemplateVersion,
        *,
        expected_number: int,
    ) -> None:
        if (
            version.tenant_id != template.tenant_id
            or version.template_id != template.id
            or version.version != expected_number
        ):
            raise ValueError("template version does not belong to the aggregate")

    @staticmethod
    def _validate_event(
        template: WorkflowTemplate,
        event: TemplateAuditEvent,
        *,
        aggregate_version: int,
    ) -> None:
        if (
            event.tenant_id != template.tenant_id
            or event.template_id != template.id
            or event.aggregate_version != aggregate_version
        ):
            raise ValueError("template event does not belong to the aggregate")

    def _append_template_event(self, event: TemplateAuditEvent) -> None:
        key = (event.tenant_id, event.id)
        if key in self._event_ids:
            raise ValueError("duplicate template event id")
        self._event_ids.add(key)
        self._events.append(deepcopy(event))
