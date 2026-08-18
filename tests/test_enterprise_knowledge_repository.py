from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from larkflow.knowledge import (
    EnterpriseKnowledgeAuthorizationProof,
    EnterpriseKnowledgePublication,
    EnterpriseKnowledgeRef,
)
from larkflow.knowledge.repository import (
    EnterpriseKnowledgeConflictError,
    EnterpriseKnowledgeNotFoundError,
    InMemoryEnterpriseKnowledgeRepository,
)


NOW = datetime(2026, 8, 19, 1, 0, tzinfo=timezone.utc)


def _publication(
    version_id: str = "v1",
    *,
    tenant_id: str = "tenant-a",
    source_id: str = "enterprise:release_policy",
    publisher: str = "admin-a",
    authorized: bool = False,
    size_bytes: int = 128,
) -> EnterpriseKnowledgePublication:
    content_sha256 = ("a" if version_id == "v1" else "b") * 64
    proof = (
        EnterpriseKnowledgeAuthorizationProof(
            tenant_id=tenant_id,
            source_id=source_id,
            version_id=version_id,
            content_sha256=content_sha256,
            authorized_by_person_id=publisher,
            authorized_at=NOW,
        )
        if authorized
        else None
    )
    return EnterpriseKnowledgePublication(
        ref=EnterpriseKnowledgeRef(
            tenant_id=tenant_id,
            source_id=source_id,
            version_id=version_id,
            display_label="发布流程规范",
            media_type="text/markdown",
            size_bytes=size_bytes,
            content_sha256=content_sha256,
            published_at=NOW,
            authorization_proof_id=(proof.proof_id if proof is not None else None),
            authorization_fingerprint=(
                proof.fingerprint if proof is not None else None
            ),
        ),
        published_by_person_id=publisher,
        authorization_proof=proof,
    )


def test_publish_is_idempotent_and_audited_once() -> None:
    repository = InMemoryEnterpriseKnowledgeRepository()
    publication = _publication()

    assert repository.publish(publication) == publication
    assert repository.publish(publication) == publication

    assert repository.list_published("tenant-a") == (publication.ref,)
    audit = repository.list_audit(
        "tenant-a",
        publication.ref.source_id,
        publication.ref.version_id,
    )
    assert [event.event_type for event in audit] == [
        "enterprise_knowledge.published"
    ]
    assert "content" not in audit[0].snapshot
    assert "object_key" not in audit[0].snapshot


def test_revoke_is_idempotent_and_allows_a_new_version() -> None:
    repository = InMemoryEnterpriseKnowledgeRepository()
    first = repository.publish(_publication("v1"))
    revoked_at = NOW + timedelta(minutes=5)

    revoked = repository.revoke(
        "tenant-a",
        first.ref.source_id,
        "v1",
        actor_person_id="admin-b",
        now=revoked_at,
    )
    repeated = repository.revoke(
        "tenant-a",
        first.ref.source_id,
        "v1",
        actor_person_id="admin-b",
        now=revoked_at + timedelta(minutes=1),
    )

    assert revoked.status == "revoked"
    assert repeated == revoked
    assert repository.list_published("tenant-a") == ()
    second = repository.publish(_publication("v2"))
    assert repository.list_published("tenant-a") == (second.ref,)
    assert repository.list_versions("tenant-a", limit=10) == (second, revoked)
    assert [
        event.event_type
        for event in repository.list_audit(
            "tenant-a",
            first.ref.source_id,
            "v1",
        )
    ] == ["enterprise_knowledge.published", "enterprise_knowledge.revoked"]


def test_existing_version_and_active_version_conflicts_fail_closed() -> None:
    repository = InMemoryEnterpriseKnowledgeRepository()
    repository.publish(_publication("v1"))

    with pytest.raises(EnterpriseKnowledgeConflictError, match="already exists"):
        repository.publish(_publication("v1", publisher="admin-b"))
    with pytest.raises(
        EnterpriseKnowledgeConflictError,
        match="published version",
    ):
        repository.publish(_publication("v2"))


def test_catalog_is_tenant_isolated_and_missing_revoke_is_hidden() -> None:
    repository = InMemoryEnterpriseKnowledgeRepository()
    repository.publish(_publication(tenant_id="tenant-a"))
    repository.publish(_publication(tenant_id="tenant-b"))

    assert {ref.tenant_id for ref in repository.list_published("tenant-a")} == {
        "tenant-a"
    }
    assert {
        item.ref.tenant_id
        for item in repository.list_versions("tenant-a", limit=10)
    } == {"tenant-a"}
    with pytest.raises(EnterpriseKnowledgeNotFoundError):
        repository.revoke(
            "tenant-b",
            "enterprise:missing",
            "v1",
            actor_person_id="admin-b",
            now=NOW,
        )


def test_competing_versions_publish_only_one_active_version() -> None:
    repository = InMemoryEnterpriseKnowledgeRepository()

    def publish(version_id: str) -> str:
        try:
            return repository.publish(_publication(version_id)).ref.version_id
        except EnterpriseKnowledgeConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, ("v1", "v2")))

    assert results.count("conflict") == 1
    assert len(repository.list_published("tenant-a")) == 1


def test_authorization_proof_survives_read_revoke_and_safe_audit() -> None:
    repository = InMemoryEnterpriseKnowledgeRepository()
    publication = repository.publish(_publication(authorized=True))

    assert repository.get_version("tenant-a", publication.ref.source_id, "v1") == (
        publication
    )
    revoked = repository.revoke(
        "tenant-a",
        publication.ref.source_id,
        "v1",
        actor_person_id="admin-b",
        now=NOW + timedelta(minutes=1),
    )

    assert revoked.authorization_proof == publication.authorization_proof
    snapshot = repository.list_audit(
        "tenant-a", publication.ref.source_id, "v1"
    )[0].snapshot
    assert snapshot["authorization_proof"]["proof_id"].startswith("kp_")
    encoded = str(snapshot)
    assert "authorized_by_person_id" not in encoded
    assert "admin-a" not in encoded
    assert "authorization_statement" not in encoded


def test_revoked_versions_still_consume_retained_quota() -> None:
    repository = InMemoryEnterpriseKnowledgeRepository(
        max_retained_versions=1,
        max_retained_bytes=128,
    )
    repository.publish(_publication("v1"))
    repository.revoke(
        "tenant-a",
        "enterprise:release_policy",
        "v1",
        actor_person_id="admin-a",
        now=NOW + timedelta(minutes=1),
    )

    assert repository.retained_usage("tenant-a") == (1, 128)
    with pytest.raises(EnterpriseKnowledgeConflictError, match="version quota"):
        repository.publish(_publication("v2"))


def test_retained_byte_quota_is_tenant_scoped() -> None:
    repository = InMemoryEnterpriseKnowledgeRepository(
        max_retained_versions=10,
        max_retained_bytes=128,
    )
    repository.publish(_publication(size_bytes=128))
    repository.revoke(
        "tenant-a",
        "enterprise:release_policy",
        "v1",
        actor_person_id="admin-a",
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(EnterpriseKnowledgeConflictError, match="byte quota"):
        repository.publish(_publication("v2", size_bytes=1))
    assert repository.publish(
        _publication(tenant_id="tenant-b", size_bytes=128)
    ).ref.tenant_id == "tenant-b"


@pytest.mark.parametrize("limit", [0, 1001, True])
def test_version_listing_rejects_unbounded_limits(limit) -> None:
    repository = InMemoryEnterpriseKnowledgeRepository()

    with pytest.raises(ValueError, match="limit"):
        repository.list_versions("tenant-a", limit=limit)
