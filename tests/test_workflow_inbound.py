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


def verify(inbox, reader, clock):
    return TaskVerificationWorker(
        inbox,
        reader,
        tenant_id=TENANT,
        worker_id="verification-1",
        clock=clock,
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
    assert attempt.result == {
        "submission": "feishu_task_completed",
        "task_guid": "task-1",
        "completed_at": "1785585600000",
    }
    assert inbox.records(TENANT)[0].outcome == "submitted:human_node"


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
