"""Application service for the first central workflow implementation slice."""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .directory import (
    DirectoryValidationError,
    PersonDirectory,
    validate_snapshot_owners,
)
from .editing import (
    GraphEditConfirmation,
    GraphEditPreview,
    GraphEditPreviewExpiredError,
    StaleGraphEditPreviewError,
    apply_future_graph_edit,
)
from .decision import (
    HumanDecision,
    HumanDecisionNotAllowedError,
    StaleHumanDecisionError,
    human_decision_config,
    normalize_human_decision_feedback,
)
from .events import AuditEvent, OutboxEvent
from .graph import validate_snapshot
from .lifecycle import (
    CancellationConfirmation,
    CancellationPreview,
    StaleCancellationError,
    apply_cancellation,
    preview_cancellation,
)
from .model import (
    AttemptStatus,
    ExecutorKind,
    InstanceSnapshot,
    InstanceStatus,
    NodeActivation,
    NodeStatus,
    QualityResult,
    QualityVerdict,
    WorkflowInstance,
    WorkflowInstanceSummary,
)
from .repository import ConcurrentUpdateError, WorkflowRepository
from .recovery import RecoveryAction, apply_failed_node_recovery
from .restart import (
    RestartConfirmation,
    RestartPreview,
    RestartPreviewExpiredError,
    RestartScope,
    StaleRestartPreviewError,
    affected_instance_restart_node_keys,
    affected_restart_node_keys,
    apply_restart,
)
from .runner import AuthorizationError, NodeRunner, StaleAttemptError
from .scheduler import Scheduler
from .transitions import TransitionError, transition_instance


