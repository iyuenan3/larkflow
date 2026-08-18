from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from larkflow.knowledge import (
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
) -> EnterpriseKnowledgePublication:
    return EnterpriseKnowledgePublication(
        ref=EnterpriseKnowledgeRef(
            tenant_id=tenant_id,
            source_id=source_id,
            version_id=version_id,
            display_label="发布流程规范",
            media_type="text/markdown",
            size_bytes=128,
            content_sha256=("a" if version_id == "v1" else "b") * 64,
            published_at=NOW,
        ),
        published_by_person_id=publisher,
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
