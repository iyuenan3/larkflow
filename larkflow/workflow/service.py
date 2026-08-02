"""Application service for the first central workflow implementation slice."""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .directory import PersonDirectory, validate_snapshot_owners
from .events import AuditEvent, OutboxEvent
from .graph import validate_snapshot
from .model import (
    ExecutorKind,
    InstanceSnapshot,
    InstanceStatus,
    NodeActivation,
    NodeStatus,
    QualityResult,
    WorkflowInstance,
    WorkflowInstanceSummary,
)
from .repository import WorkflowRepository
from .runner import AuthorizationError, NodeRunner
from .scheduler import Scheduler
from .transitions import TransitionError, transition_instance


class WorkflowService:
    """Coordinates repository boundaries, scheduling, and node submissions."""

    def __init__(
        self,
        repository: WorkflowRepository,
        *,
        scheduler: Scheduler | None = None,
        runner: NodeRunner | None = None,
        directory: PersonDirectory | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self.scheduler = scheduler or Scheduler()
        self.runner = runner or NodeRunner()
        self.directory = directory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.id_factory = id_factory or (lambda: str(uuid4()))

    def create_draft(
        self,
        *,
        instance_id: str,
        tenant_id: str,
        owner_person_id: str,
        snapshot: InstanceSnapshot,
        actor_person_id: str,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        if not instance_id.strip():
            raise ValueError("instance_id is required")
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not owner_person_id.strip():
            raise ValueError("owner_person_id is required")
        if not actor_person_id.strip():
            raise ValueError("actor_person_id is required")
        validate_snapshot(snapshot)
        if self.directory is not None:
            validate_snapshot_owners(
                self.directory,
                tenant_id=tenant_id,
                instance_owner_person_id=owner_person_id,
                node_owner_person_ids=tuple(
                    node.owner_person_id for node in snapshot.nodes
                ),
            )
        now = self.clock()
        instance = WorkflowInstance(
            id=instance_id,
            tenant_id=tenant_id,
            owner_person_id=owner_person_id,
            snapshot=snapshot,
            created_at=now,
        )
        audit = self._audit(
            instance,
            "instance.draft_created",
            actor_person_id=actor_person_id,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=0,
            now=now,
        )
        self.repository.add(instance, audit_events=(audit,))
        return self.repository.get(tenant_id, instance_id)

    def preview_draft(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        actor_person_id: str,
    ) -> WorkflowInstance:
        instance = self.repository.get(tenant_id, instance_id)
        self._require_instance_owner(instance, actor_person_id)
        if instance.status != InstanceStatus.DRAFT:
            raise TransitionError(f"instance is not a draft: {instance_id}")
        validate_snapshot(instance.snapshot)
        return instance

    def confirm_draft(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        actor_person_id: str,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        instance = self.repository.get(tenant_id, instance_id)
        expected_version = instance.version
        self._require_instance_owner(instance, actor_person_id)
        if instance.status != InstanceStatus.DRAFT:
            raise TransitionError(f"instance is not a draft: {instance_id}")
        now = self.clock()
        self.scheduler.confirm(instance, now=now)
        correlation_id = correlation_id or self.id_factory()
        audit = self._audit(
            instance,
            "instance.confirmed",
            actor_person_id=actor_person_id,
            correlation_id=correlation_id,
            aggregate_version=expected_version + 1,
            now=now,
        )
        outbox = tuple(
            self._outbox(
                instance,
                event_type="node.projection_create_requested",
                aggregate_type="node_instance",
                aggregate_id=node.id,
                aggregate_version=node.version,
                payload={
                    "instance_id": instance.id,
                    "node_key": node.node_key,
                    "attempt_no": node.current_attempt_no,
                },
                now=now,
            )
            for node in instance.nodes.values()
        )
        self.repository.save(
            instance,
            expected_version=expected_version,
            audit_events=(audit,),
            outbox_events=outbox,
        )
        return self.repository.get(tenant_id, instance_id)

    def discard_draft(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        actor_person_id: str,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        instance = self.repository.get(tenant_id, instance_id)
        expected_version = instance.version
        self._require_instance_owner(instance, actor_person_id)
        now = self.clock()
        transition_instance(instance, InstanceStatus.DISCARDED, now=now)
        audit = self._audit(
            instance,
            "instance.discarded",
            actor_person_id=actor_person_id,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
        )
        self.repository.save(
            instance,
            expected_version=expected_version,
            audit_events=(audit,),
        )
        return self.repository.get(tenant_id, instance_id)

    def dispatch_ready(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        worker_id: str | None = None,
        max_automated: int = 1,
        correlation_id: str | None = None,
    ) -> tuple[NodeActivation, ...]:
        if max_automated < 0:
            raise ValueError("max_automated cannot be negative")
        return self._dispatch(
            tenant_id,
            instance_id,
            worker_id=worker_id,
            max_automated=max_automated,
            recover_expired=False,
            correlation_id=correlation_id,
        )

    def dispatch_due(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        worker_id: str,
        max_automated: int = 1,
        automated_node_keys: Collection[str] | None = None,
        correlation_id: str | None = None,
    ) -> tuple[NodeActivation, ...]:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if max_automated < 0:
            raise ValueError("max_automated cannot be negative")
        return self._dispatch(
            tenant_id,
            instance_id,
            worker_id=worker_id,
            max_automated=max_automated,
            recover_expired=True,
            automated_node_keys=automated_node_keys,
            correlation_id=correlation_id,
        )

    def _dispatch(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        worker_id: str | None,
        max_automated: int | None,
        recover_expired: bool,
        automated_node_keys: Collection[str] | None = None,
        correlation_id: str | None,
    ) -> tuple[NodeActivation, ...]:
        instance = self.repository.get(tenant_id, instance_id)
        expected_version = instance.version
        if instance.status != InstanceStatus.RUNNING:
            raise TransitionError(f"instance is not running: {instance_id}")
        now = self.clock()
        automated_count = 0
        activations: list[NodeActivation] = []
        allowed_node_keys = (
            None
            if automated_node_keys is None
            else {str(key) for key in automated_node_keys}
        )

        for spec in instance.snapshot.nodes:
            node = instance.nodes[spec.key]
            if node.status == NodeStatus.READY and node.executor == ExecutorKind.HUMAN:
                activations.append(
                    self.runner.activate(instance, spec.key, now=now)
                )

        if recover_expired:
            for spec in instance.snapshot.nodes:
                if max_automated is not None and automated_count >= max_automated:
                    break
                node = instance.nodes[spec.key]
                if (
                    node.executor != ExecutorKind.HUMAN
                    and (
                        allowed_node_keys is None
                        or spec.key in allowed_node_keys
                    )
                    and self.runner.is_reclaimable(instance, spec.key, now=now)
                ):
                    activations.append(
                        self.runner.reclaim_expired(
                            instance,
                            spec.key,
                            worker_id=worker_id or "",
                            now=now,
                        )
                    )
                    automated_count += 1

        for spec in instance.snapshot.nodes:
            if max_automated is not None and automated_count >= max_automated:
                break
            node = instance.nodes[spec.key]
            if (
                node.status == NodeStatus.READY
                and node.executor != ExecutorKind.HUMAN
                and (
                    allowed_node_keys is None
                    or spec.key in allowed_node_keys
                )
            ):
                activations.append(
                    self.runner.activate(
                        instance,
                        spec.key,
                        worker_id=worker_id,
                        now=now,
                    )
                )
                automated_count += 1
        if activations:
            correlation_id = correlation_id or self.id_factory()
            audit_events = tuple(
                self._audit(
                    instance,
                    "node.claim_recovered"
                    if activation.recovered
                    else "node.activated",
                    actor_person_id=None,
                    correlation_id=correlation_id,
                    aggregate_version=expected_version + 1,
                    now=now,
                    node_key=activation.node_key,
                    attempt_no=activation.attempt_no,
                    payload={
                        "executor": activation.executor.value,
                        "worker_id": activation.claimed_by,
                    },
                )
                for activation in activations
            )
            outbox_events = tuple(
                self._activation_outbox(instance, activation, now=now)
                for activation in activations
                if activation.executor == ExecutorKind.HUMAN
            )
            self.repository.save(
                instance,
                expected_version=expected_version,
                audit_events=audit_events,
                outbox_events=outbox_events,
            )
        return tuple(activations)

    def submit_human(
        self,
        tenant_id: str,
        instance_id: str,
        node_key: str,
        *,
        actor_person_id: str,
        attempt_no: int,
        expected_node_version: int,
        result: Mapping[str, Any],
        quality_result: QualityResult | None = None,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        instance = self.repository.get(tenant_id, instance_id)
        expected_version = instance.version
        self._require_running(instance)
        now = self.clock()
        self.runner.submit_human(
            instance,
            node_key,
            actor_person_id=actor_person_id,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
            result=result,
            quality_result=quality_result,
            now=now,
        )
        self.scheduler.unlock_after(instance, node_key, now=now)
        audit_events = self._completion_audits(
            instance,
            "node.human_submitted",
            actor_person_id=actor_person_id,
            node_key=node_key,
            attempt_no=attempt_no,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
        )
        outbox = self._completion_outboxes(instance, node_key, now=now)
        self.repository.save(
            instance,
            expected_version=expected_version,
            audit_events=audit_events,
            outbox_events=outbox,
        )
        return self.repository.get(tenant_id, instance_id)

    def complete_automated(
        self,
        tenant_id: str,
        instance_id: str,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        result: Mapping[str, Any],
        quality_result: QualityResult | None = None,
        worker_id: str,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        instance = self.repository.get(tenant_id, instance_id)
        expected_version = instance.version
        self._require_running(instance)
        now = self.clock()
        self.runner.complete_automated(
            instance,
            node_key,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
            claim_token=claim_token,
            worker_id=worker_id,
            result=result,
            quality_result=quality_result,
            now=now,
        )
        self.scheduler.unlock_after(instance, node_key, now=now)
        audit_events = self._completion_audits(
            instance,
            "node.automated_completed",
            actor_person_id=None,
            node_key=node_key,
            attempt_no=attempt_no,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
            payload={"worker_id": worker_id},
        )
        outbox = self._completion_outboxes(instance, node_key, now=now)
        self.repository.save(
            instance,
            expected_version=expected_version,
            audit_events=audit_events,
            outbox_events=outbox,
        )
        return self.repository.get(tenant_id, instance_id)

    def renew_automated_claim(
        self,
        tenant_id: str,
        instance_id: str,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        worker_id: str,
        correlation_id: str | None = None,
    ) -> datetime:
        """Renew one current claim through the same optimistic boundary."""

        if not worker_id.strip():
            raise ValueError("worker_id is required")
        instance = self.repository.get(tenant_id, instance_id)
        expected_version = instance.version
        self._require_running(instance)
        now = self.clock()
        expires_at = self.runner.renew_automated_claim(
            instance,
            node_key,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
            claim_token=claim_token,
            worker_id=worker_id,
            now=now,
        )
        audit = self._audit(
            instance,
            "node.claim_renewed",
            actor_person_id=None,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
            node_key=node_key,
            attempt_no=attempt_no,
            payload={"worker_id": worker_id, "claim_expires_at": expires_at.isoformat()},
        )
        self.repository.save(
            instance,
            expected_version=expected_version,
            audit_events=(audit,),
        )
        return expires_at

    def fail_automated(
        self,
        tenant_id: str,
        instance_id: str,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        error_code: str,
        error_message: str,
        worker_id: str,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        instance = self.repository.get(tenant_id, instance_id)
        expected_version = instance.version
        self._require_running(instance)
        now = self.clock()
        self.runner.fail_automated(
            instance,
            node_key,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
            claim_token=claim_token,
            worker_id=worker_id,
            error_code=error_code,
            error_message=error_message,
            now=now,
        )
        self.scheduler.fail_instance(instance, now=now)
        audit = self._audit(
            instance,
            "node.automated_failed",
            actor_person_id=None,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
            node_key=node_key,
            attempt_no=attempt_no,
            payload={"worker_id": worker_id, "error_code": error_code},
        )
        outbox = self._completion_outboxes(instance, node_key, now=now)
        self.repository.save(
            instance,
            expected_version=expected_version,
            audit_events=(audit,),
            outbox_events=outbox,
        )
        return self.repository.get(tenant_id, instance_id)

    def get(self, tenant_id: str, instance_id: str) -> WorkflowInstance:
        return self.repository.get(tenant_id, instance_id)

    def get_for_owner(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        actor_person_id: str,
    ) -> WorkflowInstance:
        """Return an instance only when the current actor owns it."""
        instance = self.repository.get(tenant_id, instance_id)
        self._require_instance_owner(instance, actor_person_id)
        return instance

    def list_for_owner(
        self,
        tenant_id: str,
        *,
        actor_person_id: str,
        limit: int = 10,
    ) -> tuple[WorkflowInstanceSummary, ...]:
        """Return only recent instances owned by the current actor."""
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not actor_person_id.strip():
            raise ValueError("actor_person_id is required")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        return self.repository.list_for_owner(
            tenant_id,
            owner_person_id=actor_person_id,
            limit=limit,
        )

    def _audit(
        self,
        instance: WorkflowInstance,
        event_type: str,
        *,
        actor_person_id: str | None,
        correlation_id: str,
        aggregate_version: int,
        now: datetime,
        node_key: str | None = None,
        attempt_no: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            id=self.id_factory(),
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            event_type=event_type,
            source="workflow_service",
            correlation_id=correlation_id,
            aggregate_version=aggregate_version,
            occurred_at=now,
            actor_person_id=actor_person_id,
            node_key=node_key,
            attempt_no=attempt_no,
            payload=payload or {},
        )

    def _outbox(
        self,
        instance: WorkflowInstance,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> OutboxEvent:
        return OutboxEvent(
            id=self.id_factory(),
            tenant_id=instance.tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            event_type=event_type,
            payload=payload,
            created_at=now,
            available_at=now,
        )

    def _activation_outbox(
        self,
        instance: WorkflowInstance,
        activation: NodeActivation,
        *,
        now: datetime,
    ) -> OutboxEvent:
        return self._outbox(
            instance,
            event_type="node.projection_sync_requested",
            aggregate_type="node_instance",
            aggregate_id=activation.node_instance_id,
            aggregate_version=activation.expected_node_version,
            payload={
                "instance_id": instance.id,
                "node_key": activation.node_key,
                "attempt_no": activation.attempt_no,
                "expected_node_version": activation.expected_node_version,
                "claim_token": activation.claim_token,
            },
            now=now,
        )

    def _completion_outboxes(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        now: datetime,
    ) -> tuple[OutboxEvent, ...]:
        node = instance.nodes[node_key]
        events = [self._outbox(
            instance,
            event_type="node.projection_sync_requested",
            aggregate_type="node_instance",
            aggregate_id=node.id,
            aggregate_version=node.version,
            payload={
                "instance_id": instance.id,
                "node_key": node_key,
                "attempt_no": node.current_attempt_no,
                "status": node.status.value,
            },
            now=now,
        )]
        if instance.status == InstanceStatus.DONE:
            events.append(
                self._outbox(
                    instance,
                    event_type="instance.projection_completed_requested",
                    aggregate_type="workflow_instance",
                    aggregate_id=instance.id,
                    aggregate_version=instance.version + 1,
                    payload={"instance_id": instance.id},
                    now=now,
                )
            )
        return tuple(events)

    def _completion_audits(
        self,
        instance: WorkflowInstance,
        event_type: str,
        *,
        actor_person_id: str | None,
        node_key: str,
        attempt_no: int,
        correlation_id: str,
        aggregate_version: int,
        now: datetime,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[AuditEvent, ...]:
        events = [
            self._audit(
                instance,
                event_type,
                actor_person_id=actor_person_id,
                correlation_id=correlation_id,
                aggregate_version=aggregate_version,
                now=now,
                node_key=node_key,
                attempt_no=attempt_no,
                payload=payload,
            )
        ]
        if instance.status == InstanceStatus.DONE:
            events.append(
                self._audit(
                    instance,
                    "instance.completed",
                    actor_person_id=actor_person_id,
                    correlation_id=correlation_id,
                    aggregate_version=aggregate_version,
                    now=now,
                )
            )
        return tuple(events)

    @staticmethod
    def _require_instance_owner(
        instance: WorkflowInstance,
        actor_person_id: str,
    ) -> None:
        if actor_person_id != instance.owner_person_id:
            raise AuthorizationError("only the instance owner may perform this command")

    @staticmethod
    def _require_running(instance: WorkflowInstance) -> None:
        if instance.status != InstanceStatus.RUNNING:
            raise TransitionError(f"instance is not running: {instance.id}")
