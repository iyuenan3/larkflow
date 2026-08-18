"""Tenant-bound repositories for enterprise knowledge publication metadata."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from psycopg.types.json import Jsonb

from .contracts import (
    EnterpriseKnowledgeAuthorizationProof,
    EnterpriseKnowledgePublication,
    EnterpriseKnowledgeRef,
)


DEFAULT_MAX_RETAINED_VERSIONS_PER_TENANT = 500
DEFAULT_MAX_RETAINED_BYTES_PER_TENANT = 50 * 1024 * 1024


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

    def list_versions(
        self,
        tenant_id: str,
        *,
        limit: int,
    ) -> tuple[EnterpriseKnowledgePublication, ...]:
        ...

    def list_audit(
        self,
        tenant_id: str,
        source_id: str,
        version_id: str,
    ) -> tuple[EnterpriseKnowledgeAuditEvent, ...]:
        ...

    def get_version(
        self,
        tenant_id: str,
        source_id: str,
        version_id: str,
    ) -> EnterpriseKnowledgePublication:
        ...

    def retained_usage(self, tenant_id: str) -> tuple[int, int]:
        ...


class InMemoryEnterpriseKnowledgeRepository:
    def __init__(
        self,
        *,
        max_retained_versions: int = DEFAULT_MAX_RETAINED_VERSIONS_PER_TENANT,
        max_retained_bytes: int = DEFAULT_MAX_RETAINED_BYTES_PER_TENANT,
    ) -> None:
        _validate_quotas(max_retained_versions, max_retained_bytes)
        self._items: dict[
            tuple[str, str, str], EnterpriseKnowledgePublication
        ] = {}
        self._audit: list[EnterpriseKnowledgeAuditEvent] = []
        self._lock = RLock()
        self.max_retained_versions = max_retained_versions
        self.max_retained_bytes = max_retained_bytes

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
            retained_count, retained_bytes = self.retained_usage(
                publication.ref.tenant_id
            )
            if retained_count + 1 > self.max_retained_versions:
                raise EnterpriseKnowledgeConflictError(
                    "enterprise knowledge retained version quota exceeded"
                )
            if retained_bytes + publication.ref.size_bytes > self.max_retained_bytes:
                raise EnterpriseKnowledgeConflictError(
                    "enterprise knowledge retained byte quota exceeded"
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
                authorization_proof=existing.authorization_proof,
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

    def list_versions(
        self,
        tenant_id: str,
        *,
        limit: int,
    ) -> tuple[EnterpriseKnowledgePublication, ...]:
        _validate_limit(limit)
        with self._lock:
            items = sorted(
                (
                    publication
                    for publication in self._items.values()
                    if publication.ref.tenant_id == tenant_id
                ),
                key=lambda publication: (
                    publication.ref.published_at,
                    publication.ref.source_id,
                    publication.ref.version_id,
                ),
                reverse=True,
            )
            return tuple(items[:limit])

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

    def get_version(
        self,
        tenant_id: str,
        source_id: str,
        version_id: str,
    ) -> EnterpriseKnowledgePublication:
        with self._lock:
            item = self._items.get((tenant_id, source_id, version_id))
            if item is None:
                raise EnterpriseKnowledgeNotFoundError(source_id)
            return item

    def retained_usage(self, tenant_id: str) -> tuple[int, int]:
        with self._lock:
            items = [
                item
                for item in self._items.values()
                if item.ref.tenant_id == tenant_id
            ]
            return len(items), sum(item.ref.size_bytes for item in items)

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

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        max_retained_versions: int = DEFAULT_MAX_RETAINED_VERSIONS_PER_TENANT,
        max_retained_bytes: int = DEFAULT_MAX_RETAINED_BYTES_PER_TENANT,
    ) -> None:
        _validate_quotas(max_retained_versions, max_retained_bytes)
        self.connection_factory = connection_factory
        self.max_retained_versions = max_retained_versions
        self.max_retained_bytes = max_retained_bytes

    def publish(
        self,
        publication: EnterpriseKnowledgePublication,
    ) -> EnterpriseKnowledgePublication:
        if publication.status != "published":
            raise EnterpriseKnowledgeConflictError("only published state can be added")
        ref = publication.ref
        with self.connection_factory() as connection:
            with connection.transaction():
                _lock_tenant(connection, ref.tenant_id)
                _lock_source(connection, ref.tenant_id, ref.source_id)
                existing_row = connection.execute(
                    """
                    SELECT v.*, a.proof_id, a.authorization_scope,
                           a.policy_version, a.statement_sha256,
                           a.authorized_by_person_id, a.authorized_at,
                           a.proof_fingerprint
                    FROM workflow_enterprise_knowledge_versions AS v
                    LEFT JOIN workflow_enterprise_knowledge_authorizations AS a
                      ON a.tenant_id = v.tenant_id
                     AND a.source_id = v.source_id
                     AND a.version_id = v.version_id
                    WHERE v.tenant_id = %s AND v.source_id = %s
                      AND v.version_id = %s
                    FOR UPDATE OF v
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
                retained = connection.execute(
                    """
                    SELECT count(*) AS item_count,
                           COALESCE(sum(size_bytes), 0) AS total_bytes
                    FROM workflow_enterprise_knowledge_versions
                    WHERE tenant_id = %s
                    """,
                    (ref.tenant_id,),
                ).fetchone()
                if int(retained["item_count"]) + 1 > self.max_retained_versions:
                    raise EnterpriseKnowledgeConflictError(
                        "enterprise knowledge retained version quota exceeded"
                    )
                if (
                    int(retained["total_bytes"]) + ref.size_bytes
                    > self.max_retained_bytes
                ):
                    raise EnterpriseKnowledgeConflictError(
                        "enterprise knowledge retained byte quota exceeded"
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
                if publication.authorization_proof is not None:
                    _insert_authorization(
                        connection,
                        publication.authorization_proof,
                    )
                stored = _publication_from_row(
                    row,
                    proof=publication.authorization_proof,
                )
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
                    SELECT v.*, a.proof_id, a.authorization_scope,
                           a.policy_version, a.statement_sha256,
                           a.authorized_by_person_id, a.authorized_at,
                           a.proof_fingerprint
                    FROM workflow_enterprise_knowledge_versions AS v
                    LEFT JOIN workflow_enterprise_knowledge_authorizations AS a
                      ON a.tenant_id = v.tenant_id
                     AND a.source_id = v.source_id
                     AND a.version_id = v.version_id
                    WHERE v.tenant_id = %s AND v.source_id = %s
                      AND v.version_id = %s
                    FOR UPDATE OF v
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
                revoked = _publication_from_row(
                    updated,
                    proof=existing.authorization_proof,
                )
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
                SELECT v.*, a.proof_id, a.authorization_scope,
                       a.policy_version, a.statement_sha256,
                       a.authorized_by_person_id, a.authorized_at,
                       a.proof_fingerprint
                FROM workflow_enterprise_knowledge_versions AS v
                LEFT JOIN workflow_enterprise_knowledge_authorizations AS a
                  ON a.tenant_id = v.tenant_id
                 AND a.source_id = v.source_id
                 AND a.version_id = v.version_id
                WHERE v.tenant_id = %s AND v.status = 'published'
                ORDER BY v.source_id, v.version_id
                """,
                (tenant_id,),
            ).fetchall()
        return tuple(_publication_from_row(row).ref for row in rows)

    def list_versions(
        self,
        tenant_id: str,
        *,
        limit: int,
    ) -> tuple[EnterpriseKnowledgePublication, ...]:
        _validate_limit(limit)
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT v.*, a.proof_id, a.authorization_scope,
                       a.policy_version, a.statement_sha256,
                       a.authorized_by_person_id, a.authorized_at,
                       a.proof_fingerprint
                FROM workflow_enterprise_knowledge_versions AS v
                LEFT JOIN workflow_enterprise_knowledge_authorizations AS a
                  ON a.tenant_id = v.tenant_id
                 AND a.source_id = v.source_id
                 AND a.version_id = v.version_id
                WHERE v.tenant_id = %s
                ORDER BY v.published_at DESC, v.source_id DESC, v.version_id DESC
                LIMIT %s
                """,
                (tenant_id, limit),
            ).fetchall()
        return tuple(_publication_from_row(row) for row in rows)

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

    def get_version(
        self,
        tenant_id: str,
        source_id: str,
        version_id: str,
    ) -> EnterpriseKnowledgePublication:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT v.*, a.proof_id, a.authorization_scope,
                       a.policy_version, a.statement_sha256,
                       a.authorized_by_person_id, a.authorized_at,
                       a.proof_fingerprint
                FROM workflow_enterprise_knowledge_versions AS v
                LEFT JOIN workflow_enterprise_knowledge_authorizations AS a
                  ON a.tenant_id = v.tenant_id
                 AND a.source_id = v.source_id
                 AND a.version_id = v.version_id
                WHERE v.tenant_id = %s AND v.source_id = %s
                  AND v.version_id = %s
                """,
                (tenant_id, source_id, version_id),
            ).fetchone()
        if row is None:
            raise EnterpriseKnowledgeNotFoundError(source_id)
        return _publication_from_row(row)

    def retained_usage(self, tenant_id: str) -> tuple[int, int]:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT count(*) AS item_count,
                       COALESCE(sum(size_bytes), 0) AS total_bytes
                FROM workflow_enterprise_knowledge_versions
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            ).fetchone()
        return int(row["item_count"]), int(row["total_bytes"])


def _publication_key(
    publication: EnterpriseKnowledgePublication,
) -> tuple[str, str, str]:
    ref = publication.ref
    return (ref.tenant_id, ref.source_id, ref.version_id)


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or limit < 1 or limit > 1_000:
        raise ValueError("enterprise knowledge version limit is invalid")


def _validate_quotas(max_retained_versions: int, max_retained_bytes: int) -> None:
    if (
        isinstance(max_retained_versions, bool)
        or max_retained_versions < 1
        or isinstance(max_retained_bytes, bool)
        or max_retained_bytes < 1
    ):
        raise ValueError("enterprise knowledge retained quota is invalid")


def _lock_tenant(connection: Any, tenant_id: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"enterprise-knowledge-tenant:{tenant_id}",),
    )


def _lock_source(connection: Any, tenant_id: str, source_id: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"enterprise-knowledge:{tenant_id}:{source_id}",),
    )


def _proof_from_row(row: Any) -> EnterpriseKnowledgeAuthorizationProof | None:
    proof_id = row.get("proof_id")
    if proof_id is None:
        return None
    return EnterpriseKnowledgeAuthorizationProof(
        tenant_id=row["tenant_id"],
        source_id=row["source_id"],
        version_id=row["version_id"],
        content_sha256=row["content_sha256"],
        authorized_by_person_id=row["authorized_by_person_id"],
        authorized_at=row["authorized_at"],
        scope=row["authorization_scope"],
        policy_version=row["policy_version"],
        statement_sha256=row["statement_sha256"],
        proof_id=proof_id,
        fingerprint=row["proof_fingerprint"],
    )


def _publication_from_row(
    row: Any,
    *,
    proof: EnterpriseKnowledgeAuthorizationProof | None = None,
) -> EnterpriseKnowledgePublication:
    if proof is None:
        proof = _proof_from_row(row)
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
        authorization_proof_id=(proof.proof_id if proof is not None else None),
        authorization_fingerprint=(
            proof.fingerprint if proof is not None else None
        ),
    )
    return EnterpriseKnowledgePublication(
        ref=ref,
        published_by_person_id=row["published_by_person_id"],
        status=row["status"],
        revoked_at=row["revoked_at"],
        authorization_proof=proof,
    )


def _insert_authorization(
    connection: Any,
    proof: EnterpriseKnowledgeAuthorizationProof,
) -> None:
    connection.execute(
        """
        INSERT INTO workflow_enterprise_knowledge_authorizations (
            tenant_id, proof_id, source_id, version_id, content_sha256,
            authorization_scope, policy_version, statement_sha256,
            authorized_by_person_id, authorized_at, proof_fingerprint
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            proof.tenant_id,
            proof.proof_id,
            proof.source_id,
            proof.version_id,
            proof.content_sha256,
            proof.scope,
            proof.policy_version,
            proof.statement_sha256,
            proof.authorized_by_person_id,
            proof.authorized_at,
            proof.fingerprint,
        ),
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
            **(
                {"authorization_proof": publication.authorization_proof.safe_value()}
                if publication.authorization_proof is not None
                else {}
            ),
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
    "DEFAULT_MAX_RETAINED_BYTES_PER_TENANT",
    "DEFAULT_MAX_RETAINED_VERSIONS_PER_TENANT",
    "EnterpriseKnowledgeAuditEvent",
    "EnterpriseKnowledgeConflictError",
    "EnterpriseKnowledgeNotFoundError",
    "EnterpriseKnowledgeRepository",
    "InMemoryEnterpriseKnowledgeRepository",
    "PostgresEnterpriseKnowledgeRepository",
]
