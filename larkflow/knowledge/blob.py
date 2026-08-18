"""Immutable body storage for authorized enterprise knowledge snapshots."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
from typing import Protocol
from uuid import uuid4


_OBJECT_KEY = re.compile(
    r"^enterprise/v1/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{64}\.blob$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EnterpriseKnowledgeBlobUnavailableError(RuntimeError):
    """A storage or mount failure that may be retried."""


class EnterpriseKnowledgeBlobStore(Protocol):
    """Create-once storage that never exposes its root to callers."""

    def put_if_absent(self, object_key: str, content: bytes) -> bool:
        """Store immutable bytes and return true only when a new object was made."""

    def get(self, object_key: str) -> bytes:
        ...

    def delete(self, object_key: str) -> None:
        """Remove only an uncommitted orphan selected by exact server key."""


class InMemoryEnterpriseKnowledgeBlobStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._lock = RLock()

    def put_if_absent(self, object_key: str, content: bytes) -> bool:
        _validate_object_key(object_key)
        value = bytes(content)
        if not value:
            raise ValueError("enterprise knowledge body is empty")
        with self._lock:
            existing = self._objects.get(object_key)
            if existing is not None:
                if existing != value:
                    raise ValueError("enterprise knowledge blob is immutable")
                return False
            self._objects[object_key] = value
            return True

    def get(self, object_key: str) -> bytes:
        _validate_object_key(object_key)
        with self._lock:
            try:
                return self._objects[object_key]
            except KeyError as exc:
                raise FileNotFoundError(object_key) from exc

    def delete(self, object_key: str) -> None:
        _validate_object_key(object_key)
        with self._lock:
            self._objects.pop(object_key, None)

    def retained_usage(self) -> tuple[int, int]:
        """Expose count and bytes only to deterministic tests."""

        with self._lock:
            return len(self._objects), sum(len(value) for value in self._objects.values())


class FilesystemEnterpriseKnowledgeBlobStore:
    """Single-host development adapter with a dedicated immutable namespace."""

    def __init__(self, root: str | Path) -> None:
        raw = Path(root)
        if not raw.is_absolute():
            raise ValueError("enterprise knowledge blob root must be absolute")
        raw.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if stat.S_ISLNK(raw.lstat().st_mode):
                raise ValueError("enterprise knowledge blob root cannot be a symlink")
            self.root = raw.resolve(strict=True)
        except OSError as exc:
            raise EnterpriseKnowledgeBlobUnavailableError(
                "enterprise knowledge blob root is unavailable"
            ) from exc
        if not self.root.is_dir():
            raise ValueError("enterprise knowledge blob root must be a directory")

    def put_if_absent(self, object_key: str, content: bytes) -> bool:
        value = bytes(content)
        if not value:
            raise ValueError("enterprise knowledge body is empty")
        target = self._target(object_key, create_parent=True)
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                if self.get(object_key) != value:
                    raise ValueError("enterprise knowledge blob is immutable")
                return False
            return True
        except (FileNotFoundError, ValueError):
            raise
        except OSError as exc:
            raise EnterpriseKnowledgeBlobUnavailableError(
                "enterprise knowledge blob write failed"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, object_key: str) -> bytes:
        target = self._target(object_key, create_parent=False)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                target,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(descriptor)
                descriptor = None
                raise FileNotFoundError(object_key)
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = None
                return stream.read()
        except FileNotFoundError:
            raise
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise FileNotFoundError(object_key) from exc
            raise EnterpriseKnowledgeBlobUnavailableError(
                "enterprise knowledge blob read failed"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def delete(self, object_key: str) -> None:
        target = self._target(object_key, create_parent=False)
        try:
            if not target.exists():
                return
            if not stat.S_ISREG(target.lstat().st_mode):
                raise FileNotFoundError(object_key)
            target.unlink()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise EnterpriseKnowledgeBlobUnavailableError(
                "enterprise knowledge orphan cleanup failed"
            ) from exc

    def _target(self, object_key: str, *, create_parent: bool) -> Path:
        _validate_object_key(object_key)
        target = self.root.joinpath(*object_key.split("/"))
        parent = target.parent
        try:
            if create_parent:
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            resolved_parent = parent.resolve(strict=True)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise EnterpriseKnowledgeBlobUnavailableError(
                "enterprise knowledge blob path is unavailable"
            ) from exc
        if resolved_parent != self.root and self.root not in resolved_parent.parents:
            raise ValueError("enterprise knowledge object key escapes blob root")
        relative = resolved_parent.relative_to(self.root)
        cursor = self.root
        for part in relative.parts:
            cursor = cursor / part
            if stat.S_ISLNK(cursor.lstat().st_mode):
                raise ValueError("enterprise knowledge blob path contains a symlink")
        return resolved_parent / target.name


def enterprise_knowledge_object_key(
    *,
    tenant_id: str,
    source_id: str,
    version_id: str,
    content_sha256: str,
) -> str:
    """Derive an opaque, server-owned key without persisting a path handle."""

    if not all(
        isinstance(value, str) and value.strip()
        for value in (tenant_id, source_id, version_id)
    ):
        raise ValueError("enterprise knowledge object identity is incomplete")
    if _SHA256.fullmatch(content_sha256) is None:
        raise ValueError("enterprise knowledge object content hash is invalid")
    identity = json.dumps(
        {
            "tenant_id": tenant_id,
            "source_id": source_id,
            "version_id": version_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return f"enterprise/v1/{digest[:2]}/{digest}/{content_sha256}.blob"


def _validate_object_key(value: str) -> None:
    if not isinstance(value, str) or _OBJECT_KEY.fullmatch(value) is None:
        raise ValueError("enterprise knowledge object key is invalid")


__all__ = [
    "EnterpriseKnowledgeBlobStore",
    "EnterpriseKnowledgeBlobUnavailableError",
    "FilesystemEnterpriseKnowledgeBlobStore",
    "InMemoryEnterpriseKnowledgeBlobStore",
    "enterprise_knowledge_object_key",
]
