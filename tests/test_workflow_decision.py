"""Human accept or reject gates and their Feishu projection."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from larkflow.workflow import (
    AuthorizationError,
    ExternalMessage,
    ExternalTask,
    FEISHU_DECISION_CARD_KIND,
    HumanDecision,
    InMemoryWorkflowRepository,
    InstanceSnapshot,
    InstanceStatus,
    NodeSpec,
    NodeStatus,
    QualityVerdict,
    StaleHumanDecisionError,
    WorkflowProjectionWorker,
    WorkflowService,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
TENANT = "tenant_decision"


class RecordingTasks:
    def __init__(self) -> None:
        self.create_requests = []
        self.completed = []

    def create_task(self, request):
        self.create_requests.append(request)
        return ExternalTask(guid=f"task-{len(self.create_requests)}")

    def complete_task(self, task_guid):
        self.completed.append(task_guid)

    def task_exists(self, task_guid):
        return True


class RecordingMessages:
    def __init__(self) -> None:
        self.requests = []

    def send_message(self, request):
        self.requests.append(request)
        return ExternalMessage(message_id=f"message-{len(self.requests)}")


def decision_snapshot() -> InstanceSnapshot:
    common = {
        "objective": "Review the supplied result",
        "inputs": [],
        "outputs": [{"id": "decision", "type": "data"}],
        "acceptance": ["A Human Owner records the outcome"],
    }
    return InstanceSnapshot(
        goal="Review a source-grounded brief",
        inputs={"source_url": "https://example.invalid/issues/42"},
        nodes=(
            NodeSpec(
                "draft",
                "Supply reviewed draft",
                "person_author",
                "human",
                work=common,
            ),
            NodeSpec(
                "review",
                "Accept or return draft",
                "person_reviewer",
                "human",
                deps=("draft",),
                work={
                    **common,
                    "inputs": ["dependencies.draft"],
                    "decision": {
                        "kind": "accept_reject",
                        "reject_target": "draft",
                    },
                },
            ),
        ),
    )


def waiting_decision():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=lambda: NOW)
    service.create_draft(
        instance_id="instance_decision",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=decision_snapshot(),
    )
    service.confirm_draft(
        TENANT,
        "instance_decision",
        actor_person_id="person_owner",
    )
    draft = service.dispatch_due(
        TENANT,
        "instance_decision",
        worker_id="runtime-1",
    )[0]
    service.submit_human(
        TENANT,
        "instance_decision",
        "draft",
        actor_person_id="person_author",
        attempt_no=draft.attempt_no,
        expected_node_version=draft.expected_node_version,
        result={"content": "A durable source-grounded result"},
    )
    review = service.dispatch_due(
        TENANT,
        "instance_decision",
        worker_id="runtime-1",
    )[0]
    return service, repository, review


def test_accept_decision_completes_the_human_gate_and_instance():
    service, _, review = waiting_decision()
    before = service.get(TENANT, "instance_decision")

    accepted = service.submit_human_decision(
        TENANT,
        "instance_decision",
        "review",
        HumanDecision.ACCEPT,
        actor_person_id="person_reviewer",
        attempt_no=review.attempt_no,
        expected_instance_version=before.version,
        expected_node_version=review.expected_node_version,
    )

    assert accepted.status == InstanceStatus.DONE
    assert accepted.nodes["review"].status == NodeStatus.DONE
    assert accepted.current_attempt("review").result == {"decision": "accepted"}
    assert accepted.current_attempt("draft").result == {
        "content": "A durable source-grounded result"
    }


def test_reject_decision_fails_current_attempt_but_preserves_upstream_evidence():
    service, _, review = waiting_decision()
    before = service.get(TENANT, "instance_decision")

    rejected = service.submit_human_decision(
        TENANT,
        "instance_decision",
        "review",
        HumanDecision.REJECT,
        actor_person_id="person_reviewer",
        attempt_no=review.attempt_no,
        expected_instance_version=before.version,
        expected_node_version=review.expected_node_version,
    )

    assert rejected.status == InstanceStatus.FAILED
    assert rejected.nodes["review"].status == NodeStatus.FAILED
    attempt = rejected.current_attempt("review")
    assert attempt.result == {"decision": "rejected"}
    assert attempt.quality_result is not None
    assert attempt.quality_result.verdict == QualityVerdict.FAIL
    assert rejected.current_attempt("draft").result == {
        "content": "A durable source-grounded result"
    }

    preview = service.preview_node_restart(
        TENANT,
        "instance_decision",
        "draft",
        actor_person_id="person_owner",
    )
    assert preview.affected_node_keys == ("draft", "review")
    confirmation = service.confirm_node_restart(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    )
    restarted = confirmation.instance
    assert restarted.status == InstanceStatus.RUNNING
    assert restarted.nodes["draft"].current_attempt_no == 2
    assert restarted.nodes["draft"].status == NodeStatus.READY
    assert restarted.nodes["review"].current_attempt_no == 2
    assert restarted.nodes["review"].status == NodeStatus.PENDING
    assert restarted.attempts[("review", 1)].result == {"decision": "rejected"}


def test_decision_requires_the_node_owner_and_current_card_versions():
    service, _, review = waiting_decision()
    before = service.get(TENANT, "instance_decision")

    with pytest.raises(AuthorizationError, match="node owner"):
        service.submit_human_decision(
            TENANT,
            "instance_decision",
            "review",
            HumanDecision.ACCEPT,
            actor_person_id="person_owner",
            attempt_no=review.attempt_no,
            expected_instance_version=before.version,
            expected_node_version=review.expected_node_version,
        )
    with pytest.raises(StaleHumanDecisionError, match="instance changed"):
        service.submit_human_decision(
            TENANT,
            "instance_decision",
            "review",
            HumanDecision.ACCEPT,
            actor_person_id="person_reviewer",
            attempt_no=review.attempt_no,
            expected_instance_version=before.version - 1,
            expected_node_version=review.expected_node_version,
        )


def test_decision_node_projects_a_card_instead_of_a_second_task():
    service, repository, review = waiting_decision()
    tasks = RecordingTasks()
    messages = RecordingMessages()
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        tasks,
        message_adapter=messages,
        tenant_id=TENANT,
        worker_id="projection-1",
        clock=lambda: NOW,
    )

    report = worker.run_once()

    assert report.messages_sent == 1
    assert len(messages.requests) == 1
    assert tasks.create_requests == []
    card = messages.requests[0].card
    assert card is not None
    rendered = str(card)
    assert "接受" in rendered
    assert "退回" in rendered
    assert "human_decision_accept" in rendered
    assert "human_decision_reject" in rendered
    assert f"'attempt_no': {review.attempt_no}" in rendered
    instance = service.get(TENANT, "instance_decision")
    projection = repository.get_projection(
        TENANT,
        instance.nodes["review"].id,
        review.attempt_no,
        FEISHU_DECISION_CARD_KIND,
    )
    assert projection is not None
    assert projection.external_id == "message-1"
