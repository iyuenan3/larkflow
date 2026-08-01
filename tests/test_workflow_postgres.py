"""Opt-in integration test against a disposable PostgreSQL database."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    InstanceSnapshot,
    InstanceStatus,
    NodeRunner,
    NodeSpec,
    PostgresWorkflowRepository,
    WorkflowService,
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


class BarrierRepository(PostgresWorkflowRepository):
    """Make competing dispatches read the same aggregate version."""

    def __init__(self, connection_factory, barrier: Barrier) -> None:
        super().__init__(connection_factory)
        self.barrier = barrier

    def get(self, tenant_id: str, instance_id: str):
        instance = super().get(tenant_id, instance_id)
        self.barrier.wait(timeout=5)
        return instance


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
