"""Durable Feishu Task inbound event contract tests."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from larkflow.workflow import (
    ExternalTaskState,
    FEISHU_TASK_KIND,
    InMemoryWorkflowInbox,
    InMemoryWorkflowRepository,
    InboxStatus,
    InstanceSnapshot,
    NodeSpec,
    NodeStatus,
    ProjectionRecord,
    RecoveryAction,
    TASK_POLL_EVENT,
    TASK_POLL_SOURCE,
    TaskCompletionPoller,
    TaskVerificationWorker,
    TaskEventInboxBridge,
    WorkflowInboundWorker,
    WorkflowService,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
TENANT = "tenant_inbound"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


class TaskReader:
    def __init__(self, state: ExternalTaskState) -> None:
        self.state = state
        self.calls = []

    def get_task(self, task_guid: str) -> ExternalTaskState:
        self.calls.append(task_guid)
        return self.state


def task_state(**changes) -> ExternalTaskState:
    state = ExternalTaskState(
        guid="task-1",
        status="done",
        mode=1,
        completed_at="1785585600000",
        source=6,
        extra="lf-binding",
        assignee_ids=("person_reviewer",),
        completed_assignee_ids=("person_reviewer",),
    )
    return replace(state, **changes)


def setup_inbound(clock: Clock):
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    service.create_draft(
        instance_id="instance_inbound",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec(
                    "approve",
                    "Approve brief",
                    "person_reviewer",
                    "human",
                    work={
                        "objective": "Approve",
                        "inputs": [],
                        "outputs": [{"id": "decision", "type": "data"}],
                        "acceptance": ["A decision exists"],
                    },
                ),
            )
        ),
    )
    service.confirm_draft(
        TENANT,
        "instance_inbound",
        actor_person_id="person_owner",
    )
    activation = service.dispatch_due(
        TENANT,
        "instance_inbound",
        worker_id="runtime-1",
    )[0]
    node = service.get(TENANT, "instance_inbound").nodes["approve"]
    repository.save_projection(
        ProjectionRecord(
            id="projection-1",
            tenant_id=TENANT,
            instance_id="instance_inbound",
            node_instance_id=node.id,
            attempt_no=activation.attempt_no,
            kind=FEISHU_TASK_KIND,
            external_id="task-1",
            external_url="https://example.invalid/task-1",
            idempotency_key="lf-binding",
            sync_version=node.version,
            state={"node_status": "waiting_human", "completed": False},
            created_at=clock.now,
            updated_at=clock.now,
        )
    )
    inbox = InMemoryWorkflowInbox()
    bridge = TaskEventInboxBridge(inbox, tenant_id=TENANT, clock=clock)
    return service, repository, inbox, bridge


def event(event_id="event-1"):
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "create_time": "1785585600000",
            "token": "must-not-be-persisted",
        },
        "event": {
            "task_guid": "task-1",
            "event_types": ["task_completed_update"],
        },
    }


def verify(inbox, reader, clock, *, max_attempts=24):
    return TaskVerificationWorker(
        inbox,
        reader,
        tenant_id=TENANT,
        worker_id="verification-1",
        clock=clock,
        max_attempts=max_attempts,
    )


def worker(service, repository, inbox, clock):
    return WorkflowInboundWorker(
        service,
        repository,
        repository,
        inbox,
        tenant_id=TENANT,
        worker_id="inbound-1",
        clock=clock,
    )


def test_bridge_keeps_only_the_completion_signal_and_dedupes_event_id():
    clock = Clock()
    _, _, inbox, bridge = setup_inbound(clock)

    assert bridge("task.task.update_user_access_v2", event()) is True
    assert bridge("task.task.update_user_access_v2", event()) is False
    assert bridge(
        "task.task.update_user_access_v2",
        {
            "header": {"event_id": "event-2"},
            "event": {"task_guid": "task-1", "event_types": ["task_summary_update"]},
        },
    ) is False

    records = inbox.records(TENANT)
    assert len(records) == 1
    assert records[0].event.task_guid == "task-1"
    assert not hasattr(records[0].event, "token")


def test_verified_owner_completion_submits_the_current_human_attempt():
    clock = Clock()
    service, repository, inbox, bridge = setup_inbound(clock)
    bridge("task.task.update_user_access_v2", event())
    reader = TaskReader(task_state())

    assert worker(service, repository, inbox, clock).run_once().claimed == 0
    assert verify(inbox, reader, clock).run_once().verified == 1
    report = worker(service, repository, inbox, clock).run_once()

    assert report.claimed == 1
    assert report.submitted == 1
    restored = service.get(TENANT, "instance_inbound")
    assert restored.nodes["approve"].status == NodeStatus.DONE
    attempt = restored.current_attempt("approve")
    assert attempt.submitted_by_person_id == "person_reviewer"
    assert attempt.result == {"confirmed": True}
    assert inbox.records(TENANT)[0].outcome == "submitted:human_node"


def test_legacy_task_completion_cannot_bypass_an_accept_or_reject_card():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    common_work = {
        "objective": "Review the current result",
        "inputs": [],
        "outputs": [{"id": "decision", "type": "data"}],
        "acceptance": ["A Human decision exists"],
    }
    service.create_draft(
        instance_id="instance_decision_inbound",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec(
                    "draft",
                    "Supply draft",
                    "person_author",
                    "human",
                    work=common_work,
                ),
                NodeSpec(
                    "review",
                    "Accept or return draft",
                    "person_reviewer",
                    "human",
                    deps=("draft",),
                    work={
                        **common_work,
                        "inputs": ["dependencies.draft"],
                        "decision": {
                            "kind": "accept_reject",
                            "reject_target": "draft",
                        },
                    },
                ),
            )
        ),
    )
    service.confirm_draft(
        TENANT,
        "instance_decision_inbound",
        actor_person_id="person_owner",
    )
    draft = service.dispatch_due(
        TENANT,
        "instance_decision_inbound",
        worker_id="runtime-1",
    )[0]
    service.submit_human(
        TENANT,
        "instance_decision_inbound",
        "draft",
        actor_person_id="person_author",
        attempt_no=draft.attempt_no,
        expected_node_version=draft.expected_node_version,
        result={"content": "draft result"},
    )
    review = service.dispatch_due(
        TENANT,
        "instance_decision_inbound",
        worker_id="runtime-1",
    )[0]
    current = service.get(TENANT, "instance_decision_inbound")
    review_node = current.nodes["review"]
    repository.save_projection(
        ProjectionRecord(
            id="legacy-decision-task",
            tenant_id=TENANT,
            instance_id=current.id,
            node_instance_id=review_node.id,
            attempt_no=review.attempt_no,
            kind=FEISHU_TASK_KIND,
            external_id="task-1",
            external_url="https://example.invalid/task-1",
            idempotency_key="lf-binding",
            sync_version=review_node.version,
            state={"node_status": "waiting_human", "completed": False},
            created_at=clock.now,
            updated_at=clock.now,
        )
    )
    inbox = InMemoryWorkflowInbox()
    bridge = TaskEventInboxBridge(inbox, tenant_id=TENANT, clock=clock)
    bridge("task.task.update_user_access_v2", event("event-decision-task"))
    reader = TaskReader(task_state())

    assert verify(inbox, reader, clock).run_once().verified == 1
    report = worker(service, repository, inbox, clock).run_once()

    assert report.submitted == 0
    assert report.rejected == 1
    restored = service.get(TENANT, "instance_decision_inbound")
    assert restored.nodes["review"].status == NodeStatus.WAITING_HUMAN
    assert (
        inbox.records(TENANT)[0].outcome
        == "rejected:decision_requires_card"
    )


def test_completion_poll_enqueues_a_durable_deduped_signal_and_reuses_intake():
    clock = Clock()
    service, repository, inbox, _ = setup_inbound(clock)
    reader = TaskReader(task_state())
    poller = TaskCompletionPoller(
        repository,
        repository,
        inbox,
        reader,
        tenant_id=TENANT,
        batch_size=1,
        clock=clock,
    )

    first = poller.run_once()
    second = poller.run_once()

    assert first.instances_scanned == 1
    assert first.nodes_scanned == 1
    assert first.tasks_read == 1
    assert first.completions_observed == 1
    assert first.signals_appended == 1
    assert second.duplicates == 1
    record = inbox.records(TENANT)[0]
    assert record.event.id.startswith("task-poll-")
    assert record.event.source == TASK_POLL_SOURCE
    assert record.event.event_type == TASK_POLL_EVENT
    assert record.event.occurred_at == NOW

    assert verify(inbox, reader, clock).run_once().verified == 1
    assert worker(service, repository, inbox, clock).run_once().submitted == 1
    assert (
        service.get(TENANT, "instance_inbound").nodes["approve"].status
        == NodeStatus.DONE
    )


def test_completion_poll_submits_an_agent_failure_human_takeover_attempt():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    service.create_draft(
        instance_id="instance_takeover",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec(
                    "draft",
                    "Draft summary",
                    "person_agent_owner",
                    "agent",
                    work={
                        "objective": "Draft the summary",
                        "outputs": [{"id": "summary", "type": "data"}],
                        "acceptance": ["A summary exists"],
                    },
                ),
            )
        ),
    )
    service.confirm_draft(TENANT, "instance_takeover", actor_person_id="person_owner")
    activation = service.dispatch_due(
        TENANT,
        "instance_takeover",
        worker_id="edge_1",
    )[0]
    failed = service.fail_automated(
        TENANT,
        "instance_takeover",
        "draft",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        claim_token=activation.claim_token or "",
        error_code="provider_timeout",
        error_message="provider unavailable",
        worker_id="edge_1",
    )
    failed_node = failed.nodes["draft"]
    takeover = service.recover_failed_node(
        TENANT,
        failed.id,
        "draft",
        RecoveryAction.HUMAN_TAKEOVER,
        actor_person_id="person_agent_owner",
        expected_instance_version=failed.version,
        expected_node_version=failed_node.version,
        expected_attempt_no=failed_node.current_attempt_no,
    )
    node = takeover.nodes["draft"]
    repository.save_projection(
        ProjectionRecord(
            id="projection-takeover",
            tenant_id=TENANT,
            instance_id=takeover.id,
            node_instance_id=node.id,
            attempt_no=node.current_attempt_no,
            kind=FEISHU_TASK_KIND,
            external_id="task-1",
            external_url="https://example.invalid/task-1",
            idempotency_key="lf-binding",
            sync_version=node.version,
            state={"node_status": "waiting_human", "completed": False},
            created_at=clock.now,
            updated_at=clock.now,
        )
    )
    inbox = InMemoryWorkflowInbox()
    reader = TaskReader(
        task_state(
            assignee_ids=("person_agent_owner",),
            completed_assignee_ids=("person_agent_owner",),
        )
    )

    poll = TaskCompletionPoller(
        repository,
        repository,
        inbox,
        reader,
        tenant_id=TENANT,
        clock=clock,
    ).run_once()
    assert poll.signals_appended == 1
    assert verify(inbox, reader, clock).run_once().verified == 1
    assert worker(service, repository, inbox, clock).run_once().submitted == 1

    completed = service.get(TENANT, takeover.id)
    assert completed.nodes["draft"].status == NodeStatus.DONE
    assert completed.current_attempt("draft").submitted_by_person_id == (
        "person_agent_owner"
    )


def test_completion_poll_keeps_todo_tasks_out_of_the_inbox():
    clock = Clock()
    _, repository, inbox, _ = setup_inbound(clock)
    poller = TaskCompletionPoller(
        repository,
        repository,
        inbox,
        TaskReader(task_state(status="todo", completed_at=None)),
        tenant_id=TENANT,
        clock=clock,
    )

    report = poller.run_once()

    assert report.tasks_read == 1
    assert report.pending == 1
    assert report.signals_appended == 0
    assert report.failed == 0
    assert inbox.records(TENANT) == ()


def test_completion_poll_isolates_one_task_read_failure():
    clock = Clock()
    service, repository, inbox, _ = setup_inbound(clock)
    service.create_draft(
        instance_id="instance_other",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec(
                    "approve_other",
                    "Approve other brief",
                    "person_reviewer",
                    "human",
                    work={
                        "objective": "Approve",
                        "inputs": [],
                        "outputs": [{"id": "decision", "type": "data"}],
                        "acceptance": ["A decision exists"],
                    },
                ),
            )
        ),
    )
    service.confirm_draft(
        TENANT,
        "instance_other",
        actor_person_id="person_owner",
    )
    activation = service.dispatch_due(
        TENANT,
        "instance_other",
        worker_id="runtime-1",
    )[0]
    node = service.get(TENANT, "instance_other").nodes["approve_other"]
    repository.save_projection(
        ProjectionRecord(
            id="projection-2",
            tenant_id=TENANT,
            instance_id="instance_other",
            node_instance_id=node.id,
            attempt_no=activation.attempt_no,
            kind=FEISHU_TASK_KIND,
            external_id="task-2",
            external_url="https://example.invalid/task-2",
            idempotency_key="lf-binding-2",
            sync_version=node.version,
            state={"node_status": "waiting_human", "completed": False},
            created_at=clock.now,
            updated_at=clock.now,
        )
    )

    class PartlyFailingReader:
        def get_task(self, task_guid):
            if task_guid == "task-1":
                raise RuntimeError("temporary read failure")
            return task_state(guid=task_guid)

    report = TaskCompletionPoller(
        repository,
        repository,
        inbox,
        PartlyFailingReader(),
        tenant_id=TENANT,
        batch_size=1,
        clock=clock,
    ).run_once()

    assert report.instances_scanned == 2
    assert report.failed == 1
    assert report.signals_appended == 1
    assert report.errors == (
        "instance_inbound/approve: RuntimeError: temporary read failure",
    )
    assert inbox.records(TENANT)[0].event.task_guid == "task-2"


def test_readback_lag_retries_then_submits_without_losing_the_event():
    clock = Clock()
    service, repository, inbox, bridge = setup_inbound(clock)
    bridge("task.task.update_user_access_v2", event())
    reader = TaskReader(task_state(completed_assignee_ids=()))
    verification = verify(inbox, reader, clock)
    inbound = worker(service, repository, inbox, clock)

    first = verification.run_once()
    assert first.failed == 1
    assert inbox.records(TENANT)[0].status == InboxStatus.FAILED
    assert verification.run_once().claimed == 0

    clock.now += timedelta(seconds=5)
    reader.state = task_state()
    second = verification.run_once()
    assert second.verified == 1
    assert inbound.run_once().submitted == 1
    assert inbox.records(TENANT)[0].attempt_count == 3


def test_verification_stops_retrying_after_the_attempt_budget_is_exhausted():
    clock = Clock()
    _, _, inbox, bridge = setup_inbound(clock)
    bridge("task.task.update_user_access_v2", event("event-exhausted"))
    reader = TaskReader(task_state(status="todo", completed_at=None))
    verification = verify(inbox, reader, clock, max_attempts=2)

    first = verification.run_once()
    assert first.failed == 1
    assert first.exhausted == 0

    clock.now += timedelta(seconds=5)
    second = verification.run_once()
    record = inbox.records(TENANT)[0]

    assert second.failed == 1
    assert second.exhausted == 1
    assert record.status == InboxStatus.EXHAUSTED
    assert record.attempt_count == 2
    assert record.processed_at == clock.now
    assert record.outcome == "exhausted:verification_attempts"
    assert record.failure_stage == "verification"
    assert "exhausted after 2 attempts" in (record.last_error or "")

    clock.now += timedelta(days=1)
    assert verification.run_once().claimed == 0
    assert reader.calls == ["task-1", "task-1"]


def test_old_or_unbound_tasks_cannot_advance_target_state():
    clock = Clock()
    service, repository, inbox, bridge = setup_inbound(clock)
    bridge("task.task.update_user_access_v2", event("event-old"))
    reader = TaskReader(task_state(mode=2, extra=None))
    assert verify(inbox, reader, clock).run_once().verified == 1

    report = worker(service, repository, inbox, clock).run_once()

    assert report.rejected == 1
    assert service.get(TENANT, "instance_inbound").nodes["approve"].status == NodeStatus.WAITING_HUMAN
    assert inbox.records(TENANT)[0].outcome == "rejected:task_binding_mismatch"


def test_a_second_event_after_submission_is_a_noop_without_another_task_read():
    clock = Clock()
    service, repository, inbox, bridge = setup_inbound(clock)
    reader = TaskReader(task_state())
    verification = verify(inbox, reader, clock)
    inbound = worker(service, repository, inbox, clock)
    bridge("task.task.update_user_access_v2", event("event-first"))
    assert verification.run_once().verified == 1
    assert inbound.run_once().submitted == 1
    bridge("task.task.update_user_access_v2", event("event-second"))
    assert verification.run_once().verified == 1

    report = inbound.run_once()

    assert report.noops == 1
    assert reader.calls == ["task-1", "task-1"]
    assert sorted(record.outcome for record in inbox.records(TENANT)) == [
        "noops:already_terminal",
        "submitted:human_node",
    ]
