"""Typed, immutable context authorized for one planning request."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class SourceRef:
    """Stable, non-secret identity for one authorized source."""

    source_id: str
    kind: str
    label: str
    content_sha256: str

    def __post_init__(self) -> None:
        if _SOURCE_ID.fullmatch(self.source_id) is None:
            raise ValueError("context source_id is invalid")
        if self.kind != "attachment":
            raise ValueError("context source kind is unsupported")
        if not self.label.strip():
            raise ValueError("context source label is required")
        if _SHA256.fullmatch(self.content_sha256) is None:
            raise ValueError("context source hash is invalid")


@dataclass(frozen=True)
class AttachmentRef:
    """Safe attachment metadata retained by a workflow snapshot."""

    attachment_id: str
    source_id: str
    display_filename: str
    media_type: str
    size_bytes: int
    content_sha256: str
    data_classification: str = "internal"
    egress_decision: str = "allow"

    def __post_init__(self) -> None:
        if not self.attachment_id.strip():
            raise ValueError("attachment_id is required")
        if _SOURCE_ID.fullmatch(self.source_id) is None:
            raise ValueError("attachment source_id is invalid")
        if not self.display_filename.strip():
            raise ValueError("attachment display filename is required")
        if self.media_type not in {"text/plain", "text/markdown"}:
            raise ValueError("attachment media type is unsupported")
        if self.size_bytes < 1:
            raise ValueError("attachment size must be positive")
        if _SHA256.fullmatch(self.content_sha256) is None:
            raise ValueError("attachment hash is invalid")
        if self.data_classification != "internal":
            raise ValueError("attachment classification must be internal")
        if self.egress_decision not in {"allow", "deny"}:
            raise ValueError("attachment egress decision is invalid")

    def snapshot_value(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "source_id": self.source_id,
            "display_filename": self.display_filename,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "content_sha256": self.content_sha256,
            "data_classification": self.data_classification,
            "egress_decision": self.egress_decision,
        }


@dataclass(frozen=True)
class ContextChunk:
    """One ordered, integrity-bound text chunk. Text is hidden from repr."""

    source_id: str
    order: int
    text: str = field(repr=False)
    text_sha256: str = ""

    def __post_init__(self) -> None:
        if _SOURCE_ID.fullmatch(self.source_id) is None:
            raise ValueError("context chunk source_id is invalid")
        if self.order < 0:
            raise ValueError("context chunk order must be non-negative")
        if not self.text:
            raise ValueError("context chunk text is required")
        actual = sha256_hex(self.text.encode("utf-8"))
        if self.text_sha256 and self.text_sha256 != actual:
            raise ValueError("context chunk hash does not match text")
        object.__setattr__(self, "text_sha256", actual)


@dataclass(frozen=True)
class ContextBundle:
    """Canonical planning context after authorization and egress checks."""

    tenant_id: str
    scope_kind: str
    scope_id: str
    purpose: str
    actor_person_id: str
    sources: tuple[SourceRef, ...]
    attachments: tuple[AttachmentRef, ...]
    chunks: tuple[ContextChunk, ...] = field(repr=False)
    data_classification: str = "internal"
    egress_decision: str = "allow"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        for name in ("tenant_id", "scope_id", "actor_person_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"context bundle {name} is required")
        if self.scope_kind != "console_draft_request":
            raise ValueError("context bundle scope kind is unsupported")
        if self.purpose != "planning":
            raise ValueError("context bundle purpose must be planning")
        if not self.sources or not self.attachments or not self.chunks:
            raise ValueError("context bundle must contain authorized material")
        if self.data_classification != "internal":
            raise ValueError("context bundle classification must be internal")
        if self.egress_decision != "allow":
            raise ValueError("context bundle egress must be allowed")
        created_at = _utc(self.created_at)
        expires_at = _utc(self.expires_at) if self.expires_at is not None else None
        if expires_at is None or expires_at <= created_at:
            raise ValueError("context bundle expiry is invalid")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        expected_sources = tuple(item.source_id for item in self.sources)
        if expected_sources != tuple(item.source_id for item in self.attachments):
            raise ValueError("context bundle source manifest is inconsistent")
        if tuple(chunk.order for chunk in self.chunks) != tuple(range(len(self.chunks))):
            raise ValueError("context bundle chunks must have canonical order")
        if any(chunk.source_id not in expected_sources for chunk in self.chunks):
            raise ValueError("context bundle chunk source is not authorized")
        actual = context_bundle_fingerprint(self)
        if self.fingerprint and self.fingerprint != actual:
            raise ValueError("context bundle fingerprint is invalid")
        object.__setattr__(self, "fingerprint", actual)

    def snapshot_manifest(self) -> dict[str, Any]:
        return {
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "purpose": self.purpose,
            "data_classification": self.data_classification,
            "egress_decision": self.egress_decision,
            "fingerprint": self.fingerprint,
            "attachments": [item.snapshot_value() for item in self.attachments],
        }

    def prompt_sources(self) -> tuple[dict[str, str], ...]:
        labels = {item.source_id: item.label for item in self.sources}
        return tuple(
            {
                "source_id": chunk.source_id,
                "label": labels[chunk.source_id],
                "content": chunk.text,
            }
            for chunk in self.chunks
        )


def context_bundle_fingerprint(bundle: ContextBundle) -> str:
    manifest = {
        "tenant_id": bundle.tenant_id,
        "scope": {"kind": bundle.scope_kind, "id": bundle.scope_id},
        "purpose": bundle.purpose,
        "data_classification": bundle.data_classification,
        "egress_decision": bundle.egress_decision,
        "sources": [
            {
                "source_id": item.source_id,
                "kind": item.kind,
                "content_sha256": item.content_sha256,
            }
            for item in bundle.sources
        ],
        "attachments": [
            {
                "attachment_id": item.attachment_id,
                "source_id": item.source_id,
                "content_sha256": item.content_sha256,
                "data_classification": item.data_classification,
                "egress_decision": item.egress_decision,
            }
            for item in bundle.attachments
        ],
        "chunks": [
            {
                "source_id": item.source_id,
                "order": item.order,
                "text_sha256": item.text_sha256,
            }
            for item in bundle.chunks
        ],
    }
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_hex(canonical)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("context bundle timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "AttachmentRef",
    "ContextBundle",
    "ContextChunk",
    "SourceRef",
    "context_bundle_fingerprint",
    "sha256_hex",
]
