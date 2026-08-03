"""Opt-in integration test against a disposable PostgreSQL database."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier
from uuid import uuid4

import pytest
from psycopg.errors import RaiseException

from larkflow.workflow import (
    AutomatedExecutor,
    ConcurrentUpdateError,
    ExecutionRequest,
    ExecutionResult,
    ExecutorKind,
    EdgeControlService,
    DeviceRevokedError,
    ExternalTask,
    ExternalTaskState,
    InstanceSnapshot,
    InstanceStatus,
    InvalidInboxClaimError,
    NodeRunner,
    NodeSpec,
    PostgresWorkflowInbox,
    PostgresWorkflowRepository,
    PostgresEdgeStore,
    PairingCodeUsedError,
    ProjectionRecord,
    RestartScope,
    TaskCompletionSignal,
    TemplateService,
    TemplateStatus,
    WorkflowService,
    WorkflowProjectionWorker,
    WorkflowWorker,
    apply_migrations,
    postgres_connection_factory,
)


POSTGRES_DSN = os.environ.get("LARKFLOW_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="set LARKFLOW_TEST_POSTGRES_DSN to a disposable PostgreSQL database",
)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingExecutor(AutomatedExecutor):
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        return ExecutionResult(result={"value": "recovered"})


class RecordingProjectionTasks:
    def __init__(self) -> None:
        self.tasks = {}
        self.next_task_number = 1

    def create_task(self, request):
        existing = self.tasks.get(request.idempotency_key)
        if existing is not None:
            return existing
        task = ExternalTask(guid=f"task_{self.next_task_number}")
        self.next_task_number += 1
        self.tasks[request.idempotency_key] = task
        return task

    def complete_task(self, _task_guid):
        return None

    def task_exists(self, task_guid):
        return any(task.guid == task_guid for task in self.tasks.values())

    def delete_task(self, task_guid):
        self.tasks = {
            key: task for key, task in self.tasks.items() if task.guid != task_guid
        }


class BarrierRepository(PostgresWorkflowRepository):
    """Make competing dispatches read the same aggregate version."""

    def __init__(self, connection_factory, barrier: Barrier) -> None:
        super().__init__(connection_factory)
        self.barrier = barrier

    def get(self, tenant_id: str, instance_id: str):
        instance = super().get(tenant_id, instance_id)
        self.barrier.wait(timeout=5)
        return instance


class BarrierRestartRepository(PostgresWorkflowRepository):
    """Make two restart confirmations reach the atomic save together."""

    def __init__(self, connection_factory, barrier: Barrier) -> None:
        super().__init__(connection_factory)
        self.barrier = barrier

    def save_restart(self, *args, **kwargs):
        self.barrier.wait(timeout=5)
        return super().save_restart(*args, **kwargs)


def template_document() -> dict:
    return {
        "schema_version": "0.2",
        "template": {
            "id": "postgres_review",
            "version": 1,
            "name": "PostgreSQL review",
            "status": "draft",
            "locked": True,
        },
        "goal": "Verify template persistence",
        "parameters": {"brief": {"type": "text", "required": True}},
        "nodes": [
            {
                "id": "review",
                "title": "Review",
                "owner_role": "project_owner",
                "executor": "human",
                "deps": [],
                "work": {
                    "objective": "Review the brief",
                    "inputs": ["instance_inputs.brief"],
                    "outputs": [{"id": "decision", "type": "data"}],
                    "acceptance": ["A decision exists"],
                },
            }
        ],
    }


def test_postgres_persists_template_lifecycle_and_frozen_instance_snapshot():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_template_{suffix}"
    template_id = f"postgres_review_{suffix}"
    source = template_document()
    source["template"]["id"] = template_id
    instance_id = f"instance_template_{suffix}"
    repository = PostgresWorkflowRepository(connection_factory)
    templates = TemplateService(repository)

    template, version = templates.create_template(
        tenant_id=tenant_id,
        actor_person_id="person_owner",
        document=source,
    )
    enabled = templates.enable(
        tenant_id,
        template_id,
        actor_person_id="person_owner",
    )
    snapshot = templates.instantiate(
        tenant_id,
        template_id,
        inputs={"brief": "Synthetic PostgreSQL validation"},
        owner_bindings={"project_owner": "person_owner"},
    )
    created = WorkflowService(repository).create_draft(
        instance_id=instance_id,
        tenant_id=tenant_id,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot,
    )

    assert template.status == TemplateStatus.DRAFT
    assert enabled.status == TemplateStatus.ENABLED
    assert version.id == f"{template_id}:1"
    assert created.snapshot.template_version_id == version.id
    assert created.snapshot.locked is True
    assert repository.get(tenant_id, instance_id).snapshot == snapshot
    assert [event.event_type for event in repository.template_audit_log(
        tenant_id, template_id
    )] == ["template.created", "template.enabled"]

    with pytest.raises(RaiseException):
        with connection_factory() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE workflow_template_versions SET locked = false
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, version.id),
                )
    with pytest.raises(RaiseException):
        with connection_factory() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    DELETE FROM workflow_template_events
                    WHERE tenant_id = %s AND template_id = %s
                    """,
                    (tenant_id, template_id),
                )


