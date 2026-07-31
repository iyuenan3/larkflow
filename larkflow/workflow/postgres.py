"""PostgreSQL persistence adapter for the target workflow aggregate."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import secrets
from typing import Any

from psycopg.types.json import Jsonb

from .events import (
    AuditEvent,
    InvalidOutboxClaimError,
    OutboxClaim,
    OutboxEvent,
)
from .model import (
    AttemptStatus,
    ExecutorKind,
    FrozenDict,
    InstanceStatus,
    NodeAttempt,
    NodeInstance,
    NodeStatus,
    WorkflowInstance,
)
from .repository import (
    ConcurrentUpdateError,
    InstanceAlreadyExistsError,
    InstanceNotFoundError,
)
from .serde import (
    quality_from_dict,
    quality_to_dict,
    snapshot_from_dict,
    snapshot_to_dict,
    to_json_value,
)


ConnectionFactory = Callable[[], Any]


class PostgresWorkflowRepository:
    """Persist one aggregate and its audit/outbox records in one transaction."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self.connection_factory = connection_factory

    def add(
        self,
        instance: WorkflowInstance,
        *,
        audit_events: tuple[AuditEvent, ...] = (),
        outbox_events: tuple[OutboxEvent, ...] = (),
    ) -> None:
        self._require_created_at(instance)
        self._validate_events(instance, audit_events, outbox_events)
        with self.connection_factory() as connection:
            with connection.transaction():
                inserted = connection.execute(
                    """
                    INSERT INTO workflow_instances (
                        tenant_id, id, owner_person_id, template_version_id,
                        status, graph_revision, version, schema_version, goal,
                        inputs, snapshot, created_at, confirmed_at, completed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (tenant_id, id) DO NOTHING
                    RETURNING id
                    """,
                    self._instance_values(instance),
                ).fetchone()
                if inserted is None:
                    raise InstanceAlreadyExistsError(instance.id)
                self._write_children(connection, instance)
                self._write_audit(connection, audit_events)
                self._write_outbox(connection, outbox_events)

    def get(self, tenant_id: str, instance_id: str) -> WorkflowInstance:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_instances
                WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, instance_id),
            ).fetchone()
            if row is None:
                raise InstanceNotFoundError((tenant_id, instance_id))
            nodes = connection.execute(
                """
                SELECT * FROM workflow_node_instances
                WHERE tenant_id = %s AND instance_id = %s
                ORDER BY node_key
                """,
                (tenant_id, instance_id),
            ).fetchall()
            attempts = connection.execute(
                """
                SELECT * FROM workflow_node_attempts
                WHERE tenant_id = %s AND instance_id = %s
                ORDER BY node_key, attempt_no
                """,
                (tenant_id, instance_id),
            ).fetchall()
        return self._load_instance(row, nodes, attempts)

    def save(
        self,
        instance: WorkflowInstance,
        *,
        expected_version: int,
        audit_events: tuple[AuditEvent, ...] = (),
        outbox_events: tuple[OutboxEvent, ...] = (),
    ) -> None:
        self._require_created_at(instance)
        self._validate_events(instance, audit_events, outbox_events)
        with self.connection_factory() as connection:
            with connection.transaction():
                updated = connection.execute(
                    """
                    UPDATE workflow_instances
                    SET owner_person_id = %s,
                        template_version_id = %s,
                        status = %s,
                        graph_revision = %s,
                        version = version + 1,
                        schema_version = %s,
                        goal = %s,
                        inputs = %s,
                        snapshot = %s,
                        confirmed_at = %s,
                        completed_at = %s
                    WHERE tenant_id = %s AND id = %s AND version = %s
                    RETURNING version
                    """,
                    (
                        instance.owner_person_id,
                        instance.snapshot.template_version_id,
                        instance.status.value,
                        instance.graph_revision,
                        instance.snapshot.schema_version,
                        instance.snapshot.goal,
                        Jsonb(to_json_value(instance.snapshot.inputs)),
                        Jsonb(snapshot_to_dict(instance.snapshot)),
                        instance.confirmed_at,
                        instance.completed_at,
                        instance.tenant_id,
                        instance.id,
                        expected_version,
                    ),
                ).fetchone()
                if updated is None:
                    current = connection.execute(
                        """
                        SELECT version FROM workflow_instances
                        WHERE tenant_id = %s AND id = %s
                        """,
                        (instance.tenant_id, instance.id),
                    ).fetchone()
                    if current is None:
                        raise InstanceNotFoundError((instance.tenant_id, instance.id))
                    raise ConcurrentUpdateError(
                        f"instance {instance.id} expected version {expected_version}, "
                        f"found {current['version']}"
                    )
                instance.version = int(updated["version"])
                self._write_children(connection, instance)
                self._write_audit(connection, audit_events)
                self._write_outbox(connection, outbox_events)

    def claim_outbox(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 100,
        claim_ttl: timedelta = timedelta(minutes=5),
    ) -> tuple[OutboxClaim, ...]:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if limit < 1:
            raise ValueError("limit must be positive")
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        claim_token = secrets.token_urlsafe(24)
        claim_expires_at = now + claim_ttl
        with self.connection_factory() as connection:
            with connection.transaction():
                rows = connection.execute(
                    """
                    WITH selected AS (
                        SELECT tenant_id, id
                        FROM workflow_outbox_events
                        WHERE tenant_id = %s
                          AND (
                            (status IN ('pending', 'failed') AND available_at <= %s)
                            OR (status = 'processing' AND claim_expires_at <= %s)
                          )
                        ORDER BY available_at, created_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE workflow_outbox_events AS event
                    SET status = 'processing',
                        attempt_count = event.attempt_count + 1,
                        claimed_by = %s,
                        claim_token = %s,
                        claim_expires_at = %s
                    FROM selected
                    WHERE event.tenant_id = selected.tenant_id
                      AND event.id = selected.id
                    RETURNING event.*
                    """,
                    (
                        tenant_id,
                        now,
                        now,
                        limit,
                        worker_id,
                        claim_token,
                        claim_expires_at,
                    ),
                ).fetchall()
        claims = [
            OutboxClaim(
                event=self._outbox_event_from_row(row),
                claim_token=claim_token,
                claimed_by=worker_id,
                claim_expires_at=claim_expires_at,
                attempt_count=int(row["attempt_count"]),
            )
            for row in rows
        ]
        claims.sort(
            key=lambda claim: (
                claim.event.available_at,
                claim.event.created_at,
                claim.event.id,
            )
        )
        return tuple(claims)

    def mark_outbox_published(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        now: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            with connection.transaction():
                updated = connection.execute(
                    """
                    UPDATE workflow_outbox_events
                    SET status = 'published', published_at = %s,
                        claimed_by = NULL, claim_token = NULL,
                        claim_expires_at = NULL, last_error = NULL
                    WHERE tenant_id = %s AND id = %s
                      AND status = 'processing' AND claim_token = %s
                    RETURNING id
                    """,
                    (now, tenant_id, event_id, claim_token),
                ).fetchone()
                if updated is None:
                    raise InvalidOutboxClaimError(event_id)

    def mark_outbox_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            with connection.transaction():
                updated = connection.execute(
                    """
                    UPDATE workflow_outbox_events
                    SET status = 'failed', available_at = %s, last_error = %s,
                        claimed_by = NULL, claim_token = NULL,
                        claim_expires_at = NULL
                    WHERE tenant_id = %s AND id = %s
                      AND status = 'processing' AND claim_token = %s
                    RETURNING id
                    """,
                    (retry_at, error, tenant_id, event_id, claim_token),
                ).fetchone()
                if updated is None:
                    raise InvalidOutboxClaimError(event_id)

    @staticmethod
    def _require_created_at(instance: WorkflowInstance) -> None:
        if instance.created_at is None:
            raise ValueError("instance.created_at is required for persistence")

    @staticmethod
    def _validate_events(
        instance: WorkflowInstance,
        audit_events: tuple[AuditEvent, ...],
        outbox_events: tuple[OutboxEvent, ...],
    ) -> None:
        for event in audit_events:
            if event.tenant_id != instance.tenant_id or event.instance_id != instance.id:
                raise ValueError("audit event does not belong to the aggregate")
        for event in outbox_events:
            if event.tenant_id != instance.tenant_id:
                raise ValueError("outbox event does not belong to the aggregate tenant")

    @staticmethod
    def _instance_values(instance: WorkflowInstance) -> tuple[Any, ...]:
        return (
            instance.tenant_id,
            instance.id,
            instance.owner_person_id,
            instance.snapshot.template_version_id,
            instance.status.value,
            instance.graph_revision,
            instance.version,
            instance.snapshot.schema_version,
            instance.snapshot.goal,
            Jsonb(to_json_value(instance.snapshot.inputs)),
            Jsonb(snapshot_to_dict(instance.snapshot)),
            instance.created_at,
            instance.confirmed_at,
            instance.completed_at,
        )

    def _write_children(self, connection: Any, instance: WorkflowInstance) -> None:
        for node_key, node in instance.nodes.items():
            connection.execute(
                """
                INSERT INTO workflow_node_instances (
                    tenant_id, instance_id, id, node_key, owner_person_id,
                    executor, status, current_attempt_no, version,
                    ready_at, started_at, completed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, instance_id, node_key) DO UPDATE SET
                    owner_person_id = EXCLUDED.owner_person_id,
                    executor = EXCLUDED.executor,
                    status = EXCLUDED.status,
                    current_attempt_no = EXCLUDED.current_attempt_no,
                    version = EXCLUDED.version,
                    ready_at = EXCLUDED.ready_at,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at
                """,
                (
                    instance.tenant_id,
                    instance.id,
                    node.id,
                    node_key,
                    node.owner_person_id,
                    node.executor.value,
                    node.status.value,
                    node.current_attempt_no,
                    node.version,
                    node.ready_at,
                    node.started_at,
                    node.completed_at,
                ),
            )
        for spec in instance.snapshot.nodes:
            for dependency in spec.deps:
                connection.execute(
                    """
                    INSERT INTO workflow_node_dependencies (
                        tenant_id, instance_id, node_key, dependency_key
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (instance.tenant_id, instance.id, spec.key, dependency),
                )
        for (node_key, attempt_no), attempt in instance.attempts.items():
            connection.execute(
                """
                INSERT INTO workflow_node_attempts (
                    tenant_id, instance_id, node_key, attempt_no, id,
                    node_instance_id, status, input_snapshot, result,
                    quality_result, claim_token, claim_expires_at,
                    started_at, completed_at, submitted_by_person_id,
                    error_code, error_message
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (tenant_id, instance_id, node_key, attempt_no) DO UPDATE SET
                    status = EXCLUDED.status,
                    input_snapshot = EXCLUDED.input_snapshot,
                    result = EXCLUDED.result,
                    quality_result = EXCLUDED.quality_result,
                    claim_token = EXCLUDED.claim_token,
                    claim_expires_at = EXCLUDED.claim_expires_at,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at,
                    submitted_by_person_id = EXCLUDED.submitted_by_person_id,
                    error_code = EXCLUDED.error_code,
                    error_message = EXCLUDED.error_message
                """,
                (
                    instance.tenant_id,
                    instance.id,
                    node_key,
                    attempt_no,
                    attempt.id,
                    attempt.node_instance_id,
                    attempt.status.value,
                    Jsonb(to_json_value(attempt.input_snapshot)),
                    Jsonb(to_json_value(attempt.result)) if attempt.result is not None else None,
                    Jsonb(quality_to_dict(attempt.quality_result))
                    if attempt.quality_result is not None
                    else None,
                    attempt.claim_token,
                    attempt.claim_expires_at,
                    attempt.started_at,
                    attempt.completed_at,
                    attempt.submitted_by_person_id,
                    attempt.error_code,
                    attempt.error_message,
                ),
            )

    @staticmethod
    def _write_audit(connection: Any, events: tuple[AuditEvent, ...]) -> None:
        for event in events:
            connection.execute(
                """
                INSERT INTO workflow_audit_events (
                    tenant_id, id, instance_id, node_key, attempt_no,
                    event_type, actor_person_id, source, correlation_id,
                    aggregate_version, payload, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, id) DO NOTHING
                """,
                (
                    event.tenant_id,
                    event.id,
                    event.instance_id,
                    event.node_key,
                    event.attempt_no,
                    event.event_type,
                    event.actor_person_id,
                    event.source,
                    event.correlation_id,
                    event.aggregate_version,
                    Jsonb(to_json_value(event.payload)),
                    event.occurred_at,
                ),
            )

    @staticmethod
    def _write_outbox(connection: Any, events: tuple[OutboxEvent, ...]) -> None:
        for event in events:
            connection.execute(
                """
                INSERT INTO workflow_outbox_events (
                    tenant_id, id, aggregate_type, aggregate_id,
                    aggregate_version, event_type, payload,
                    available_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    tenant_id, event_type, aggregate_type,
                    aggregate_id, aggregate_version
                ) DO NOTHING
                """,
                (
                    event.tenant_id,
                    event.id,
                    event.aggregate_type,
                    event.aggregate_id,
                    event.aggregate_version,
                    event.event_type,
                    Jsonb(to_json_value(event.payload)),
                    event.available_at,
                    event.created_at,
                ),
            )

    @staticmethod
    def _load_instance(
        row: dict[str, Any],
        node_rows: list[dict[str, Any]],
        attempt_rows: list[dict[str, Any]],
    ) -> WorkflowInstance:
        instance = WorkflowInstance(
            id=row["id"],
            tenant_id=row["tenant_id"],
            owner_person_id=row["owner_person_id"],
            snapshot=snapshot_from_dict(row["snapshot"]),
            status=InstanceStatus(row["status"]),
            graph_revision=int(row["graph_revision"]),
            version=int(row["version"]),
            created_at=row["created_at"],
            confirmed_at=row["confirmed_at"],
            completed_at=row["completed_at"],
        )
        for node_row in node_rows:
            instance.nodes[node_row["node_key"]] = NodeInstance(
                id=node_row["id"],
                instance_id=node_row["instance_id"],
                node_key=node_row["node_key"],
                owner_person_id=node_row["owner_person_id"],
                executor=ExecutorKind(node_row["executor"]),
                status=NodeStatus(node_row["status"]),
                current_attempt_no=int(node_row["current_attempt_no"]),
                version=int(node_row["version"]),
                ready_at=node_row["ready_at"],
                started_at=node_row["started_at"],
                completed_at=node_row["completed_at"],
            )
        for attempt_row in attempt_rows:
            key = (attempt_row["node_key"], int(attempt_row["attempt_no"]))
            instance.attempts[key] = NodeAttempt(
                id=attempt_row["id"],
                node_instance_id=attempt_row["node_instance_id"],
                attempt_no=int(attempt_row["attempt_no"]),
                status=AttemptStatus(attempt_row["status"]),
                input_snapshot=FrozenDict(attempt_row["input_snapshot"] or {}),
                result=FrozenDict(attempt_row["result"])
                if attempt_row["result"] is not None
                else None,
                quality_result=quality_from_dict(attempt_row["quality_result"]),
                claim_token=attempt_row["claim_token"],
                claim_expires_at=attempt_row["claim_expires_at"],
                started_at=attempt_row["started_at"],
                completed_at=attempt_row["completed_at"],
                submitted_by_person_id=attempt_row["submitted_by_person_id"],
                error_code=attempt_row["error_code"],
                error_message=attempt_row["error_message"],
            )
        return instance

    @staticmethod
    def _outbox_event_from_row(row: dict[str, Any]) -> OutboxEvent:
        return OutboxEvent(
            id=row["id"],
            tenant_id=row["tenant_id"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            aggregate_version=int(row["aggregate_version"]),
            event_type=row["event_type"],
            payload=row["payload"],
            created_at=row["created_at"],
            available_at=row["available_at"],
        )
