"""Tenant-bound repositories for enterprise knowledge publication metadata."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from psycopg.types.json import Jsonb

from .contracts import EnterpriseKnowledgePublication, EnterpriseKnowledgeRef


class EnterpriseKnowledgeNotFoundError(LookupError):
    pass


class EnterpriseKnowledgeConflictError(ValueError):
    pass


@dataclass(frozen=True)
class EnterpriseKnowledgeAuditEvent:
    tenant_id: str
    event_id: str
    source_id: str
    version_id: str
    event_type: str
    actor_person_id: str
    snapshot: dict[str, Any]
    occurred_at: datetime


class EnterpriseKnowledgeRepository(Protocol):
    def publish(
        self,
        publication: EnterpriseKnowledgePublication,
    ) -> EnterpriseKnowledgePublication:
        ...

    def revoke(
        self,
        tenant_id: str,
        source_id: str,
        version_id: str,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> EnterpriseKnowledgePublication:
        ...

    def list_published(self, tenant_id: str) -> tuple[EnterpriseKnowledgeRef, ...]:
        ...


class InMemoryEnterpriseKnowledgeRepository:
    def __init__(self) -> None:
        self._items: dict[
            tuple[str, str, str], EnterpriseKnowledgePublication
        ] = {}
        self._audit: list[EnterpriseKnowledgeAuditEvent] = []
        self._lock = RLock()

    def publish(
        self,
        publication: EnterpriseKnowledgePublication,
    ) -> EnterpriseKnowledgePublication:
        if publication.status != "published":
            raise EnterpriseKnowledgeConflictError("only published state can be added")
        key = _publication_key(publication)
        with self._lock:
            existing = self._items.get(key)
            if existing is not None:
                if existing == publication:
                    return existing
                raise EnterpriseKnowledgeConflictError(
                    "enterprise knowledge version already exists"
                )
            if self._active_for_source(
                publication.ref.tenant_id,
                publication.ref.source_id,
            ) is not None:
                raise EnterpriseKnowledgeConflictError(
                    "enterprise knowledge source already has a published version"
                )
            self._items[key] = publication
            self._append_audit(
                publication,
                event_type="enterprise_knowledge.published",
                actor_person_id=publication.published_by_person_id,
                occurred_at=publication.ref.published_at,
            )
            return publication

    def revoke(
        self,
        tenant_id: str,
        source_id: str,
        version_id: str,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> EnterpriseKnowledgePublication:
        key = (tenant_id, source_id, version_id)
        with self._lock:
            existing = self._items.get(key)
            if existing is None:
                raise EnterpriseKnowledgeNotFoundError(source_id)
            if existing.status == "revoked":
                return existing
            revoked = EnterpriseKnowledgePublication(
                ref=existing.ref,
                published_by_person_id=existing.published_by_person_id,
                status="revoked",
                revoked_at=now,
            )
            self._items[key] = revoked
            self._append_audit(
                revoked,
                event_type="enterprise_knowledge.revoked",
                actor_person_id=actor_person_id,
                occurred_at=now,
            )
            return revoked

    def list_published(self, tenant_id: str) -> tuple[EnterpriseKnowledgeRef, ...]:
        with self._lock:
            return tuple(
                item.ref
                for item in sorted(
                    (
                        publication
                        for publication in self._items.values()
                        if publication.ref.tenant_id == tenant_id
                        and publication.status == "published"
                    ),
                    key=lambda publication: (
                        publication.ref.source_id,
                        publication.ref.version_id,
                    ),
                )
            )

    def list_audit(
        self,
        tenant_id: str,
        source_id: str,
        version_id: str,
    ) -> tuple[EnterpriseKnowledgeAuditEvent, ...]:
        with self._lock:
            return tuple(
                event
                for event in self._audit
                if event.tenant_id == tenant_id
                and event.source_id == source_id
                and event.version_id == version_id
            )

    def _active_for_source(
        self,
        tenant_id: str,
        source_id: str,
    ) -> EnterpriseKnowledgePublication | None:
        return next(
            (
                publication
                for publication in self._items.values()
                if publication.ref.tenant_id == tenant_id
                and publication.ref.source_id == source_id
                and publication.status == "published"
            ),
            None,
        )

    def _append_audit(
        self,
        publication: EnterpriseKnowledgePublication,
        *,
        event_type: str,
        actor_person_id: str,
        occurred_at: datetime,
    ) -> None:
        self._audit.append(
            _audit_event(
                publication,
                event_type=event_type,
                actor_person_id=actor_person_id,
                occurred_at=occurred_at,
            )
        )


class PostgresEnterpriseKnowledgeRepository:
    """PostgreSQL catalog with per-source serialization and append-only audit."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def publish(
        self,
        publication: EnterpriseKnowledgePublication,
    ) -> EnterpriseKnowledgePublication:
        if publication.status != "published":
            raise EnterpriseKnowledgeConflictError("only published state can be added")
        ref = publication.ref
        with self.connection_factory() as connection:
            with connection.transaction():
                _lock_source(connection, ref.tenant_id, ref.source_id)
                existing_row = connection.execute(
                    """
                    SELECT * FROM workflow_enterprise_knowledge_versions
                    WHERE tenant_id = %s AND source_id = %s AND version_id = %s
                    FOR UPDATE
                    """,
                    (ref.tenant_id, ref.source_id, ref.version_id),
                ).fetchone()
                if existing_row is not None:
                    existing = _publication_from_row(existing_row)
                    if existing == publication:
                        return existing
                    raise EnterpriseKnowledgeConflictError(
                        "enterprise knowledge version already exists"
                    )
                active = connection.execute(
                    """
                    SELECT 1 FROM workflow_enterprise_knowledge_versions
                    WHERE tenant_id = %s AND source_id = %s
                      AND status = 'published'
                    """,
                    (ref.tenant_id, ref.source_id),
                ).fetchone()
                if active is not None:
                    raise EnterpriseKnowledgeConflictError(
                        "enterprise knowledge source already has a published version"
                    )
                row = connection.execute(
                    """
                    INSERT INTO workflow_enterprise_knowledge_versions (
                        tenant_id, source_id, version_id, display_label,
                        media_type, size_bytes, content_sha256,
                        data_classification, model_egress_policy,
                        published_by_person_id, published_at, status, revoked_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'published', NULL
                    )
                    RETURNING *
                    """,
                    (
                        ref.tenant_id,
                        ref.source_id,
                        ref.version_id,
                        ref.display_label,
                        ref.media_type,
                        ref.size_bytes,
                        ref.content_sha256,
                        ref.data_classification,
                        ref.egress_decision,
                        publication.published_by_person_id,
                        ref.published_at,
                    ),
                ).fetchone()
                stored = _publication_from_row(row)
                _insert_audit(
                    connection,
                    stored,
                    event_type="enterprise_knowledge.published",
                    actor_person_id=publication.published_by_person_id,
                    occurred_at=ref.published_at,
                )
                return stored

    def revoke(
        self,
        tenant_id: str,
        source_id: str,
        version_id: str,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> EnterpriseKnowledgePublication:
        with self.connection_factory() as connection:
            with connection.transaction():
                _lock_source(connection, tenant_id, source_id)
                row = connection.execute(
                    """
                    SELECT * FROM workflow_enterprise_knowledge_versions
                    WHERE tenant_id = %s AND source_id = %s AND version_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, source_id, version_id),
                ).fetchone()
                if row is None:
                    raise EnterpriseKnowledgeNotFoundError(source_id)
                existing = _publication_from_row(row)
                if existing.status == "revoked":
                    return existing
                updated = connection.execute(
                    """
                    UPDATE workflow_enterprise_knowledge_versions
                    SET status = 'revoked', revoked_at = %s
                    WHERE tenant_id = %s AND source_id = %s AND version_id = %s
                    RETURNING *
                    """,
                    (now, tenant_id, source_id, version_id),
                ).fetchone()
                revoked = _publication_from_row(updated)
                _insert_audit(
                    connection,
                    revoked,
                    event_type="enterprise_knowledge.revoked",
                    actor_person_id=actor_person_id,
                    occurred_at=now,
                )
                return revoked

    def list_published(self, tenant_id: str) -> tuple[EnterpriseKnowledgeRef, ...]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_enterprise_knowledge_versions
                WHERE tenant_id = %s AND status = 'published'
                ORDER BY source_id, version_id
                """,
                (tenant_id,),
            ).fetchall()
        return tuple(_publication_from_row(row).ref for row in rows)

    def list_audit(
        self,
        tenant_id: str,
        source_id: str,
        version_id: str,
    ) -> tuple[EnterpriseKnowledgeAuditEvent, ...]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_enterprise_knowledge_audit
                WHERE tenant_id = %s AND source_id = %s AND version_id = %s
                ORDER BY occurred_at, id
                """,
                (tenant_id, source_id, version_id),
            ).fetchall()
        return tuple(_audit_from_row(row) for row in rows)


def _publication_key(
    publication: EnterpriseKnowledgePublication,
) -> tuple[str, str, str]:
    ref = publication.ref
    return (ref.tenant_id, ref.source_id, ref.version_id)


def _lock_source(connection: Any, tenant_id: str, source_id: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"enterprise-knowledge:{tenant_id}:{source_id}",),
    )


def _publication_from_row(row: Any) -> EnterpriseKnowledgePublication:
    ref = EnterpriseKnowledgeRef(
        tenant_id=row["tenant_id"],
        source_id=row["source_id"],
        version_id=row["version_id"],
        display_label=row["display_label"],
        media_type=row["media_type"],
        size_bytes=int(row["size_bytes"]),
        content_sha256=row["content_sha256"],
        published_at=row["published_at"],
        data_classification=row["data_classification"],
        egress_decision=row["model_egress_policy"],
    )
    return EnterpriseKnowledgePublication(
        ref=ref,
        published_by_person_id=row["published_by_person_id"],
        status=row["status"],
        revoked_at=row["revoked_at"],
    )


def _audit_event(
    publication: EnterpriseKnowledgePublication,
    *,
    event_type: str,
    actor_person_id: str,
    occurred_at: datetime,
) -> EnterpriseKnowledgeAuditEvent:
    ref = publication.ref
    return EnterpriseKnowledgeAuditEvent(
        tenant_id=ref.tenant_id,
        event_id=uuid4().hex,
        source_id=ref.source_id,
        version_id=ref.version_id,
        event_type=event_type,
        actor_person_id=actor_person_id,
        snapshot={
            **ref.snapshot_value(),
            "status": publication.status,
            "revoked_at": (
                publication.revoked_at.isoformat()
                if publication.revoked_at is not None
                else None
            ),
        },
        occurred_at=occurred_at,
    )


def _insert_audit(
    connection: Any,
    publication: EnterpriseKnowledgePublication,
    *,
    event_type: str,
    actor_person_id: str,
    occurred_at: datetime,
) -> None:
    event = _audit_event(
        publication,
        event_type=event_type,
        actor_person_id=actor_person_id,
        occurred_at=occurred_at,
    )
    connection.execute(
        """
        INSERT INTO workflow_enterprise_knowledge_audit (
            tenant_id, id, source_id, version_id, event_type,
            actor_person_id, snapshot, occurred_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event.tenant_id,
            event.event_id,
            event.source_id,
            event.version_id,
            event.event_type,
            event.actor_person_id,
            Jsonb(event.snapshot),
            event.occurred_at,
        ),
    )


def _audit_from_row(row: Any) -> EnterpriseKnowledgeAuditEvent:
    return EnterpriseKnowledgeAuditEvent(
        tenant_id=row["tenant_id"],
        event_id=row["id"],
        source_id=row["source_id"],
        version_id=row["version_id"],
        event_type=row["event_type"],
        actor_person_id=row["actor_person_id"],
        snapshot=dict(row["snapshot"]),
        occurred_at=row["occurred_at"],
    )


__all__ = [
    "EnterpriseKnowledgeAuditEvent",
    "EnterpriseKnowledgeConflictError",
    "EnterpriseKnowledgeNotFoundError",
    "EnterpriseKnowledgeRepository",
    "InMemoryEnterpriseKnowledgeRepository",
    "PostgresEnterpriseKnowledgeRepository",
]
