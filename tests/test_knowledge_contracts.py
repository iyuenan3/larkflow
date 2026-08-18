from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from larkflow.knowledge import (
    EnterpriseKnowledgePublication,
    EnterpriseKnowledgeRef,
    EnterpriseKnowledgeSelection,
)


NOW = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)


def _ref(
    source_id: str = "enterprise:release_policy",
    *,
    tenant_id: str = "tenant-a",
    version_id: str = "v1",
    content_sha256: str = "a" * 64,
    egress_decision: str = "deny",
) -> EnterpriseKnowledgeRef:
    return EnterpriseKnowledgeRef(
        tenant_id=tenant_id,
        source_id=source_id,
        version_id=version_id,
        display_label="发布流程规范",
        media_type="text/markdown",
        size_bytes=128,
        content_sha256=content_sha256,
        published_at=NOW,
        egress_decision=egress_decision,
    )


def test_publication_returns_runtime_safe_ref_while_published() -> None:
    publication = EnterpriseKnowledgePublication(
        ref=_ref(),
        published_by_person_id="admin-person",
    )

    authorized = publication.authorized_ref()

    assert authorized.source_id == "enterprise:release_policy"
    assert "admin-person" not in repr(publication)
    manifest = authorized.snapshot_value()
    assert "published_by_person_id" not in manifest
    assert "object_key" not in manifest
    assert "content" not in manifest


def test_revoked_publication_cannot_authorize_new_context() -> None:
    publication = EnterpriseKnowledgePublication(
        ref=_ref(),
        published_by_person_id="admin-person",
        status="revoked",
        revoked_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="revoked"):
        publication.authorized_ref()


@pytest.mark.parametrize(
    ("status", "revoked_at", "message"),
    [
        ("published", NOW + timedelta(minutes=1), "cannot be revoked"),
        ("revoked", None, "needs revoked_at"),
        ("revoked", NOW - timedelta(minutes=1), "precedes publication"),
    ],
)
def test_publication_state_is_fail_closed(
    status: str,
    revoked_at: datetime | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        EnterpriseKnowledgePublication(
            ref=_ref(),
            published_by_person_id="admin-person",
            status=status,
            revoked_at=revoked_at,
        )


def test_selection_is_canonical_and_fingerprint_is_order_independent() -> None:
    first = _ref("enterprise:alpha", content_sha256="a" * 64)
    second = _ref("enterprise:beta", content_sha256="b" * 64)

    left = EnterpriseKnowledgeSelection(
        tenant_id="tenant-a",
        sources=(second, first),
        created_at=NOW,
    )
    right = EnterpriseKnowledgeSelection(
        tenant_id="tenant-a",
        sources=(first, second),
        created_at=NOW + timedelta(hours=1),
    )

    assert tuple(item.source_id for item in left.sources) == (
        "enterprise:alpha",
        "enterprise:beta",
    )
    assert left.fingerprint == right.fingerprint
    assert left.snapshot_manifest()["kind"] == "enterprise_shared"


def test_selection_fingerprint_binds_version_hash_and_egress() -> None:
    baseline = EnterpriseKnowledgeSelection("tenant-a", (_ref(),), NOW)
    version_changed = EnterpriseKnowledgeSelection(
        "tenant-a",
        (_ref(version_id="v2"),),
        NOW,
    )
    hash_changed = EnterpriseKnowledgeSelection(
        "tenant-a",
        (_ref(content_sha256="b" * 64),),
        NOW,
    )
    egress_changed = EnterpriseKnowledgeSelection(
        "tenant-a",
        (_ref(egress_decision="allow"),),
        NOW,
    )

    assert len(
        {
            baseline.fingerprint,
            version_changed.fingerprint,
            hash_changed.fingerprint,
            egress_changed.fingerprint,
        }
    ) == 4


def test_selection_rejects_cross_tenant_and_duplicate_sources() -> None:
    with pytest.raises(ValueError, match="crosses tenants"):
        EnterpriseKnowledgeSelection(
            "tenant-a",
            (_ref(tenant_id="tenant-b"),),
            NOW,
        )
    with pytest.raises(ValueError, match="repeats a source"):
        EnterpriseKnowledgeSelection(
            "tenant-a",
            (_ref(version_id="v1"), _ref(version_id="v2")),
            NOW,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_id", "attachment:source", "source_id"),
        ("version_id", "", "version_id"),
        ("media_type", "application/pdf", "media type"),
        ("size_bytes", 0, "size"),
        ("content_sha256", "not-a-hash", "hash"),
        ("data_classification", "public", "classification"),
        ("egress_decision", "inherit", "egress"),
    ],
)
def test_source_ref_rejects_unsupported_or_ambiguous_values(
    field: str,
    value: object,
    message: str,
) -> None:
    values = {
        "tenant_id": "tenant-a",
        "source_id": "enterprise:release_policy",
        "version_id": "v1",
        "display_label": "发布流程规范",
        "media_type": "text/markdown",
        "size_bytes": 128,
        "content_sha256": "a" * 64,
        "published_at": NOW,
        "data_classification": "internal",
        "egress_decision": "deny",
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        EnterpriseKnowledgeRef(**values)  # type: ignore[arg-type]


def test_contracts_are_immutable_and_require_aware_timestamps() -> None:
    ref = _ref()
    with pytest.raises(FrozenInstanceError):
        ref.version_id = "v2"  # type: ignore[misc]
    with pytest.raises(ValueError, match="timezone-aware"):
        EnterpriseKnowledgeRef(
            tenant_id="tenant-a",
            source_id="enterprise:release_policy",
            version_id="v1",
            display_label="发布流程规范",
            media_type="text/plain",
            size_bytes=1,
            content_sha256="a" * 64,
            published_at=datetime(2026, 8, 19),
        )


def test_explicit_fingerprint_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        EnterpriseKnowledgeSelection(
            tenant_id="tenant-a",
            sources=(_ref(),),
            created_at=NOW,
            fingerprint="0" * 64,
        )
