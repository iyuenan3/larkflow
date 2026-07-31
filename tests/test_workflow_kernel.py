"""Behavioral tests for the target central workflow kernel."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from larkflow.workflow import (
    AttemptStatus,
    AuthorizationError,
    ClaimExpiredError,
    ConcurrentUpdateError,
    ExecutorKind,
    GraphValidationError,
    InMemoryWorkflowRepository,
    InstanceSnapshot,
    InstanceStatus,
    InvalidClaimError,
    NodeRunner,
    NodeSpec,
    NodeStatus,
    QualityResult,
    QualityVerdict,
    StaleAttemptError,
    TransitionError,
    WorkflowInstance,
    WorkflowService,
    reachable_downstream,
    topological_order,
    validate_snapshot,
)


NOW = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


def node_work(*, tool_kind: str | None = None) -> dict:
    work = {
        "objective": "Produce the required result",
        "inputs": [],
        "outputs": [{"id": "result", "type": "data", "required": True}],
        "acceptance": ["The required result exists"],
    }
    if tool_kind is not None:
        work["tool"] = {"kind": tool_kind, "args": {}}
    return work


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def linear_snapshot() -> InstanceSnapshot:
    return InstanceSnapshot(
        goal="publish a reviewed brief",
        inputs={"source": {"kind": "draft"}},
        nodes=(
            NodeSpec(
                key="collect_brief",
                title="Collect brief",
                owner_person_id="person_owner",
                executor=ExecutorKind.HUMAN,
                work=node_work(),
            ),
            NodeSpec(
                key="write_draft",
                title="Write draft",
                owner_person_id="person_editor",
                executor=ExecutorKind.AGENT,
                deps=("collect_brief",),
                work={**node_work(), "prompt": "Draft from the approved brief"},
            ),
            NodeSpec(
                key="publish_doc",
                title="Publish document",
                owner_person_id="person_owner",
                executor=ExecutorKind.TOOL,
                deps=("write_draft",),
                work=node_work(tool_kind="document.publish"),
            ),
        ),
    )


def build_service(
    snapshot: InstanceSnapshot | None = None,
    *,
    clock: Clock | None = None,
) -> tuple[WorkflowService, InMemoryWorkflowRepository]:
    repository = InMemoryWorkflowRepository()
    token_values = iter(("claim-agent", "claim-tool", "claim-extra"))
    service = WorkflowService(
        repository,
        runner=NodeRunner(token_factory=lambda: next(token_values)),
        clock=clock or Clock(),
    )
    service.create_draft(
        instance_id="instance_1",
        tenant_id="tenant_1",
        owner_person_id="person_owner",
        snapshot=snapshot or linear_snapshot(),
    )
    return service, repository


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        (
            (NodeSpec("only", "Only", "owner", "human", work=node_work()),),
            "unsupported snapshot schema version",
        ),
        ((), "at least one node"),
        (
            (
                NodeSpec("same", "First", "owner", "human", work=node_work()),
                NodeSpec(
                    "same",
                    "Second",
                    "owner",
                    "tool",
                    work=node_work(tool_kind="test.tool"),
                ),
            ),
            "duplicate node keys",
        ),
        (
            (NodeSpec("Bad-Key", "Bad", "owner", "human", work=node_work()),),
            "lower snake_case",
        ),
        (
            (NodeSpec("missing_owner", "Bad", "", "human", work=node_work()),),
            "owner is required",
        ),
        (
            (
                NodeSpec(
                    "child",
                    "Child",
                    "owner",
                    "tool",
                    deps=("missing",),
                    work=node_work(tool_kind="test.tool"),
                ),
            ),
            "unknown dependencies",
        ),
        (
            (
                NodeSpec(
                    "first",
                    "First",
                    "owner",
                    "human",
                    deps=("second",),
                    work=node_work(),
                ),
                NodeSpec(
                    "second",
                    "Second",
                    "owner",
                    "human",
                    deps=("first",),
                    work=node_work(),
                ),
            ),
            "contains a cycle",
        ),
    ],
)
def test_snapshot_validation_rejects_unsafe_graphs(nodes, message):
    schema_version = "invalid" if message.startswith("unsupported") else "0.2"
    with pytest.raises(GraphValidationError, match=message):
        validate_snapshot(InstanceSnapshot(nodes=nodes, schema_version=schema_version))


def test_snapshot_validation_requires_acceptance_and_tool_kind():
    incomplete_work = node_work()
    incomplete_work["acceptance"] = []
    with pytest.raises(GraphValidationError, match="work acceptance is required"):
        validate_snapshot(
            InstanceSnapshot(
                nodes=(
                    NodeSpec(
                        "review", "Review", "owner", "human", work=incomplete_work
                    ),
                )
            )
        )

    with pytest.raises(GraphValidationError, match="tool definition is required"):
        validate_snapshot(
            InstanceSnapshot(
                nodes=(
                    NodeSpec("publish", "Publish", "owner", "tool", work=node_work()),
                )
            )
        )


def test_failed_quality_result_requires_evidence():
    with pytest.raises(ValueError, match="requires evidence"):
        QualityResult(QualityVerdict.FAIL)


def test_snapshot_is_deeply_immutable_and_has_stable_graph_queries():
    snapshot = linear_snapshot()

    with pytest.raises(TypeError):
        snapshot.inputs["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.inputs["source"]["kind"] = "template"  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.node("write_draft").work["prompt"] = "changed"  # type: ignore[index]

    assert topological_order(snapshot) == (
        "collect_brief",
        "write_draft",
        "publish_doc",
    )
    assert reachable_downstream(snapshot, "collect_brief") == {
        "write_draft",
        "publish_doc",
    }


def test_only_instance_owner_can_confirm_and_confirm_creates_initial_attempts():
    service, _ = build_service()

    with pytest.raises(AuthorizationError, match="instance owner"):
        service.confirm_draft("instance_1", actor_person_id="person_editor")

    instance = service.confirm_draft("instance_1", actor_person_id="person_owner")

    assert instance.status == InstanceStatus.RUNNING
    assert instance.nodes["collect_brief"].status == NodeStatus.READY
    assert instance.nodes["write_draft"].status == NodeStatus.PENDING
    assert instance.nodes["publish_doc"].status == NodeStatus.PENDING
    assert [attempt.status for attempt in instance.attempts.values()] == [
        AttemptStatus.PENDING,
        AttemptStatus.PENDING,
        AttemptStatus.PENDING,
    ]
    with pytest.raises(TransitionError, match="not a draft"):
        service.confirm_draft("instance_1", actor_person_id="person_owner")


def test_human_agent_and_tool_flow_unlocks_successors_and_finishes_instance():
    service, _ = build_service()
    service.confirm_draft("instance_1", actor_person_id="person_owner")

    human = service.dispatch_ready("instance_1")[0]
    assert human.executor == ExecutorKind.HUMAN
    assert human.status == NodeStatus.WAITING_HUMAN
    assert human.claim_token is None

    with pytest.raises(AuthorizationError, match="node owner"):
        service.submit_human(
            "instance_1",
            "collect_brief",
            actor_person_id="person_editor",
            attempt_no=human.attempt_no,
            expected_node_version=human.expected_node_version,
            result={"brief": "approved"},
        )

    instance = service.submit_human(
        "instance_1",
        "collect_brief",
        actor_person_id="person_owner",
        attempt_no=human.attempt_no,
        expected_node_version=human.expected_node_version,
        result={"brief": "approved"},
    )
    assert instance.nodes["collect_brief"].status == NodeStatus.DONE
    with pytest.raises(TypeError):
        instance.current_attempt("collect_brief").result["brief"] = "changed"  # type: ignore[index]
    assert instance.nodes["write_draft"].status == NodeStatus.READY
    assert instance.nodes["publish_doc"].status == NodeStatus.PENDING

    agent = service.dispatch_ready("instance_1")[0]
    assert agent.executor == ExecutorKind.AGENT
    assert agent.status == NodeStatus.RUNNING
    assert agent.claim_token == "claim-agent"

    with pytest.raises(InvalidClaimError):
        service.complete_automated(
            "instance_1",
            "write_draft",
            attempt_no=agent.attempt_no,
            expected_node_version=agent.expected_node_version,
            claim_token="wrong-token",
            result={"document": "draft"},
        )
    assert service.get("instance_1").nodes["write_draft"].status == NodeStatus.RUNNING

    quality = QualityResult(
        QualityVerdict.PASS,
        evidence="all acceptance checks passed",
        suggestion="",
    )
    instance = service.complete_automated(
        "instance_1",
        "write_draft",
        attempt_no=agent.attempt_no,
        expected_node_version=agent.expected_node_version,
        claim_token=agent.claim_token or "",
        result={"document": "draft"},
        quality_result=quality,
    )
    assert instance.current_attempt("write_draft").quality_result == quality
    assert instance.nodes["publish_doc"].status == NodeStatus.READY

    tool = service.dispatch_ready("instance_1")[0]
    assert tool.executor == ExecutorKind.TOOL
    assert tool.claim_token == "claim-tool"
    instance = service.complete_automated(
        "instance_1",
        "publish_doc",
        attempt_no=tool.attempt_no,
        expected_node_version=tool.expected_node_version,
        claim_token=tool.claim_token or "",
        result={"url": "https://example.invalid/document"},
    )

    assert instance.status == InstanceStatus.DONE
    assert all(node.status == NodeStatus.DONE for node in instance.nodes.values())


def test_fan_in_unlocks_only_after_every_dependency_is_done():
    snapshot = InstanceSnapshot(
        nodes=(
            NodeSpec(
                "legal_review", "Legal review", "legal", "human", work=node_work()
            ),
            NodeSpec(
                "brand_review", "Brand review", "brand", "human", work=node_work()
            ),
            NodeSpec(
                "release",
                "Release",
                "owner",
                "tool",
                deps=("legal_review", "brand_review"),
                work=node_work(tool_kind="release.publish"),
            ),
        )
    )
    service, _ = build_service(snapshot)
    service.confirm_draft("instance_1", actor_person_id="person_owner")
    activations = {item.node_key: item for item in service.dispatch_ready("instance_1")}

    instance = service.submit_human(
        "instance_1",
        "legal_review",
        actor_person_id="legal",
        attempt_no=activations["legal_review"].attempt_no,
        expected_node_version=activations["legal_review"].expected_node_version,
        result={"approved": True},
    )
    assert instance.nodes["release"].status == NodeStatus.PENDING

    instance = service.submit_human(
        "instance_1",
        "brand_review",
        actor_person_id="brand",
        attempt_no=activations["brand_review"].attempt_no,
        expected_node_version=activations["brand_review"].expected_node_version,
        result={"approved": True},
    )
    assert instance.nodes["release"].status == NodeStatus.READY


def test_stale_version_and_expired_claim_are_rejected_without_mutation():
    clock = Clock()
    service, _ = build_service(clock=clock)
    service.confirm_draft("instance_1", actor_person_id="person_owner")
    human = service.dispatch_ready("instance_1")[0]

    with pytest.raises(StaleAttemptError, match="expected version"):
        service.submit_human(
            "instance_1",
            "collect_brief",
            actor_person_id="person_owner",
            attempt_no=human.attempt_no,
            expected_node_version=human.expected_node_version - 1,
            result={"brief": "approved"},
        )

    service.submit_human(
        "instance_1",
        "collect_brief",
        actor_person_id="person_owner",
        attempt_no=human.attempt_no,
        expected_node_version=human.expected_node_version,
        result={"brief": "approved"},
    )
    agent = service.dispatch_ready("instance_1")[0]
    clock.now += timedelta(minutes=6)

    with pytest.raises(ClaimExpiredError):
        service.complete_automated(
            "instance_1",
            "write_draft",
            attempt_no=agent.attempt_no,
            expected_node_version=agent.expected_node_version,
            claim_token=agent.claim_token or "",
            result={"document": "late"},
        )
    instance = service.get("instance_1")
    assert instance.nodes["write_draft"].status == NodeStatus.RUNNING
    assert instance.current_attempt("write_draft").result is None


def test_automated_failure_records_error_and_fails_instance():
    snapshot = InstanceSnapshot(
        nodes=(
            NodeSpec(
                "sync_data",
                "Sync data",
                "owner",
                "tool",
                work=node_work(tool_kind="data.sync"),
            ),
        )
    )
    service, _ = build_service(snapshot)
    service.confirm_draft("instance_1", actor_person_id="person_owner")
    activation = service.dispatch_ready("instance_1")[0]

    instance = service.fail_automated(
        "instance_1",
        "sync_data",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        claim_token=activation.claim_token or "",
        error_code="upstream_timeout",
        error_message="upstream did not answer",
    )

    assert instance.status == InstanceStatus.FAILED
    assert instance.nodes["sync_data"].status == NodeStatus.FAILED
    assert instance.current_attempt("sync_data").error_code == "upstream_timeout"


def test_failed_instance_rejects_late_result_from_parallel_node():
    snapshot = InstanceSnapshot(
        nodes=(
            NodeSpec(
                "first_job",
                "First job",
                "owner",
                "agent",
                work=node_work(),
            ),
            NodeSpec(
                "second_job",
                "Second job",
                "owner",
                "tool",
                work=node_work(tool_kind="second.run"),
            ),
        )
    )
    service, _ = build_service(snapshot)
    service.confirm_draft("instance_1", actor_person_id="person_owner")
    activations = {item.node_key: item for item in service.dispatch_ready("instance_1")}

    first = activations["first_job"]
    service.fail_automated(
        "instance_1",
        "first_job",
        attempt_no=first.attempt_no,
        expected_node_version=first.expected_node_version,
        claim_token=first.claim_token or "",
        error_code="provider_error",
        error_message="provider failed",
    )

    second = activations["second_job"]
    with pytest.raises(TransitionError, match="instance is not running"):
        service.complete_automated(
            "instance_1",
            "second_job",
            attempt_no=second.attempt_no,
            expected_node_version=second.expected_node_version,
            claim_token=second.claim_token or "",
            result={"value": "late"},
        )
    assert service.get("instance_1").current_attempt("second_job").result is None


def test_repository_rejects_lost_updates():
    repository = InMemoryWorkflowRepository()
    instance = WorkflowInstance(
        id="instance_1",
        tenant_id="tenant_1",
        owner_person_id="owner",
        snapshot=linear_snapshot(),
    )
    repository.add(instance)
    first = repository.get("instance_1")
    second = repository.get("instance_1")

    first.graph_revision = 2
    repository.save(first, expected_version=0)
    second.graph_revision = 3
    with pytest.raises(ConcurrentUpdateError, match="expected version 0, found 1"):
        repository.save(second, expected_version=0)
