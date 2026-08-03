"""Transactional outbox projection tests."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from larkflow.workflow import (
    ExternalDocument,
    ExternalMessage,
    ExternalTask,
    FEISHU_DOCUMENT_KIND,
    FEISHU_INSTANCE_MESSAGE_KIND,
    FEISHU_MESSAGE_KIND,
    FEISHU_TASK_KIND,
    InMemoryWorkflowRepository,
    InstanceSnapshot,
    NodeSpec,
    NodeStatus,
    OutboxEvent,
    OutboxStatus,
    RecoveryAction,
    WorkflowInstance,
    WorkflowProjectionWorker,
    WorkflowService,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
TENANT = "tenant_projection"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class RecordingTasks:
    def __init__(self, *, fail_after_creates: int = 0) -> None:
        self.fail_after_creates = fail_after_creates
        self.create_requests = []
        self.completed = []
        self.tasks = {}
        self.next_task_number = 1
        self.existence_checks = []

    def create_task(self, request):
        self.create_requests.append(request)
        existing = self.tasks.get(request.idempotency_key)
        if existing is not None:
            return existing
        task = ExternalTask(
            guid=f"task-{self.next_task_number}",
            url=f"https://example.invalid/tasks/{self.next_task_number}",
        )
        self.next_task_number += 1
        self.tasks[request.idempotency_key] = task
        if self.fail_after_creates:
            self.fail_after_creates -= 1
            raise RuntimeError("Feishu response was lost after task creation")
        return task

    def complete_task(self, task_guid):
        self.completed.append(task_guid)

    def task_exists(self, task_guid):
        self.existence_checks.append(task_guid)
        return any(task.guid == task_guid for task in self.tasks.values())

    def delete_task(self, task_guid):
        self.tasks = {
            key: task for key, task in self.tasks.items() if task.guid != task_guid
        }


class RecordingMessages:
    def __init__(self) -> None:
        self.requests = []

    def send_message(self, request):
        self.requests.append(request)
        return ExternalMessage(message_id=f"message-{len(self.requests)}")


class RecordingDocuments:
    def __init__(self) -> None:
        self.requests = []

    def create_document(self, request):
        self.requests.append(request)
        number = len(self.requests)
        return ExternalDocument(
            document_id=f"document-{number}",
            url=f"https://example.invalid/docs/document-{number}",
        )


def human_snapshot() -> InstanceSnapshot:
    return InstanceSnapshot(
        goal="Approve the launch brief",
        nodes=(
            NodeSpec(
                "approve",
                "Approve brief",
                "person_reviewer",
                "human",
                work={
                    "objective": "Approve the launch brief",
                    "inputs": [],
                    "outputs": [{"id": "decision", "type": "data"}],
                    "acceptance": ["A decision is recorded"],
                },
            ),
        ),
    )


def mixed_snapshot() -> InstanceSnapshot:
    work = {
        "objective": "Complete the current step",
        "inputs": [],
        "outputs": [{"id": "result", "type": "data"}],
        "acceptance": ["A result is recorded"],
    }
    return InstanceSnapshot(
        goal="Confirm, generate, and review",
        inputs={"brief": "Summarize the launch decision"},
        nodes=(
            NodeSpec(
                "confirm",
                "Confirm brief",
                "person_reviewer",
                "human",
                work={**work, "inputs": ["instance_inputs.brief"]},
            ),
            NodeSpec(
                "draft",
                "Draft summary",
                "person_owner",
                "agent",
                deps=("confirm",),
                work={
                    **work,
                    "agent": {
                        "kind": "llm.generate",
                        "model_role": "default",
                        "instructions": "Write the summary",
                    },
                },
            ),
            NodeSpec(
                "review",
                "Review summary",
                "person_reviewer",
                "human",
                deps=("draft",),
                work=work,
            ),
        ),
    )


def setup_human(clock: Clock):
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    service.create_draft(
        instance_id="instance_projection",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=human_snapshot(),
    )
    service.confirm_draft(
        TENANT,
        "instance_projection",
        actor_person_id="person_owner",
    )
    tasks = RecordingTasks()
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        tasks,
        tenant_id=TENANT,
        worker_id="projection_1",
        clock=clock,
        id_factory=lambda: "projection_1",
    )
    return service, repository, tasks, worker


def test_human_task_is_created_after_activation_and_completed_after_submission():
    clock = Clock()
    service, repository, tasks, worker = setup_human(clock)

    initial = worker.run_once()
    assert initial.claimed == 1
    assert initial.noops == 1
    assert tasks.create_requests == []

    activation = service.dispatch_due(
        TENANT,
        "instance_projection",
        worker_id="runtime_1",
    )[0]
    assert activation.status == NodeStatus.WAITING_HUMAN
    created = worker.run_once()
    assert created.tasks_created == 1
    request = tasks.create_requests[0]
    assert request.owner_person_id == "person_reviewer"
    assert request.summary == "Approve brief"
    assert request.idempotency_key.startswith("lf-")
    assert len(request.idempotency_key) == 51

    node = service.get(TENANT, "instance_projection").nodes["approve"]
    projection = repository.get_projection(
        TENANT,
        node.id,
        1,
        FEISHU_TASK_KIND,
    )
    assert projection is not None
    assert projection.external_id == "task-1"
    assert projection.state == {
        "node_status": NodeStatus.WAITING_HUMAN.value,
        "completed": False,
    }

    service.submit_human(
        TENANT,
        "instance_projection",
        "approve",
        actor_person_id="person_reviewer",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        result={"decision": "approved"},
    )
    completed = worker.run_once()
    assert completed.tasks_completed == 1
    assert tasks.completed == ["task-1"]
    assert worker.run_once().claimed == 0


def test_final_human_task_contains_the_committed_agent_result():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    service.create_draft(
        instance_id="instance_mixed",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=mixed_snapshot(),
    )
    service.confirm_draft(TENANT, "instance_mixed", actor_person_id="person_owner")
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
    assert projection.run_once().noops == 3

    confirmation = service.dispatch_due(
        TENANT,
        "instance_mixed",
        worker_id="runtime_1",
    )[0]
    assert projection.run_once().tasks_created == 1
    confirmation_request = tasks.create_requests[-1]
    assert "流程输入" in confirmation_request.description
    assert "[brief]" in confirmation_request.description
    assert "Summarize the launch decision" in confirmation_request.description
    service.submit_human(
        TENANT,
        "instance_mixed",
        "confirm",
        actor_person_id="person_reviewer",
        attempt_no=confirmation.attempt_no,
        expected_node_version=confirmation.expected_node_version,
        result={"approved": True},
    )
    agent = service.dispatch_due(
        TENANT,
        "instance_mixed",
        worker_id="runtime_1",
        max_automated=1,
    )[0]
    service.complete_automated(
        TENANT,
        "instance_mixed",
        "draft",
        attempt_no=agent.attempt_no,
        expected_node_version=agent.expected_node_version,
        claim_token=agent.claim_token or "",
        worker_id="runtime_1",
        result={
            "content": "结论：发布条件已经满足。下一步：安排灰度验证。",
            "agent_kind": "llm.generate",
            "model_role": "default",
        },
    )
    review = service.dispatch_due(
        TENANT,
        "instance_mixed",
        worker_id="runtime_1",
    )[0]
    assert review.node_key == "review"

    report = projection.run_once()

    assert report.tasks_completed == 1
    assert report.tasks_created == 1
    final_request = tasks.create_requests[-1]
    assert final_request.summary == "Review summary"
    assert "上游结果" in final_request.description
    assert "[Draft summary / draft]" in final_request.description
    assert "结论：发布条件已经满足。下一步：安排灰度验证。" in final_request.description
    assert "agent_kind" not in final_request.description


def test_ambiguous_external_create_retries_with_the_same_idempotency_key():
    clock = Clock()
    service, repository, _, initial_worker = setup_human(clock)
    assert initial_worker.run_once().noops == 1
    service.dispatch_due(
        TENANT,
        "instance_projection",
        worker_id="runtime_1",
    )
    tasks = RecordingTasks(fail_after_creates=1)
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        tasks,
        tenant_id=TENANT,
        worker_id="projection_1",
        clock=clock,
        retry_base=timedelta(seconds=5),
        retry_max=timedelta(seconds=20),
    )

    report = worker.run_once()
    assert report.claimed == 1
    assert report.published == 0
    assert report.failed == 1
    failed = [
        record
        for record in repository.outbox_records(TENANT)
        if record.status == OutboxStatus.FAILED
    ]
    assert len(failed) == 1
    assert "RuntimeError" in (failed[0].last_error or "")
    assert len(tasks.tasks) == 1
    assert repository.projection_records(TENANT) == ()
    assert worker.run_once().claimed == 0

    clock.now += timedelta(seconds=5)
    recovered = worker.run_once()
    assert recovered.tasks_created == 1
    assert recovered.failed == 0
    assert len(tasks.create_requests) == 2
    assert (
        tasks.create_requests[0].idempotency_key
        == tasks.create_requests[1].idempotency_key
    )
    assert len(tasks.tasks) == 1


def test_non_human_projection_events_are_published_without_external_io():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    service.create_draft(
        instance_id="instance_tool",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec(
                    "publish",
                    "Publish",
                    "person_owner",
                    "tool",
                    work={
                        "objective": "Publish",
                        "inputs": [],
                        "outputs": [{"id": "url", "type": "data"}],
                        "acceptance": ["A URL exists"],
                        "tool": {"kind": "document.publish", "args": {}},
                    },
                ),
            )
        ),
    )
    service.confirm_draft(TENANT, "instance_tool", actor_person_id="person_owner")
    tasks = RecordingTasks()
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        tasks,
        tenant_id=TENANT,
        worker_id="projection_1",
        clock=clock,
    )

    report = worker.run_once()

    assert report.published == 1
    assert report.noops == 1
    assert tasks.create_requests == []
    assert repository.projection_records(TENANT) == ()


def test_failed_agent_projects_a_recovery_card_and_takeover_task():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    service.create_draft(
        instance_id="instance_failure_card",
        tenant_id=TENANT,
        owner_person_id="person_initiator",
        actor_person_id="person_initiator",
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
    service.confirm_draft(
        TENANT,
        "instance_failure_card",
        actor_person_id="person_initiator",
    )
    tasks = RecordingTasks()
    messages = RecordingMessages()
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        tasks,
        messages,
        tenant_id=TENANT,
        worker_id="projection_1",
        clock=clock,
    )
    assert worker.run_once().noops == 1
    activation = service.dispatch_due(
        TENANT,
        "instance_failure_card",
        worker_id="edge_1",
    )[0]
    failed = service.fail_automated(
        TENANT,
        "instance_failure_card",
        "draft",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        claim_token=activation.claim_token or "",
        worker_id="edge_1",
        error_code="provider_timeout",
        error_message="secret provider response",
    )

    assert worker.run_once().messages_sent == 1
    request = messages.requests[0]
    assert request.recipient_person_id == "person_agent_owner"
    assert request.card is not None
    rendered = str(request.card)
    assert "provider_timeout" in rendered
    assert "secret provider response" not in rendered
    buttons = request.card["body"]["elements"][1]["columns"]
    actions = {
        column["elements"][0]["behaviors"][0]["value"]["action"]
        for column in buttons
    }
    assert actions == {"retry", "human_takeover"}

    node = failed.nodes["draft"]
    service.recover_failed_node(
        TENANT,
        failed.id,
        "draft",
        RecoveryAction.HUMAN_TAKEOVER,
        actor_person_id="person_agent_owner",
        expected_instance_version=failed.version,
        expected_node_version=node.version,
        expected_attempt_no=node.current_attempt_no,
    )
    projected = worker.run_once()

    assert projected.tasks_created == 1
    assert tasks.create_requests[0].owner_person_id == "person_agent_owner"
    assert "Agent 失败后的人工接管" in tasks.create_requests[0].description


def test_projection_ignores_stale_event_for_future_node_removed_by_graph_edit():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    service.create_draft(
        instance_id="instance_edit_projection",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=mixed_snapshot(),
    )
    service.confirm_draft(
        TENANT,
        "instance_edit_projection",
        actor_person_id="person_owner",
    )
    preview = service.preview_graph_edit(
        TENANT,
        "instance_edit_projection",
        ({"op": "remove_node", "node_key": "review"},),
        actor_person_id="person_owner",
    )
    service.confirm_graph_edit(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    )
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        RecordingTasks(),
        tenant_id=TENANT,
        worker_id="projection_edit",
        clock=clock,
    )

    report = worker.run_once()

    assert report.failed == 0
    assert report.noops == 3
    assert worker.run_once().claimed == 0
    assert "review" not in service.get(
        TENANT,
        "instance_edit_projection",
    ).nodes


def test_automated_result_and_completed_instance_project_to_im_and_doc():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    service.create_draft(
        instance_id="instance_outputs",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            goal="Produce a reviewed summary",
            nodes=(
                NodeSpec(
                    "draft",
                    "Draft summary",
                    "person_owner",
                    "agent",
                    work={
                        "objective": "Draft the summary",
                        "inputs": [],
                        "outputs": [{"id": "content", "type": "document"}],
                        "acceptance": ["A summary exists"],
                        "agent": {
                            "kind": "llm.generate",
                            "model_role": "default",
                            "instructions": "Write the summary",
                        },
                    },
                ),
            ),
        ),
    )
    service.confirm_draft(TENANT, "instance_outputs", actor_person_id="person_owner")
    messages = RecordingMessages()
    documents = RecordingDocuments()
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        RecordingTasks(),
        message_adapter=messages,
        document_adapter=documents,
        tenant_id=TENANT,
        worker_id="projection_1",
        clock=clock,
    )
    assert worker.run_once().noops == 1
    activation = service.dispatch_due(
        TENANT,
        "instance_outputs",
        worker_id="runtime_1",
    )[0]
    service.complete_automated(
        TENANT,
        "instance_outputs",
        "draft",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        claim_token=activation.claim_token or "",
        worker_id="runtime_1",
        result={"content": "A complete summary"},
    )

    report = worker.run_once()

    assert report.messages_sent == 2
    assert report.documents_created == 1
    assert any("A complete summary" in request.text for request in messages.requests)
    assert any("汇总文档" in request.text for request in messages.requests)
    assert "<title>Produce a reviewed summary</title>" in documents.requests[0].content_xml
    node = service.get(TENANT, "instance_outputs").nodes["draft"]
    records = {
        record.kind: record
        for record in repository.projection_records(TENANT)
        if record.node_instance_id == node.id
    }
    assert set(records) == {
        FEISHU_MESSAGE_KIND,
        FEISHU_DOCUMENT_KIND,
        FEISHU_INSTANCE_MESSAGE_KIND,
    }
    assert worker.run_once().claimed == 0


def test_explicit_reconciliation_repairs_a_missing_instance_completion_outbox():
    clock = Clock()
    service, source, _, _ = setup_human(clock)
    activation = service.dispatch_due(
        TENANT,
        "instance_projection",
        worker_id="runtime_1",
    )[0]
    service.submit_human(
        TENANT,
        "instance_projection",
        "approve",
        actor_person_id="person_reviewer",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        result={"decision": "approved"},
    )

    repository = InMemoryWorkflowRepository()
    repository.add(source.get(TENANT, "instance_projection"))
    messages = RecordingMessages()
    documents = RecordingDocuments()
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        RecordingTasks(),
        message_adapter=messages,
        document_adapter=documents,
        tenant_id=TENANT,
        worker_id="projection_repair",
        clock=clock,
    )

    repaired = worker.reconcile_instance_completion("instance_projection")

    assert repaired.documents_created == 1
    assert repaired.messages_sent == 1
    assert repaired.noops == 0
    assert len(documents.requests) == 1
    assert len(messages.requests) == 1
    assert "流程已完成" in messages.requests[0].text

    unchanged = worker.reconcile_instance_completion("instance_projection")

    assert unchanged.documents_created == 0
    assert unchanged.messages_sent == 0
    assert unchanged.noops == 1
    assert len(documents.requests) == 1
    assert len(messages.requests) == 1


def test_completed_instance_restart_creates_new_completion_projections():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    service.create_draft(
        instance_id="instance_completion_restart",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=human_snapshot(),
    )
    service.confirm_draft(
        TENANT,
        "instance_completion_restart",
        actor_person_id="person_owner",
    )
    tasks = RecordingTasks()
    messages = RecordingMessages()
    documents = RecordingDocuments()
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        tasks,
        message_adapter=messages,
        document_adapter=documents,
        tenant_id=TENANT,
        worker_id="projection_restart",
        clock=clock,
    )
    assert worker.run_once().noops == 1

    first = service.dispatch_ready(TENANT, "instance_completion_restart")[0]
    assert worker.run_once().tasks_created == 1
    service.submit_human(
        TENANT,
        "instance_completion_restart",
        "approve",
        actor_person_id="person_reviewer",
        attempt_no=first.attempt_no,
        expected_node_version=first.expected_node_version,
        result={"decision": "approved"},
    )
    first_completion = worker.run_once()
    assert first_completion.documents_created == 1
    assert first_completion.messages_sent == 1

    preview = service.preview_instance_restart(
        TENANT,
        "instance_completion_restart",
        actor_person_id="person_owner",
    )
    service.confirm_restart(
        TENANT,
        preview.id,
        actor_person_id="person_owner",
    )
    assert worker.run_once().tasks_completed == 0
    second = service.dispatch_ready(TENANT, "instance_completion_restart")[0]
    assert worker.run_once().tasks_created == 1
    service.submit_human(
        TENANT,
        "instance_completion_restart",
        "approve",
        actor_person_id="person_reviewer",
        attempt_no=second.attempt_no,
        expected_node_version=second.expected_node_version,
        result={"decision": "approved again"},
    )
    second_completion = worker.run_once()

    assert second_completion.documents_created == 1
    assert second_completion.messages_sent == 1
    assert len(documents.requests) == 2
    assert len(messages.requests) == 2
    records = repository.projection_records(TENANT)
    completion_records = [
        record
        for record in records
        if record.kind in {FEISHU_DOCUMENT_KIND, FEISHU_INSTANCE_MESSAGE_KIND}
    ]
    assert {record.attempt_no for record in completion_records} == {1, 2}
    assert len({record.idempotency_key for record in completion_records}) == 4


def test_projection_worker_does_not_claim_events_owned_by_other_consumers():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    aggregate = WorkflowInstance(
        id="instance_other_event",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        snapshot=InstanceSnapshot(nodes=()),
        created_at=NOW,
    )
    repository.add(
        aggregate,
        outbox_events=(
            OutboxEvent(
                id="outbox_other",
                tenant_id=TENANT,
                aggregate_type="instance",
                aggregate_id=aggregate.id,
                aggregate_version=0,
                event_type="instance.notification_requested",
                payload={"instance_id": aggregate.id},
                created_at=NOW,
                available_at=NOW,
            ),
        ),
    )
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        RecordingTasks(),
        tenant_id=TENANT,
        worker_id="projection_1",
        clock=clock,
    )

    assert worker.run_once().claimed == 0
    assert repository.outbox_records(TENANT)[0].status == OutboxStatus.PENDING


def test_projection_identity_and_version_cannot_change():
    clock = Clock()
    service, repository, _, worker = setup_human(clock)
    service.dispatch_due(
        TENANT,
        "instance_projection",
        worker_id="runtime_1",
    )
    worker.run_once()
    projection = repository.projection_records(TENANT)[0]

    with pytest.raises(ValueError, match="external id"):
        repository.save_projection(replace(projection, external_id="task-other"))

    with pytest.raises(ValueError, match="version"):
        repository.save_projection(replace(projection, sync_version=-1))


def test_startup_reconciliation_rebuilds_a_missing_current_human_task():
    clock = Clock()
    service, repository, tasks, worker = setup_human(clock)
    service.dispatch_due(
        TENANT,
        "instance_projection",
        worker_id="runtime_1",
    )

    rebuilt = worker.reconcile_all(batch_size=1)

    assert rebuilt.instances_scanned == 1
    assert rebuilt.nodes_scanned == 1
    assert rebuilt.tasks_created == 1
    assert rebuilt.failed == 0
    assert len(tasks.create_requests) == 1
    assert tasks.existence_checks == []
    assert len(repository.projection_records(TENANT)) == 1

    unchanged = worker.reconcile_all(batch_size=1)

    assert unchanged.unchanged == 1
    assert unchanged.tasks_created == 0
    assert len(tasks.create_requests) == 1
    assert tasks.existence_checks == ["task-1"]


def test_startup_reconciliation_continues_after_one_instance_fails():
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=clock)
    for instance_id in ("instance_a", "instance_b"):
        service.create_draft(
            instance_id=instance_id,
            tenant_id=TENANT,
            owner_person_id="person_owner",
            actor_person_id="person_owner",
            snapshot=human_snapshot(),
        )
        service.confirm_draft(TENANT, instance_id, actor_person_id="person_owner")
        service.dispatch_due(TENANT, instance_id, worker_id="runtime_1")
    tasks = RecordingTasks(fail_after_creates=1)
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        tasks,
        tenant_id=TENANT,
        worker_id="projection_1",
        clock=clock,
    )

    first = worker.reconcile_all(batch_size=1)

    assert first.instances_scanned == 2
    assert first.nodes_scanned == 2
    assert first.tasks_created == 1
    assert first.failed == 1
    assert "instance_a" in first.errors[0]
    assert len(tasks.tasks) == 2
    assert len(repository.projection_records(TENANT)) == 1

    recovered = worker.reconcile_all(batch_size=1)

    assert recovered.tasks_created == 1
    assert recovered.unchanged == 1
    assert recovered.failed == 0
    assert len(tasks.tasks) == 2
    assert len(repository.projection_records(TENANT)) == 2


def test_startup_reconciliation_does_not_create_a_missing_terminal_task():
    clock = Clock()
    service, repository, tasks, worker = setup_human(clock)
    activation = service.dispatch_due(
        TENANT,
        "instance_projection",
        worker_id="runtime_1",
    )[0]
    service.submit_human(
        TENANT,
        "instance_projection",
        "approve",
        actor_person_id="person_reviewer",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        result={"decision": "approved"},
    )

    report = worker.reconcile_all()

    assert report.tasks_created == 0
    assert report.tasks_completed == 0
    assert report.instances_scanned == 0
    assert report.nodes_scanned == 0
    assert tasks.create_requests == []


def test_startup_reconciliation_completes_an_existing_terminal_task():
    clock = Clock()
    service, _, tasks, worker = setup_human(clock)
    activation = service.dispatch_due(
        TENANT,
        "instance_projection",
        worker_id="runtime_1",
    )[0]
    assert worker.reconcile_all().tasks_created == 1
    service.submit_human(
        TENANT,
        "instance_projection",
        "approve",
        actor_person_id="person_reviewer",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        result={"decision": "approved"},
    )

    report = worker.reconcile_all()

    assert report.tasks_completed == 1
    assert tasks.completed == ["task-1"]


def test_startup_reconciliation_recreates_a_confirmed_missing_external_task():
    clock = Clock()
    service, repository, tasks, worker = setup_human(clock)
    service.dispatch_due(
        TENANT,
        "instance_projection",
        worker_id="runtime_1",
    )
    assert worker.reconcile_all().tasks_created == 1
    original = repository.projection_records(TENANT)[0]
    tasks.delete_task(original.external_id or "")

    rebuilt = worker.reconcile_all()

    current = repository.projection_records(TENANT)[0]
    assert rebuilt.tasks_recreated == 1
    assert current.external_id == "task-2"
    assert current.external_id != original.external_id
    assert current.idempotency_key != original.idempotency_key
    assert current.state["repair_generation"] == 1

    unchanged = worker.reconcile_all()
    assert unchanged.unchanged == 1
    assert len(tasks.create_requests) == 2


def test_missing_task_recreation_reuses_one_repair_key_after_a_lost_response():
    clock = Clock()
    service, repository, tasks, worker = setup_human(clock)
    service.dispatch_due(
        TENANT,
        "instance_projection",
        worker_id="runtime_1",
    )
    assert worker.reconcile_all().tasks_created == 1
    original = repository.projection_records(TENANT)[0]
    tasks.delete_task(original.external_id or "")
    tasks.fail_after_creates = 1

    lost = worker.reconcile_all()

    assert lost.failed == 1
    assert repository.projection_records(TENANT)[0] == original
    repair_key = tasks.create_requests[-1].idempotency_key

    recovered = worker.reconcile_all()

    replacement = repository.projection_records(TENANT)[0]
    assert recovered.tasks_recreated == 1
    assert tasks.create_requests[-1].idempotency_key == repair_key
    assert replacement.external_id == "task-2"
    assert len(tasks.tasks) == 1