def test_postgres_owner_instance_list_is_isolated_ordered_and_bounded():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_owner_list_{suffix}"
    owner_person_id = f"person_owner_{suffix}"
    repository = PostgresWorkflowRepository(connection_factory)
    clock = Clock(datetime(2026, 8, 3, 2, 0, tzinfo=timezone.utc))
    service = WorkflowService(repository, clock=clock)
    work = {
        "objective": "Review the brief",
        "inputs": [],
        "outputs": [{"id": "decision", "type": "data"}],
        "acceptance": ["A decision exists"],
    }
    snapshot = InstanceSnapshot(
        goal="Owner list isolation",
        nodes=(NodeSpec("review", "Review", owner_person_id, "human", work=work),),
    )
    expected_ids = []
    for index in range(12):
        clock.now = datetime(2026, 8, 3, 2, index, tzinfo=timezone.utc)
        instance_id = f"owner_{index:02d}_{suffix}"
        expected_ids.append(instance_id)
        service.create_draft(
            instance_id=instance_id,
            tenant_id=tenant_id,
            owner_person_id=owner_person_id,
            actor_person_id=owner_person_id,
            snapshot=snapshot,
        )
    clock.now = datetime(2026, 8, 3, 3, 0, tzinfo=timezone.utc)
    service.create_draft(
        instance_id=f"foreign_owner_{suffix}",
        tenant_id=tenant_id,
        owner_person_id=f"person_other_{suffix}",
        actor_person_id=f"person_other_{suffix}",
        snapshot=snapshot,
    )

    summaries = service.list_for_owner(
        tenant_id,
        actor_person_id=owner_person_id,
        limit=10,
    )

    assert [summary.id for summary in summaries] == list(reversed(expected_ids[2:]))
    assert all(summary.completed_nodes == 0 for summary in summaries)
    assert all(summary.total_nodes == 1 for summary in summaries)
    with connection_factory() as connection:
        index_row = connection.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'workflow_instances_owner_recent_idx'
            """
        ).fetchone()
    assert index_row is not None


@pytest.mark.parametrize("restart_scope", (RestartScope.NODE, RestartScope.INSTANCE))
def test_postgres_restart_preview_is_durable_and_consumed_exactly_once(
    restart_scope,
):
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_restart_{suffix}"
    instance_id = f"instance_restart_{suffix}"
    owner = f"person_owner_{suffix}"
    repository = PostgresWorkflowRepository(connection_factory)
    clock = Clock(datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc))
    service = WorkflowService(repository, clock=clock)
    snapshot = InstanceSnapshot(
        nodes=(
            NodeSpec(
                "review",
                "Review",
                owner,
                "human",
                work={
                    "objective": "Review the brief",
                    "inputs": [],
                    "outputs": [{"id": "decision", "type": "data"}],
                    "acceptance": ["A decision exists"],
                },
            ),
        )
    )
    service.create_draft(
        instance_id=instance_id,
        tenant_id=tenant_id,
        owner_person_id=owner,
        actor_person_id=owner,
        snapshot=snapshot,
    )
    service.confirm_draft(tenant_id, instance_id, actor_person_id=owner)
    activation = service.dispatch_ready(tenant_id, instance_id)[0]
    service.submit_human(
        tenant_id,
        instance_id,
        "review",
        actor_person_id=owner,
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        result={"decision": "approved"},
    )
    version_before = repository.get(tenant_id, instance_id).version
    if restart_scope == RestartScope.NODE:
        preview = service.preview_node_restart(
            tenant_id,
            instance_id,
            "review",
            actor_person_id=owner,
        )
    else:
        preview = service.preview_instance_restart(
            tenant_id,
            instance_id,
            actor_person_id=owner,
        )
    assert repository.get_restart_preview(tenant_id, preview.id) == preview

    barrier = Barrier(2)

    def confirm(_index):
        concurrent = WorkflowService(
            BarrierRestartRepository(connection_factory, barrier),
            clock=clock,
        )
        return concurrent.confirm_restart(
            tenant_id,
            preview.id,
            actor_person_id=owner,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmations = list(pool.map(confirm, (1, 2)))

    assert sorted(item.already_applied for item in confirmations) == [False, True]
    restarted = repository.get(tenant_id, instance_id)
    assert restarted.version == version_before + 1
    assert restarted.status == InstanceStatus.RUNNING
    assert restarted.nodes["review"].current_attempt_no == 2
    assert restarted.nodes["review"].status.value == "ready"
    assert restarted.attempts[("review", 1)].result == {"decision": "approved"}
    assert restarted.attempts[("review", 2)].result is None
    stored = repository.get_restart_preview(tenant_id, preview.id)
    assert stored.consumed_at == clock.now
    assert stored.applied_instance_version == restarted.version
    with connection_factory() as connection:
        audit_count = connection.execute(
            """
            SELECT count(*) AS count FROM workflow_audit_events
            WHERE tenant_id = %s AND instance_id = %s
              AND event_type = %s
            """,
            (
                tenant_id,
                instance_id,
                (
                    "instance.node_restarted"
                    if restart_scope == RestartScope.NODE
                    else "instance.restarted"
                ),
            ),
        ).fetchone()["count"]
        index_row = connection.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'workflow_restart_previews_open_idx'
            """
        ).fetchone()
    assert audit_count == 1
    assert index_row is not None
    assert stored.scope == restart_scope


