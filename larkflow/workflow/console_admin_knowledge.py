"""Tenant-scoped administration for immutable enterprise knowledge content."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import secrets
from typing import Any

from larkflow.knowledge.blob import (
    EnterpriseKnowledgeBlobStore,
    EnterpriseKnowledgeBlobUnavailableError,
    enterprise_knowledge_object_key,
)
from larkflow.knowledge.contracts import (
    ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1,
    ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1,
    EnterpriseKnowledgeAuthorizationProof,
    EnterpriseKnowledgePublication,
    EnterpriseKnowledgeRef,
)
from larkflow.knowledge.repository import (
    EnterpriseKnowledgeConflictError,
    EnterpriseKnowledgeNotFoundError,
    EnterpriseKnowledgeRepository,
)

from .console import ConsolePrincipal, ConsoleResourceNotFoundError
from .console_admin import ConsoleAdminReadService


MAX_ENTERPRISE_KNOWLEDGE_CONTENT_BYTES = 131_072


class EnterpriseKnowledgeContentUnavailableError(RuntimeError):
    """The content store is disabled or temporarily unavailable."""


class ConsoleAdminKnowledgeService:
    """Publish and govern tenant-wide content through server-owned policy."""

    def __init__(
        self,
        repository: EnterpriseKnowledgeRepository,
        authorizer: ConsoleAdminReadService,
        blob_store: EnterpriseKnowledgeBlobStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.authorizer = authorizer
        self.blob_store = blob_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def content_publication_enabled(self) -> bool:
        return self.blob_store is not None

    def list_versions(
        self,
        principal: ConsolePrincipal,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._authorize(principal)
        if isinstance(limit, bool) or limit < 1 or limit > 100:
            raise ValueError("knowledge version limit must be between 1 and 100")
        items = self.repository.list_versions(principal.tenant_id, limit=limit)
        return {
            "scope": "current_tenant",
            "versions": [self._publication_payload(item) for item in items],
            "total": len(items),
            "limit": limit,
        }

    def publish(
        self,
        principal: ConsolePrincipal,
        *,
        source_id: str,
        version_id: str,
        display_label: str,
        media_type: str,
        content: str,
        content_sha256: str,
        egress_decision: str,
        authorization_statement: str,
        authorization_policy_version: str,
    ) -> dict[str, Any]:
        self._authorize(principal)
        blob_store = self.blob_store
        if blob_store is None:
            raise EnterpriseKnowledgeContentUnavailableError(
                "enterprise knowledge content publication is unavailable"
            )
        if not isinstance(display_label, str) or not display_label.strip():
            raise ValueError("knowledge display_label is invalid")
        if len(display_label.strip()) > 200:
            raise ValueError("knowledge display_label is invalid")
        if not isinstance(content, str) or not content:
            raise ValueError("knowledge content must be non-empty UTF-8 text")
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_ENTERPRISE_KNOWLEDGE_CONTENT_BYTES:
            raise ValueError("knowledge content exceeds the byte limit")
        actual_hash = hashlib.sha256(content_bytes).hexdigest()
        if (
            not isinstance(content_sha256, str)
            or not secrets.compare_digest(content_sha256, actual_hash)
        ):
            raise ValueError("knowledge content hash does not match the body")
        if not isinstance(authorization_statement, str) or not secrets.compare_digest(
            authorization_statement,
            ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1,
        ):
            raise ValueError("knowledge tenant-wide authorization statement is invalid")
        if (
            not isinstance(authorization_policy_version, str)
            or not secrets.compare_digest(
                authorization_policy_version,
                ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1,
            )
        ):
            raise ValueError("knowledge authorization policy version is invalid")

        now = self._now()
        proof = EnterpriseKnowledgeAuthorizationProof(
            tenant_id=principal.tenant_id,
            source_id=source_id,
            version_id=version_id,
            content_sha256=actual_hash,
            authorized_by_person_id=principal.person_id,
            authorized_at=now,
        )
        publication = EnterpriseKnowledgePublication(
            ref=EnterpriseKnowledgeRef(
                tenant_id=principal.tenant_id,
                source_id=source_id,
                version_id=version_id,
                display_label=display_label.strip(),
                media_type=media_type,
                size_bytes=len(content_bytes),
                content_sha256=actual_hash,
                published_at=now,
                data_classification="internal",
                egress_decision=egress_decision,
                authorization_proof_id=proof.proof_id,
                authorization_fingerprint=proof.fingerprint,
            ),
            published_by_person_id=principal.person_id,
            authorization_proof=proof,
        )
        object_key = enterprise_knowledge_object_key(
            tenant_id=principal.tenant_id,
            source_id=source_id,
            version_id=version_id,
            content_sha256=actual_hash,
        )
        try:
            created = blob_store.put_if_absent(object_key, content_bytes)
        except EnterpriseKnowledgeBlobUnavailableError as exc:
            raise EnterpriseKnowledgeContentUnavailableError(
                "enterprise knowledge content storage is unavailable"
            ) from exc

        try:
            stored = self.repository.publish(publication)
        except EnterpriseKnowledgeConflictError:
            existing = self._matching_existing(publication)
            if existing is not None:
                stored = existing
            else:
                if created:
                    self._delete_orphan(blob_store, object_key)
                raise
        except Exception:
            if created:
                self._delete_orphan(blob_store, object_key)
            raise
        return {"version": self._publication_payload(stored)}

    def revoke(
        self,
        principal: ConsolePrincipal,
        source_id: str,
        version_id: str,
    ) -> dict[str, Any]:
        self._authorize(principal)
        publication = self.repository.revoke(
            principal.tenant_id,
            source_id,
            version_id,
            actor_person_id=principal.person_id,
            now=self._now(),
        )
        return {"version": self._publication_payload(publication)}

    def audit(
        self,
        principal: ConsolePrincipal,
        source_id: str,
        version_id: str,
        *,
        limit: int = 20,
    ) -> dict[str, Any]:
        self._authorize(principal)
        if isinstance(limit, bool) or limit < 1 or limit > 100:
            raise ValueError("knowledge audit limit must be between 1 and 100")
        events = self.repository.list_audit(
            principal.tenant_id,
            source_id,
            version_id,
        )
        if not events:
            raise ConsoleResourceNotFoundError("enterprise knowledge version")
        selected = events[-limit:]
        return {
            "scope": "current_tenant",
            "source_id": source_id,
            "version_id": version_id,
            "events": [
                {
                    "id": event.event_id,
                    "event_type": event.event_type,
                    "actor_relation": (
                        "you"
                        if event.actor_person_id == principal.person_id
                        else "member"
                    ),
                    "occurred_at": _utc(event.occurred_at).isoformat(),
                    "snapshot": _safe_snapshot(event.snapshot),
                }
                for event in selected
            ],
            "total": len(selected),
            "limit": limit,
        }

    def _authorize(self, principal: ConsolePrincipal) -> None:
        if not self.authorizer.is_admin(principal):
            raise ConsoleResourceNotFoundError("enterprise knowledge catalog")

    def _now(self) -> datetime:
        return _utc(self._clock())

    def _matching_existing(
        self,
        candidate: EnterpriseKnowledgePublication,
    ) -> EnterpriseKnowledgePublication | None:
        try:
            existing = self.repository.get_version(
                candidate.ref.tenant_id,
                candidate.ref.source_id,
                candidate.ref.version_id,
            )
        except EnterpriseKnowledgeNotFoundError:
            return None
        if existing.status != "published" or existing.authorization_proof is None:
            return None
        comparable = (
            "display_label",
            "media_type",
            "size_bytes",
            "content_sha256",
            "data_classification",
            "egress_decision",
        )
        if any(
            getattr(existing.ref, field) != getattr(candidate.ref, field)
            for field in comparable
        ):
            return None
        if existing.published_by_person_id != candidate.published_by_person_id:
            return None
        return existing

    @staticmethod
    def _delete_orphan(
        blob_store: EnterpriseKnowledgeBlobStore,
        object_key: str,
    ) -> None:
        try:
            blob_store.delete(object_key)
        except (FileNotFoundError, EnterpriseKnowledgeBlobUnavailableError):
            pass

    @staticmethod
    def _publication_payload(
        publication: EnterpriseKnowledgePublication,
    ) -> dict[str, Any]:
        value = {
            **publication.ref.snapshot_value(),
            "content_available": publication.authorization_proof is not None,
            "status": publication.status,
            "revoked_at": (
                publication.revoked_at.isoformat()
                if publication.revoked_at is not None
                else None
            ),
        }
        if publication.authorization_proof is not None:
            value["authorization"] = publication.authorization_proof.safe_value()
        return value


def _safe_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "source_id",
        "version_id",
        "display_label",
        "media_type",
        "size_bytes",
        "content_sha256",
        "published_at",
        "data_classification",
        "egress_decision",
        "authorization_proof_id",
        "authorization_fingerprint",
        "authorization_proof",
        "status",
        "revoked_at",
    }
    return {key: snapshot[key] for key in sorted(allowed & set(snapshot))}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("knowledge admin clock must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "ConsoleAdminKnowledgeService",
    "EnterpriseKnowledgeContentUnavailableError",
    "MAX_ENTERPRISE_KNOWLEDGE_CONTENT_BYTES",
]
