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


class NodeRunner:
    """Claims work and accepts results without performing external I/O."""

    def __init__(
        self,
        *,
        claim_ttl: timedelta = timedelta(minutes=5),
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.claim_ttl = claim_ttl
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(24))

    def activate(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        now: datetime,
    ) -> NodeActivation:
        node = instance.nodes[node_key]
        attempt = instance.current_attempt(node_key)
        if node.status != NodeStatus.READY:
            raise TransitionError(f"node is not ready: {node_key}")

        claim_token: str | None = None
        claim_expires_at: datetime | None = None
        if node.executor == ExecutorKind.HUMAN:
            transition_node(node, NodeStatus.WAITING_HUMAN, now=now)
            transition_attempt(attempt, AttemptStatus.WAITING_HUMAN, now=now)
        else:
            claim_token = self.token_factory()
            claim_expires_at = now + self.claim_ttl
            transition_node(node, NodeStatus.RUNNING, now=now)
            transition_attempt(attempt, AttemptStatus.RUNNING, now=now)
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
            claim_token=claim_token,
            claim_expires_at=claim_expires_at,
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
        node = instance.nodes[node_key]
        if actor_person_id != node.owner_person_id:
            raise AuthorizationError(f"only the node owner may submit: {node_key}")
        attempt = self._current_attempt(instance, node_key, attempt_no)
        self._expect_node(node_key, node.version, expected_node_version)
        if node.status != NodeStatus.WAITING_HUMAN:
            raise TransitionError(f"node is not waiting for a human: {node_key}")

        transition_node(node, NodeStatus.DONE, now=now)
        transition_attempt(attempt, AttemptStatus.DONE, now=now)
        attempt.result = FrozenDict(result)
        attempt.quality_result = quality_result
        attempt.submitted_by_person_id = actor_person_id

    def complete_automated(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        result: Mapping[str, Any],
        quality_result: QualityResult | None,
        now: datetime,
    ) -> None:
        node = instance.nodes[node_key]
        if node.executor == ExecutorKind.HUMAN:
            raise TransitionError(f"human node cannot submit an automated claim: {node_key}")
        attempt = self._current_attempt(instance, node_key, attempt_no)
        self._expect_node(node_key, node.version, expected_node_version)
        self._expect_claim(attempt.claim_token, claim_token)
        if attempt.claim_expires_at is not None and now > attempt.claim_expires_at:
            raise ClaimExpiredError(f"claim expired: {node_key}")
        if node.status != NodeStatus.RUNNING:
            raise TransitionError(f"node is not running: {node_key}")

        transition_node(node, NodeStatus.DONE, now=now)
        transition_attempt(attempt, AttemptStatus.DONE, now=now)
        attempt.result = FrozenDict(result)
        attempt.quality_result = quality_result

    def fail_automated(
        self,
        instance: WorkflowInstance,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
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
        if attempt.claim_expires_at is not None and now > attempt.claim_expires_at:
            raise ClaimExpiredError(f"claim expired: {node_key}")
        if node.status != NodeStatus.RUNNING:
            raise TransitionError(f"node is not running: {node_key}")

        transition_node(node, NodeStatus.FAILED, now=now)
        transition_attempt(attempt, AttemptStatus.FAILED, now=now)
        attempt.error_code = error_code
        attempt.error_message = error_message

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
