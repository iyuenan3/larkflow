from __future__ import annotations

import os

import pytest

from larkflow.knowledge.blob import (
    FilesystemEnterpriseKnowledgeBlobStore,
    InMemoryEnterpriseKnowledgeBlobStore,
    enterprise_knowledge_object_key,
)


def _key(*, version_id: str = "v1", content_sha256: str = "a" * 64) -> str:
    return enterprise_knowledge_object_key(
        tenant_id="tenant-a",
        source_id="enterprise:release_policy",
        version_id=version_id,
        content_sha256=content_sha256,
    )


def test_object_key_is_deterministic_opaque_and_version_bound() -> None:
    first = _key()

    assert first == _key()
    assert first != _key(version_id="v2")
    assert "tenant-a" not in first
    assert "release_policy" not in first


def test_in_memory_blob_is_create_once_and_exactly_cleaned() -> None:
    store = InMemoryEnterpriseKnowledgeBlobStore()
    key = _key()

    assert store.put_if_absent(key, b"safe body") is True
    assert store.put_if_absent(key, b"safe body") is False
    with pytest.raises(ValueError, match="immutable"):
        store.put_if_absent(key, b"different")
    assert store.get(key) == b"safe body"
    store.delete(key)
    with pytest.raises(FileNotFoundError):
        store.get(key)


def test_filesystem_blob_round_trip_does_not_follow_target_symlink(tmp_path) -> None:
    store = FilesystemEnterpriseKnowledgeBlobStore(tmp_path / "knowledge")
    key = _key(content_sha256="b" * 64)

    assert store.put_if_absent(key, b"public synthetic body") is True
    assert store.put_if_absent(key, b"public synthetic body") is False
    assert store.get(key) == b"public synthetic body"

    target = store.root.joinpath(*key.split("/"))
    target.unlink()
    os.symlink(tmp_path / "outside", target)
    with pytest.raises(FileNotFoundError):
        store.get(key)


@pytest.mark.parametrize(
    "value",
    ["../escape", "/absolute", "enterprise/v1/not-a-key"],
)
def test_blob_rejects_client_selected_paths(value: str) -> None:
    store = InMemoryEnterpriseKnowledgeBlobStore()

    with pytest.raises(ValueError, match="object key"):
        store.put_if_absent(value, b"body")
