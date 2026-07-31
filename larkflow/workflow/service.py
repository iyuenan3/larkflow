"""Application service for the first central workflow implementation slice."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .graph import validate_snapshot
from .model import (
    InstanceSnapshot,
    InstanceStatus,
    NodeActivation,
    NodeStatus,
    QualityResult,
    WorkflowInstance,
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.scheduler = scheduler or Scheduler()
        self.runner = runner or NodeRunner()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create_draft(
        self,
        *,
        instance_id: str,
        tenant_id: str,
        owner_person_id: str,
        snapshot: InstanceSnapshot,
    ) -> WorkflowInstance:
        if not instance_id.strip():
            raise ValueError("instance_id is required")
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not owner_person_id.strip():
            raise ValueError("owner_person_id is required")
        validate_snapshot(snapshot)
        instance = WorkflowInstance(
            id=instance_id,
            tenant_id=tenant_id,
            owner_person_id=owner_person_id,
            snapshot=snapshot,
            created_at=self.clock(),
        )
        self.repository.add(instance)
        return self.repository.get(instance_id)

    def confirm_draft(
        self,
        instance_id: str,
        *,
        actor_person_id: str,
    ) -> WorkflowInstance:
        instance = self.repository.get(instance_id)
        expected_version = instance.version
        self._require_instance_owner(instance, actor_person_id)
        if instance.status != InstanceStatus.DRAFT:
            raise TransitionError(f"instance is not a draft: {instance_id}")
        self.scheduler.confirm(instance, now=self.clock())
        self.repository.save(instance, expected_version=expected_version)
        return self.repository.get(instance_id)

    def discard_draft(
        self,
        instance_id: str,
        *,
        actor_person_id: str,
    ) -> WorkflowInstance:
        instance = self.repository.get(instance_id)
        expected_version = instance.version
        self._require_instance_owner(instance, actor_person_id)
        transition_instance(instance, InstanceStatus.DISCARDED, now=self.clock())
        self.repository.save(instance, expected_version=expected_version)
        return self.repository.get(instance_id)

    def dispatch_ready(self, instance_id: str) -> tuple[NodeActivation, ...]:
        instance = self.repository.get(instance_id)
        expected_version = instance.version
        if instance.status != InstanceStatus.RUNNING:
            raise TransitionError(f"instance is not running: {instance_id}")
        now = self.clock()
        activations = tuple(
            self.runner.activate(instance, spec.key, now=now)
            for spec in instance.snapshot.nodes
            if instance.nodes[spec.key].status == NodeStatus.READY
        )
        if activations:
            self.repository.save(instance, expected_version=expected_version)
        return activations

    def submit_human(
        self,
        instance_id: str,
        node_key: str,
        *,
        actor_person_id: str,
        attempt_no: int,
        expected_node_version: int,
        result: Mapping[str, Any],
        quality_result: QualityResult | None = None,
    ) -> WorkflowInstance:
        instance = self.repository.get(instance_id)
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
        self.repository.save(instance, expected_version=expected_version)
        return self.repository.get(instance_id)

    def complete_automated(
        self,
        instance_id: str,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        result: Mapping[str, Any],
        quality_result: QualityResult | None = None,
    ) -> WorkflowInstance:
        instance = self.repository.get(instance_id)
        expected_version = instance.version
        self._require_running(instance)
        now = self.clock()
        self.runner.complete_automated(
            instance,
            node_key,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
            claim_token=claim_token,
            result=result,
            quality_result=quality_result,
            now=now,
        )
        self.scheduler.unlock_after(instance, node_key, now=now)
        self.repository.save(instance, expected_version=expected_version)
        return self.repository.get(instance_id)

    def fail_automated(
        self,
        instance_id: str,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        error_code: str,
        error_message: str,
    ) -> WorkflowInstance:
        instance = self.repository.get(instance_id)
        expected_version = instance.version
        self._require_running(instance)
        now = self.clock()
        self.runner.fail_automated(
            instance,
            node_key,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
            claim_token=claim_token,
            error_code=error_code,
            error_message=error_message,
            now=now,
        )
        self.scheduler.fail_instance(instance, now=now)
        self.repository.save(instance, expected_version=expected_version)
        return self.repository.get(instance_id)

    def get(self, instance_id: str) -> WorkflowInstance:
        return self.repository.get(instance_id)

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