def test_postgres_persists_a_dependent_draft_before_nodes_are_materialized():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_draft_{suffix}"
    instance_id = f"instance_draft_{suffix}"
    repository = PostgresWorkflowRepository(connection_factory)
    service = WorkflowService(repository)
    work = {
        "objective": "Complete the step",
        "inputs": [],
        "outputs": [{"id": "result", "type": "data"}],
        "acceptance": ["A result exists"],
    }

    draft = service.create_draft(
        instance_id=instance_id,
        tenant_id=tenant_id,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec("first", "First", "person_owner", "human", work=work),
                NodeSpec(
                    "second",
                    "Second",
                    "person_owner",
                    "human",
                    deps=("first",),
                    work=work,
                ),
            )
        ),
    )

    assert draft.nodes == {}
    assert repository.get(tenant_id, instance_id) == draft

    confirmed = service.confirm_draft(
        tenant_id,
        instance_id,
        actor_person_id="person_owner",
    )
    assert tuple(confirmed.nodes) == ("first", "second")
    with connection_factory() as connection:
        dependencies = connection.execute(
            """
            SELECT node_key, dependency_key
            FROM workflow_node_dependencies
            WHERE tenant_id = %s AND instance_id = %s
            """,
            (tenant_id, instance_id),
        ).fetchall()
    assert dependencies == [{"node_key": "second", "dependency_key": "first"}]


