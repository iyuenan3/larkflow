"""PostgreSQL persistence adapter for the target workflow aggregate."""
from __future__ import annotations

from collections.abc import Callable, Collection
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
from .inbound import (
    InboxClaim,
    InvalidInboxClaimError,
    TaskCompletionSignal,
    task_state_from_dict,
    task_state_to_dict,
)
from .model import (
    AttemptStatus,
    ExecutorKind,
    FrozenDict,
    InstanceStatus,
    NodeAttempt,
    NodeInstance,
    NodeStatus,
    TemplateAuditEvent,
    TemplateStatus,
    WorkflowInstance,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from .repository import (
    ConcurrentTemplateUpdateError,
    ConcurrentUpdateError,
    InstanceAlreadyExistsError,
    InstanceNotFoundError,
    TemplateAlreadyExistsError,
    TemplateNotFoundError,
)
from .projection import ProjectionRecord
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

    def add_template(
        self,
        template: WorkflowTemplate,
        initial_version: WorkflowTemplateVersion,
        event: TemplateAuditEvent,
    ) -> None:
        self._validate_template_version(template, initial_version, expected_number=1)
        self._validate_template_event(template, event, aggregate_version=0)
        with self.connection_factory() as connection:
            with connection.transaction():
                inserted = connection.execute(
                    """
                    INSERT INTO workflow_templates (
                        tenant_id, id, name, status, version,
                        created_at, updated_at, deleted_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        template.tenant_id,
                        template.id,
                        template.name,
                        template.status.value,
                        template.version,
                        template.created_at,
                        template.updated_at,
                        template.deleted_at,
                    ),
                ).fetchone()
                if inserted is None:
                    raise TemplateAlreadyExistsError(template.id)
                self._insert_template_version(connection, initial_version)
                self._insert_template_event(connection, event)

    def get_template(self, tenant_id: str, template_id: str) -> WorkflowTemplate:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_templates
                WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, template_id),
            ).fetchone()
        if row is None:
            raise TemplateNotFoundError((tenant_id, template_id))
        return self._template_from_row(row)

    def list_templates(self, tenant_id: str) -> tuple[WorkflowTemplate, ...]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_templates
                WHERE tenant_id = %s
                ORDER BY created_at, id
                """,
                (tenant_id,),
            ).fetchall()
        return tuple(self._template_from_row(row) for row in rows)

    def get_template_version(
        self,
        tenant_id: str,
        template_id: str,
        version: int | None = None,
    ) -> WorkflowTemplateVersion:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_template_versions
                WHERE tenant_id = %s AND template_id = %s
                  AND (%s::integer IS NULL OR version = %s)
                ORDER BY version DESC
                LIMIT 1
                """,
                (tenant_id, template_id, version, version),
            ).fetchone()
        if row is None:
            raise TemplateNotFoundError((tenant_id, template_id, version))
        return self._template_version_from_row(row)

    def add_template_version(
        self,
        template_version: WorkflowTemplateVersion,
        *,
        expected_template_version: int,
        updated_at: datetime,
        event: TemplateAuditEvent,
    ) -> WorkflowTemplate:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT * FROM workflow_templates
                    WHERE tenant_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (template_version.tenant_id, template_version.template_id),
                ).fetchone()
                if row is None:
                    raise TemplateNotFoundError(
                        (template_version.tenant_id, template_version.template_id)
                    )
                template = self._template_from_row(row)
                if template.version != expected_template_version:
                    raise ConcurrentTemplateUpdateError(template.id)
                latest_number = connection.execute(
                    """
                    SELECT max(version) AS version
                    FROM workflow_template_versions
                    WHERE tenant_id = %s AND template_id = %s
                    """,
                    (template.tenant_id, template.id),
                ).fetchone()["version"]
                self._validate_template_version(
                    template,
                    template_version,
                    expected_number=int(latest_number) + 1,
                )
                self._validate_template_event(
                    template,
                    event,
                    aggregate_version=expected_template_version + 1,
                )
                updated = connection.execute(
                    """
                    UPDATE workflow_templates
                    SET version = version + 1, updated_at = %s
                    WHERE tenant_id = %s AND id = %s AND version = %s
                    RETURNING *
                    """,
                    (
                        updated_at,
                        template.tenant_id,
                        template.id,
                        expected_template_version,
                    ),
                ).fetchone()
                if updated is None:
                    raise ConcurrentTemplateUpdateError(template.id)
                self._insert_template_version(connection, template_version)
                self._insert_template_event(connection, event)
        return self._template_from_row(updated)

    def set_template_status(
        self,
        tenant_id: str,
        template_id: str,
        *,
        expected_template_version: int,
        status: TemplateStatus,
        updated_at: datetime,
        deleted_at: datetime | None,
        event: TemplateAuditEvent,
    ) -> WorkflowTemplate:
        current = self.get_template(tenant_id, template_id)
        self._validate_template_event(
            current,
            event,
            aggregate_version=expected_template_version + 1,
        )
        with self.connection_factory() as connection:
            with connection.transaction():
                updated = connection.execute(
                    """
                    UPDATE workflow_templates
                    SET status = %s, version = version + 1,
                        updated_at = %s, deleted_at = %s
                    WHERE tenant_id = %s AND id = %s AND version = %s
                    RETURNING *
                    """,
                    (
                        TemplateStatus(status).value,
                        updated_at,
                        deleted_at,
                        tenant_id,
                        template_id,
                        expected_template_version,
                    ),
                ).fetchone()
                if updated is None:
                    exists = connection.execute(
                        """
                        SELECT 1 FROM workflow_templates
                        WHERE tenant_id = %s AND id = %s
                        """,
                        (tenant_id, template_id),
                    ).fetchone()
                    if exists is None:
                        raise TemplateNotFoundError((tenant_id, template_id))
                    raise ConcurrentTemplateUpdateError(template_id)
                self._insert_template_event(connection, event)
        return self._template_from_row(updated)

    def template_audit_log(
        self,
        tenant_id: str,
        template_id: str,
    ) -> tuple[TemplateAuditEvent, ...]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_template_events
                WHERE tenant_id = %s AND template_id = %s
                ORDER BY aggregate_version, occurred_at, id
                """,
                (tenant_id, template_id),
            ).fetchall()
        return tuple(self._template_event_from_row(row) for row in rows)

    def get_projection(
        self,
        tenant_id: str,
        node_instance_id: str,
        attempt_no: int,
        kind: str,
    ) -> ProjectionRecord | None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_projections
                WHERE tenant_id = %s
                  AND node_instance_id = %s
                  AND attempt_no = %s
                  AND kind = %s
                """,
                (tenant_id, node_instance_id, attempt_no, kind),
            ).fetchone()
        return self._projection_from_row(row) if row is not None else None

    def get_projection_by_external_id(
        self,
        tenant_id: str,
        kind: str,
        external_id: str,
    ) -> ProjectionRecord | None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_projections
                WHERE tenant_id = %s AND kind = %s AND external_id = %s
                """,
                (tenant_id, kind, external_id),
            ).fetchone()
        return self._projection_from_row(row) if row is not None else None

    def save_projection(self, projection: ProjectionRecord) -> None:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    INSERT INTO workflow_projections (
                        tenant_id, id, instance_id, node_instance_id,
                        attempt_no, kind, external_id, external_url,
                        idempotency_key, sync_version, state,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (
                        tenant_id, node_instance_id, attempt_no, kind
                    ) DO UPDATE SET
                        external_id = EXCLUDED.external_id,
                        external_url = EXCLUDED.external_url,
                        sync_version = GREATEST(
                            workflow_projections.sync_version,
                            EXCLUDED.sync_version
                        ),
                        state = EXCLUDED.state,
                        updated_at = EXCLUDED.updated_at
                    WHERE workflow_projections.idempotency_key = EXCLUDED.idempotency_key
                      AND (
                        workflow_projections.external_id IS NULL
                        OR workflow_projections.external_id = EXCLUDED.external_id
                      )
                      AND EXCLUDED.sync_version >= workflow_projections.sync_version
                    RETURNING *
                    """,
                    (
                        projection.tenant_id,
                        projection.id,
                        projection.instance_id,
                        projection.node_instance_id,
                        projection.attempt_no,
                        projection.kind,
                        projection.external_id,
                        projection.external_url,
                        projection.idempotency_key,
                        projection.sync_version,
                        Jsonb(to_json_value(projection.state)),
                        projection.created_at,
                        projection.updated_at,
                    ),
                ).fetchone()
                if row is None:
                    raise ValueError("projection identity or version cannot change")

    def runnable_instance_ids(
        self,
        tenant_id: str,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT node.instance_id,
                       min(
                           CASE
                               WHEN node.status = 'ready' THEN node.ready_at
                               ELSE attempt.claim_expires_at
                           END
                       ) AS due_at
                FROM workflow_node_instances AS node
                JOIN workflow_instances AS instance
                  ON instance.tenant_id = node.tenant_id
                 AND instance.id = node.instance_id
                LEFT JOIN workflow_node_attempts AS attempt
                  ON attempt.tenant_id = node.tenant_id
                 AND attempt.instance_id = node.instance_id
                 AND attempt.node_key = node.node_key
                 AND attempt.attempt_no = node.current_attempt_no
                WHERE node.tenant_id = %s
                  AND instance.status = 'running'
                  AND (
                      node.status = 'ready'
                      OR (
                          node.status = 'running'
                          AND node.executor IN ('agent', 'tool')
                          AND attempt.status = 'running'
                          AND attempt.claim_expires_at <= %s
                      )
                  )
                GROUP BY node.instance_id
                ORDER BY due_at, node.instance_id
                LIMIT %s
                """,
                (tenant_id, now, limit),
            ).fetchall()
        return tuple(row["instance_id"] for row in rows)

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
        event_types: Collection[str] | None = None,
    ) -> tuple[OutboxClaim, ...]:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if limit < 1:
            raise ValueError("limit must be positive")
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        accepted_types = (
            sorted(set(event_types)) if event_types is not None else None
        )
        if accepted_types == []:
            return ()
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
                          AND (%s::text[] IS NULL OR event_type = ANY(%s::text[]))
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
                        accepted_types,
                        accepted_types,
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
            if spec.key not in instance.nodes:
                continue
            for dependency in spec.deps:
                if dependency not in instance.nodes:
                    continue
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
                    quality_result, claimed_by, claim_token, claim_expires_at,
                    started_at, completed_at, submitted_by_person_id,
                    error_code, error_message
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (tenant_id, instance_id, node_key, attempt_no) DO UPDATE SET
                    status = EXCLUDED.status,
                    input_snapshot = EXCLUDED.input_snapshot,
                    result = EXCLUDED.result,
                    quality_result = EXCLUDED.quality_result,
                    claimed_by = EXCLUDED.claimed_by,
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
                    attempt.claimed_by,
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
    def _insert_template_version(
        connection: Any,
        version: WorkflowTemplateVersion,
    ) -> None:
        connection.execute(
            """
            INSERT INTO workflow_template_versions (
                tenant_id, id, template_id, version, schema_version,
                locked, definition, content_hash, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                version.tenant_id,
                version.id,
                version.template_id,
                version.version,
                version.schema_version,
                version.locked,
                Jsonb(to_json_value(version.definition)),
                version.content_hash,
                version.created_at,
            ),
        )

    @staticmethod
    def _insert_template_event(connection: Any, event: TemplateAuditEvent) -> None:
        connection.execute(
            """
            INSERT INTO workflow_template_events (
                tenant_id, id, template_id, event_type,
                actor_person_id, aggregate_version, payload, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.tenant_id,
                event.id,
                event.template_id,
                event.event_type,
                event.actor_person_id,
                event.aggregate_version,
                Jsonb(to_json_value(event.payload)),
                event.occurred_at,
            ),
        )

    @staticmethod
    def _validate_template_version(
        template: WorkflowTemplate,
        version: WorkflowTemplateVersion,
        *,
        expected_number: int,
    ) -> None:
        if (
            version.tenant_id != template.tenant_id
            or version.template_id != template.id
            or version.version != expected_number
        ):
            raise ValueError("template version does not belong to the aggregate")

    @staticmethod
    def _validate_template_event(
        template: WorkflowTemplate,
        event: TemplateAuditEvent,
        *,
        aggregate_version: int,
    ) -> None:
        if (
            event.tenant_id != template.tenant_id
            or event.template_id != template.id
            or event.aggregate_version != aggregate_version
        ):
            raise ValueError("template event does not belong to the aggregate")

    @staticmethod
    def _template_from_row(row: dict[str, Any]) -> WorkflowTemplate:
        return WorkflowTemplate(
            id=row["id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            status=TemplateStatus(row["status"]),
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )

    @staticmethod
    def _template_version_from_row(
        row: dict[str, Any],
    ) -> WorkflowTemplateVersion:
        return WorkflowTemplateVersion(
            id=row["id"],
            tenant_id=row["tenant_id"],
            template_id=row["template_id"],
            version=int(row["version"]),
            schema_version=row["schema_version"],
            locked=bool(row["locked"]),
            definition=row["definition"],
            content_hash=row["content_hash"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _template_event_from_row(row: dict[str, Any]) -> TemplateAuditEvent:
        return TemplateAuditEvent(
            id=row["id"],
            tenant_id=row["tenant_id"],
            template_id=row["template_id"],
            event_type=row["event_type"],
            actor_person_id=row["actor_person_id"],
            aggregate_version=int(row["aggregate_version"]),
            payload=row["payload"],
            occurred_at=row["occurred_at"],
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
                claimed_by=attempt_row["claimed_by"],
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

    @staticmethod
    def _projection_from_row(row: dict[str, Any]) -> ProjectionRecord:
        return ProjectionRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            instance_id=row["instance_id"],
            node_instance_id=row["node_instance_id"],
            attempt_no=int(row["attempt_no"]),
            kind=row["kind"],
            external_id=row["external_id"],
            external_url=row["external_url"],
            idempotency_key=row["idempotency_key"],
            sync_version=int(row["sync_version"]),
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class PostgresWorkflowInbox:
    """Durable inbox adapter kept separate from aggregate write authority."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self.connection_factory = connection_factory

    def append_inbox(self, event: TaskCompletionSignal) -> bool:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO workflow_inbox_events (
                    tenant_id, id, source, event_type, external_id,
                    event_types, available_at, occurred_at, received_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, id) DO NOTHING
                RETURNING id
                """,
                (
                    event.tenant_id,
                    event.id,
                    "feishu_event_bus",
                    "task.task.update_user_access_v2",
                    event.task_guid,
                    Jsonb(list(event.event_types)),
                    event.received_at,
                    event.occurred_at,
                    event.received_at,
                ),
            ).fetchone()
        return row is not None

    def claim_inbox(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
    ) -> tuple[InboxClaim, ...]:
        if not worker_id.strip():
            raise ValueError("inbound worker_id is required")
        if limit < 1:
            raise ValueError("inbound claim_limit must be positive")
        if claim_ttl <= timedelta(0):
            raise ValueError("inbound claim_ttl must be positive")
        token = secrets.token_urlsafe(24)
        expires_at = now + claim_ttl
        with self.connection_factory() as connection:
            with connection.transaction():
                rows = connection.execute(
                    """
                    WITH selected AS (
                        SELECT tenant_id, id
                        FROM workflow_inbox_events
                        WHERE tenant_id = %s
                          AND (
                            (status = 'verified' AND available_at <= %s)
                            OR (
                                status = 'failed'
                                AND failure_stage = 'processing'
                                AND available_at <= %s
                            )
                            OR (status = 'processing' AND claim_expires_at <= %s)
                          )
                        ORDER BY available_at, received_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE workflow_inbox_events AS event
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
                        now,
                        limit,
                        worker_id,
                        token,
                        expires_at,
                    ),
                ).fetchall()
        return tuple(
            InboxClaim(
                event=self._event_from_row(row),
                claim_token=token,
                claimed_by=worker_id,
                claim_expires_at=expires_at,
                attempt_count=int(row["attempt_count"]),
                task_state=task_state_from_dict(row["verified_payload"])
                if row["verified_payload"] is not None
                else None,
            )
            for row in rows
        )

    def claim_inbox_verification(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
    ) -> tuple[InboxClaim, ...]:
        if not worker_id.strip():
            raise ValueError("verification worker_id is required")
        if limit < 1:
            raise ValueError("verification claim_limit must be positive")
        if claim_ttl <= timedelta(0):
            raise ValueError("verification claim_ttl must be positive")
        token = secrets.token_urlsafe(24)
        expires_at = now + claim_ttl
        with self.connection_factory() as connection:
            with connection.transaction():
                rows = connection.execute(
                    """
                    WITH selected AS (
                        SELECT tenant_id, id
                        FROM workflow_inbox_events
                        WHERE tenant_id = %s
                          AND (
                            (status = 'pending' AND available_at <= %s)
                            OR (
                                status = 'failed'
                                AND failure_stage = 'verification'
                                AND available_at <= %s
                            )
                            OR (status = 'verifying' AND claim_expires_at <= %s)
                          )
                        ORDER BY available_at, received_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE workflow_inbox_events AS event
                    SET status = 'verifying',
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
                        now,
                        limit,
                        worker_id,
                        token,
                        expires_at,
                    ),
                ).fetchall()
        return tuple(
            InboxClaim(
                event=self._event_from_row(row),
                claim_token=token,
                claimed_by=worker_id,
                claim_expires_at=expires_at,
                attempt_count=int(row["attempt_count"]),
            )
            for row in rows
        )

    def mark_inbox_verified(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        task_state,
        now: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                UPDATE workflow_inbox_events
                SET status = 'verified', verified_payload = %s,
                    available_at = %s, failure_stage = NULL,
                    claimed_by = NULL, claim_token = NULL,
                    claim_expires_at = NULL, last_error = NULL
                WHERE tenant_id = %s AND id = %s
                  AND status = 'verifying' AND claim_token = %s
                RETURNING id
                """,
                (
                    Jsonb(task_state_to_dict(task_state)),
                    now,
                    tenant_id,
                    event_id,
                    claim_token,
                ),
            ).fetchone()
        if row is None:
            raise InvalidInboxClaimError(event_id)

    def mark_inbox_verification_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                UPDATE workflow_inbox_events
                SET status = 'failed', available_at = %s,
                    failure_stage = 'verification', last_error = %s,
                    claimed_by = NULL, claim_token = NULL,
                    claim_expires_at = NULL
                WHERE tenant_id = %s AND id = %s
                  AND status = 'verifying' AND claim_token = %s
                RETURNING id
                """,
                (retry_at, error, tenant_id, event_id, claim_token),
            ).fetchone()
        if row is None:
            raise InvalidInboxClaimError(event_id)

    def mark_inbox_verification_exhausted(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        now: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                UPDATE workflow_inbox_events
                SET status = 'exhausted', available_at = %s,
                    processed_at = %s,
                    outcome = 'exhausted:verification_attempts',
                    failure_stage = 'verification', last_error = %s,
                    claimed_by = NULL, claim_token = NULL,
                    claim_expires_at = NULL
                WHERE tenant_id = %s AND id = %s
                  AND status = 'verifying' AND claim_token = %s
                RETURNING id
                """,
                (now, now, error, tenant_id, event_id, claim_token),
            ).fetchone()
        if row is None:
            raise InvalidInboxClaimError(event_id)

    def mark_inbox_processed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        outcome: str,
        now: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                UPDATE workflow_inbox_events
                SET status = 'processed', processed_at = %s, outcome = %s,
                    failure_stage = NULL,
                    claimed_by = NULL, claim_token = NULL,
                    claim_expires_at = NULL, last_error = NULL
                WHERE tenant_id = %s AND id = %s
                  AND status = 'processing' AND claim_token = %s
                RETURNING id
                """,
                (now, outcome, tenant_id, event_id, claim_token),
            ).fetchone()
        if row is None:
            raise InvalidInboxClaimError(event_id)

    def mark_inbox_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                UPDATE workflow_inbox_events
                SET status = 'failed', available_at = %s, last_error = %s,
                    failure_stage = 'processing',
                    claimed_by = NULL, claim_token = NULL,
                    claim_expires_at = NULL
                WHERE tenant_id = %s AND id = %s
                  AND status = 'processing' AND claim_token = %s
                RETURNING id
                """,
                (retry_at, error, tenant_id, event_id, claim_token),
            ).fetchone()
        if row is None:
            raise InvalidInboxClaimError(event_id)

    @staticmethod
    def _event_from_row(row: dict[str, Any]) -> TaskCompletionSignal:
        return TaskCompletionSignal(
            id=row["id"],
            tenant_id=row["tenant_id"],
            task_guid=row["external_id"],
            event_types=tuple(row["event_types"]),
            occurred_at=row["occurred_at"],
            received_at=row["received_at"],
        )
