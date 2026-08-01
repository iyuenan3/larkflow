"""One-tick runtime worker for durable workflow execution."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .model import (
    ExecutorKind,
    FrozenDict,
    NodeActivation,
    QualityResult,
)
from .repository import ConcurrentUpdateError, WorkflowRepository
from .runner import (
    ClaimExpiredError,
    InvalidClaimError,
    StaleAttemptError,
)
from .service import WorkflowService
from .transitions import TransitionError


@dataclass(frozen=True)
class ExecutionRequest:
    tenant_id: str
    instance_id: str
    node_key: str
    attempt_id: str
    attempt_no: int
    owner_person_id: str
    executor: ExecutorKind
    work: Mapping[str, Any]
    input_snapshot: Mapping[str, Any]
    expected_node_version: int
    claim_token: str
    claim_expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "executor", ExecutorKind(self.executor))
        object.__setattr__(self, "work", FrozenDict(self.work))
        object.__setattr__(self, "input_snapshot", FrozenDict(self.input_snapshot))

    @property
    def idempotency_key(self) -> str:
        return f"{self.tenant_id}:{self.attempt_id}"


@dataclass(frozen=True)
class ExecutionResult:
    result: Mapping[str, Any]
    quality_result: QualityResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", FrozenDict(self.result))


class AutomatedExecutor(Protocol):
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        ...


@dataclass(frozen=True)
class WorkflowWorkerReport:
    candidates: int = 0
    human_dispatched: int = 0
    automated_claimed: int = 0
    recovered: int = 0
    completed: int = 0
    failed: int = 0
    conflicts: int = 0
    stale_results: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class WorkflowWorker:
    """Find due instances, commit claims, then call external executors."""

    def __init__(
        self,
        service: WorkflowService,
        repository: WorkflowRepository,
        *,
        tenant_id: str,
        worker_id: str,
        executors: Mapping[ExecutorKind, AutomatedExecutor],
        clock: Callable[[], datetime] | None = None,
        candidate_limit: int = 100,
    ) -> None:
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        if ExecutorKind.HUMAN in executors:
            raise ValueError("human work cannot be registered as an automated executor")
        self.service = service
        self.repository = repository
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.executors = {
            ExecutorKind(kind): executor for kind, executor in executors.items()
        }
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.candidate_limit = candidate_limit

    def run_once(self) -> WorkflowWorkerReport:
        now = self.clock()
        instance_ids = self.repository.runnable_instance_ids(
            self.tenant_id,
            now=now,
            limit=self.candidate_limit,
        )
        human_dispatched = 0
        automated_claimed = 0
        recovered = 0
        completed = 0
        failed = 0
        conflicts = 0
        stale_results = 0
        errors: list[str] = []

        remaining = 1
        for instance_id in instance_ids:
            try:
                activations = self.service.dispatch_due(
                    self.tenant_id,
                    instance_id,
                    worker_id=self.worker_id,
                    max_automated=remaining,
                )
            except ConcurrentUpdateError:
                conflicts += 1
                continue

            for activation in activations:
                if activation.executor == ExecutorKind.HUMAN:
                    human_dispatched += 1
                    continue
                automated_claimed += 1
                recovered += int(activation.recovered)
                remaining -= 1
                request = self._execution_request(activation)
                executor = self.executors.get(activation.executor)
                if executor is None:
                    error = f"no executor registered for {activation.executor.value}"
                    if self._fail(activation, "executor_not_configured", error):
                        failed += 1
                    else:
                        stale_results += 1
                    errors.append(error)
                    continue
                try:
                    result = executor.execute(request)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    if self._fail(activation, "executor_error", error):
                        failed += 1
                    else:
                        stale_results += 1
                    errors.append(error)
                    continue
                if self._complete(activation, result):
                    completed += 1
                else:
                    stale_results += 1

        return WorkflowWorkerReport(
            candidates=len(instance_ids),
            human_dispatched=human_dispatched,
            automated_claimed=automated_claimed,
            recovered=recovered,
            completed=completed,
            failed=failed,
            conflicts=conflicts,
            stale_results=stale_results,
            errors=tuple(errors),
        )

    def _execution_request(self, activation: NodeActivation) -> ExecutionRequest:
        instance = self.service.get(self.tenant_id, activation.instance_id)
        attempt = instance.current_attempt(activation.node_key)
        if activation.claim_token is None or activation.claim_expires_at is None:
            raise InvalidClaimError("automated activation is missing its claim lease")
        return ExecutionRequest(
            tenant_id=self.tenant_id,
            instance_id=activation.instance_id,
            node_key=activation.node_key,
            attempt_id=activation.attempt_id,
            attempt_no=activation.attempt_no,
            owner_person_id=activation.owner_person_id,
            executor=activation.executor,
            work=instance.snapshot.node(activation.node_key).work,
            input_snapshot=attempt.input_snapshot,
            expected_node_version=activation.expected_node_version,
            claim_token=activation.claim_token,
            claim_expires_at=activation.claim_expires_at,
        )

    def _complete(
        self,
        activation: NodeActivation,
        result: ExecutionResult,
    ) -> bool:
        try:
            self.service.complete_automated(
                self.tenant_id,
                activation.instance_id,
                activation.node_key,
                attempt_no=activation.attempt_no,
                expected_node_version=activation.expected_node_version,
                claim_token=activation.claim_token or "",
                result=result.result,
                quality_result=result.quality_result,
                worker_id=self.worker_id,
            )
        except self._stale_result_errors():
            return False
        return True

    def _fail(
        self,
        activation: NodeActivation,
        error_code: str,
        error_message: str,
    ) -> bool:
        try:
            self.service.fail_automated(
                self.tenant_id,
                activation.instance_id,
                activation.node_key,
                attempt_no=activation.attempt_no,
                expected_node_version=activation.expected_node_version,
                claim_token=activation.claim_token or "",
                error_code=error_code,
                error_message=error_message,
                worker_id=self.worker_id,
            )
        except self._stale_result_errors():
            return False
        return True

    @staticmethod
    def _stale_result_errors() -> tuple[type[Exception], ...]:
        return (
            ClaimExpiredError,
            ConcurrentUpdateError,
            InvalidClaimError,
            StaleAttemptError,
            TransitionError,
        )
