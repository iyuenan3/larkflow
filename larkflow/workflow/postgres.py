"""PostgreSQL persistence adapter for the target workflow aggregate."""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
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
from .editing import (
    GraphEditPreview,
    GraphEditPreviewExpiredError,
    GraphEditPreviewNotFoundError,
)
from .inbound import (
    InboxClaim,
    InvalidInboxClaimError,
    TaskCompletionSignal,
    task_state_from_dict,
    task_state_to_dict,
)
from .im_commands import (
    IMCommandClaim,
    IMCommandSignal,
    IMReplyClaim,
    InvalidIMCommandClaimError,
    _mention_from_dict,
    _mention_to_dict,
    _reply_key,
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
    WorkflowInstanceSummary,
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
from .restart import (
    RestartPreview,
    RestartPreviewExpiredError,
    RestartPreviewNotFoundError,
)
from .role_bindings import (
    InvalidRoleBindingClaimError,
    RoleBindingActionClaim,
    RoleBindingActionSignal,
    RoleBindingCardClaim,
    RoleBindingReplyClaim,
    RoleBindingRequest,
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

    def list_for_owner(
        self,
        tenant_id: str,
        *,
        owner_person_id: str,
        limit: int = 10,
    ) -> tuple[WorkflowInstanceSummary, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT instance.id,
                       instance.goal,
                       instance.status,
                       instance.created_at,
                       jsonb_array_length(instance.snapshot -> 'nodes')::integer
                           AS total_nodes,
                       (
                           SELECT COUNT(*)::integer
                           FROM workflow_node_instances AS node
                           WHERE node.tenant_id = instance.tenant_id
                             AND node.instance_id = instance.id
                             AND node.status = 'done'
                       ) AS completed_nodes
                FROM workflow_instances AS instance
                WHERE instance.tenant_id = %s
                  AND instance.owner_person_id = %s
                ORDER BY instance.created_at DESC, instance.id DESC
                LIMIT %s
                """,
                (tenant_id, owner_person_id, limit),
            ).fetchall()
        return tuple(
            WorkflowInstanceSummary(
                id=row["id"],
                goal=row["goal"],
                status=InstanceStatus(row["status"]),
                completed_nodes=int(row["completed_nodes"]),
                total_nodes=int(row["total_nodes"]),
                created_at=row["created_at"],
            )
            for row in rows
        )

    def add_restart_preview(self, preview: RestartPreview) -> None:
        with self.connection_factory() as connection:
            inserted = connection.execute(
                """
                INSERT INTO workflow_restart_previews (
                    tenant_id, id, instance_id, actor_person_id, node_key,
                    affected_node_keys, expected_instance_version,
                    graph_revision, created_at, expires_at,
                    consumed_at, applied_instance_version, scope
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (tenant_id, id) DO NOTHING
                RETURNING id
                """,
                (
                    preview.tenant_id,
                    preview.id,
                    preview.instance_id,
                    preview.actor_person_id,
                    preview.node_key,
                    Jsonb(list(preview.affected_node_keys)),
                    preview.expected_instance_version,
                    preview.graph_revision,
                    preview.created_at,
                    preview.expires_at,
                    preview.consumed_at,
                    preview.applied_instance_version,
                    preview.scope.value,
                ),
            ).fetchone()
        if inserted is None:
            raise ValueError(f"restart preview already exists: {preview.id}")

    def add_graph_edit_preview(self, preview: GraphEditPreview) -> None:
        with self.connection_factory() as connection:
            inserted = connection.execute(
                """
                INSERT INTO workflow_graph_edit_previews (
                    tenant_id, id, instance_id, actor_person_id, operations,
                    added_node_keys, updated_node_keys, removed_node_keys,
                    candidate_snapshot_hash, expected_instance_version,
                    graph_revision, proposed_graph_revision,
                    created_at, expires_at, consumed_at,
                    applied_instance_version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (tenant_id, id) DO NOTHING
                RETURNING id
                """,
                (
                    preview.tenant_id,
                    preview.id,
                    preview.instance_id,
                    preview.actor_person_id,
                    Jsonb(to_json_value(preview.operations)),
                    Jsonb(list(preview.added_node_keys)),
                    Jsonb(list(preview.updated_node_keys)),
                    Jsonb(list(preview.removed_node_keys)),
                    preview.candidate_snapshot_hash,
                    preview.expected_instance_version,
                    preview.graph_revision,
                    preview.proposed_graph_revision,
                    preview.created_at,
                    preview.expires_at,
                    preview.consumed_at,
                    preview.applied_instance_version,
                ),
            ).fetchone()
        if inserted is None:
            raise ValueError(f"graph edit preview already exists: {preview.id}")

    def get_graph_edit_preview(
        self,
        tenant_id: str,
        preview_id: str,
    ) -> GraphEditPreview:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_graph_edit_previews
                WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, preview_id),
            ).fetchone()
        if row is None:
            raise GraphEditPreviewNotFoundError((tenant_id, preview_id))
        return self._graph_edit_preview_from_row(row)

    def save_graph_edit(
        self,
        instance: WorkflowInstance,
        *,
        preview: GraphEditPreview,
        expected_version: int,
        consumed_at: datetime,
        audit_events: tuple[AuditEvent, ...] = (),
        outbox_events: tuple[OutboxEvent, ...] = (),
    ) -> bool:
        self._require_created_at(instance)
        self._validate_events(instance, audit_events, outbox_events)
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT * FROM workflow_graph_edit_previews
                    WHERE tenant_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (preview.tenant_id, preview.id),
                ).fetchone()
                if row is None:
                    raise GraphEditPreviewNotFoundError(
                        (preview.tenant_id, preview.id)
                    )
                stored = self._graph_edit_preview_from_row(row)
                if stored.consumed_at is not None:
                    return False
                if stored != preview:
                    raise ValueError("graph edit preview changed before confirmation")
                if consumed_at >= stored.expires_at:
                    raise GraphEditPreviewExpiredError(preview.id)

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
                        raise InstanceNotFoundError(
                            (instance.tenant_id, instance.id)
                        )
                    raise ConcurrentUpdateError(
                        f"instance {instance.id} expected version "
                        f"{expected_version}, found {current['version']}"
                    )
                instance.version = int(updated["version"])
                self._write_children(connection, instance)
                self._write_audit(connection, audit_events)
                self._write_outbox(connection, outbox_events)
                consumed = connection.execute(
                    """
                    UPDATE workflow_graph_edit_previews
                    SET consumed_at = %s, applied_instance_version = %s
                    WHERE tenant_id = %s AND id = %s
                      AND consumed_at IS NULL
                    RETURNING id
                    """,
                    (
                        consumed_at,
                        instance.version,
                        preview.tenant_id,
                        preview.id,
                    ),
                ).fetchone()
                if consumed is None:
                    raise RuntimeError("graph edit preview was consumed concurrently")
        return True

    def get_restart_preview(
        self,
        tenant_id: str,
        preview_id: str,
    ) -> RestartPreview:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_restart_previews
                WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, preview_id),
            ).fetchone()
        if row is None:
            raise RestartPreviewNotFoundError((tenant_id, preview_id))
        return self._restart_preview_from_row(row)

    def save_restart(
        self,
        instance: WorkflowInstance,
        *,
        preview: RestartPreview,
        expected_version: int,
        consumed_at: datetime,
        audit_events: tuple[AuditEvent, ...] = (),
        outbox_events: tuple[OutboxEvent, ...] = (),
    ) -> bool:
        self._require_created_at(instance)
        self._validate_events(instance, audit_events, outbox_events)
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT * FROM workflow_restart_previews
                    WHERE tenant_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (preview.tenant_id, preview.id),
                ).fetchone()
                if row is None:
                    raise RestartPreviewNotFoundError(
                        (preview.tenant_id, preview.id)
                    )
                stored = self._restart_preview_from_row(row)
                if stored.consumed_at is not None:
                    return False
                if stored != preview:
                    raise ValueError("restart preview changed before confirmation")
                if consumed_at >= stored.expires_at:
                    raise RestartPreviewExpiredError(preview.id)

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
                        raise InstanceNotFoundError(
                            (instance.tenant_id, instance.id)
                        )
                    raise ConcurrentUpdateError(
                        f"instance {instance.id} expected version "
                        f"{expected_version}, found {current['version']}"
                    )
                instance.version = int(updated["version"])
                self._write_children(connection, instance)
                self._write_audit(connection, audit_events)
                self._write_outbox(connection, outbox_events)
                consumed = connection.execute(
                    """
                    UPDATE workflow_restart_previews
                    SET consumed_at = %s, applied_instance_version = %s
                    WHERE tenant_id = %s AND id = %s
                      AND consumed_at IS NULL
                    RETURNING id
                    """,
                    (
                        consumed_at,
                        instance.version,
                        preview.tenant_id,
                        preview.id,
                    ),
                ).fetchone()
                if consumed is None:
                    raise RuntimeError("restart preview was consumed concurrently")
        return True

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

    def projection_instance_ids(
        self,
        tenant_id: str,
        *,
        after_id: str | None = None,
        limit: int = 100,
    ) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT instance.id
                FROM workflow_instances AS instance
                JOIN workflow_node_instances AS node
                  ON node.tenant_id = instance.tenant_id
                 AND node.instance_id = instance.id
                LEFT JOIN workflow_projections AS projection
                  ON projection.tenant_id = node.tenant_id
                 AND projection.node_instance_id = node.id
                 AND projection.attempt_no = node.current_attempt_no
                 AND projection.kind = 'feishu_task'
                WHERE instance.tenant_id = %s
                  AND (%s::text IS NULL OR instance.id > %s)
                  AND (
                    node.status = 'waiting_human'
                    OR (
                      node.status IN ('done', 'failed', 'canceled')
                      AND projection.id IS NOT NULL
                      AND coalesce(projection.state->>'completed', 'false') <> 'true'
                    )
                  )
                ORDER BY instance.id
                LIMIT %s
                """,
                (tenant_id, after_id, after_id, limit),
            ).fetchall()
        return tuple(str(row["id"]) for row in rows)

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

    def replace_projection_external(
        self,
        projection: ProjectionRecord,
        *,
        expected_external_id: str,
        expected_idempotency_key: str,
    ) -> None:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    UPDATE workflow_projections
                    SET external_id = %s,
                        external_url = %s,
                        idempotency_key = %s,
                        sync_version = %s,
                        state = %s,
                        updated_at = %s
                    WHERE tenant_id = %s
                      AND node_instance_id = %s
                      AND attempt_no = %s
                      AND kind = %s
                      AND id = %s
                      AND external_id = %s
                      AND idempotency_key = %s
                      AND sync_version <= %s
                    RETURNING id
                    """,
                    (
                        projection.external_id,
                        projection.external_url,
                        projection.idempotency_key,
                        projection.sync_version,
                        Jsonb(to_json_value(projection.state)),
                        projection.updated_at,
                        projection.tenant_id,
                        projection.node_instance_id,
                        projection.attempt_no,
                        projection.kind,
                        projection.id,
                        expected_external_id,
                        expected_idempotency_key,
                        projection.sync_version,
                    ),
                ).fetchone()
                if row is None:
                    raise ValueError("projection replacement lost a concurrent update")

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
        node_keys = list(instance.nodes)
        connection.execute(
            """
            DELETE FROM workflow_node_dependencies
            WHERE tenant_id = %s AND instance_id = %s
            """,
            (instance.tenant_id, instance.id),
        )
        connection.execute(
            """
            DELETE FROM workflow_node_attempts
            WHERE tenant_id = %s AND instance_id = %s
              AND NOT (node_key = ANY(%s))
            """,
            (instance.tenant_id, instance.id, node_keys),
        )
        connection.execute(
            """
            DELETE FROM workflow_node_instances
            WHERE tenant_id = %s AND instance_id = %s
              AND NOT (node_key = ANY(%s))
            """,
            (instance.tenant_id, instance.id, node_keys),
        )
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
    def _restart_preview_from_row(row: dict[str, Any]) -> RestartPreview:
        affected = row["affected_node_keys"]
        if not isinstance(affected, list) or not all(
            isinstance(item, str) for item in affected
        ):
            raise ValueError("restart preview affected_node_keys is invalid")
        return RestartPreview(
            id=row["id"],
            tenant_id=row["tenant_id"],
            instance_id=row["instance_id"],
            actor_person_id=row["actor_person_id"],
            node_key=row["node_key"],
            affected_node_keys=tuple(affected),
            expected_instance_version=int(row["expected_instance_version"]),
            graph_revision=int(row["graph_revision"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            scope=row["scope"],
            consumed_at=row["consumed_at"],
            applied_instance_version=(
                int(row["applied_instance_version"])
                if row["applied_instance_version"] is not None
                else None
            ),
        )

    @staticmethod
    def _graph_edit_preview_from_row(row: dict[str, Any]) -> GraphEditPreview:
        operations = row["operations"]
        added = row["added_node_keys"]
        updated = row["updated_node_keys"]
        removed = row["removed_node_keys"]
        if not isinstance(operations, list) or not all(
            isinstance(item, dict) for item in operations
        ):
            raise ValueError("graph edit preview operations are invalid")
        for name, values in (
            ("added_node_keys", added),
            ("updated_node_keys", updated),
            ("removed_node_keys", removed),
        ):
            if not isinstance(values, list) or not all(
                isinstance(item, str) for item in values
            ):
                raise ValueError(f"graph edit preview {name} is invalid")
        return GraphEditPreview(
            id=row["id"],
            tenant_id=row["tenant_id"],
            instance_id=row["instance_id"],
            actor_person_id=row["actor_person_id"],
            operations=tuple(operations),
            added_node_keys=tuple(added),
            updated_node_keys=tuple(updated),
            removed_node_keys=tuple(removed),
            candidate_snapshot_hash=row["candidate_snapshot_hash"],
            expected_instance_version=int(row["expected_instance_version"]),
            graph_revision=int(row["graph_revision"]),
            proposed_graph_revision=int(row["proposed_graph_revision"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
            applied_instance_version=(
                int(row["applied_instance_version"])
                if row["applied_instance_version"] is not None
                else None
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
                    event.source,
                    event.event_type,
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
            source=row["source"],
            event_type=row["event_type"],
        )


class PostgresIMCommandStore:
    """Durable IM command and reply queue with split credential authority."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self.connection_factory = connection_factory

    def append_im_command(self, event: IMCommandSignal) -> bool:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO workflow_im_commands (
                    tenant_id, id, message_id, chat_id, sender_person_id,
                    text, mentions, card_update_token, available_at,
                    occurred_at, received_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    event.tenant_id,
                    event.id,
                    event.message_id,
                    event.chat_id,
                    event.sender_person_id,
                    event.text,
                    Jsonb([_mention_to_dict(item) for item in event.mentions]),
                    event.card_update_token,
                    event.available_at or event.received_at,
                    event.occurred_at,
                    event.received_at,
                ),
            ).fetchone()
        return row is not None

    def release_im_command(
        self,
        tenant_id: str,
        event_id: str,
        *,
        available_at: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                UPDATE workflow_im_commands
                SET available_at = LEAST(available_at, %s)
                WHERE tenant_id = %s AND id = %s AND status = 'pending'
                RETURNING id
                """,
                (available_at, tenant_id, event_id),
            ).fetchone()
        if row is None:
            raise InvalidIMCommandClaimError(event_id)

    def claim_im_verification(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[IMCommandClaim, ...]:
        return self._claim_commands(
            tenant_id,
            worker_id=worker_id,
            now=now,
            limit=limit,
            claim_ttl=claim_ttl,
            stage="verification",
        )

    def claim_im_commands(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[IMCommandClaim, ...]:
        return self._claim_commands(
            tenant_id,
            worker_id=worker_id,
            now=now,
            limit=limit,
            claim_ttl=claim_ttl,
            stage="processing",
        )

    def _claim_commands(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
        stage: str,
    ) -> tuple[IMCommandClaim, ...]:
        self._validate_claim(worker_id, limit, claim_ttl)
        if stage == "verification":
            active = "verifying"
            predicate = """
                (status = 'pending' AND available_at <= %s)
                OR (
                    status = 'failed' AND failure_stage = 'verification'
                    AND available_at <= %s
                )
                OR (status = 'verifying' AND claim_expires_at <= %s)
            """
        else:
            active = "processing"
            predicate = """
                (status = 'verified' AND available_at <= %s)
                OR (
                    status = 'failed' AND failure_stage = 'processing'
                    AND available_at <= %s
                )
                OR (status = 'processing' AND claim_expires_at <= %s)
            """
        token = secrets.token_urlsafe(24)
        expires_at = now + claim_ttl
        sql = f"""
            WITH selected AS (
                SELECT tenant_id, id
                FROM workflow_im_commands
                WHERE tenant_id = %s AND ({predicate})
                ORDER BY available_at, received_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE workflow_im_commands AS command
            SET status = %s,
                attempt_count = command.attempt_count + 1,
                claimed_by = %s,
                claim_token = %s,
                claim_expires_at = %s
            FROM selected
            WHERE command.tenant_id = selected.tenant_id
              AND command.id = selected.id
            RETURNING command.*
        """
        with self.connection_factory() as connection:
            with connection.transaction():
                rows = connection.execute(
                    sql,
                    (
                        tenant_id,
                        now,
                        now,
                        now,
                        limit,
                        active,
                        worker_id,
                        token,
                        expires_at,
                    ),
                ).fetchall()
        return tuple(
            IMCommandClaim(
                event=self._command_from_row(row),
                claim_token=token,
                attempt_count=int(row["attempt_count"]),
            )
            for row in rows
        )

    def mark_im_verified(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        now: datetime,
    ) -> None:
        self._settle_command(
            """
            UPDATE workflow_im_commands
            SET status = 'verified', verified_at = %s, available_at = %s,
                failure_stage = NULL, last_error = NULL,
                claimed_by = NULL, claim_token = NULL,
                claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status = 'verifying' AND claim_token = %s
            RETURNING id
            """,
            (now, now, tenant_id, event_id, claim_token),
            event_id,
        )

    def mark_im_verification_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        self._settle_command(
            """
            UPDATE workflow_im_commands
            SET status = 'failed', available_at = %s,
                failure_stage = 'verification', last_error = %s,
                claimed_by = NULL, claim_token = NULL,
                claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status = 'verifying' AND claim_token = %s
            RETURNING id
            """,
            (retry_at, error, tenant_id, event_id, claim_token),
            event_id,
        )

    def mark_im_verification_rejected(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        outcome: str,
        reply_text: str,
        now: datetime,
    ) -> None:
        status = "exhausted" if outcome.startswith("exhausted:") else "processed"
        self._settle_command(
            """
            UPDATE workflow_im_commands
            SET status = %s, processed_at = %s, outcome = %s,
                failure_stage = 'verification', reply_text = %s,
                reply_status = 'pending', reply_available_at = %s,
                claimed_by = NULL, claim_token = NULL,
                claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status = 'verifying' AND claim_token = %s
            RETURNING id
            """,
            (
                status,
                now,
                outcome,
                reply_text,
                now,
                tenant_id,
                event_id,
                claim_token,
            ),
            event_id,
        )

    def mark_im_processed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        outcome: str,
        instance_id: str | None,
        reply_text: str,
        now: datetime,
    ) -> None:
        self._settle_command(
            """
            UPDATE workflow_im_commands
            SET status = 'processed', processed_at = %s, outcome = %s,
                instance_id = %s, failure_stage = NULL, last_error = NULL,
                reply_text = %s, reply_status = 'pending',
                reply_available_at = %s,
                claimed_by = NULL, claim_token = NULL,
                claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status = 'processing' AND claim_token = %s
            RETURNING id
            """,
            (
                now,
                outcome,
                instance_id,
                reply_text,
                now,
                tenant_id,
                event_id,
                claim_token,
            ),
            event_id,
        )

    def mark_im_role_binding_requested(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        request: RoleBindingRequest,
        now: datetime,
    ) -> None:
        if request.command_id != event_id or request.tenant_id != tenant_id:
            raise ValueError("role-binding request does not match the IM command")
        self._settle_command(
            """
            UPDATE workflow_im_commands
            SET status = 'processed', processed_at = %s,
                outcome = 'role_binding_requested',
                failure_stage = NULL, last_error = NULL,
                role_binding_request = %s,
                role_binding_candidates = NULL,
                reply_kind = 'role_binding_card',
                reply_text = %s, reply_status = 'pending',
                reply_available_at = %s,
                claimed_by = NULL, claim_token = NULL,
                claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status = 'processing' AND claim_token = %s
            RETURNING id
            """,
            (
                now,
                Jsonb(_role_request_to_dict(request)),
                "请选择每个流程角色的负责人。",
                now,
                tenant_id,
                event_id,
                claim_token,
            ),
            event_id,
        )

    def mark_im_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        self._settle_command(
            """
            UPDATE workflow_im_commands
            SET status = 'failed', available_at = %s,
                failure_stage = 'processing', last_error = %s,
                claimed_by = NULL, claim_token = NULL,
                claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status = 'processing' AND claim_token = %s
            RETURNING id
            """,
            (retry_at, error, tenant_id, event_id, claim_token),
            event_id,
        )

    def claim_im_replies(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[IMReplyClaim, ...]:
        self._validate_claim(worker_id, limit, claim_ttl)
        token = secrets.token_urlsafe(24)
        expires_at = now + claim_ttl
        with self.connection_factory() as connection:
            with connection.transaction():
                rows = connection.execute(
                    """
                    WITH selected AS (
                        SELECT tenant_id, id
                        FROM workflow_im_commands
                        WHERE tenant_id = %s
                          AND reply_kind = 'text'
                          AND (
                            (reply_status IN ('pending', 'failed')
                                AND reply_available_at <= %s)
                            OR (
                                reply_status = 'sending'
                                AND reply_claim_expires_at <= %s
                            )
                          )
                        ORDER BY reply_available_at, processed_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE workflow_im_commands AS command
                    SET reply_status = 'sending',
                        reply_attempt_count = command.reply_attempt_count + 1,
                        reply_claimed_by = %s,
                        reply_claim_token = %s,
                        reply_claim_expires_at = %s
                    FROM selected
                    WHERE command.tenant_id = selected.tenant_id
                      AND command.id = selected.id
                    RETURNING command.*
                    """,
                    (
                        tenant_id,
                        now,
                        now,
                        limit,
                        worker_id,
                        token,
                        expires_at,
                    ),
                ).fetchall()
        return tuple(
            IMReplyClaim(
                event_id=row["id"],
                tenant_id=row["tenant_id"],
                chat_id=row["chat_id"],
                text=row["reply_text"],
                idempotency_key=_reply_key(row["id"]),
                claim_token=token,
                attempt_count=int(row["reply_attempt_count"]),
                card_update_token=row["card_update_token"],
            )
            for row in rows
        )

    def mark_im_reply_sent(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        external_id: str,
        now: datetime,
    ) -> None:
        self._settle_command(
            """
            UPDATE workflow_im_commands
            SET reply_status = 'sent', reply_external_id = %s,
                reply_sent_at = %s, reply_last_error = NULL,
                reply_claimed_by = NULL, reply_claim_token = NULL,
                reply_claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND reply_status = 'sending' AND reply_claim_token = %s
            RETURNING id
            """,
            (external_id, now, tenant_id, event_id, claim_token),
            event_id,
        )

    def mark_im_reply_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        self._settle_command(
            """
            UPDATE workflow_im_commands
            SET reply_status = 'failed', reply_available_at = %s,
                reply_last_error = %s,
                reply_claimed_by = NULL, reply_claim_token = NULL,
                reply_claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND reply_status = 'sending' AND reply_claim_token = %s
            RETURNING id
            """,
            (retry_at, error, tenant_id, event_id, claim_token),
            event_id,
        )

    def claim_role_binding_cards(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[RoleBindingCardClaim, ...]:
        self._validate_claim(worker_id, limit, claim_ttl)
        token = secrets.token_urlsafe(24)
        expires_at = now + claim_ttl
        with self.connection_factory() as connection:
            with connection.transaction():
                rows = connection.execute(
                    """
                    WITH selected AS (
                        SELECT tenant_id, id
                        FROM workflow_im_commands
                        WHERE tenant_id = %s
                          AND reply_kind = 'role_binding_card'
                          AND (
                            (reply_status IN ('pending', 'failed')
                                AND reply_available_at <= %s)
                            OR (
                                reply_status = 'sending'
                                AND reply_claim_expires_at <= %s
                            )
                          )
                        ORDER BY reply_available_at, processed_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE workflow_im_commands AS command
                    SET reply_status = 'sending',
                        reply_attempt_count = command.reply_attempt_count + 1,
                        reply_claimed_by = %s,
                        reply_claim_token = %s,
                        reply_claim_expires_at = %s
                    FROM selected
                    WHERE command.tenant_id = selected.tenant_id
                      AND command.id = selected.id
                    RETURNING command.*
                    """,
                    (
                        tenant_id,
                        now,
                        now,
                        limit,
                        worker_id,
                        token,
                        expires_at,
                    ),
                ).fetchall()
        return tuple(
            RoleBindingCardClaim(
                request=_role_request_from_command_row(row),
                claim_token=token,
                attempt_count=int(row["reply_attempt_count"]),
            )
            for row in rows
        )

    def mark_role_binding_card_sent(
        self,
        tenant_id: str,
        command_id: str,
        *,
        claim_token: str,
        candidate_person_ids: tuple[str, ...],
        external_id: str,
        now: datetime,
    ) -> None:
        self._settle_command(
            """
            UPDATE workflow_im_commands
            SET role_binding_candidates = %s,
                reply_status = 'sent', reply_external_id = %s,
                reply_sent_at = %s, reply_last_error = NULL,
                reply_claimed_by = NULL, reply_claim_token = NULL,
                reply_claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND reply_kind = 'role_binding_card'
              AND reply_status = 'sending' AND reply_claim_token = %s
            RETURNING id
            """,
            (
                Jsonb(list(candidate_person_ids)),
                external_id,
                now,
                tenant_id,
                command_id,
                claim_token,
            ),
            command_id,
        )

    def mark_role_binding_card_failed(
        self,
        tenant_id: str,
        command_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        self._settle_command(
            """
            UPDATE workflow_im_commands
            SET reply_status = 'failed', reply_available_at = %s,
                reply_last_error = %s,
                reply_claimed_by = NULL, reply_claim_token = NULL,
                reply_claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND reply_kind = 'role_binding_card'
              AND reply_status = 'sending' AND reply_claim_token = %s
            RETURNING id
            """,
            (retry_at, error, tenant_id, command_id, claim_token),
            command_id,
        )

    def append_role_binding_action(self, event: RoleBindingActionSignal) -> bool:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO workflow_role_binding_actions (
                    tenant_id, id, message_id, chat_id, operator_person_id,
                    action_tag, action_name, form_value, update_token,
                    available_at, occurred_at, received_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    event.tenant_id,
                    event.id,
                    event.message_id,
                    event.chat_id,
                    event.operator_person_id,
                    event.action_tag,
                    event.action_name,
                    event.form_value,
                    event.update_token,
                    event.available_at or event.received_at,
                    event.occurred_at,
                    event.received_at,
                ),
            ).fetchone()
        return row is not None

    def release_role_binding_action(
        self,
        tenant_id: str,
        event_id: str,
        *,
        available_at: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                UPDATE workflow_role_binding_actions
                SET available_at = LEAST(available_at, %s)
                WHERE tenant_id = %s AND id = %s AND status = 'pending'
                RETURNING id
                """,
                (available_at, tenant_id, event_id),
            ).fetchone()
        if row is None:
            raise InvalidRoleBindingClaimError(event_id)

    def claim_role_binding_verification(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[RoleBindingActionClaim, ...]:
        return self._claim_role_binding_actions(
            tenant_id,
            worker_id=worker_id,
            now=now,
            limit=limit,
            claim_ttl=claim_ttl,
            stage="verification",
        )

    def claim_role_binding_actions(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[RoleBindingActionClaim, ...]:
        return self._claim_role_binding_actions(
            tenant_id,
            worker_id=worker_id,
            now=now,
            limit=limit,
            claim_ttl=claim_ttl,
            stage="processing",
        )

    def _claim_role_binding_actions(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
        stage: str,
    ) -> tuple[RoleBindingActionClaim, ...]:
        self._validate_claim(worker_id, limit, claim_ttl)
        if stage == "verification":
            active = "verifying"
            predicate = """
                (status = 'pending' AND available_at <= %s)
                OR (
                    status = 'failed' AND failure_stage = 'verification'
                    AND available_at <= %s
                )
                OR (status = 'verifying' AND claim_expires_at <= %s)
            """
        else:
            active = "processing"
            predicate = """
                (status = 'verified' AND available_at <= %s)
                OR (
                    status = 'failed' AND failure_stage = 'processing'
                    AND available_at <= %s
                )
                OR (status = 'processing' AND claim_expires_at <= %s)
            """
        token = secrets.token_urlsafe(24)
        expires_at = now + claim_ttl
        sql = f"""
            WITH selected AS (
                SELECT tenant_id, id
                FROM workflow_role_binding_actions
                WHERE tenant_id = %s AND ({predicate})
                ORDER BY available_at, received_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            ), updated AS (
                UPDATE workflow_role_binding_actions AS action
                SET status = %s,
                    attempt_count = action.attempt_count + 1,
                    claimed_by = %s,
                    claim_token = %s,
                    claim_expires_at = %s
                FROM selected
                WHERE action.tenant_id = selected.tenant_id
                  AND action.id = selected.id
                RETURNING action.*
            )
            SELECT updated.*,
                   command.id AS command_id,
                   command.message_id AS command_message_id,
                   command.chat_id AS command_chat_id,
                   command.sender_person_id AS command_sender_person_id,
                   command.role_binding_request,
                   command.role_binding_candidates,
                   command.reply_external_id AS card_message_id
            FROM updated
            LEFT JOIN workflow_im_commands AS command
              ON command.tenant_id = updated.tenant_id
             AND command.reply_kind = 'role_binding_card'
             AND command.reply_external_id = updated.message_id
        """
        with self.connection_factory() as connection:
            with connection.transaction():
                rows = connection.execute(
                    sql,
                    (
                        tenant_id,
                        now,
                        now,
                        now,
                        limit,
                        active,
                        worker_id,
                        token,
                        expires_at,
                    ),
                ).fetchall()
        return tuple(
            _role_action_claim_from_row(row, token=token)
            for row in rows
        )

    def mark_role_binding_verified(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        owner_bindings: Mapping[str, str],
        now: datetime,
    ) -> None:
        self._settle_role_action(
            """
            UPDATE workflow_role_binding_actions
            SET status = 'verified', verified_at = %s, available_at = %s,
                owner_bindings = %s, failure_stage = NULL, last_error = NULL,
                claimed_by = NULL, claim_token = NULL,
                claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status = 'verifying' AND claim_token = %s
            RETURNING id
            """,
            (
                now,
                now,
                Jsonb(dict(owner_bindings)),
                tenant_id,
                event_id,
                claim_token,
            ),
            event_id,
        )

    def mark_role_binding_rejected(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        outcome: str,
        reply_text: str | None,
        now: datetime,
    ) -> None:
        self._settle_role_action(
            """
            UPDATE workflow_role_binding_actions
            SET status = 'rejected', processed_at = %s, outcome = %s,
                reply_text = %s,
                reply_status = CASE WHEN %s IS NULL THEN NULL ELSE 'pending' END,
                reply_available_at = CASE WHEN %s IS NULL THEN NULL ELSE %s END,
                claimed_by = NULL, claim_token = NULL,
                claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status IN ('verifying', 'processing') AND claim_token = %s
            RETURNING id
            """,
            (
                now,
                outcome,
                reply_text,
                reply_text,
                reply_text,
                now,
                tenant_id,
                event_id,
                claim_token,
            ),
            event_id,
        )

    def mark_role_binding_verification_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        self._mark_role_binding_action_failed(
            tenant_id,
            event_id,
            claim_token=claim_token,
            error=error,
            retry_at=retry_at,
            stage="verification",
        )

    def mark_role_binding_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        self._mark_role_binding_action_failed(
            tenant_id,
            event_id,
            claim_token=claim_token,
            error=error,
            retry_at=retry_at,
            stage="processing",
        )

    def _mark_role_binding_action_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
        stage: str,
    ) -> None:
        active = "verifying" if stage == "verification" else "processing"
        self._settle_role_action(
            """
            UPDATE workflow_role_binding_actions
            SET status = 'failed', available_at = %s,
                failure_stage = %s, last_error = %s,
                claimed_by = NULL, claim_token = NULL,
                claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status = %s AND claim_token = %s
            RETURNING id
            """,
            (
                retry_at,
                stage,
                error,
                tenant_id,
                event_id,
                active,
                claim_token,
            ),
            event_id,
        )

    def mark_role_binding_processed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        instance_id: str,
        reply_text: str,
        now: datetime,
    ) -> None:
        self._settle_role_action(
            """
            UPDATE workflow_role_binding_actions
            SET status = 'processed', processed_at = %s,
                outcome = 'draft_created', instance_id = %s,
                failure_stage = NULL, last_error = NULL,
                reply_text = %s, reply_status = 'pending',
                reply_available_at = %s,
                claimed_by = NULL, claim_token = NULL,
                claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status = 'processing' AND claim_token = %s
            RETURNING id
            """,
            (
                now,
                instance_id,
                reply_text,
                now,
                tenant_id,
                event_id,
                claim_token,
            ),
            event_id,
        )

    def claim_role_binding_replies(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[RoleBindingReplyClaim, ...]:
        self._validate_claim(worker_id, limit, claim_ttl)
        token = secrets.token_urlsafe(24)
        expires_at = now + claim_ttl
        with self.connection_factory() as connection:
            with connection.transaction():
                rows = connection.execute(
                    """
                    WITH selected AS (
                        SELECT tenant_id, id
                        FROM workflow_role_binding_actions
                        WHERE tenant_id = %s
                          AND (
                            (reply_status IN ('pending', 'failed')
                                AND reply_available_at <= %s)
                            OR (
                                reply_status = 'sending'
                                AND reply_claim_expires_at <= %s
                            )
                          )
                        ORDER BY reply_available_at, processed_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    ), updated AS (
                        UPDATE workflow_role_binding_actions AS action
                        SET reply_status = 'sending',
                            reply_attempt_count = action.reply_attempt_count + 1,
                            reply_claimed_by = %s,
                            reply_claim_token = %s,
                            reply_claim_expires_at = %s
                        FROM selected
                        WHERE action.tenant_id = selected.tenant_id
                          AND action.id = selected.id
                        RETURNING action.*
                    )
                    SELECT updated.*,
                           command.id AS command_id,
                           command.message_id AS command_message_id,
                           command.chat_id AS command_chat_id,
                           command.sender_person_id AS command_sender_person_id,
                           command.role_binding_request,
                           command.role_binding_candidates,
                           command.reply_external_id AS card_message_id
                    FROM updated
                    LEFT JOIN workflow_im_commands AS command
                      ON command.tenant_id = updated.tenant_id
                     AND command.reply_kind = 'role_binding_card'
                     AND command.reply_external_id = updated.message_id
                    """,
                    (
                        tenant_id,
                        now,
                        now,
                        limit,
                        worker_id,
                        token,
                        expires_at,
                    ),
                ).fetchall()
        return tuple(
            RoleBindingReplyClaim(
                action=_role_action_from_row(row),
                request=(
                    None
                    if row.get("command_id") is None
                    else _role_request_from_joined_row(row)
                ),
                owner_bindings=_owner_bindings_from_row(row),
                instance_id=row.get("instance_id"),
                text=str(row["reply_text"]),
                claim_token=token,
                attempt_count=int(row["reply_attempt_count"]),
            )
            for row in rows
        )

    def mark_role_binding_reply_sent(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        external_id: str,
        now: datetime,
    ) -> None:
        self._settle_role_action(
            """
            UPDATE workflow_role_binding_actions
            SET reply_status = 'sent', reply_external_id = %s,
                reply_sent_at = %s, reply_last_error = NULL,
                reply_claimed_by = NULL, reply_claim_token = NULL,
                reply_claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND reply_status = 'sending' AND reply_claim_token = %s
            RETURNING id
            """,
            (external_id, now, tenant_id, event_id, claim_token),
            event_id,
        )

    def mark_role_binding_reply_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        self._settle_role_action(
            """
            UPDATE workflow_role_binding_actions
            SET reply_status = 'failed', reply_available_at = %s,
                reply_last_error = %s,
                reply_claimed_by = NULL, reply_claim_token = NULL,
                reply_claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND reply_status = 'sending' AND reply_claim_token = %s
            RETURNING id
            """,
            (retry_at, error, tenant_id, event_id, claim_token),
            event_id,
        )

    def _settle_role_action(
        self,
        sql: str,
        parameters: tuple[Any, ...],
        event_id: str,
    ) -> None:
        with self.connection_factory() as connection:
            row = connection.execute(sql, parameters).fetchone()
        if row is None:
            raise InvalidRoleBindingClaimError(event_id)

    def _settle_command(
        self,
        sql: str,
        parameters: tuple[Any, ...],
        event_id: str,
    ) -> None:
        with self.connection_factory() as connection:
            row = connection.execute(sql, parameters).fetchone()
        if row is None:
            raise InvalidIMCommandClaimError(event_id)

    @staticmethod
    def _command_from_row(row: dict[str, Any]) -> IMCommandSignal:
        raw_mentions = row.get("mentions") or []
        if not isinstance(raw_mentions, list) or not all(
            isinstance(item, Mapping) for item in raw_mentions
        ):
            raise ValueError("persisted IM mentions must be an object array")
        return IMCommandSignal(
            id=row["id"],
            tenant_id=row["tenant_id"],
            message_id=row["message_id"],
            chat_id=row["chat_id"],
            sender_person_id=row["sender_person_id"],
            text=row["text"],
            occurred_at=row["occurred_at"],
            received_at=row["received_at"],
            mentions=tuple(_mention_from_dict(item) for item in raw_mentions),
            card_update_token=row.get("card_update_token"),
        )

    @staticmethod
    def _validate_claim(
        worker_id: str,
        limit: int,
        claim_ttl: timedelta,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("IM worker_id is required")
        if limit < 1:
            raise ValueError("IM claim_limit must be positive")
        if claim_ttl <= timedelta(0):
            raise ValueError("IM claim_ttl must be positive")


def _role_request_to_dict(request: RoleBindingRequest) -> dict[str, Any]:
    return {
        "template_id": request.template_id,
        "template_version": request.template_version,
        "goal": request.goal,
        "inputs": to_json_value(request.inputs),
        "roles": list(request.roles),
    }


def _role_request_from_command_row(row: Mapping[str, Any]) -> RoleBindingRequest:
    return _role_request_from_values(
        command_id=row.get("id"),
        tenant_id=row.get("tenant_id"),
        message_id=row.get("message_id"),
        chat_id=row.get("chat_id"),
        initiator_person_id=row.get("sender_person_id"),
        raw_request=row.get("role_binding_request"),
        raw_candidates=row.get("role_binding_candidates"),
        card_message_id=row.get("reply_external_id"),
    )


def _role_request_from_joined_row(row: Mapping[str, Any]) -> RoleBindingRequest:
    return _role_request_from_values(
        command_id=row.get("command_id"),
        tenant_id=row.get("tenant_id"),
        message_id=row.get("command_message_id"),
        chat_id=row.get("command_chat_id"),
        initiator_person_id=row.get("command_sender_person_id"),
        raw_request=row.get("role_binding_request"),
        raw_candidates=row.get("role_binding_candidates"),
        card_message_id=row.get("card_message_id"),
    )


def _role_request_from_values(
    *,
    command_id: Any,
    tenant_id: Any,
    message_id: Any,
    chat_id: Any,
    initiator_person_id: Any,
    raw_request: Any,
    raw_candidates: Any,
    card_message_id: Any,
) -> RoleBindingRequest:
    fields = {
        "command_id": command_id,
        "tenant_id": tenant_id,
        "message_id": message_id,
        "chat_id": chat_id,
        "initiator_person_id": initiator_person_id,
    }
    if not all(isinstance(value, str) and value for value in fields.values()):
        raise ValueError("persisted role-binding request identity is incomplete")
    if not isinstance(raw_request, Mapping):
        raise ValueError("persisted role-binding request must be an object")
    template_id = raw_request.get("template_id")
    template_version = raw_request.get("template_version")
    goal = raw_request.get("goal")
    inputs = raw_request.get("inputs")
    roles = raw_request.get("roles")
    if not isinstance(template_id, str) or not template_id:
        raise ValueError("persisted role-binding template_id is invalid")
    if isinstance(template_version, bool) or not isinstance(template_version, int):
        raise ValueError("persisted role-binding template_version is invalid")
    if not isinstance(goal, str) or not isinstance(inputs, Mapping):
        raise ValueError("persisted role-binding request content is invalid")
    if not isinstance(roles, list) or not roles or not all(
        isinstance(role, str) and role for role in roles
    ):
        raise ValueError("persisted role-binding roles are invalid")
    candidates = raw_candidates or []
    if not isinstance(candidates, list) or not all(
        isinstance(person_id, str) and person_id for person_id in candidates
    ):
        raise ValueError("persisted role-binding candidates are invalid")
    if card_message_id is not None and not isinstance(card_message_id, str):
        raise ValueError("persisted role-binding card message is invalid")
    return RoleBindingRequest(
        command_id=command_id,
        tenant_id=tenant_id,
        message_id=message_id,
        chat_id=chat_id,
        initiator_person_id=initiator_person_id,
        template_id=template_id,
        template_version=template_version,
        goal=goal,
        inputs=dict(inputs),
        roles=tuple(roles),
        candidate_person_ids=tuple(candidates),
        card_message_id=card_message_id,
    )


def _role_action_from_row(row: Mapping[str, Any]) -> RoleBindingActionSignal:
    return RoleBindingActionSignal(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        message_id=str(row["message_id"]),
        chat_id=str(row["chat_id"]),
        operator_person_id=str(row["operator_person_id"]),
        action_tag=str(row["action_tag"]),
        action_name=str(row["action_name"]),
        form_value=str(row["form_value"]),
        update_token=str(row["update_token"]),
        occurred_at=row["occurred_at"],
        received_at=row["received_at"],
    )


def _owner_bindings_from_row(row: Mapping[str, Any]) -> dict[str, str]:
    raw = row.get("owner_bindings") or {}
    if not isinstance(raw, Mapping) or not all(
        isinstance(role, str)
        and role
        and isinstance(person_id, str)
        and person_id
        for role, person_id in raw.items()
    ):
        raise ValueError("persisted owner bindings are invalid")
    return dict(raw)


def _role_action_claim_from_row(
    row: Mapping[str, Any],
    *,
    token: str,
) -> RoleBindingActionClaim:
    request = (
        None
        if row.get("command_id") is None
        else _role_request_from_joined_row(row)
    )
    return RoleBindingActionClaim(
        action=_role_action_from_row(row),
        request=request,
        claim_token=token,
        attempt_count=int(row["attempt_count"]),
        owner_bindings=_owner_bindings_from_row(row),
        instance_id=row.get("instance_id"),
        reply_text=row.get("reply_text"),
    )
