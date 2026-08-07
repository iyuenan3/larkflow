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
    HumanDecisionFeedbackError,
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
REJECTION_FEEDBACK = (
    "移除与当前试点无关的发布步骤，并依据实际观察结果定义验收标准。"
)


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
        self.card_updates = []

    def send_message(self, request):
        self.requests.append(request)
        return ExternalMessage(message_id=f"message-{len(self.requests)}")

    def update_chat_card_message(self, *, message_id, card):
        self.card_updates.append((message_id, card))


def decision_snapshot() -> InstanceSnapshot:
    common = {
        "objective": "Review the supplied result",
        "inputs": [],
        "outputs": [{"id": "decision", "type": "data"}],
        "acceptance": ["A Human Owner records the outcome"],
    }
    return InstanceSnapshot(
        goal="Review a source-grounded brief",
        inputs={
            "source_registry": {
                "source_url": "https://example.invalid/issues/42",
                "facts": [{"id": "F1", "text": "来源事实"}],
                "open_questions": [],
            }
        },
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
                    "inputs": [
                        "instance_inputs.source_registry",
                        "dependencies.draft",
                    ],
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
    service, repository, review = waiting_decision()
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
        feedback=REJECTION_FEEDBACK,
    )

    assert rejected.status == InstanceStatus.FAILED
    assert rejected.nodes["review"].status == NodeStatus.FAILED
    attempt = rejected.current_attempt("review")
    assert attempt.result == {
        "decision": "rejected",
        "feedback": REJECTION_FEEDBACK,
    }
    assert attempt.quality_result is not None
    assert attempt.quality_result.verdict == QualityVerdict.FAIL
    assert REJECTION_FEEDBACK in attempt.quality_result.evidence
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
    assert restarted.attempts[("review", 1)].result == {
        "decision": "rejected",
        "feedback": REJECTION_FEEDBACK,
    }
    assert restarted.current_attempt("draft").input_snapshot["rework_feedback"] == {
        "source_node_key": "review",
        "source_attempt_no": 1,
        "feedback": REJECTION_FEEDBACK,
    }

    service.dispatch_due(
        TENANT,
        "instance_decision",
        worker_id="runtime-2",
    )
    activated = service.get(TENANT, "instance_decision")
    assert activated.current_attempt("draft").input_snapshot["rework_feedback"] == {
        "source_node_key": "review",
        "source_attempt_no": 1,
        "feedback": REJECTION_FEEDBACK,
    }
    rejection_audit = next(
        event
        for event in repository.audit_log(TENANT, "instance_decision")
        if event.event_type == "node.human_decision_rejected"
    )
    assert rejection_audit.payload["feedback"] == REJECTION_FEEDBACK


@pytest.mark.parametrize("feedback", [None, "", "   \n"])
def test_reject_decision_requires_non_empty_feedback(feedback):
    service, _, review = waiting_decision()
    before = service.get(TENANT, "instance_decision")

    with pytest.raises(HumanDecisionFeedbackError, match="必须填写具体意见"):
        service.submit_human_decision(
            TENANT,
            "instance_decision",
            "review",
            HumanDecision.REJECT,
            actor_person_id="person_reviewer",
            attempt_no=review.attempt_no,
            expected_instance_version=before.version,
            expected_node_version=review.expected_node_version,
            feedback=feedback,
        )

    unchanged = service.get(TENANT, "instance_decision")
    assert unchanged.version == before.version
    assert unchanged.status == InstanceStatus.RUNNING
    assert unchanged.nodes["review"].status == NodeStatus.WAITING_HUMAN


def test_reject_decision_bounds_feedback_and_accept_discards_it():
    service, _, review = waiting_decision()
    before = service.get(TENANT, "instance_decision")

    with pytest.raises(HumanDecisionFeedbackError, match="不能超过 1000"):
        service.submit_human_decision(
            TENANT,
            "instance_decision",
            "review",
            HumanDecision.REJECT,
            actor_person_id="person_reviewer",
            attempt_no=review.attempt_no,
            expected_instance_version=before.version,
            expected_node_version=review.expected_node_version,
            feedback="x" * 1_001,
        )

    accepted = service.submit_human_decision(
        TENANT,
        "instance_decision",
        "review",
        HumanDecision.ACCEPT,
        actor_person_id="person_reviewer",
        attempt_no=review.attempt_no,
        expected_instance_version=before.version,
        expected_node_version=review.expected_node_version,
        feedback="这段伪造文本不得进入接受结果",
    )
    assert accepted.current_attempt("review").result == {"decision": "accepted"}