class HumanTaskTransferNotAllowedError(RuntimeError):
    """The current Human assignment cannot be transferred safely."""


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
        restart_preview_ttl: timedelta = timedelta(minutes=15),
        graph_edit_preview_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        if restart_preview_ttl <= timedelta(0):
            raise ValueError("restart_preview_ttl must be positive")
        if graph_edit_preview_ttl <= timedelta(0):
            raise ValueError("graph_edit_preview_ttl must be positive")
        self.repository = repository
        self.scheduler = scheduler or Scheduler()
        self.runner = runner or NodeRunner()
        self.directory = directory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.id_factory = id_factory or (lambda: str(uuid4()))
        self.restart_preview_ttl = restart_preview_ttl
        self.graph_edit_preview_ttl = graph_edit_preview_ttl

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

    def pause_instance(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        actor_person_id: str,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        """Stop new dispatch while allowing already active work to settle."""

        instance = self.repository.get(tenant_id, instance_id)
        self._require_instance_owner(instance, actor_person_id)
        if instance.status == InstanceStatus.PAUSED:
            return instance
        expected_version = instance.version
        now = self.clock()
        transition_instance(instance, InstanceStatus.PAUSED, now=now)
        audit = self._audit(
            instance,
            "instance.paused",
            actor_person_id=actor_person_id,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
            payload={
                "active_node_keys": tuple(
                    spec.key
                    for spec in instance.snapshot.nodes
                    if instance.nodes[spec.key].status
                    in {NodeStatus.RUNNING, NodeStatus.WAITING_HUMAN}
                ),
            },
        )
        try:
            self.repository.save(
                instance,
                expected_version=expected_version,
                audit_events=(audit,),
            )
        except ConcurrentUpdateError:
            current = self.repository.get(tenant_id, instance_id)
            self._require_instance_owner(current, actor_person_id)
            if current.status == InstanceStatus.PAUSED:
                return current
            raise
        return self.repository.get(tenant_id, instance_id)

    def resume_instance(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        actor_person_id: str,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        """Resume dispatch for one paused process without replacing Attempts."""

        instance = self.repository.get(tenant_id, instance_id)
        self._require_instance_owner(instance, actor_person_id)
        if instance.status == InstanceStatus.RUNNING:
            return instance
        expected_version = instance.version
        now = self.clock()
        transition_instance(instance, InstanceStatus.RUNNING, now=now)
        audit = self._audit(
            instance,
            "instance.resumed",
            actor_person_id=actor_person_id,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
        )
        try:
            self.repository.save(
                instance,
                expected_version=expected_version,
                audit_events=(audit,),
            )
        except ConcurrentUpdateError:
            current = self.repository.get(tenant_id, instance_id)
            self._require_instance_owner(current, actor_person_id)
            if current.status == InstanceStatus.RUNNING:
                return current
            raise
        return self.repository.get(tenant_id, instance_id)

    def preview_cancellation(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        actor_person_id: str,
    ) -> CancellationPreview:
        """Preview a terminal cancellation without changing the process."""

        instance = self.repository.get(tenant_id, instance_id)
        self._require_instance_owner(instance, actor_person_id)
        return preview_cancellation(instance)

    def confirm_cancellation(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        actor_person_id: str,
        expected_instance_version: int,
        correlation_id: str | None = None,
    ) -> CancellationConfirmation:
        """Cancel unfinished work through a version-bound confirmation."""

        instance = self.repository.get(tenant_id, instance_id)
        self._require_instance_owner(instance, actor_person_id)
        if instance.status == InstanceStatus.CANCELED:
            return CancellationConfirmation(
                instance=instance,
                canceled_node_keys=tuple(
                    spec.key
                    for spec in instance.snapshot.nodes
                    if instance.nodes[spec.key].status == NodeStatus.CANCELED
                ),
                revoked_claim_node_keys=(),
                already_applied=True,
            )
        expected_version = instance.version
        if expected_version != expected_instance_version:
            raise StaleCancellationError(
                "instance changed after the cancellation preview"
            )
        now = self.clock()
        from_status = instance.status
        human_work = {
            spec.key: (
                instance.nodes[spec.key].executor == ExecutorKind.HUMAN
                or instance.current_attempt(spec.key).status
                == AttemptStatus.WAITING_HUMAN
                or instance.current_attempt(spec.key).submitted_by_person_id is not None
            )
            for spec in instance.snapshot.nodes
        }
        canceled_node_keys, revoked_claim_node_keys = apply_cancellation(
            instance,
            expected_instance_version=expected_instance_version,
            now=now,
        )
        audit = self._audit(
            instance,
            "instance.canceled",
            actor_person_id=actor_person_id,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
            payload={
                "from_status": from_status.value,
                "canceled_node_keys": canceled_node_keys,
                "revoked_claim_node_keys": revoked_claim_node_keys,
            },
        )
        outbox = tuple(
            self._outbox(
                instance,
                event_type="node.projection_sync_requested",
                aggregate_type="node_instance",
                aggregate_id=instance.nodes[node_key].id,
                aggregate_version=instance.nodes[node_key].version,
                payload={
                    "instance_id": instance.id,
                    "node_key": node_key,
                    "attempt_no": instance.nodes[node_key].current_attempt_no,
                    "status": NodeStatus.CANCELED.value,
                },
                now=now,
            )
            for node_key in canceled_node_keys
            if human_work[node_key]
        )
        try:
            self.repository.save(
                instance,
                expected_version=expected_version,
                audit_events=(audit,),
                outbox_events=outbox,
            )
        except ConcurrentUpdateError as exc:
            current = self.repository.get(tenant_id, instance_id)
            self._require_instance_owner(current, actor_person_id)
            if current.status == InstanceStatus.CANCELED:
                return CancellationConfirmation(
                    instance=current,
                    canceled_node_keys=tuple(
                        spec.key
                        for spec in current.snapshot.nodes
                        if current.nodes[spec.key].status == NodeStatus.CANCELED
                    ),
                    revoked_claim_node_keys=(),
                    already_applied=True,
                )
            raise StaleCancellationError(
                "instance changed while applying cancellation"
            ) from exc
        return CancellationConfirmation(
            instance=self.repository.get(tenant_id, instance_id),
            canceled_node_keys=canceled_node_keys,
            revoked_claim_node_keys=revoked_claim_node_keys,
        )

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
        self._require_active(instance)
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

    def transfer_human_task(
        self,
        tenant_id: str,
        instance_id: str,
        node_key: str,
        *,
        actor_person_id: str,
        new_owner_person_id: str,
        attempt_no: int,
        expected_node_version: int,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        """Transfer one active ordinary Human task without rewriting its Snapshot."""

        if not new_owner_person_id.strip():
            raise ValueError("new_owner_person_id is required")
        instance = self.repository.get(tenant_id, instance_id)
        expected_version = instance.version
        self._require_active(instance)
        try:
            node = instance.nodes[node_key]
            spec = instance.snapshot.node(node_key)
        except KeyError as exc:
            raise HumanTaskTransferNotAllowedError(
                f"unknown Human task node: {node_key}"
            ) from exc
        if (
            node.executor != ExecutorKind.HUMAN
            or human_decision_config(spec.work) is not None
        ):
            raise HumanTaskTransferNotAllowedError(
                "only ordinary Human tasks can be transferred"
            )
        if (
            node.status != NodeStatus.WAITING_HUMAN
            or instance.current_attempt(node_key).status
            != AttemptStatus.WAITING_HUMAN
        ):
            raise HumanTaskTransferNotAllowedError(
                "Human task is not waiting for its current owner"
            )
        if attempt_no != node.current_attempt_no:
            raise StaleAttemptError(
                f"node {node_key} is on attempt {node.current_attempt_no}, got {attempt_no}"
            )
        if node.version != expected_node_version:
            raise StaleAttemptError(
                f"node {node_key} expected version {expected_node_version}, found {node.version}"
            )
        if actor_person_id != node.owner_person_id:
            raise AuthorizationError("only the current node owner may transfer the task")
        new_owner_person_id = new_owner_person_id.strip()
        if new_owner_person_id == node.owner_person_id:
            raise HumanTaskTransferNotAllowedError(
                "Human task is already assigned to that person"
            )
        if self.directory is None:
            raise HumanTaskTransferNotAllowedError(
                "tenant directory validation is unavailable"
            )
        try:
            person = self.directory.get_person(tenant_id, new_owner_person_id)
        except DirectoryValidationError as exc:
            raise HumanTaskTransferNotAllowedError(
                "new owner is not an active tenant member"
            ) from exc
        if person.person_id != new_owner_person_id or not person.active:
            raise HumanTaskTransferNotAllowedError(
                "new owner is not an active tenant member"
            )

        previous_owner_person_id = node.owner_person_id
        node.owner_person_id = new_owner_person_id
        node.version += 1
        now = self.clock()
        audit = self._audit(
            instance,
            "node.human_task_transferred",
            actor_person_id=actor_person_id,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
            node_key=node_key,
            attempt_no=attempt_no,
            payload={
                "from_owner_person_id": previous_owner_person_id,
                "to_owner_person_id": new_owner_person_id,
                "authored_owner_preserved": True,
            },
        )
        outbox = self._outbox(
            instance,
            event_type="node.projection_sync_requested",
            aggregate_type="node_instance",
            aggregate_id=node.id,
            aggregate_version=node.version,
            payload={
                "instance_id": instance.id,
                "node_key": node_key,
                "attempt_no": attempt_no,
                "status": node.status.value,
                "transfer_from_person_id": previous_owner_person_id,
            },
            now=now,
        )
        self.repository.save(
            instance,
            expected_version=expected_version,
            audit_events=(audit,),
            outbox_events=(outbox,),
        )
        return self.repository.get(tenant_id, instance_id)

    def submit_human_decision(
        self,
        tenant_id: str,
        instance_id: str,
        node_key: str,
        decision: HumanDecision,
        *,
        actor_person_id: str,
        attempt_no: int,
        expected_instance_version: int,
        expected_node_version: int,
        feedback: str | None = None,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        """Accept or reject one version-bound Human decision card."""

        decision = HumanDecision(decision)
        instance = self.repository.get(tenant_id, instance_id)
        if instance.version != expected_instance_version:
            raise StaleHumanDecisionError(
                "instance changed after the decision card was sent"
            )
        expected_version = instance.version
        self._require_active(instance)
        try:
            node = instance.nodes[node_key]
            spec = instance.snapshot.node(node_key)
        except KeyError as exc:
            raise HumanDecisionNotAllowedError(
                f"unknown Human decision node: {node_key}"
            ) from exc
        config = human_decision_config(spec.work)
        if config is None or node.executor != ExecutorKind.HUMAN:
            raise HumanDecisionNotAllowedError(
                f"node is not an accept or reject decision: {node_key}"
            )
        if actor_person_id != node.owner_person_id:
            raise AuthorizationError(f"only the node owner may submit: {node_key}")
        normalized_feedback = normalize_human_decision_feedback(decision, feedback)
        now = self.clock()
        if decision == HumanDecision.ACCEPT:
            self.runner.submit_human(
                instance,
                node_key,
                actor_person_id=actor_person_id,
                attempt_no=attempt_no,
                expected_node_version=expected_node_version,
                result={"decision": "accepted"},
                quality_result=None,
                now=now,
            )
            self.scheduler.unlock_after(instance, node_key, now=now)
            event_type = "node.human_decision_accepted"
        else:
            quality = QualityResult(
                verdict=QualityVerdict.FAIL,
                evidence=f"节点 Owner 明确退回：{normalized_feedback}",
                suggestion=(
                    "由 Instance Owner 通过节点重启预览选择返工范围，"
                    "新 Attempt 会携带本次退回意见，旧 Attempt、结果和审计继续保留。"
                ),
            )
            self.runner.reject_human(
                instance,
                node_key,
                actor_person_id=actor_person_id,
                attempt_no=attempt_no,
                expected_node_version=expected_node_version,
                result={
                    "decision": "rejected",
                    "feedback": normalized_feedback,
                },
                quality_result=quality,
                now=now,
            )
            self.scheduler.fail_instance(instance, now=now)
            event_type = "node.human_decision_rejected"
        audit_events = self._completion_audits(
            instance,
            event_type,
            actor_person_id=actor_person_id,
            node_key=node_key,
            attempt_no=attempt_no,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
            payload={
                "decision": decision.value,
                "reject_target": config.get("reject_target"),
                "feedback": normalized_feedback,
            },
        )
        outbox = self._completion_outboxes(instance, node_key, now=now)
        try:
            self.repository.save(
                instance,
                expected_version=expected_version,
                audit_events=audit_events,
                outbox_events=outbox,
            )
        except ConcurrentUpdateError as exc:
            raise StaleHumanDecisionError(
                "instance changed while applying the decision"
            ) from exc
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
        self._require_active(instance)
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
        self._require_active(instance)
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
        self._require_active(instance)
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

    def recover_failed_node(
        self,
        tenant_id: str,
        instance_id: str,
        node_key: str,
        action: RecoveryAction,
        *,
        actor_person_id: str,
        expected_instance_version: int,
        expected_node_version: int,
        expected_attempt_no: int,
        correlation_id: str | None = None,
    ) -> WorkflowInstance:
        """Retry or hand a failed automated node to its accountable owner."""

        action = RecoveryAction(action)
        instance = self.repository.get(tenant_id, instance_id)
        expected_version = instance.version
        now = self.clock()
        affected = apply_failed_node_recovery(
            instance,
            node_key,
            action,
            actor_person_id=actor_person_id,
            expected_instance_version=expected_instance_version,
            expected_node_version=expected_node_version,
            expected_attempt_no=expected_attempt_no,
            now=now,
        )
        node = instance.nodes[node_key]
        event_type = (
            "node.automated_retry_started"
            if action == RecoveryAction.RETRY
            else "node.human_takeover_started"
        )
        audit = self._audit(
            instance,
            event_type,
            actor_person_id=actor_person_id,
            correlation_id=correlation_id or self.id_factory(),
            aggregate_version=expected_version + 1,
            now=now,
            node_key=node_key,
            attempt_no=node.current_attempt_no,
            payload={
                "failed_attempt_no": expected_attempt_no,
                "recovery_action": action.value,
                "affected_node_keys": affected,
            },
        )
        outbox_events: tuple[OutboxEvent, ...] = ()
        if action == RecoveryAction.HUMAN_TAKEOVER:
            outbox_events = (
                self._outbox(
                    instance,
                    event_type="node.projection_create_requested",
                    aggregate_type="node_instance",
                    aggregate_id=node.id,
                    aggregate_version=node.version,
                    payload={
                        "instance_id": instance.id,
                        "node_key": node_key,
                        "attempt_no": node.current_attempt_no,
                        "status": node.status.value,
                        "recovery_action": action.value,
                    },
                    now=now,
                ),
            )
        self.repository.save(
            instance,
            expected_version=expected_version,
            audit_events=(audit,),
            outbox_events=outbox_events,
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

    def preview_graph_edit(
        self,
        tenant_id: str,
        instance_id: str,
        operations: Sequence[Mapping[str, Any]],
        *,
        actor_person_id: str,
    ) -> GraphEditPreview:
        """Persist a future-region edit preview without changing the aggregate."""

        instance = self.repository.get(tenant_id, instance_id)
        self._require_instance_owner(instance, actor_person_id)
        expected_version = instance.version
        graph_revision = instance.graph_revision
        now = self.clock()
        plan = apply_future_graph_edit(instance, operations, now=now)
        self._validate_edited_snapshot_owners(instance)
        preview = GraphEditPreview(
            id=self.id_factory(),
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            actor_person_id=actor_person_id,
            operations=plan.operations,
            added_node_keys=plan.added_node_keys,
            updated_node_keys=plan.updated_node_keys,
            removed_node_keys=plan.removed_node_keys,
            candidate_snapshot_hash=plan.candidate_snapshot_hash,
            expected_instance_version=expected_version,
            graph_revision=graph_revision,
            proposed_graph_revision=plan.proposed_graph_revision,
            created_at=now,
            expires_at=now + self.graph_edit_preview_ttl,
        )
        self.repository.add_graph_edit_preview(preview)
        return preview

    def confirm_graph_edit(
        self,
        tenant_id: str,
        preview_id: str,
        *,
        actor_person_id: str,
    ) -> GraphEditConfirmation:
        """Consume one future-region edit preview atomically."""

        preview = self.repository.get_graph_edit_preview(tenant_id, preview_id)
        if preview.actor_person_id != actor_person_id:
            raise AuthorizationError("only the preview actor may confirm graph edit")
        instance = self.repository.get(tenant_id, preview.instance_id)
        self._require_instance_owner(instance, actor_person_id)
        if preview.consumed_at is not None:
            return GraphEditConfirmation(
                instance=instance,
                preview=preview,
                already_applied=True,
            )
        now = self.clock()
        if now >= preview.expires_at:
            raise GraphEditPreviewExpiredError(preview.id)
        if (
            instance.version != preview.expected_instance_version
            or instance.graph_revision != preview.graph_revision
        ):
            raise StaleGraphEditPreviewError(
                "instance changed after graph edit preview"
            )

        expected_version = instance.version
        plan = apply_future_graph_edit(instance, preview.operations, now=now)
        if (
            plan.operations != preview.operations
            or plan.added_node_keys != preview.added_node_keys
            or plan.updated_node_keys != preview.updated_node_keys
            or plan.removed_node_keys != preview.removed_node_keys
            or plan.candidate_snapshot_hash != preview.candidate_snapshot_hash
            or plan.proposed_graph_revision != preview.proposed_graph_revision
        ):
            raise StaleGraphEditPreviewError(
                "graph edit semantics changed after preview"
            )
        self._validate_edited_snapshot_owners(instance)
        audit = self._audit(
            instance,
            "instance.graph_edited",
            actor_person_id=actor_person_id,
            correlation_id=preview.id,
            aggregate_version=expected_version + 1,
            now=now,
            payload={
                "operations": preview.operations,
                "added_node_keys": preview.added_node_keys,
                "updated_node_keys": preview.updated_node_keys,
                "removed_node_keys": preview.removed_node_keys,
                "previous_graph_revision": preview.graph_revision,
                "graph_revision": preview.proposed_graph_revision,
                "candidate_snapshot_hash": preview.candidate_snapshot_hash,
            },
        )
        outbox_events: tuple[OutboxEvent, ...] = ()
        if instance.status == InstanceStatus.DONE:
            outbox_events = (
                self._outbox(
                    instance,
                    event_type="instance.projection_completed_requested",
                    aggregate_type="workflow_instance",
                    aggregate_id=instance.id,
                    aggregate_version=expected_version + 1,
                    payload={
                        "instance_id": instance.id,
                        "status": instance.status.value,
                    },
                    now=now,
                ),
            )
        try:
            saved = self.repository.save_graph_edit(
                instance,
                preview=preview,
                expected_version=expected_version,
                consumed_at=now,
                audit_events=(audit,),
                outbox_events=outbox_events,
            )
        except ConcurrentUpdateError as exc:
            raise StaleGraphEditPreviewError(
                "instance changed while confirming graph edit"
            ) from exc
        if not saved:
            current_preview = self.repository.get_graph_edit_preview(
                tenant_id,
                preview_id,
            )
            return GraphEditConfirmation(
                instance=self.repository.get(tenant_id, preview.instance_id),
                preview=current_preview,
                already_applied=True,
            )
        return GraphEditConfirmation(
            instance=self.repository.get(tenant_id, instance.id),
            preview=self.repository.get_graph_edit_preview(tenant_id, preview_id),
        )

    def _validate_edited_snapshot_owners(
        self,
        instance: WorkflowInstance,
    ) -> None:
        if self.directory is None:
            return
        validate_snapshot_owners(
            self.directory,
            tenant_id=instance.tenant_id,
            instance_owner_person_id=instance.owner_person_id,
            node_owner_person_ids=tuple(
                node.owner_person_id for node in instance.snapshot.nodes
            ),
        )

    def preview_node_restart(
        self,
        tenant_id: str,
        instance_id: str,
        node_key: str,
        *,
        actor_person_id: str,
    ) -> RestartPreview:
        """Persist a short-lived preview without changing the aggregate."""

        instance = self.repository.get(tenant_id, instance_id)
        self._require_instance_owner(instance, actor_person_id)
        affected = affected_restart_node_keys(instance, node_key)
        return self._add_restart_preview(
            instance,
            actor_person_id=actor_person_id,
            scope=RestartScope.NODE,
            node_key=node_key,
            affected_node_keys=affected,
        )

    def preview_instance_restart(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        actor_person_id: str,
    ) -> RestartPreview:
        """Persist a short-lived full-graph restart preview."""

        instance = self.repository.get(tenant_id, instance_id)
        self._require_instance_owner(instance, actor_person_id)
        affected = affected_instance_restart_node_keys(instance)
        return self._add_restart_preview(
            instance,
            actor_person_id=actor_person_id,
            scope=RestartScope.INSTANCE,
            node_key=None,
            affected_node_keys=affected,
        )

    def _add_restart_preview(
        self,
        instance: WorkflowInstance,
        *,
        actor_person_id: str,
        scope: RestartScope,
        node_key: str | None,
        affected_node_keys: tuple[str, ...],
    ) -> RestartPreview:
        now = self.clock()
        preview = RestartPreview(
            id=self.id_factory(),
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            actor_person_id=actor_person_id,
            node_key=node_key,
            affected_node_keys=affected_node_keys,
            expected_instance_version=instance.version,
            graph_revision=instance.graph_revision,
            created_at=now,
            expires_at=now + self.restart_preview_ttl,
            scope=scope,
        )
        self.repository.add_restart_preview(preview)
        return preview

    def confirm_node_restart(
        self,
        tenant_id: str,
        preview_id: str,
        *,
        actor_person_id: str,
    ) -> RestartConfirmation:
        """Consume a node-scoped preview for backwards-compatible callers."""

        return self._confirm_restart(
            tenant_id,
            preview_id,
            actor_person_id=actor_person_id,
            expected_scope=RestartScope.NODE,
        )

    def confirm_restart(
        self,
        tenant_id: str,
        preview_id: str,
        *,
        actor_person_id: str,
    ) -> RestartConfirmation:
        """Consume either explicit restart scope atomically."""

        return self._confirm_restart(
            tenant_id,
            preview_id,
            actor_person_id=actor_person_id,
        )

    def _confirm_restart(
        self,
        tenant_id: str,
        preview_id: str,
        *,
        actor_person_id: str,
        expected_scope: RestartScope | None = None,
    ) -> RestartConfirmation:
        """Consume one preview and atomically create fresh Attempts."""

        preview = self.repository.get_restart_preview(tenant_id, preview_id)
        if expected_scope is not None and preview.scope != expected_scope:
            raise StaleRestartPreviewError("restart preview scope does not match")
        if preview.actor_person_id != actor_person_id:
            raise AuthorizationError("only the preview actor may confirm restart")
        instance = self.repository.get(tenant_id, preview.instance_id)
        self._require_instance_owner(instance, actor_person_id)
        if preview.consumed_at is not None:
            return RestartConfirmation(
                instance=instance,
                preview=preview,
                already_applied=True,
            )
        now = self.clock()
        if now >= preview.expires_at:
            raise RestartPreviewExpiredError(preview.id)
        expected_version = instance.version
        old_attempt_nos = {
            node_key: instance.nodes[node_key].current_attempt_no
            for node_key in preview.affected_node_keys
        }
        old_human_work = {
            node_key: (
                instance.nodes[node_key].executor == ExecutorKind.HUMAN
                or instance.current_attempt(node_key).status.value
                == NodeStatus.WAITING_HUMAN.value
                or instance.current_attempt(node_key).submitted_by_person_id is not None
            )
            for node_key in preview.affected_node_keys
        }
        apply_restart(instance, preview, now=now)
        audit_event_type = (
            "instance.node_restarted"
            if preview.scope == RestartScope.NODE
            else "instance.restarted"
        )
        audit_node_key = preview.node_key
        audit = self._audit(
            instance,
            audit_event_type,
            actor_person_id=actor_person_id,
            correlation_id=preview.id,
            aggregate_version=expected_version + 1,
            now=now,
            node_key=audit_node_key,
            attempt_no=(
                instance.nodes[audit_node_key].current_attempt_no
                if audit_node_key is not None
                else None
            ),
            payload={
                "scope": preview.scope.value,
                "affected_node_keys": preview.affected_node_keys,
                "graph_revision": preview.graph_revision,
                "preview_instance_version": preview.expected_instance_version,
            },
        )
        outbox = tuple(
            self._outbox(
                instance,
                event_type="node.projection_sync_requested",
                aggregate_type="node_instance",
                aggregate_id=instance.nodes[node_key].id,
                aggregate_version=instance.nodes[node_key].version,
                payload={
                    "instance_id": instance.id,
                    "node_key": node_key,
                    "attempt_no": old_attempt_nos[node_key],
                    "status": "canceled",
                },
                now=now,
            )
            for node_key in preview.affected_node_keys
            if old_human_work[node_key]
        )
        try:
            saved = self.repository.save_restart(
                instance,
                preview=preview,
                expected_version=expected_version,
                consumed_at=now,
                audit_events=(audit,),
                outbox_events=outbox,
            )
        except ConcurrentUpdateError as exc:
            raise StaleRestartPreviewError(
                "instance changed while confirming restart"
            ) from exc
        if not saved:
            current_preview = self.repository.get_restart_preview(
                tenant_id,
                preview_id,
            )
            return RestartConfirmation(
                instance=self.repository.get(tenant_id, preview.instance_id),
                preview=current_preview,
                already_applied=True,
            )
        return RestartConfirmation(
            instance=self.repository.get(tenant_id, instance.id),
            preview=self.repository.get_restart_preview(tenant_id, preview_id),
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

    @staticmethod
    def _require_active(instance: WorkflowInstance) -> None:
        if instance.status not in {InstanceStatus.RUNNING, InstanceStatus.PAUSED}:
            raise TransitionError(f"instance is not running: {instance.id}")
