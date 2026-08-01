"""Persistent runtime worker and automated claim recovery tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from larkflow.workflow import (
    AttemptStatus,
    AutomatedExecutor,
    ExecutionRequest,
    ExecutionResult,
    ExecutorKind,
    InMemoryWorkflowRepository,
    InstanceSnapshot,
    InstanceStatus,
    InvalidClaimError,
    NodeRunner,
    NodeSpec,
    NodeStatus,
    StaleAttemptError,
    WorkflowService,
    WorkflowWorker,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
TENANT = "tenant_runtime"


def node_work(*, tool_kind: str | None = None) -> dict:
    work = {
        "objective": "Produce the result",
        "inputs": [],
        "outputs": [{"id": "result", "type": "data"}],
        "acceptance": ["The result exists"],
    }
    if tool_kind is not None:
        work["tool"] = {"kind": tool_kind, "args": {}}
    return work


def automated_snapshot() -> InstanceSnapshot:
    return InstanceSnapshot(
        goal="run one automated node",
        nodes=(
            NodeSpec(
                "generate",
                "Generate",
                "person_owner",
                "agent",
                work={**node_work(), "prompt": "Generate the result"},
            ),
        ),
    )


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingExecutor(AutomatedExecutor):
    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"value": "done"}
        self.requests: list[ExecutionRequest] = []
        self.on_execute = None

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        if self.on_execute is not None:
            self.on_execute(request)
        return ExecutionResult(result=self.result)


class WorkerCrash(BaseException):
    pass


class CrashExecutor(AutomatedExecutor):
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        raise WorkerCrash("process stopped after committing the claim")


class FailingExecutor(AutomatedExecutor):
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        raise RuntimeError("provider rejected the request")


def build_runtime(
    *,
    clock: Clock,
    snapshot: InstanceSnapshot | None = None,
) -> tuple[WorkflowService, InMemoryWorkflowRepository]:
    repository = InMemoryWorkflowRepository()
    tokens = iter(("claim-first", "claim-recovered", "claim-third"))
    service = WorkflowService(
        repository,
        runner=NodeRunner(
            claim_ttl=timedelta(minutes=5),
            token_factory=lambda: next(tokens),
        ),
        clock=clock,
    )
    service.create_draft(
        instance_id="instance_runtime",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot or automated_snapshot(),
    )
    service.confirm_draft(
        TENANT,
        "instance_runtime",
        actor_person_id="person_owner",
    )
    return service, repository


def worker(
    service: WorkflowService,
    repository: InMemoryWorkflowRepository,
    executor: AutomatedExecutor,
    *,
    clock: Clock,
    worker_id: str,
) -> WorkflowWorker:
    return WorkflowWorker(
        service,
        repository,
        tenant_id=TENANT,
        worker_id=worker_id,
        executors={ExecutorKind.AGENT: executor},
        clock=clock,
    )


def test_worker_commits_claim_before_calling_external_executor():
    clock = Clock()
    service, repository = build_runtime(clock=clock)
    executor = RecordingExecutor()

    def assert_committed(request: ExecutionRequest) -> None:
        persisted = repository.get(TENANT, "instance_runtime")
        attempt = persisted.current_attempt("generate")
        assert persisted.nodes["generate"].status == NodeStatus.RUNNING
        assert attempt.status == AttemptStatus.RUNNING
        assert attempt.claim_token == request.claim_token
        assert attempt.claimed_by == "worker_1"

    executor.on_execute = assert_committed
    report = worker(
        service,
        repository,
        executor,
        clock=clock,
        worker_id="worker_1",
    ).run_once()

    assert report.automated_claimed == 1
    assert report.completed == 1
    assert report.failed == 0
    assert report.recovered == 0
    assert executor.requests[0].idempotency_key.endswith(":attempt:1")
    assert service.get(TENANT, "instance_runtime").status == InstanceStatus.DONE


def test_worker_receives_committed_upstream_results_in_the_input_snapshot():
    clock = Clock()
    snapshot = InstanceSnapshot(
        inputs={"topic": "launch"},
        nodes=(
            NodeSpec("review", "Review", "reviewer", "human", work=node_work()),
            NodeSpec(
                "generate",
                "Generate",
                "person_owner",
                "agent",
                deps=("review",),
                work={**node_work(), "prompt": "Use the review"},
            ),
        ),
    )
    service, repository = build_runtime(clock=clock, snapshot=snapshot)
    human = service.dispatch_due(
        TENANT,
        "instance_runtime",
        worker_id="worker_1",
        max_automated=1,
    )[0]
    service.submit_human(
        TENANT,
        "instance_runtime",
        "review",
        actor_person_id="reviewer",
        attempt_no=human.attempt_no,
        expected_node_version=human.expected_node_version,
        result={"approved": True},
    )
    executor = RecordingExecutor()

    report = worker(
        service,
        repository,
        executor,
        clock=clock,
        worker_id="worker_1",
    ).run_once()

    assert report.completed == 1
    request = executor.requests[0]
    assert request.input_snapshot["instance_inputs"]["topic"] == "launch"
    assert request.input_snapshot["dependencies"]["review"]["approved"] is True
    with pytest.raises(TypeError):
        request.input_snapshot["dependencies"]["review"]["approved"] = False


def test_crash_after_claim_is_recovered_with_same_attempt_and_new_token():
    clock = Clock()
    service, repository = build_runtime(clock=clock)
    crashing = CrashExecutor()

    with pytest.raises(WorkerCrash):
        worker(
            service,
            repository,
            crashing,
            clock=clock,
            worker_id="worker_1",
        ).run_once()

    crashed_request = crashing.requests[0]
    stranded = service.get(TENANT, "instance_runtime")
    stranded_attempt = stranded.current_attempt("generate")
    assert stranded.nodes["generate"].status == NodeStatus.RUNNING
    assert stranded_attempt.claimed_by == "worker_1"
    assert stranded_attempt.claim_token == "claim-first"

    before_expiry = RecordingExecutor()
    report = worker(
        service,
        repository,
        before_expiry,
        clock=clock,
        worker_id="worker_2",
    ).run_once()
    assert report.candidates == 0
    assert before_expiry.requests == []

    clock.now += timedelta(minutes=5)
    recovered = RecordingExecutor()
    report = worker(
        service,
        repository,
        recovered,
        clock=clock,
        worker_id="worker_2",
    ).run_once()

    assert report.recovered == 1
    assert report.completed == 1
    assert recovered.requests[0].attempt_id == crashed_request.attempt_id
    assert recovered.requests[0].claim_token == "claim-recovered"
    assert recovered.requests[0].claim_token != crashed_request.claim_token
    finished = service.get(TENANT, "instance_runtime")
    assert finished.status == InstanceStatus.DONE
    assert finished.current_attempt("generate").claimed_by is None

    audit_types = [
        event.event_type
        for event in repository.audit_log(TENANT, "instance_runtime")
    ]
    assert "node.claim_recovered" in audit_types


def test_recovery_invalidates_the_old_worker_before_accepting_new_result():
    clock = Clock()
    service, _ = build_runtime(clock=clock)
    first = service.dispatch_due(
        TENANT,
        "instance_runtime",
        worker_id="worker_1",
        max_automated=1,
    )[0]
    clock.now += timedelta(minutes=5)
    recovered = service.dispatch_due(
        TENANT,
        "instance_runtime",
        worker_id="worker_2",
        max_automated=1,
    )[0]

    assert recovered.recovered is True
    assert recovered.attempt_id == first.attempt_id
    assert recovered.expected_node_version > first.expected_node_version
    with pytest.raises(StaleAttemptError):
        service.complete_automated(
            TENANT,
            "instance_runtime",
            "generate",
            attempt_no=first.attempt_no,
            expected_node_version=first.expected_node_version,
            claim_token=first.claim_token or "",
            result={"value": "late"},
            worker_id="worker_1",
        )

    finished = service.complete_automated(
        TENANT,
        "instance_runtime",
        "generate",
        attempt_no=recovered.attempt_no,
        expected_node_version=recovered.expected_node_version,
        claim_token=recovered.claim_token or "",
        result={"value": "accepted"},
        worker_id="worker_2",
    )
    assert finished.status == InstanceStatus.DONE


def test_claim_token_cannot_be_used_under_a_different_worker_identity():
    clock = Clock()
    service, _ = build_runtime(clock=clock)
    activation = service.dispatch_due(
        TENANT,
        "instance_runtime",
        worker_id="worker_1",
        max_automated=1,
    )[0]

    with pytest.raises(InvalidClaimError, match="does not own"):
        service.complete_automated(
            TENANT,
            "instance_runtime",
            "generate",
            attempt_no=activation.attempt_no,
            expected_node_version=activation.expected_node_version,
            claim_token=activation.claim_token or "",
            result={"value": "spoofed"},
            worker_id="worker_other",
        )


def test_executor_error_fails_the_instance_and_clears_the_claim():
    clock = Clock()
    service, repository = build_runtime(clock=clock)
    report = worker(
        service,
        repository,
        FailingExecutor(),
        clock=clock,
        worker_id="worker_1",
    ).run_once()

    assert report.failed == 1
    assert report.errors == ("RuntimeError: provider rejected the request",)
    failed = service.get(TENANT, "instance_runtime")
    assert failed.status == InstanceStatus.FAILED
    assert failed.current_attempt("generate").claimed_by is None
    assert failed.current_attempt("generate").claim_token is None


def test_dispatch_due_limits_automated_claims_but_dispatches_all_humans():
    clock = Clock()
    snapshot = InstanceSnapshot(
        nodes=(
            NodeSpec("review", "Review", "reviewer", "human", work=node_work()),
            NodeSpec(
                "agent_one",
                "Agent one",
                "owner",
                "agent",
                work={**node_work(), "prompt": "One"},
            ),
            NodeSpec(
                "tool_two",
                "Tool two",
                "owner",
                "tool",
                work=node_work(tool_kind="tool.two"),
            ),
        )
    )
    service, _ = build_runtime(clock=clock, snapshot=snapshot)

    activations = service.dispatch_due(
        TENANT,
        "instance_runtime",
        worker_id="worker_1",
        max_automated=1,
    )

    assert [item.node_key for item in activations] == ["review", "agent_one"]
    current = service.get(TENANT, "instance_runtime")
    assert current.nodes["review"].status == NodeStatus.WAITING_HUMAN
    assert current.nodes["agent_one"].status == NodeStatus.RUNNING
    assert current.nodes["tool_two"].status == NodeStatus.READY


def test_runnable_scan_is_tenant_scoped_and_includes_expired_claims():
    clock = Clock()
    service, repository = build_runtime(clock=clock)

    assert repository.runnable_instance_ids(TENANT, now=clock.now) == (
        "instance_runtime",
    )
    assert repository.runnable_instance_ids("tenant_other", now=clock.now) == ()

    service.dispatch_due(
        TENANT,
        "instance_runtime",
        worker_id="worker_1",
        max_automated=1,
    )
    assert repository.runnable_instance_ids(TENANT, now=clock.now) == ()

    clock.now += timedelta(minutes=5)
    assert repository.runnable_instance_ids(TENANT, now=clock.now) == (
        "instance_runtime",
    )