def test_rework_feedback_reaches_only_the_restart_target_and_preserves_upstream():
    common = {
        "objective": "Produce or review one result",
        "inputs": [],
        "outputs": [{"id": "result", "type": "data"}],
        "acceptance": ["A Human Owner records the result"],
    }
    snapshot = InstanceSnapshot(
        goal="Preserve approved context while reworking a draft",
        nodes=(
            NodeSpec("context", "Confirm context", "person_owner", "human", work=common),
            NodeSpec(
                "draft",
                "Write draft",
                "person_author",
                "human",
                deps=("context",),
                work={**common, "inputs": ["dependencies.context"]},
            ),
            NodeSpec(
                "review",
                "Review draft",
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
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=lambda: NOW)
    service.create_draft(
        instance_id="instance_rework_scope",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot,
    )
    service.confirm_draft(TENANT, "instance_rework_scope", actor_person_id="person_owner")
    context = service.dispatch_due(
        TENANT,
        "instance_rework_scope",
        worker_id="runtime-scope",
    )[0]
    service.submit_human(
        TENANT,
        "instance_rework_scope",
        "context",
        actor_person_id="person_owner",
        attempt_no=context.attempt_no,
        expected_node_version=context.expected_node_version,
        result={"brief": "approved context"},
    )
    draft = service.dispatch_due(
        TENANT,
        "instance_rework_scope",
        worker_id="runtime-scope",
    )[0]
    service.submit_human(
        TENANT,
        "instance_rework_scope",
        "draft",
        actor_person_id="person_author",
        attempt_no=draft.attempt_no,
        expected_node_version=draft.expected_node_version,
        result={"content": "first draft"},
    )
    review = service.dispatch_due(
        TENANT,
        "instance_rework_scope",
        worker_id="runtime-scope",
    )[0]
    before_reject = service.get(TENANT, "instance_rework_scope")
    service.submit_human_decision(
        TENANT,
        "instance_rework_scope",
        "review",
        HumanDecision.REJECT,
        actor_person_id="person_reviewer",
        attempt_no=review.attempt_no,
        expected_instance_version=before_reject.version,
        expected_node_version=review.expected_node_version,
        feedback=REJECTION_FEEDBACK,
    )
    preview = service.preview_node_restart(
        TENANT,
        "instance_rework_scope",
        "draft",
        actor_person_id="person_owner",
    )
    restarted = service.confirm_node_restart(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    ).instance

    assert preview.affected_node_keys == ("draft", "review")
    assert restarted.nodes["context"].current_attempt_no == 1
    assert restarted.current_attempt("context").result == {
        "brief": "approved context"
    }
    assert "rework_feedback" not in restarted.current_attempt("context").input_snapshot
    assert restarted.current_attempt("draft").input_snapshot["rework_feedback"][
        "feedback"
    ] == REJECTION_FEEDBACK
    assert "rework_feedback" not in restarted.current_attempt("review").input_snapshot


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
    assert "填写意见并退回" in rendered
    assert "human_decision_accept" in rendered
    assert "human_decision_reject" in rendered
    context = next(
        element["content"]
        for element in card["body"]["elements"]
        if element.get("tag") == "markdown" and "**流程输入**" in element["content"]
    )
    assert "```json" in context
    assert '"source_url": "https://example.invalid/issues/42"' in context
    assert "](https://example.invalid/issues/42%22)" not in context
    reject_form = next(
        element
        for element in card["body"]["elements"]
        if element.get("tag") == "form"
    )
    feedback = reject_form["elements"][0]
    assert feedback["name"] == "rejection_feedback"
    assert feedback["required"] is True
    assert feedback["max_length"] == 1_000
    reject_button = reject_form["elements"][-1]
    assert reject_button["form_action_type"] == "submit"
    assert "behaviors" not in reject_button
    assert "action_type" not in reject_button
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
    assert projection.state["decision_binding"] == {
        "kind": "human_decision",
        "instance_id": "instance_decision",
        "node_key": "review",
        "attempt_no": review.attempt_no,
        "node_version": instance.nodes["review"].version,
        "instance_version": instance.version,
    }

    preview = service.preview_cancellation(
        TENANT,
        "instance_decision",
        actor_person_id="person_owner",
    )
    service.confirm_cancellation(
        TENANT,
        "instance_decision",
        actor_person_id="person_owner",
        expected_instance_version=preview.expected_instance_version,
    )
    canceled_report = worker.run_once()

    assert canceled_report.cards_updated == 1
    assert len(messages.card_updates) == 1
    message_id, canceled_card = messages.card_updates[0]
    assert message_id == "message-1"
    assert "复核已取消" in str(canceled_card)
    assert "button" not in str(canceled_card)
    assert "form" not in str(canceled_card)
    projection = repository.get_projection(
        TENANT,
        instance.nodes["review"].id,
        review.attempt_no,
        FEISHU_DECISION_CARD_KIND,
    )
    assert projection is not None
    assert projection.state["settled"] is True
    assert projection.state["node_status"] == NodeStatus.CANCELED.value