def test_postgres_round_trip_audit_outbox_and_optimistic_concurrency():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    assert apply_migrations(connection_factory) == ()

    suffix = uuid4().hex
    tenant_id = f"tenant_{suffix}"
    instance_id = f"instance_{suffix}"
    repository = PostgresWorkflowRepository(connection_factory)
    service = WorkflowService(
        repository,
        runner=NodeRunner(token_factory=lambda: f"claim_{suffix}"),
        clock=lambda: datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
    )
    service.create_draft(
        instance_id=instance_id,
        tenant_id=tenant_id,
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
                        "outputs": [{"id": "document", "type": "document"}],
                        "acceptance": ["A URL exists"],
                        "tool": {"kind": "document.publish", "args": {}},
                    },
                ),
            )
        ),
    )
    service.confirm_draft(
        tenant_id, instance_id, actor_person_id="person_owner"
    )
    activation = service.dispatch_ready(
        tenant_id, instance_id, worker_id="worker_1"
    )[0]
    finished = service.complete_automated(
        tenant_id,
        instance_id,
        "publish",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        claim_token=activation.claim_token or "",
        result={"url": "https://example.invalid/document"},
        worker_id="worker_1",
    )
    assert finished.status == InstanceStatus.DONE

    restored = repository.get(tenant_id, instance_id)
    assert restored == finished
    assert restored.current_attempt("publish").claim_token is None
    assert restored.current_attempt("publish").claim_expires_at is None
    first = repository.get(tenant_id, instance_id)
    second = repository.get(tenant_id, instance_id)
    repository.save(first, expected_version=first.version)
    with pytest.raises(ConcurrentUpdateError):
        repository.save(second, expected_version=second.version)

    with connection_factory() as connection:
        audit_types = [
            row["event_type"]
            for row in connection.execute(
                """
                SELECT event_type FROM workflow_audit_events
                WHERE tenant_id = %s AND instance_id = %s
                ORDER BY occurred_at, id
                """,
                (tenant_id, instance_id),
            ).fetchall()
        ]
        outbox_count = connection.execute(
            "SELECT count(*) AS count FROM workflow_outbox_events WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()["count"]
    assert set(audit_types) == {
        "instance.draft_created",
        "instance.confirmed",
        "node.activated",
        "node.automated_completed",
        "instance.completed",
    }
    assert outbox_count == 2

    node = restored.nodes["publish"]
    projection = ProjectionRecord(
        id=f"projection_{suffix}",
        tenant_id=tenant_id,
        instance_id=instance_id,
        node_instance_id=node.id,
        attempt_no=1,
        kind="feishu_task",
        external_id=f"task_{suffix}",
        external_url="https://example.invalid/task",
        idempotency_key=f"idem_{suffix}",
        sync_version=node.version,
        state={"node_status": "done", "completed": True},
        created_at=datetime(2026, 8, 1, 10, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 1, 10, 1, tzinfo=timezone.utc),
    )
    repository.save_projection(projection)
    assert repository.get_projection(
        tenant_id,
        node.id,
        1,
        "feishu_task",
    ) == projection

    claim = repository.claim_outbox(
        tenant_id,
        worker_id="outbox_worker_1",
        now=datetime(2026, 8, 1, 10, 1, tzinfo=timezone.utc),
        limit=1,
    )[0]
    repository.mark_outbox_published(
        tenant_id,
        claim.event.id,
        claim_token=claim.claim_token,
        now=datetime(2026, 8, 1, 10, 2, tzinfo=timezone.utc),
    )
    with connection_factory() as connection:
        status = connection.execute(
            """
            SELECT status FROM workflow_outbox_events
            WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, claim.event.id),
        ).fetchone()["status"]
        assert status == "published"
        with pytest.raises(RaiseException, match="append-only"):
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE workflow_audit_events SET payload = '{}'::jsonb
                    WHERE tenant_id = %s AND instance_id = %s
                    """,
                    (tenant_id, instance_id),
                )


def test_postgres_projection_reconciliation_rebuilds_missing_tasks():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_projection_{suffix}"
    instance_id = f"instance_projection_{suffix}"
    repository = PostgresWorkflowRepository(connection_factory)
    service = WorkflowService(repository)
    work = {
        "objective": "Review the brief",
        "inputs": [],
        "outputs": [{"id": "decision", "type": "data"}],
        "acceptance": ["A decision exists"],
    }
    service.create_draft(
        instance_id=instance_id,
        tenant_id=tenant_id,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec(
                    "review",
                    "Review",
                    "person_owner",
                    "human",
                    work=work,
                ),
            )
        ),
    )
    service.confirm_draft(tenant_id, instance_id, actor_person_id="person_owner")
    assert repository.projection_instance_ids(tenant_id) == ()
    service.dispatch_due(tenant_id, instance_id, worker_id="runtime_1")
    assert repository.projection_instance_ids(tenant_id) == (instance_id,)
    tasks = RecordingProjectionTasks()
    worker = WorkflowProjectionWorker(
        repository,
        repository,
        repository,
        tasks,
        tenant_id=tenant_id,
        worker_id="projection_1",
    )

    created = worker.reconcile_all(batch_size=1)

    assert created.tasks_created == 1
    node = repository.get(tenant_id, instance_id).nodes["review"]
    original = repository.get_projection(
        tenant_id,
        node.id,
        1,
        "feishu_task",
    )
    assert original is not None
    tasks.delete_task(original.external_id or "")

    rebuilt = worker.reconcile_all(batch_size=1)

    replacement = repository.get_projection(
        tenant_id,
        node.id,
        1,
        "feishu_task",
    )
    assert replacement is not None
    assert rebuilt.tasks_recreated == 1
    assert replacement.external_id != original.external_id
    assert replacement.state["repair_generation"] == 1


