"""Immutable contracts for a tenant-wide enterprise knowledge catalog."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^enterprise:[a-z][a-z0-9_.:-]{0,116}$")
_VERSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MEDIA_TYPES = frozenset({"text/plain", "text/markdown"})


@dataclass(frozen=True)
class EnterpriseKnowledgeRef:
    """Runtime-safe identity of one explicitly published source version."""

    tenant_id: str
    source_id: str
    version_id: str
    display_label: str
    media_type: str
    size_bytes: int
    content_sha256: str
    published_at: datetime
    data_classification: str = "internal"
    egress_decision: str = "deny"

    def __post_init__(self) -> None:
        _require_text(self.tenant_id, "enterprise knowledge tenant_id")
        if _SOURCE_ID.fullmatch(self.source_id) is None:
            raise ValueError("enterprise knowledge source_id is invalid")
        if _VERSION_ID.fullmatch(self.version_id) is None:
            raise ValueError("enterprise knowledge version_id is invalid")
        _require_text(self.display_label, "enterprise knowledge display label")
        if self.media_type not in _MEDIA_TYPES:
            raise ValueError("enterprise knowledge media type is unsupported")
        if self.size_bytes < 1:
            raise ValueError("enterprise knowledge size must be positive")
        if _SHA256.fullmatch(self.content_sha256) is None:
            raise ValueError("enterprise knowledge hash is invalid")
        if self.data_classification != "internal":
            raise ValueError("enterprise knowledge classification must be internal")
        if self.egress_decision not in {"allow", "deny"}:
            raise ValueError("enterprise knowledge egress decision is invalid")
        object.__setattr__(self, "published_at", _utc(self.published_at))

    def snapshot_value(self) -> dict[str, Any]:
        """Return the safe manifest value allowed outside the catalog."""
        return {
            "source_id": self.source_id,
            "version_id": self.version_id,
            "display_label": self.display_label,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "content_sha256": self.content_sha256,
            "published_at": self.published_at.isoformat(),
            "data_classification": self.data_classification,
            "egress_decision": self.egress_decision,
        }


@dataclass(frozen=True)
class EnterpriseKnowledgePublication:
    """Catalog-side publication state; never passed to a Planner or Runtime."""

    ref: EnterpriseKnowledgeRef
    published_by_person_id: str = field(repr=False)
    status: str = "published"
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(
            self.published_by_person_id,
            "enterprise knowledge publisher",
        )
        if self.status not in {"published", "revoked"}:
            raise ValueError("enterprise knowledge publication status is invalid")
        revoked_at = _utc(self.revoked_at) if self.revoked_at is not None else None
        if self.status == "published" and revoked_at is not None:
            raise ValueError("published enterprise knowledge cannot be revoked")
        if self.status == "revoked":
            if revoked_at is None:
                raise ValueError("revoked enterprise knowledge needs revoked_at")
            if revoked_at < self.ref.published_at:
                raise ValueError("enterprise knowledge revocation precedes publication")
        object.__setattr__(self, "revoked_at", revoked_at)

    def authorized_ref(self) -> EnterpriseKnowledgeRef:
        """Return the safe ref only while this exact version is published."""
        if self.status != "published":
            raise ValueError("enterprise knowledge publication is revoked")
        return self.ref


@dataclass(frozen=True)
class EnterpriseKnowledgeSelection:
    """Canonical tenant-bound selection frozen before content retrieval."""

    tenant_id: str
    sources: tuple[EnterpriseKnowledgeRef, ...]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fingerprint: str = ""

    def __post_init__(self) -> None:
        _require_text(self.tenant_id, "enterprise knowledge selection tenant_id")
        if not self.sources:
            raise ValueError("enterprise knowledge selection is empty")
        if any(source.tenant_id != self.tenant_id for source in self.sources):
            raise ValueError("enterprise knowledge selection crosses tenants")
        source_ids = tuple(source.source_id for source in self.sources)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("enterprise knowledge selection repeats a source")
        canonical_sources = tuple(
            sorted(self.sources, key=lambda item: (item.source_id, item.version_id))
        )
        object.__setattr__(self, "sources", canonical_sources)
        object.__setattr__(self, "created_at", _utc(self.created_at))
        actual = enterprise_knowledge_fingerprint(self)
        if self.fingerprint and self.fingerprint != actual:
            raise ValueError("enterprise knowledge selection fingerprint is invalid")
        object.__setattr__(self, "fingerprint", actual)

    def snapshot_manifest(self) -> dict[str, Any]:
        return {
            "kind": "enterprise_shared",
            "tenant_id": self.tenant_id,
            "fingerprint": self.fingerprint,
            "sources": [source.snapshot_value() for source in self.sources],
        }


def enterprise_knowledge_fingerprint(
    selection: EnterpriseKnowledgeSelection,
) -> str:
    """Hash the tenant, exact immutable versions, policy, and content identity."""
    manifest = {
        "tenant_id": selection.tenant_id,
        "kind": "enterprise_shared",
        "sources": [
            {
                "source_id": source.source_id,
                "version_id": source.version_id,
                "content_sha256": source.content_sha256,
                "data_classification": source.data_classification,
                "egress_decision": source.egress_decision,
            }
            for source in selection.sources
        ],
    }
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("enterprise knowledge timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "EnterpriseKnowledgePublication",
    "EnterpriseKnowledgeRef",
    "EnterpriseKnowledgeSelection",
    "enterprise_knowledge_fingerprint",
]
