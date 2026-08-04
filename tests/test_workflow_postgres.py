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
    IMCommandSignal,
    IMMention,
    InvalidInboxClaimError,
    NodeRunner,
    NodeSpec,
    PostgresWorkflowInbox,
    PostgresIMCommandStore,
    PostgresWorkerWakeup,
    PostgresWorkflowRepository,
    PostgresEdgeStore,
    PairingCodeUsedError,
    ProjectionRecord,
    RestartScope,
    RoleBindingActionSignal,
    RoleBindingVerificationWorker,
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


class BarrierGraphEditRepository(PostgresWorkflowRepository):
    """Make two graph edit confirmations reach the atomic save together."""

    def __init__(self, connection_factory, barrier: Barrier) -> None:
        super().__init__(connection_factory)
        self.barrier = barrier

    def save_graph_edit(self, *args, **kwargs):
        self.barrier.wait(timeout=5)
        return super().save_graph_edit(*args, **kwargs)


def test_postgres_committed_queue_insert_wakes_listener():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    wakeup = PostgresWorkerWakeup(connection_factory)
    assert wakeup.start() is True
    try:
        inserted = PostgresIMCommandStore(connection_factory).append_im_command(
            IMCommandSignal(
                id=f"event_wakeup_{suffix}",
                tenant_id=f"tenant_wakeup_{suffix}",
                message_id=f"message_wakeup_{suffix}",
                chat_id=f"chat_wakeup_{suffix}",
                sender_person_id="person_owner",
                text="/larkflow list",
                occurred_at=now,
                received_at=now,
            )
        )
        assert inserted is True
        assert wakeup.wait(Event(), 1.0) is False
        assert wakeup.notifications_received == 1
    finally:
        wakeup.close()


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


def test_postgres_im_command_round_trips_authenticated_mentions():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_im_{suffix}"
    now = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    store = PostgresIMCommandStore(connection_factory)
    event = IMCommandSignal(
        id=f"event_{suffix}",
        tenant_id=tenant_id,
        message_id=f"message_{suffix}",
        chat_id=f"chat_{suffix}",
        sender_person_id="person_owner",
        text="/larkflow start review reviewer=@_user_1",
        occurred_at=now,
        received_at=now,
        mentions=(IMMention("@_user_1", "person_reviewer"),),
        available_at=now + timedelta(seconds=10),
    )

    assert store.append_im_command(event) is True
    assert store.claim_im_verification(
        tenant_id,
        worker_id="verify_mentions_early",
        now=now,
        limit=1,
        claim_ttl=timedelta(minutes=1),
    ) == ()
    store.release_im_command(
        tenant_id,
        event.id,
        available_at=now,
        feedback_status="updated",
        feedback_elapsed_ms=275,
    )
    with connection_factory() as connection:
        feedback = connection.execute(
            """
            SELECT feedback_status, feedback_elapsed_ms, feedback_completed_at
            FROM workflow_im_commands
            WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, event.id),
        ).fetchone()
    assert tuple(feedback) == ("updated", 275, now)
    claims = store.claim_im_verification(
        tenant_id,
        worker_id="verify_mentions",
        now=now,
        limit=1,
        claim_ttl=timedelta(minutes=1),
    )

    assert len(claims) == 1
    assert claims[0].event == replace(event, available_at=None)


def test_postgres_unknown_role_card_can_settle_to_a_generic_rejection():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_role_reject_{suffix}"
    now = datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc)
    store = PostgresIMCommandStore(connection_factory)
    action = RoleBindingActionSignal(
        id=f"event_{suffix}",
        tenant_id=tenant_id,
        message_id=f"message_{suffix}",
        chat_id=f"chat_{suffix}",
        operator_person_id="person_intruder",
        action_tag="button",
        action_name="role_binding_submit",
        form_value='{"role__reviewer":"person_intruder"}',
        update_token=f"token_{suffix}",
        occurred_at=now,
        received_at=now,
        available_at=now + timedelta(seconds=10),
    )

    assert store.append_role_binding_action(action) is True
    assert store.append_role_binding_action(
        replace(action, id=f"event_second_{suffix}")
    ) is False
    assert store.claim_role_binding_verification(
        tenant_id,
        worker_id="verify_unknown_early",
        now=now,
        limit=1,
        claim_ttl=timedelta(minutes=1),
    ) == ()
    store.release_role_binding_action(
        tenant_id,
        action.id,
        available_at=now,
        feedback_status="updated",
        feedback_elapsed_ms=325,
    )
    with connection_factory() as connection:
        feedback = connection.execute(
            """
            SELECT feedback_status, feedback_elapsed_ms, feedback_completed_at
            FROM workflow_role_binding_actions
            WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, action.id),
        ).fetchone()
    assert tuple(feedback) == ("updated", 325, now)
    verification = store.claim_role_binding_verification(
        tenant_id,
        worker_id="verify_unknown",
        now=now,
        limit=1,
        claim_ttl=timedelta(minutes=1),
    )[0]
    assert verification.request is None
    store.mark_role_binding_rejected(
        tenant_id,
        action.id,
        claim_token=verification.claim_token,
        outcome="rejected:role_binding",
        reply_text="人员分工未执行。请重新发送流程启动命令后再试。",
        now=now,
    )

    reply = store.claim_role_binding_replies(
        tenant_id,
        worker_id="reply_unknown",
        now=now,
        limit=1,
        claim_ttl=timedelta(minutes=1),
    )[0]

    assert reply.action == replace(action, available_at=None)
    assert reply.request is None
    assert reply.instance_id is None
    assert reply.text.startswith("人员分工未执行")


