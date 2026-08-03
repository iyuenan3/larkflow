"""Restart preview, confirmation, history, and projection behavior."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from larkflow.workflow import (
    AuthorizationError,
    ExecutorKind,
    ExternalTask,
    InMemoryWorkflowRepository,
    InstanceSnapshot,
    InstanceStatus,
    NodeRunner,
    NodeSpec,
    NodeStatus,
    RestartNotAllowedError,
    RestartPreviewExpiredError,
    RestartScope,
    StaleAttemptError,
    StaleRestartPreviewError,
    WorkflowProjectionWorker,
    WorkflowService,
)


NOW = datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)
TENANT = "tenant_restart"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def work(*, tool_kind: str | None = None) -> dict:
    definition = {
        "objective": "Produce one synthetic result",
        "inputs": [],
        "outputs": [{"id": "result", "type": "data"}],
        "acceptance": ["A result exists"],
    }
    if tool_kind is not None:
        definition["tool"] = {"kind": tool_kind, "args": {}}
    return definition


def branching_snapshot() -> InstanceSnapshot:
    return InstanceSnapshot(
        goal="Restart only one branch and its downstream",
        nodes=(
            NodeSpec("intake", "Intake", "person_owner", "human", work=work()),
            NodeSpec(
                "left_draft",
                "Left draft",
                "person_owner",
                "agent",
                deps=("intake",),
                work=work(),
            ),
            NodeSpec(
                "right_review",
                "Right review",
                "person_reviewer",
                "human",
                deps=("intake",),
                work=work(),
            ),
            NodeSpec(
                "merge",
                "Merge",
                "person_owner",
                "tool",
                deps=("left_draft", "right_review"),
                work=work(tool_kind="content.merge"),
            ),
            NodeSpec(
                "final_review",
                "Final review",
                "person_owner",
                "human",
                deps=("merge",),
                work=work(),
            ),
        ),
    )


def build_service(clock: Clock | None = None):
    repository = InMemoryWorkflowRepository()
    token_values = iter(f"claim_{index}" for index in range(20))
    service = WorkflowService(
        repository,
        runner=NodeRunner(token_factory=lambda: next(token_values)),
        clock=clock or Clock(),
    )
    service.create_draft(
        instance_id="instance_restart",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=branching_snapshot(),
    )
    return service, repository


def finish_instance(service: WorkflowService) -> None:
    if service.get(TENANT, "instance_restart").status == InstanceStatus.DRAFT:
        service.confirm_draft(
            TENANT,
            "instance_restart",
            actor_person_id="person_owner",
        )
    intake = service.dispatch_ready(TENANT, "instance_restart")[0]
    service.submit_human(
        TENANT,
        "instance_restart",
        "intake",
        actor_person_id="person_owner",
        attempt_no=intake.attempt_no,
        expected_node_version=intake.expected_node_version,
        result={"brief": "synthetic"},
    )
    activations = {
        item.node_key: item
        for item in service.dispatch_ready(
            TENANT,
            "instance_restart",
            worker_id="runtime_1",
            max_automated=1,
        )
    }
    service.submit_human(
        TENANT,
        "instance_restart",
        "right_review",
        actor_person_id="person_reviewer",
        attempt_no=activations["right_review"].attempt_no,
        expected_node_version=activations["right_review"].expected_node_version,
        result={"decision": "approved"},
    )
    left = activations["left_draft"]
    service.complete_automated(
        TENANT,
        "instance_restart",
        "left_draft",
        attempt_no=left.attempt_no,
        expected_node_version=left.expected_node_version,
        claim_token=left.claim_token or "",
        worker_id="runtime_1",
        result={"content": "left result"},
    )
    merge = service.dispatch_ready(
        TENANT,
        "instance_restart",
        worker_id="runtime_1",
    )[0]
    service.complete_automated(
        TENANT,
        "instance_restart",
        "merge",
        attempt_no=merge.attempt_no,
        expected_node_version=merge.expected_node_version,
        claim_token=merge.claim_token or "",
        worker_id="runtime_1",
        result={"content": "merged"},
    )
    final = service.dispatch_ready(TENANT, "instance_restart")[0]
    service.submit_human(
        TENANT,
        "instance_restart",
        "final_review",
        actor_person_id="person_owner",
        attempt_no=final.attempt_no,
        expected_node_version=final.expected_node_version,
        result={"decision": "accepted"},
    )


def finish_restarted_branch(service: WorkflowService) -> None:
    left = service.dispatch_ready(
        TENANT,
        "instance_restart",
        worker_id="runtime_2",
    )[0]
    service.complete_automated(
        TENANT,
        "instance_restart",
        "left_draft",
        attempt_no=left.attempt_no,
        expected_node_version=left.expected_node_version,
        claim_token=left.claim_token or "",
        worker_id="runtime_2",
        result={"content": "left result v2"},
    )
    merge = service.dispatch_ready(
        TENANT,
        "instance_restart",
        worker_id="runtime_2",
    )[0]
    service.complete_automated(
        TENANT,
        "instance_restart",
        "merge",
        attempt_no=merge.attempt_no,
        expected_node_version=merge.expected_node_version,
        claim_token=merge.claim_token or "",
        worker_id="runtime_2",
        result={"content": "merged v2"},
    )
    final = service.dispatch_ready(TENANT, "instance_restart")[0]
    service.submit_human(
        TENANT,
        "instance_restart",
        "final_review",
        actor_person_id="person_owner",
        attempt_no=final.attempt_no,
        expected_node_version=final.expected_node_version,
        result={"decision": "accepted again"},
    )


def test_restart_preview_is_owner_only_read_only_and_shows_exact_downstream():
    service, repository = build_service()
    finish_instance(service)
    before = service.get(TENANT, "instance_restart")
    audit_count = len(repository.audit_log(TENANT, "instance_restart"))

    with pytest.raises(AuthorizationError):
        service.preview_node_restart(
            TENANT,
            "instance_restart",
            "left_draft",
            actor_person_id="person_intruder",
        )
    preview = service.preview_node_restart(
        TENANT,
        "instance_restart",
        "left_draft",
        actor_person_id="person_owner",
    )

    after = service.get(TENANT, "instance_restart")
    assert preview.affected_node_keys == ("left_draft", "merge", "final_review")
    assert preview.expected_instance_version == before.version
    assert preview.graph_revision == before.graph_revision
    assert after == before
    assert len(repository.audit_log(TENANT, "instance_restart")) == audit_count


def test_restart_confirmation_preserves_history_and_is_idempotent():
    service, repository = build_service()
    finish_instance(service)
    before = service.get(TENANT, "instance_restart")
    preview = service.preview_node_restart(
        TENANT,
        "instance_restart",
        "left_draft",
        actor_person_id="person_owner",
    )

    confirmation = service.confirm_node_restart(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    )
    restarted = confirmation.instance

    assert confirmation.already_applied is False
    assert restarted.status == InstanceStatus.RUNNING
    assert restarted.graph_revision == before.graph_revision
    assert restarted.nodes["intake"].current_attempt_no == 1
    assert restarted.nodes["right_review"].current_attempt_no == 1
    assert restarted.nodes["right_review"].status == NodeStatus.DONE
    assert restarted.nodes["left_draft"].current_attempt_no == 2
    assert restarted.nodes["left_draft"].status == NodeStatus.READY
    assert restarted.nodes["merge"].current_attempt_no == 2
    assert restarted.nodes["merge"].status == NodeStatus.PENDING
    assert restarted.nodes["final_review"].current_attempt_no == 2
    assert restarted.attempts[("left_draft", 1)].result == {"content": "left result"}
    assert restarted.attempts[("merge", 1)].result == {"content": "merged"}
    assert restarted.attempts[("final_review", 1)].result == {
        "decision": "accepted"
    }
    assert restarted.current_attempt("left_draft").result is None
    audit = repository.audit_log(TENANT, "instance_restart")[-1]
    assert audit.event_type == "instance.node_restarted"
    assert audit.payload["affected_node_keys"] == (
        "left_draft",
        "merge",
        "final_review",
    )
    version_after = restarted.version

    replay = service.confirm_node_restart(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    )
    assert replay.already_applied is True
    assert replay.instance.version == version_after
    assert len(
        [
            event
            for event in repository.audit_log(TENANT, "instance_restart")
            if event.event_type == "instance.node_restarted"
        ]
    ) == 1

    finish_restarted_branch(service)
    finished = service.get(TENANT, "instance_restart")
    assert finished.status == InstanceStatus.DONE
    assert finished.current_attempt("right_review").attempt_no == 1
    assert finished.current_attempt("left_draft").result == {
        "content": "left result v2"
    }


def test_full_instance_restart_resets_every_node_and_preserves_history():
    service, repository = build_service()
    finish_instance(service)
    before = service.get(TENANT, "instance_restart")

    with pytest.raises(AuthorizationError):
        service.preview_instance_restart(
            TENANT,
            "instance_restart",
            actor_person_id="person_intruder",
        )
    preview = service.preview_instance_restart(
        TENANT,
        "instance_restart",
        actor_person_id="person_owner",
    )

    assert preview.scope == RestartScope.INSTANCE
    assert preview.node_key is None
    assert preview.affected_node_keys == tuple(
        spec.key for spec in before.snapshot.nodes
    )
    assert service.get(TENANT, "instance_restart") == before

    confirmation = service.confirm_restart(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    )
    restarted = confirmation.instance

    assert confirmation.already_applied is False
    assert restarted.status == InstanceStatus.RUNNING
    assert restarted.completed_at is None
    assert restarted.graph_revision == before.graph_revision
    assert all(node.current_attempt_no == 2 for node in restarted.nodes.values())
    assert restarted.nodes["intake"].status == NodeStatus.READY
    assert all(
        restarted.nodes[node_key].status == NodeStatus.PENDING
        for node_key in ("left_draft", "right_review", "merge", "final_review")
    )
    assert restarted.attempts[("intake", 1)].result == {"brief": "synthetic"}
    assert restarted.attempts[("left_draft", 1)].result == {
        "content": "left result"
    }
    assert restarted.attempts[("final_review", 1)].result == {
        "decision": "accepted"
    }
    assert all(
        restarted.attempts[(node_key, 2)].result is None
        for node_key in preview.affected_node_keys
    )
    audit = repository.audit_log(TENANT, "instance_restart")[-1]
    assert audit.event_type == "instance.restarted"
    assert audit.node_key is None
    assert audit.payload["scope"] == "instance"
    assert audit.payload["affected_node_keys"] == preview.affected_node_keys
    version_after = restarted.version

    replay = service.confirm_restart(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    )
    assert replay.already_applied is True
    assert replay.instance.version == version_after
    assert len(
        [
            event
            for event in repository.audit_log(TENANT, "instance_restart")
            if event.event_type == "instance.restarted"
        ]
    ) == 1


def test_full_instance_restart_makes_every_root_ready():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=Clock())
    snapshot = InstanceSnapshot(
        nodes=(
            NodeSpec("root_a", "Root A", "person_owner", "human", work=work()),
            NodeSpec("root_b", "Root B", "person_owner", "human", work=work()),
            NodeSpec(
                "join",
                "Join",
                "person_owner",
                "tool",
                deps=("root_a", "root_b"),
                work=work(tool_kind="content.merge"),
            ),
        )
    )
    service.create_draft(
        instance_id="multi_root_restart",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot,
    )
    service.confirm_draft(
        TENANT,
        "multi_root_restart",
        actor_person_id="person_owner",
    )

    preview = service.preview_instance_restart(
        TENANT,
        "multi_root_restart",
        actor_person_id="person_owner",
    )
    restarted = service.confirm_restart(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    ).instance

    assert restarted.nodes["root_a"].status == NodeStatus.READY
    assert restarted.nodes["root_b"].status == NodeStatus.READY
    assert restarted.nodes["join"].status == NodeStatus.PENDING
    assert all(node.current_attempt_no == 2 for node in restarted.nodes.values())


def test_restart_invalidates_an_active_claim_and_rejects_its_late_result():
    service, _ = build_service()
    service.confirm_draft(TENANT, "instance_restart", actor_person_id="person_owner")
    intake = service.dispatch_ready(TENANT, "instance_restart")[0]
    service.submit_human(
        TENANT,
        "instance_restart",
        "intake",
        actor_person_id="person_owner",
        attempt_no=intake.attempt_no,
        expected_node_version=intake.expected_node_version,
        result={"brief": "synthetic"},
    )
    activations = {
        item.node_key: item
        for item in service.dispatch_ready(
            TENANT,
            "instance_restart",
            worker_id="runtime_1",
        )
    }
    left = activations["left_draft"]
    preview = service.preview_node_restart(
        TENANT,
        "instance_restart",
        "left_draft",
        actor_person_id="person_owner",
    )
    restarted = service.confirm_node_restart(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    ).instance

    old_attempt = restarted.attempts[("left_draft", 1)]
    assert old_attempt.status.value == "canceled"
    assert old_attempt.claim_token is None
    assert old_attempt.claimed_by is None
    with pytest.raises(StaleAttemptError):
        service.complete_automated(
            TENANT,
            "instance_restart",
            "left_draft",
            attempt_no=left.attempt_no,
            expected_node_version=left.expected_node_version,
            claim_token=left.claim_token or "",
            worker_id="runtime_1",
            result={"content": "late result"},
        )


def test_failed_instance_restart_must_cover_every_failed_node():
    service, _ = build_service()
    service.confirm_draft(TENANT, "instance_restart", actor_person_id="person_owner")
    intake = service.dispatch_ready(TENANT, "instance_restart")[0]
    service.submit_human(
        TENANT,
        "instance_restart",
        "intake",
        actor_person_id="person_owner",
        attempt_no=intake.attempt_no,
        expected_node_version=intake.expected_node_version,
        result={"brief": "synthetic"},
    )
    activations = {
        item.node_key: item
        for item in service.dispatch_ready(
            TENANT,
            "instance_restart",
            worker_id="runtime_1",
        )
    }
    left = activations["left_draft"]
    service.fail_automated(
        TENANT,
        "instance_restart",
        "left_draft",
        attempt_no=left.attempt_no,
        expected_node_version=left.expected_node_version,
        claim_token=left.claim_token or "",
        worker_id="runtime_1",
        error_code="synthetic_failure",
        error_message="synthetic failure",
    )

    with pytest.raises(RestartNotAllowedError, match="does not cover failed nodes"):
        service.preview_node_restart(
            TENANT,
            "instance_restart",
            "right_review",
            actor_person_id="person_owner",
        )
    preview = service.preview_node_restart(
        TENANT,
        "instance_restart",
        "left_draft",
        actor_person_id="person_owner",
    )
    assert preview.affected_node_keys == ("left_draft", "merge", "final_review")


def test_restart_rejects_not_started_expired_and_stale_previews():
    clock = Clock()
    service, _ = build_service(clock)
    service.confirm_draft(TENANT, "instance_restart", actor_person_id="person_owner")
    with pytest.raises(RestartNotAllowedError):
        service.preview_node_restart(
            TENANT,
            "instance_restart",
            "left_draft",
            actor_person_id="person_owner",
        )

    finish_instance(service)
    expiring = service.preview_node_restart(
        TENANT,
        "instance_restart",
        "left_draft",
        actor_person_id="person_owner",
    )
    clock.now += timedelta(minutes=15)
    with pytest.raises(RestartPreviewExpiredError):
        service.confirm_node_restart(
            TENANT,
            expiring.id,
            actor_person_id="person_owner",
        )

    clock.now += timedelta(seconds=1)
    stale = service.preview_node_restart(
        TENANT,
        "instance_restart",
        "left_draft",
        actor_person_id="person_owner",
    )
    other = service.preview_node_restart(
        TENANT,
        "instance_restart",
        "right_review",
        actor_person_id="person_owner",
    )
    service.confirm_node_restart(
        TENANT,
        other.id,
        actor_person_id="person_owner",
    )
    with pytest.raises(StaleRestartPreviewError):
        service.confirm_node_restart(
            TENANT,
            stale.id,
            actor_person_id="person_owner",
        )
    assert service.get(TENANT, "instance_restart").nodes["left_draft"].current_attempt_no == 1


class RecordingTasks:
    def __init__(self) -> None:
        self.created = []
        self.completed = []

    def create_task(self, request):
        self.created.append(request)
        return ExternalTask(guid=f"task_{len(self.created)}")

    def complete_task(self, task_guid):
        self.completed.append(task_guid)

    def task_exists(self, _task_guid):
        return True


def test_restart_closes_the_old_human_task_before_creating_a_new_attempt_task():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    snapshot = InstanceSnapshot(
        nodes=(
            NodeSpec(
                "review",
                "Review",
                "person_owner",
                ExecutorKind.HUMAN,
                work=work(),
            ),
        )
    )
    service.create_draft(
        instance_id="human_restart",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot,
    )
    service.confirm_draft(TENANT, "human_restart", actor_person_id="person_owner")
    service.dispatch_ready(TENANT, "human_restart")
    tasks = RecordingTasks()
    projection = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        tasks,
        tenant_id=TENANT,
        worker_id="projection_1",
        clock=clock,
    )
    assert projection.run_once().tasks_created == 1

    preview = service.preview_node_restart(
        TENANT,
        "human_restart",
        "review",
        actor_person_id="person_owner",
    )
    service.confirm_node_restart(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    )
    closed = projection.run_once()

    assert closed.tasks_completed == 1
    assert tasks.completed == ["task_1"]
    service.dispatch_ready(TENANT, "human_restart")
    created = projection.run_once()
    assert created.tasks_created == 1
    assert [request.attempt_no for request in tasks.created] == [1, 2]
    assert tasks.created[0].idempotency_key != tasks.created[1].idempotency_key
