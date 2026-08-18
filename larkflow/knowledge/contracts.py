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
_PROOF_ID = re.compile(r"^kp_[0-9a-f]{32}$")
ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1 = "tenant_all_members_v1"
ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1 = (
    "I confirm that this immutable content snapshot may be used by all "
    "members of the current tenant in larkflow."
)


@dataclass(frozen=True)
class EnterpriseKnowledgeAuthorizationProof:
    """Server-owned proof of one administrator's tenant-wide authorization."""

    tenant_id: str
    source_id: str
    version_id: str
    content_sha256: str
    authorized_by_person_id: str = field(repr=False)
    authorized_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    scope: str = "tenant_all_members"
    policy_version: str = ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1
    statement_sha256: str = ""
    proof_id: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        _require_text(self.tenant_id, "enterprise knowledge proof tenant_id")
        if _SOURCE_ID.fullmatch(self.source_id) is None:
            raise ValueError("enterprise knowledge proof source_id is invalid")
        if _VERSION_ID.fullmatch(self.version_id) is None:
            raise ValueError("enterprise knowledge proof version_id is invalid")
        if _SHA256.fullmatch(self.content_sha256) is None:
            raise ValueError("enterprise knowledge proof content hash is invalid")
        _require_text(
            self.authorized_by_person_id,
            "enterprise knowledge proof administrator",
        )
        if self.scope != "tenant_all_members":
            raise ValueError("enterprise knowledge proof scope is invalid")
        if self.policy_version != ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1:
            raise ValueError("enterprise knowledge proof policy is invalid")
        expected_statement = hashlib.sha256(
            ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1.encode("utf-8")
        ).hexdigest()
        if self.statement_sha256 and self.statement_sha256 != expected_statement:
            raise ValueError("enterprise knowledge proof statement is invalid")
        object.__setattr__(self, "statement_sha256", expected_statement)
        object.__setattr__(self, "authorized_at", _utc(self.authorized_at))
        actual = enterprise_knowledge_authorization_fingerprint(self)
        if self.fingerprint and self.fingerprint != actual:
            raise ValueError("enterprise knowledge proof fingerprint is invalid")
        object.__setattr__(self, "fingerprint", actual)
        proof_id = f"kp_{actual[:32]}"
        if self.proof_id and self.proof_id != proof_id:
            raise ValueError("enterprise knowledge proof id is invalid")
        object.__setattr__(self, "proof_id", proof_id)

    def safe_value(self) -> dict[str, Any]:
        """Return proof evidence without administrator identity or raw statement."""

        return {
            "proof_id": self.proof_id,
            "fingerprint": self.fingerprint,
            "scope": self.scope,
            "policy_version": self.policy_version,
            "statement_sha256": self.statement_sha256,
            "authorized_at": self.authorized_at.isoformat(),
        }


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
    authorization_proof_id: str | None = None
    authorization_fingerprint: str | None = None

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
        proof_values = (
            self.authorization_proof_id,
            self.authorization_fingerprint,
        )
        if any(value is None for value in proof_values) != all(
            value is None for value in proof_values
        ):
            raise ValueError("enterprise knowledge authorization proof is incomplete")
        if self.authorization_proof_id is not None:
            if _PROOF_ID.fullmatch(self.authorization_proof_id) is None:
                raise ValueError("enterprise knowledge authorization proof id is invalid")
            if _SHA256.fullmatch(self.authorization_fingerprint or "") is None:
                raise ValueError(
                    "enterprise knowledge authorization fingerprint is invalid"
                )
        object.__setattr__(self, "published_at", _utc(self.published_at))

    @property
    def content_authorized(self) -> bool:
        return self.authorization_proof_id is not None

    def snapshot_value(self) -> dict[str, Any]:
        """Return the safe manifest value allowed outside the catalog."""
        value = {
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
        if self.authorization_proof_id is not None:
            value["authorization_proof_id"] = self.authorization_proof_id
            value["authorization_fingerprint"] = self.authorization_fingerprint
        return value


@dataclass(frozen=True)
class EnterpriseKnowledgePublication:
    """Catalog-side publication state; never passed to a Planner or Runtime."""

    ref: EnterpriseKnowledgeRef
    published_by_person_id: str = field(repr=False)
    status: str = "published"
    revoked_at: datetime | None = None
    authorization_proof: EnterpriseKnowledgeAuthorizationProof | None = field(
        default=None,
        repr=False,
    )

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
        proof = self.authorization_proof
        if proof is None:
            if self.ref.content_authorized:
                raise ValueError("enterprise knowledge proof body is missing")
        else:
            if (
                proof.tenant_id != self.ref.tenant_id
                or proof.source_id != self.ref.source_id
                or proof.version_id != self.ref.version_id
                or proof.content_sha256 != self.ref.content_sha256
                or proof.proof_id != self.ref.authorization_proof_id
                or proof.fingerprint != self.ref.authorization_fingerprint
            ):
                raise ValueError("enterprise knowledge proof binding is invalid")

    def authorized_ref(self) -> EnterpriseKnowledgeRef:
        """Return the safe ref only while this exact version is published."""
        if self.status != "published":
            raise ValueError("enterprise knowledge publication is revoked")
        if self.authorization_proof is None:
            raise ValueError("enterprise knowledge publication lacks authorization proof")
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
                "authorization_proof_id": source.authorization_proof_id,
                "authorization_fingerprint": source.authorization_fingerprint,
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


def enterprise_knowledge_authorization_fingerprint(
    proof: EnterpriseKnowledgeAuthorizationProof,
) -> str:
    """Bind authorization to tenant, actor, exact content, scope, and time."""

    canonical = json.dumps(
        {
            "tenant_id": proof.tenant_id,
            "source_id": proof.source_id,
            "version_id": proof.version_id,
            "content_sha256": proof.content_sha256,
            "authorized_by_person_id": proof.authorized_by_person_id,
            "authorized_at": proof.authorized_at.isoformat(),
            "scope": proof.scope,
            "policy_version": proof.policy_version,
            "statement_sha256": proof.statement_sha256,
        },
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
    "ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1",
    "ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1",
    "EnterpriseKnowledgeAuthorizationProof",
    "EnterpriseKnowledgePublication",
    "EnterpriseKnowledgeRef",
    "EnterpriseKnowledgeSelection",
    "enterprise_knowledge_authorization_fingerprint",
    "enterprise_knowledge_fingerprint",
]
