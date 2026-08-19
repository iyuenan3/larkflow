"""Central coordinator for Human, Agent, and Tool node attempts."""
from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from .model import (
    AttemptStatus,
    ExecutorKind,
    FrozenDict,
    NodeActivation,
    NodeAttempt,
    NodeStatus,
    QualityResult,
    WorkflowInstance,
)
from .transitions import TransitionError, transition_attempt, transition_node


class AuthorizationError(PermissionError):
    pass


class StaleAttemptError(RuntimeError):
    pass


class InvalidClaimError(RuntimeError):
    pass


class ClaimExpiredError(RuntimeError):
    pass


class ClaimNotExpiredError(RuntimeError):
    pass


class NodeRunner:
    """Claims work and accepts results without performing external I/O."""

    def __init__(
        self,
        *,
        claim_ttl: timedelta = timedelta(minutes=5),
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        self.claim_ttl = claim_ttl
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(24))

    def activate(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        worker_id: str | None = None,
        now: datetime,
    ) -> NodeActivation:
        node = instance.nodes[node_key]
        attempt = instance.current_attempt(node_key)
        if node.status != NodeStatus.READY:
            raise TransitionError(f"node is not ready: {node_key}")

        attempt.input_snapshot = self._capture_input_snapshot(instance, node_key)
        claim_token: str | None = None
        claim_expires_at: datetime | None = None
        if node.executor == ExecutorKind.HUMAN:
            transition_node(node, NodeStatus.WAITING_HUMAN, now=now)
            transition_attempt(attempt, AttemptStatus.WAITING_HUMAN, now=now)
        else:
            worker_id = self._require_worker_id(worker_id)
            claim_token = self.token_factory()
            claim_expires_at = now + self.claim_ttl
            transition_node(node, NodeStatus.RUNNING, now=now)
            transition_attempt(attempt, AttemptStatus.RUNNING, now=now)
            attempt.claimed_by = worker_id
            attempt.claim_token = claim_token
            attempt.claim_expires_at = claim_expires_at

        return NodeActivation(
            instance_id=instance.id,
            node_key=node_key,
            node_instance_id=node.id,
            attempt_id=attempt.id,
            attempt_no=attempt.attempt_no,
            owner_person_id=node.owner_person_id,
            executor=node.executor,
            status=node.status,
            expected_node_version=node.version,
            claimed_by=attempt.claimed_by,
            claim_token=claim_token,
            claim_expires_at=claim_expires_at,
        )

    def reclaim_expired(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        worker_id: str,
        now: datetime,
    ) -> NodeActivation:
        node = instance.nodes[node_key]
        if node.executor == ExecutorKind.HUMAN:
            raise TransitionError(f"human node has no automated claim: {node_key}")
        attempt = instance.current_attempt(node_key)
        if node.status != NodeStatus.RUNNING or attempt.status != AttemptStatus.RUNNING:
            raise TransitionError(f"node has no running automated attempt: {node_key}")
        if attempt.claim_expires_at is None or now < attempt.claim_expires_at:
            raise ClaimNotExpiredError(f"claim has not expired: {node_key}")

        worker_id = self._require_worker_id(worker_id)
        claim_token = self.token_factory()
        claim_expires_at = now + self.claim_ttl
        node.version += 1
        attempt.claimed_by = worker_id
        attempt.claim_token = claim_token
        attempt.claim_expires_at = claim_expires_at
        return NodeActivation(
            instance_id=instance.id,
            node_key=node_key,
            node_instance_id=node.id,
            attempt_id=attempt.id,
            attempt_no=attempt.attempt_no,
            owner_person_id=node.owner_person_id,
            executor=node.executor,
            status=node.status,
            expected_node_version=node.version,
            claimed_by=worker_id,
            claim_token=claim_token,
            claim_expires_at=claim_expires_at,
            recovered=True,
        )

    @staticmethod
    def is_reclaimable(
        instance: WorkflowInstance,
        node_key: str,
        *,
        now: datetime,
    ) -> bool:
        node = instance.nodes[node_key]
        if node.executor == ExecutorKind.HUMAN or node.status != NodeStatus.RUNNING:
            return False
        attempt = instance.current_attempt(node_key)
        return (
            attempt.status == AttemptStatus.RUNNING
            and attempt.claim_expires_at is not None
            and attempt.claim_expires_at <= now
        )

    def submit_human(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        actor_person_id: str,
        attempt_no: int,
        expected_node_version: int,
        result: Mapping[str, Any],
        quality_result: QualityResult | None,
        now: datetime,
    ) -> None:
        self.check_human_submission(
            instance,
            node_key,
            actor_person_id=actor_person_id,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
        )
        node = instance.nodes[node_key]
        attempt = instance.current_attempt(node_key)

        transition_node(node, NodeStatus.DONE, now=now)
        transition_attempt(attempt, AttemptStatus.DONE, now=now)
        attempt.result = FrozenDict(result)
        attempt.quality_result = quality_result
        attempt.submitted_by_person_id = actor_person_id

    def check_human_submission(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        actor_person_id: str,
        attempt_no: int,
        expected_node_version: int,
    ) -> None:
        """Validate Human authority and attempt state without mutating it."""

        node = instance.nodes[node_key]
        if actor_person_id != node.owner_person_id:
            raise AuthorizationError(f"only the node owner may submit: {node_key}")
        self._current_attempt(instance, node_key, attempt_no)
        self._expect_node(node_key, node.version, expected_node_version)
        if node.status != NodeStatus.WAITING_HUMAN:
            raise TransitionError(f"node is not waiting for a human: {node_key}")

    def reject_human(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        actor_person_id: str,
        attempt_no: int,
        expected_node_version: int,
        result: Mapping[str, Any],
        quality_result: QualityResult,
        now: datetime,
    ) -> None:
        """Record an explicit Human rejection without rewriting prior work."""

        node = instance.nodes[node_key]
        if actor_person_id != node.owner_person_id:
            raise AuthorizationError(f"only the node owner may reject: {node_key}")
        attempt = self._current_attempt(instance, node_key, attempt_no)
        self._expect_node(node_key, node.version, expected_node_version)
        if node.status != NodeStatus.WAITING_HUMAN:
            raise TransitionError(f"node is not waiting for a human: {node_key}")
        if quality_result.verdict.value != "fail":
            raise ValueError("Human rejection requires failed quality evidence")

        transition_node(node, NodeStatus.FAILED, now=now)
        transition_attempt(attempt, AttemptStatus.FAILED, now=now)
        attempt.result = FrozenDict(result)
        attempt.quality_result = quality_result
        attempt.submitted_by_person_id = actor_person_id
        attempt.error_code = "human_rejected"
        attempt.error_message = "Node Owner rejected the submitted result"

    def complete_automated(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        worker_id: str,
        result: Mapping[str, Any],
        quality_result: QualityResult | None,
        now: datetime,
    ) -> None:
        self.check_automated_completion(
            instance,
            node_key,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
            claim_token=claim_token,
            worker_id=worker_id,
            now=now,
        )
        node = instance.nodes[node_key]
        attempt = instance.current_attempt(node_key)

        transition_node(node, NodeStatus.DONE, now=now)
        transition_attempt(attempt, AttemptStatus.DONE, now=now)
        attempt.result = FrozenDict(result)
        attempt.quality_result = quality_result
        attempt.claimed_by = None
        attempt.claim_token = None
        attempt.claim_expires_at = None

    def check_automated_completion(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        worker_id: str,
        now: datetime,
    ) -> None:
        """Validate an automated claim without committing its result."""

        node = instance.nodes[node_key]
        if node.executor == ExecutorKind.HUMAN:
            raise TransitionError(f"human node cannot submit an automated claim: {node_key}")
        attempt = self._current_attempt(instance, node_key, attempt_no)
        self._expect_node(node_key, node.version, expected_node_version)
        self._expect_claim(attempt.claim_token, claim_token)
        self._expect_worker(attempt.claimed_by, worker_id)
        if attempt.claim_expires_at is not None and now >= attempt.claim_expires_at:
            raise ClaimExpiredError(f"claim expired: {node_key}")
        if node.status != NodeStatus.RUNNING:
            raise TransitionError(f"node is not running: {node_key}")

    def renew_automated_claim(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        worker_id: str,
        now: datetime,
    ) -> datetime:
        """Extend a live automated claim without changing its identity."""

        node = instance.nodes[node_key]
        if node.executor == ExecutorKind.HUMAN:
            raise TransitionError(f"human node has no automated claim: {node_key}")
        attempt = self._current_attempt(instance, node_key, attempt_no)
        self._expect_node(node_key, node.version, expected_node_version)
        self._expect_claim(attempt.claim_token, claim_token)
        self._expect_worker(attempt.claimed_by, worker_id)
        if attempt.claim_expires_at is None or now >= attempt.claim_expires_at:
            raise ClaimExpiredError(f"claim expired: {node_key}")
        if (
            node.status != NodeStatus.RUNNING
            or attempt.status != AttemptStatus.RUNNING
        ):
            raise TransitionError(f"node is not running: {node_key}")

        attempt.claim_expires_at = max(
            attempt.claim_expires_at,
            now + self.claim_ttl,
        )
        return attempt.claim_expires_at

    def fail_automated(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        worker_id: str,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> None:
        node = instance.nodes[node_key]
        if node.executor == ExecutorKind.HUMAN:
            raise TransitionError(f"human node cannot fail an automated claim: {node_key}")
        attempt = self._current_attempt(instance, node_key, attempt_no)
        self._expect_node(node_key, node.version, expected_node_version)
        self._expect_claim(attempt.claim_token, claim_token)
        self._expect_worker(attempt.claimed_by, worker_id)
        if attempt.claim_expires_at is not None and now >= attempt.claim_expires_at:
            raise ClaimExpiredError(f"claim expired: {node_key}")
        if node.status != NodeStatus.RUNNING:
            raise TransitionError(f"node is not running: {node_key}")

        transition_node(node, NodeStatus.FAILED, now=now)
        transition_attempt(attempt, AttemptStatus.FAILED, now=now)
        attempt.error_code = error_code
        attempt.error_message = error_message
        attempt.claimed_by = None
        attempt.claim_token = None
        attempt.claim_expires_at = None

    @staticmethod
    def _current_attempt(
        instance: WorkflowInstance,
        node_key: str,
        attempt_no: int,
    ) -> NodeAttempt:
        node = instance.nodes[node_key]
        if attempt_no != node.current_attempt_no:
            raise StaleAttemptError(
                f"node {node_key} is on attempt {node.current_attempt_no}, got {attempt_no}"
            )
        return instance.current_attempt(node_key)

    @staticmethod
    def _expect_node(node_key: str, actual: int, expected: int) -> None:
        if actual != expected:
            raise StaleAttemptError(
                f"node {node_key} expected version {expected}, found {actual}"
            )

    @staticmethod
    def _expect_claim(actual: str | None, supplied: str) -> None:
        if not actual or not secrets.compare_digest(actual, supplied):
            raise InvalidClaimError("claim token does not match the current attempt")

    @staticmethod
    def _expect_worker(actual: str | None, supplied: str) -> None:
        if not actual or not secrets.compare_digest(actual, supplied):
            raise InvalidClaimError("worker does not own the current attempt claim")

    @staticmethod
    def _require_worker_id(worker_id: str | None) -> str:
        if worker_id is None or not worker_id.strip():
            raise ValueError("worker_id is required for automated claims")
        return worker_id

    @staticmethod
    def _capture_input_snapshot(
        instance: WorkflowInstance,
        node_key: str,
    ) -> FrozenDict:
        spec = instance.snapshot.node(node_key)
        pending_snapshot = instance.current_attempt(node_key).input_snapshot
        dependencies: dict[str, Any] = {}
        dependency_provenance: dict[str, dict[str, Any]] = {}
        for dependency in spec.deps:
            dependency_spec = instance.snapshot.node(dependency)
            dependency_attempt = instance.current_attempt(dependency)
            tool_kind = None
            if dependency_spec.executor == ExecutorKind.TOOL:
                tool = dependency_spec.work.get("tool")
                if isinstance(tool, Mapping) and isinstance(tool.get("kind"), str):
                    tool_kind = tool["kind"]
            dependencies[dependency] = dependency_attempt.result
            dependency_provenance[dependency] = {
                "node_key": dependency,
                "executor": dependency_spec.executor.value,
                "tool_kind": tool_kind,
                "attempt_id": dependency_attempt.id,
                "attempt_no": dependency_attempt.attempt_no,
            }
        captured: dict[str, Any] = {
            "instance_inputs": instance.snapshot.inputs,
            "dependencies": dependencies,
            "dependency_provenance": dependency_provenance,
            "work": spec.work,
        }
        rework_feedback = pending_snapshot.get("rework_feedback")
        if isinstance(rework_feedback, Mapping):
            captured["rework_feedback"] = rework_feedback
        return FrozenDict(captured)