def test_postgres_inbox_dedupes_and_allows_only_one_competing_claim():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_inbox_{suffix}"
    event = TaskCompletionSignal(
        id=f"event_{suffix}",
        tenant_id=tenant_id,
        task_guid=f"task_{suffix}",
        event_types=("task_completed_update",),
        occurred_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        received_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        source="feishu_task_poll",
        event_type="larkflow.task.completion_reconciled_v1",
    )
    inbox = PostgresWorkflowInbox(connection_factory)
    assert inbox.append_inbox(event) is True
    assert inbox.append_inbox(event) is False

    def verify_claim(worker_id):
        return inbox.claim_inbox_verification(
            tenant_id,
            worker_id=worker_id,
            now=datetime(2026, 8, 1, 10, 1, tzinfo=timezone.utc),
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        verification_claims = list(
            pool.map(verify_claim, ("verification_1", "verification_2"))
        )
    assert sorted(len(items) for items in verification_claims) == [0, 1]
    verification = next(items[0] for items in verification_claims if items)
    assert verification.event.source == "feishu_task_poll"
    assert (
        verification.event.event_type
        == "larkflow.task.completion_reconciled_v1"
    )
    inbox.mark_inbox_verified(
        tenant_id,
        event.id,
        claim_token=verification.claim_token,
        task_state=ExternalTaskState(
            guid=event.task_guid,
            status="done",
            mode=1,
            completed_at="1785585600000",
            source=6,
            extra="binding",
            assignee_ids=("person_owner",),
            completed_assignee_ids=("person_owner",),
        ),
        now=datetime(2026, 8, 1, 10, 1, tzinfo=timezone.utc),
    )

    def claim(worker_id):
        return inbox.claim_inbox(
            tenant_id,
            worker_id=worker_id,
            now=datetime(2026, 8, 1, 10, 1, tzinfo=timezone.utc),
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("inbound_1", "inbound_2")))
    assert sorted(len(items) for items in claims) == [0, 1]
    claimed = next(items[0] for items in claims if items)
    assert claimed.task_state is not None
    with pytest.raises(InvalidInboxClaimError):
        inbox.mark_inbox_processed(
            tenant_id,
            event.id,
            claim_token="wrong",
            outcome="submitted:human_node",
            now=datetime(2026, 8, 1, 10, 2, tzinfo=timezone.utc),
        )
    inbox.mark_inbox_processed(
        tenant_id,
        event.id,
        claim_token=claimed.claim_token,
        outcome="submitted:human_node",
        now=datetime(2026, 8, 1, 10, 2, tzinfo=timezone.utc),
    )
    assert claim("inbound_3") == ()

    exhausted_event = replace(
        event,
        id=f"event_exhausted_{suffix}",
        task_guid=f"task_exhausted_{suffix}",
    )
    assert inbox.append_inbox(exhausted_event) is True
    exhausted_claim = inbox.claim_inbox_verification(
        tenant_id,
        worker_id="verification_exhausted",
        now=datetime(2026, 8, 1, 10, 3, tzinfo=timezone.utc),
        limit=1,
    )[0]
    inbox.mark_inbox_verification_exhausted(
        tenant_id,
        exhausted_event.id,
        claim_token=exhausted_claim.claim_token,
        error="Task completion is still not visible; exhausted after 2 attempts",
        now=datetime(2026, 8, 1, 10, 4, tzinfo=timezone.utc),
    )
    with connection_factory() as connection:
        exhausted_row = connection.execute(
            """
            SELECT status, processed_at, outcome, failure_stage, last_error
            FROM workflow_inbox_events
            WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, exhausted_event.id),
        ).fetchone()
    assert exhausted_row == {
        "status": "exhausted",
        "processed_at": datetime(2026, 8, 1, 10, 4, tzinfo=timezone.utc),
        "outcome": "exhausted:verification_attempts",
        "failure_stage": "verification",
        "last_error": "Task completion is still not visible; exhausted after 2 attempts",
    }
    assert inbox.claim_inbox_verification(
        tenant_id,
        worker_id="verification_after_exhaustion",
        now=datetime(2026, 8, 2, 10, 4, tzinfo=timezone.utc),
        limit=1,
    ) == ()


def test_postgres_worker_recovers_an_expired_automated_claim():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)

    suffix = uuid4().hex
    tenant_id = f"tenant_runtime_{suffix}"
    instance_id = f"instance_runtime_{suffix}"
    repository = PostgresWorkflowRepository(connection_factory)
    clock = Clock(datetime(2026, 8, 1, 11, 0, tzinfo=timezone.utc))
    tokens = iter((f"first_{suffix}", f"recovered_{suffix}"))
    service = WorkflowService(
        repository,
        runner=NodeRunner(
            claim_ttl=timedelta(minutes=5),
            token_factory=lambda: next(tokens),
        ),
        clock=clock,
    )
    service.create_draft(
        instance_id=instance_id,
        tenant_id=tenant_id,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec(
                    "generate",
                    "Generate",
                    "person_owner",
                    "agent",
                    work={
                        "objective": "Generate",
                        "inputs": [],
                        "outputs": [{"id": "value", "type": "data"}],
                        "acceptance": ["A value exists"],
                        "prompt": "Generate a value",
                    },
                ),
            )
        ),
    )
    service.confirm_draft(
        tenant_id,
        instance_id,
        actor_person_id="person_owner",
    )
    stranded = service.dispatch_due(
        tenant_id,
        instance_id,
        worker_id="worker_1",
        max_automated=1,
    )[0]
    assert repository.runnable_instance_ids(tenant_id, now=clock.now) == ()

    clock.now += timedelta(minutes=5)
    assert repository.runnable_instance_ids(tenant_id, now=clock.now) == (
        instance_id,
    )
    executor = RecordingExecutor()
    report = WorkflowWorker(
        service,
        repository,
        tenant_id=tenant_id,
        worker_id="worker_2",
        executors={ExecutorKind.AGENT: executor},
        clock=clock,
    ).run_once()

    assert report.recovered == 1
    assert report.completed == 1
    assert executor.requests[0].attempt_id == stranded.attempt_id
    assert executor.requests[0].claim_token != stranded.claim_token
    finished = repository.get(tenant_id, instance_id)
    assert finished.status == InstanceStatus.DONE
    assert finished.current_attempt("generate").claimed_by is None

    with connection_factory() as connection:
        audit_types = {
            row["event_type"]
            for row in connection.execute(
                """
                SELECT event_type FROM workflow_audit_events
                WHERE tenant_id = %s AND instance_id = %s
                """,
                (tenant_id, instance_id),
            ).fetchall()
        }
    assert "node.claim_recovered" in audit_types


def test_postgres_allows_only_one_worker_to_claim_the_same_node():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)

    suffix = uuid4().hex
    tenant_id = f"tenant_compete_{suffix}"
    instance_id = f"instance_compete_{suffix}"
    repository = PostgresWorkflowRepository(connection_factory)
    setup = WorkflowService(
        repository,
        clock=lambda: datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )
    setup.create_draft(
        instance_id=instance_id,
        tenant_id=tenant_id,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec(
                    "generate",
                    "Generate",
                    "person_owner",
                    "agent",
                    work={
                        "objective": "Generate",
                        "inputs": [],
                        "outputs": [{"id": "value", "type": "data"}],
                        "acceptance": ["A value exists"],
                        "prompt": "Generate a value",
                    },
                ),
            )
        ),
    )
    setup.confirm_draft(
        tenant_id,
        instance_id,
        actor_person_id="person_owner",
    )

    barrier = Barrier(2)

    def claim(worker_id: str):
        competing_repository = BarrierRepository(connection_factory, barrier)
        service = WorkflowService(
            competing_repository,
            runner=NodeRunner(token_factory=lambda: f"claim_{worker_id}_{suffix}"),
            clock=lambda: datetime(2026, 8, 1, 12, 1, tzinfo=timezone.utc),
        )
        return service.dispatch_due(
            tenant_id,
            instance_id,
            worker_id=worker_id,
            max_automated=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, worker_id) for worker_id in ("one", "two")]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except ConcurrentUpdateError as exc:
                outcomes.append(exc)

    activations = [outcome for outcome in outcomes if isinstance(outcome, tuple)]
    conflicts = [
        outcome for outcome in outcomes if isinstance(outcome, ConcurrentUpdateError)
    ]
    assert len(activations) == 1
    assert len(conflicts) == 1
    winner = activations[0][0]
    persisted = repository.get(tenant_id, instance_id)
    assert persisted.current_attempt("generate").claimed_by == winner.claimed_by

    with connection_factory() as connection:
        activation_count = connection.execute(
            """
            SELECT count(*) AS count FROM workflow_audit_events
            WHERE tenant_id = %s AND instance_id = %s
              AND event_type = 'node.activated'
            """,
            (tenant_id, instance_id),
        ).fetchone()["count"]
    assert activation_count == 1
    PostgresWorkflowInbox,


def test_postgres_edge_pairing_is_one_time_and_revocation_is_audited():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_edge_{suffix}"
    repository = PostgresWorkflowRepository(connection_factory)
    edge_store = PostgresEdgeStore(connection_factory)
    ids = (f"edge_{suffix}_{index}" for index in range(20))
    secrets = (f"secret_{suffix}_{index}" for index in range(20))
    clock = Clock(datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc))
    edge = EdgeControlService(
        edge_store,
        WorkflowService(repository, clock=clock),
        repository,
        clock=clock,
        id_factory=lambda: next(ids),
        secret_factory=lambda: next(secrets),
    )

    grant = edge.issue_pairing(
        tenant_id=tenant_id,
        person_id="person_owner",
        actor_person_id="person_owner",
    )
    paired = edge.pair_device(
        grant.code,
        name="PostgreSQL Edge",
        capabilities=("personal.readonly",),
    )

    assert edge.authenticate(paired.credential).id == paired.device.id
    with pytest.raises(PairingCodeUsedError, match="already been used"):
        edge.pair_device(
            grant.code,
            name="Replay",
            capabilities=("personal.readonly",),
        )
    edge.revoke_device(
        tenant_id=tenant_id,
        device_id=paired.device.id,
        actor_person_id="person_owner",
        reason="integration test",
    )
    with pytest.raises(DeviceRevokedError, match="revoked"):
        edge.authenticate(paired.credential)
    assert [event.event_type for event in edge_store.audit_log(tenant_id)] == [
        "edge.pairing_issued",
        "edge.device_paired",
        "edge.device_revoked",
    ]