def test_postgres_role_verification_records_each_item_completion_time():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_role_completion_{suffix}"
    base = datetime(2026, 8, 4, 7, 0, tzinfo=timezone.utc)
    values = iter((base, base + timedelta(seconds=1), base + timedelta(seconds=2)))
    store = PostgresIMCommandStore(connection_factory)

    class UnusedDirectory:
        def get_person(self, tenant_id, person_id):
            raise AssertionError("unknown-card rejection must not query the directory")

    for index in (1, 2):
        action = RoleBindingActionSignal(
            id=f"event_{index}_{suffix}",
            tenant_id=tenant_id,
            message_id=f"message_{index}_{suffix}",
            chat_id=f"chat_{suffix}",
            operator_person_id="person_owner",
            action_tag="button",
            action_name="role_binding_submit",
            form_value='{"role__reviewer":"person_owner"}',
            update_token=f"token_{index}_{suffix}",
            occurred_at=base,
            received_at=base,
        )
        assert store.append_role_binding_action(action) is True

    report = RoleBindingVerificationWorker(
        store,
        UnusedDirectory(),
        tenant_id=tenant_id,
        worker_id="verify_completion",
        clock=lambda: next(values),
    ).run_once()

    assert report.claimed == 2
    assert report.rejected == 2
    with connection_factory() as connection:
        rows = connection.execute(
            """
            SELECT status, processed_at
            FROM workflow_role_binding_actions
            WHERE tenant_id = %s
            ORDER BY id
            """,
            (tenant_id,),
        ).fetchall()
    assert rows == [
        {"status": "rejected", "processed_at": base + timedelta(seconds=1)},
        {"status": "rejected", "processed_at": base + timedelta(seconds=2)},
    ]


