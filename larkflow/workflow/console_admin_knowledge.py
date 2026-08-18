"""Tenant-scoped metadata administration for enterprise knowledge."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from larkflow.knowledge.contracts import (
    EnterpriseKnowledgePublication,
    EnterpriseKnowledgeRef,
)
from larkflow.knowledge.repository import EnterpriseKnowledgeRepository

from .console import ConsolePrincipal, ConsoleResourceNotFoundError
from .console_admin import ConsoleAdminReadService


class ConsoleAdminKnowledgeService:
    """Authorize metadata-only catalog operations through Console admin policy."""

    def __init__(
        self,
        repository: EnterpriseKnowledgeRepository,
        authorizer: ConsoleAdminReadService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.authorizer = authorizer
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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
            "metadata_only": True,
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
        size_bytes: int,
        content_sha256: str,
        egress_decision: str,
    ) -> dict[str, Any]:
        self._authorize(principal)
        if not isinstance(display_label, str) or len(display_label.strip()) > 256:
            raise ValueError("knowledge display_label is invalid")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise ValueError("knowledge size_bytes must be an integer")
        now = self._now()
        publication = EnterpriseKnowledgePublication(
            ref=EnterpriseKnowledgeRef(
                tenant_id=principal.tenant_id,
                source_id=source_id,
                version_id=version_id,
                display_label=display_label.strip(),
                media_type=media_type,
                size_bytes=size_bytes,
                content_sha256=content_sha256,
                published_at=now,
                data_classification="internal",
                egress_decision=egress_decision,
            ),
            published_by_person_id=principal.person_id,
        )
        stored = self.repository.publish(publication)
        return {
            "metadata_only": True,
            "version": self._publication_payload(stored),
        }

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
        return {
            "metadata_only": True,
            "version": self._publication_payload(publication),
        }

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

    @staticmethod
    def _publication_payload(
        publication: EnterpriseKnowledgePublication,
    ) -> dict[str, Any]:
        return {
            **publication.ref.snapshot_value(),
            "status": publication.status,
            "revoked_at": (
                publication.revoked_at.isoformat()
                if publication.revoked_at is not None
                else None
            ),
        }


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
        "status",
        "revoked_at",
    }
    return {key: snapshot[key] for key in sorted(allowed & set(snapshot))}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("knowledge admin clock must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = ["ConsoleAdminKnowledgeService"]