def test_postgres_two_interactive_replicas_claim_distinct_single_items():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_interactive_competition_{suffix}"
    now = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
    store = PostgresIMCommandStore(connection_factory)
    for index in (1, 2):
        assert store.append_role_binding_action(
            RoleBindingActionSignal(
                id=f"event_{index}_{suffix}",
                tenant_id=tenant_id,
                message_id=f"message_{index}_{suffix}",
                chat_id=f"chat_{suffix}",
                operator_person_id="person_owner",
                action_tag="button",
                action_name="role_binding_submit",
                form_value='{"role__reviewer":"person_owner"}',
                update_token=f"token_{index}_{suffix}",
                occurred_at=now,
                received_at=now,
            )
        ) is True

    barrier = Barrier(2)

    def claim_one(worker_id: str):
        barrier.wait(timeout=5)
        claims = PostgresIMCommandStore(
            connection_factory
        ).claim_role_binding_verification(
            tenant_id,
            worker_id=worker_id,
            now=now,
            limit=1,
            claim_ttl=timedelta(minutes=1),
        )
        assert len(claims) == 1
        return claims[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(claim_one, "interactive_1")
        second = pool.submit(claim_one, "interactive_2")
        claims = (first.result(timeout=10), second.result(timeout=10))

    assert {claim.action.id for claim in claims} == {
        f"event_1_{suffix}",
        f"event_2_{suffix}",
    }
    with connection_factory() as connection:
        workers = connection.execute(
            """
            SELECT claimed_by
            FROM workflow_role_binding_actions
            WHERE tenant_id = %s
            ORDER BY claimed_by
            """,
            (tenant_id,),
        ).fetchall()
    assert [row["claimed_by"] for row in workers] == [
        "interactive_1",
        "interactive_2",
    ]


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


def test_postgres_graph_edit_is_durable_and_consumed_exactly_once():
    assert POSTGRES_DSN is not None
    connection_factory = postgres_connection_factory(POSTGRES_DSN)
    apply_migrations(connection_factory)
    suffix = uuid4().hex
    tenant_id = f"tenant_graph_edit_{suffix}"
    instance_id = f"instance_graph_edit_{suffix}"
    owner = f"person_owner_{suffix}"
    repository = PostgresWorkflowRepository(connection_factory)
    clock = Clock(datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc))
    service = WorkflowService(repository, clock=clock)
    work = {
        "objective": "Complete the step",
        "inputs": [],
        "outputs": [{"id": "result", "type": "data"}],
        "acceptance": ["A result exists"],
    }
    service.create_draft(
        instance_id=instance_id,
        tenant_id=tenant_id,
        owner_person_id=owner,
        actor_person_id=owner,
        snapshot=InstanceSnapshot(
            goal="Edit a future graph",
            nodes=(
                NodeSpec("brief", "Confirm brief", owner, "human", work=work),
                NodeSpec(
                    "draft",
                    "Draft summary",
                    owner,
                    "agent",
                    deps=("brief",),
                    work=work,
                ),
                NodeSpec(
                    "review",
                    "Review summary",
                    owner,
                    "human",
                    deps=("draft",),
                    work=work,
                ),
            ),
        ),
    )
    service.confirm_draft(tenant_id, instance_id, actor_person_id=owner)
    activation = service.dispatch_ready(tenant_id, instance_id)[0]
    service.submit_human(
        tenant_id,
        instance_id,
        "brief",
        actor_person_id=owner,
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        result={"result": "confirmed"},
    )
    version_before = repository.get(tenant_id, instance_id).version
    preview = service.preview_graph_edit(
        tenant_id,
        instance_id,
        (
            {
                "op": "update_node",
                "node_key": "draft",
                "set": {"title": "Draft revised summary"},
            },
            {"op": "remove_node", "node_key": "review"},
            {
                "op": "add_node",
                "node": {
                    "key": "archive",
                    "title": "Archive summary",
                    "owner_person_id": owner,
                    "executor": "human",
                    "deps": ["draft"],
                    "work": work,
                },
            },
        ),
        actor_person_id=owner,
    )
    assert repository.get_graph_edit_preview(tenant_id, preview.id) == preview

    barrier = Barrier(2)

    def confirm(_index):
        concurrent = WorkflowService(
            BarrierGraphEditRepository(connection_factory, barrier),
            clock=clock,
        )
        return concurrent.confirm_graph_edit(
            tenant_id,
            preview.id,
            actor_person_id=owner,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmations = list(pool.map(confirm, (1, 2)))

    assert sorted(item.already_applied for item in confirmations) == [False, True]
    edited = repository.get(tenant_id, instance_id)
    assert edited.version == version_before + 1
    assert edited.graph_revision == 2
    assert tuple(spec.key for spec in edited.snapshot.nodes) == (
        "brief",
        "draft",
        "archive",
    )
    assert edited.snapshot.node("draft").title == "Draft revised summary"
    assert edited.nodes["draft"].status.value == "ready"
    assert edited.nodes["archive"].status.value == "pending"
    assert edited.current_attempt("brief").result == {"result": "confirmed"}
    assert "review" not in edited.nodes
    assert ("review", 1) not in edited.attempts
    stored = repository.get_graph_edit_preview(tenant_id, preview.id)
    assert stored.consumed_at == clock.now
    assert stored.applied_instance_version == edited.version
    with connection_factory() as connection:
        audit_count = connection.execute(
            """
            SELECT count(*) AS count FROM workflow_audit_events
            WHERE tenant_id = %s AND instance_id = %s
              AND event_type = 'instance.graph_edited'
            """,
            (tenant_id, instance_id),
        ).fetchone()["count"]
        dependency_rows = connection.execute(
            """
            SELECT node_key, dependency_key
            FROM workflow_node_dependencies
            WHERE tenant_id = %s AND instance_id = %s
            ORDER BY node_key, dependency_key
            """,
            (tenant_id, instance_id),
        ).fetchall()
        removed_rows = connection.execute(
            """
            SELECT count(*) AS count FROM workflow_node_instances
            WHERE tenant_id = %s AND instance_id = %s AND node_key = 'review'
            """,
            (tenant_id, instance_id),
        ).fetchone()["count"]
        index_row = connection.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'workflow_graph_edit_previews_open_idx'
            """
        ).fetchone()
    assert audit_count == 1
    assert dependency_rows == [
        {"node_key": "archive", "dependency_key": "draft"},
        {"node_key": "draft", "dependency_key": "brief"},
    ]
    assert removed_rows == 0
    assert index_row is not None


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
