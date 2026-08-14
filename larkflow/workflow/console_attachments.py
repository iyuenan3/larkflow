"""Authorized text attachments for one Console draft planning request."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
from threading import RLock
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from larkflow.planning.context import (
    AttachmentRef,
    ContextBundle,
    ContextChunk,
    SourceRef,
    sha256_hex,
)

from .console import ConsolePrincipal
from .console_drafts import (
    ConsoleDraftConflictError,
    ConsoleDraftNotFoundError,
    ConsoleDraftRequest,
    InMemoryConsoleDraftRepository,
    _request_from_row,
    _utc,
)
from .draft_generation import DraftGenerationRejected


MAX_ATTACHMENTS_PER_REQUEST = 8
MAX_ATTACHMENT_BYTES = 32 * 1024
MAX_ATTACHMENTS_TOTAL_BYTES = 128 * 1024
MAX_RETAINED_ATTACHMENTS_PER_TENANT = 1024
MAX_RETAINED_ATTACHMENT_BYTES_PER_TENANT = 32 * 1024 * 1024
MAX_CONTEXT_BUNDLE_CHARS = 60_000
MAX_ATTACHMENT_UPLOAD_BODY_BYTES = 256 * 1024
MAX_ATTACHMENT_FILENAME_CHARS = 120
CONTEXT_BUNDLE_TTL = timedelta(minutes=15)

_ATTACHMENT_ID = re.compile(r"^[0-9a-f]{32}$")
_OBJECT_KEY = re.compile(r"^[0-9a-f]{16}/[0-9a-f]{32}$")
_MEDIA_TYPES = {"text/plain", "text/markdown"}
_ACTIVE_REQUEST_STATUSES = {
    "pending",
    "generating",
    "repairing",
    "creating",
    "failed",
}


class ConsoleAttachmentNotFoundError(KeyError):
    """The request or attachment is not visible to this owner."""


class ConsoleAttachmentConflictError(RuntimeError):
    """A stable attachment state or policy conflict."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AttachmentContextRejected(DraftGenerationRejected):
    """Frozen attachment material failed a deterministic integrity check."""


class AttachmentBlobUnavailableError(RuntimeError):
    """The blob backend failed transiently and the draft should retry."""


@dataclass(frozen=True)
class ConsoleAttachment:
    tenant_id: str
    attachment_id: str
    origin_request_id: str
    instance_id: str | None
    uploader_person_id: str = field(repr=False)
    display_filename: str = ""
    media_type: str = ""
    size_bytes: int = 0
    content_sha256: str = ""
    object_key: str = field(default="", repr=False)
    status: str = "ready"
    data_classification: str = "internal"
    model_egress_policy: str = "deny"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.uploader_person_id.strip():
            raise ValueError("attachment tenant and uploader are required")
        if _ATTACHMENT_ID.fullmatch(self.attachment_id) is None:
            raise ValueError("attachment id is invalid")
        if _ATTACHMENT_ID.fullmatch(self.origin_request_id) is None:
            raise ValueError("attachment origin request id is invalid")
        if _OBJECT_KEY.fullmatch(self.object_key) is None:
            raise ValueError("attachment object key is invalid")
        if (
            not self.display_filename.strip()
            or len(self.display_filename) > MAX_ATTACHMENT_FILENAME_CHARS
            or "/" in self.display_filename
            or "\\" in self.display_filename
        ):
            raise ValueError("attachment display filename is invalid")
        if self.media_type not in _MEDIA_TYPES:
            raise ValueError("attachment media type is invalid")
        if self.size_bytes < 1 or self.size_bytes > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment size is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.content_sha256) is None:
            raise ValueError("attachment content hash is invalid")
        if self.status not in {"ready", "revoked"}:
            raise ValueError("attachment status is invalid")
        if self.data_classification != "internal":
            raise ValueError("attachment classification must be internal")
        if self.model_egress_policy not in {"allow", "deny"}:
            raise ValueError("attachment egress policy is invalid")
        _utc(self.created_at)
        if self.status == "ready" and self.revoked_at is not None:
            raise ValueError("ready attachment cannot have revoked_at")
        if self.status == "revoked":
            if self.revoked_at is None:
                raise ValueError("revoked attachment requires revoked_at")
            _utc(self.revoked_at)

    def reference(self) -> AttachmentRef:
        return AttachmentRef(
            attachment_id=self.attachment_id,
            source_id=f"attachment:{self.attachment_id}",
            display_filename=self.display_filename,
            media_type=self.media_type,
            size_bytes=self.size_bytes,
            content_sha256=self.content_sha256,
            data_classification=self.data_classification,
            egress_decision=self.model_egress_policy,
        )


class AttachmentBlobStore(Protocol):
    """Opaque content store. Object keys are server-only capabilities."""

    def put(self, object_key: str, content: bytes) -> None:
        ...

    def get(self, object_key: str) -> bytes:
        ...

    def delete(self, object_key: str) -> None:
        ...


class InMemoryAttachmentBlobStore:
    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}
        self._lock = RLock()

    def put(self, object_key: str, content: bytes) -> None:
        _validated_object_key(object_key)
        with self._lock:
            if object_key in self._items:
                raise FileExistsError("attachment blob already exists")
            self._items[object_key] = bytes(content)

    def get(self, object_key: str) -> bytes:
        _validated_object_key(object_key)
        with self._lock:
            try:
                return self._items[object_key]
            except KeyError as exc:
                raise FileNotFoundError("attachment blob is missing") from exc

    def delete(self, object_key: str) -> None:
        _validated_object_key(object_key)
        with self._lock:
            self._items.pop(object_key, None)


class FilesystemAttachmentBlobStore:
    """Explicit local filesystem adapter for single-host development only."""

    def __init__(self, root: str | Path) -> None:
        candidate = Path(root)
        if not candidate.is_absolute():
            raise ValueError("attachment blob root must be absolute")
        candidate.mkdir(parents=True, exist_ok=True)
        if candidate.is_symlink():
            raise ValueError("attachment blob root cannot be a symlink")
        self.root = candidate.resolve(strict=True)

    def put(self, object_key: str, content: bytes) -> None:
        target = self._target(object_key, create_parent=True)
        if target.exists() or target.is_symlink():
            raise FileExistsError("attachment blob already exists")
        temporary = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def get(self, object_key: str) -> bytes:
        try:
            target = self._target(object_key, create_parent=False)
        except FileNotFoundError:
            raise
        except ValueError:
            raise
        except OSError as exc:
            raise AttachmentBlobUnavailableError(
                "attachment blob storage is temporarily unavailable"
            ) from exc
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target, flags)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                raise FileNotFoundError("attachment blob is missing") from exc
            if exc.errno == errno.ELOOP:
                raise ValueError("attachment blob path cannot be a symlink") from exc
            raise AttachmentBlobUnavailableError(
                "attachment blob storage is temporarily unavailable"
            ) from exc
        handle = None
        try:
            try:
                info = os.fstat(descriptor)
            except OSError as exc:
                raise AttachmentBlobUnavailableError(
                    "attachment blob storage is temporarily unavailable"
                ) from exc
            if not stat.S_ISREG(info.st_mode):
                raise FileNotFoundError("attachment blob is not a regular file")
            try:
                handle = os.fdopen(descriptor, "rb", closefd=True)
            except OSError as exc:
                raise AttachmentBlobUnavailableError(
                    "attachment blob storage is temporarily unavailable"
                ) from exc
            descriptor = None
            try:
                with handle:
                    return handle.read(MAX_ATTACHMENT_BYTES + 1)
            except OSError as exc:
                raise AttachmentBlobUnavailableError(
                    "attachment blob storage is temporarily unavailable"
                ) from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def delete(self, object_key: str) -> None:
        target = self._target(object_key, create_parent=False)
        try:
            if target.is_symlink():
                raise ValueError("attachment blob path cannot be a symlink")
            target.unlink(missing_ok=True)
        except FileNotFoundError:
            return

    def _target(self, object_key: str, *, create_parent: bool) -> Path:
        normalized = _validated_object_key(object_key)
        prefix, filename = normalized.split("/", 1)
        parent = self.root / prefix
        if create_parent:
            parent.mkdir(mode=0o700, exist_ok=True)
        if not parent.exists() or parent.is_symlink():
            raise ValueError("attachment blob path is unavailable")
        if parent.resolve(strict=True).parent != self.root:
            raise ValueError("attachment blob path escapes configured root")
        return parent / filename


class ConsoleAttachmentRepository(Protocol):
    def authorize_collecting(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> None:
        ...

    def create_for_request(self, attachment: ConsoleAttachment) -> ConsoleAttachment:
        ...

    def list_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> tuple[ConsoleAttachment, ...]:
        ...

    def revoke_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        attachment_id: str,
        *,
        requester_person_id: str,
        now: datetime,
    ) -> ConsoleAttachment:
        ...

    def freeze_and_queue(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
        now: datetime,
    ) -> ConsoleDraftRequest:
        ...

    def resolve_for_planning(
        self,
        request: ConsoleDraftRequest,
    ) -> tuple[ConsoleAttachment, ...]:
        ...

    def promote(
        self,
        request: ConsoleDraftRequest,
        *,
        instance_id: str,
        now: datetime,
    ) -> None:
        ...


class InMemoryConsoleAttachmentRepository:
    def __init__(self, draft_repository: InMemoryConsoleDraftRepository) -> None:
        self.draft_repository = draft_repository
        self._items: dict[tuple[str, str], ConsoleAttachment] = {}
        self._lock = RLock()

    def authorize_collecting(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> None:
        with self._lock:
            _require_collecting(
                self._request_owner(tenant_id, request_id, requester_person_id)
            )

    def create_for_request(self, attachment: ConsoleAttachment) -> ConsoleAttachment:
        with self._lock:
            request = self._request_owner(
                attachment.tenant_id,
                attachment.origin_request_id,
                attachment.uploader_person_id,
            )
            _require_collecting(request)
            retained_for_request = self._retained_for_request(
                attachment.tenant_id,
                attachment.origin_request_id,
            )
            retained_for_tenant = tuple(
                item
                for item in self._items.values()
                if item.tenant_id == attachment.tenant_id
            )
            _check_attachment_limits(
                retained_for_request,
                retained_for_tenant,
                attachment.size_bytes,
            )
            key = (attachment.tenant_id, attachment.attachment_id)
            if key in self._items:
                raise ConsoleAttachmentConflictError(
                    "attachment_id_conflict",
                    "附件编号冲突，请重新上传。",
                )
            self._items[key] = attachment
            return attachment

    def list_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> tuple[ConsoleAttachment, ...]:
        with self._lock:
            self._request_owner(tenant_id, request_id, requester_person_id)
            return tuple(
                sorted(
                    (
                        item
                        for item in self._items.values()
                        if item.tenant_id == tenant_id
                        and item.origin_request_id == request_id
                    ),
                    key=lambda item: (item.created_at, item.attachment_id),
                )
            )

    def revoke_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        attachment_id: str,
        *,
        requester_person_id: str,
        now: datetime,
    ) -> ConsoleAttachment:
        with self._lock:
            request = self._request_owner(tenant_id, request_id, requester_person_id)
            _require_collecting(request)
            key = (tenant_id, attachment_id)
            item = self._items.get(key)
            if (
                item is None
                or item.origin_request_id != request_id
                or item.status != "ready"
            ):
                raise ConsoleAttachmentNotFoundError(attachment_id)
            revoked = ConsoleAttachment(
                **{
                    **item.__dict__,
                    "status": "revoked",
                    "revoked_at": now,
                }
            )
            self._items[key] = revoked
            return revoked

    def freeze_and_queue(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
        now: datetime,
    ) -> ConsoleDraftRequest:
        with self._lock:
            request = self._request_owner(tenant_id, request_id, requester_person_id)
            _require_collecting(request)
            attachments = self._ready_for_request(tenant_id, request_id)
            if any(item.model_egress_policy != "allow" for item in attachments):
                raise ConsoleAttachmentConflictError(
                    "egress_denied",
                    "当前部署未允许将内部附件发送给规划模型。",
                )
            manifest = tuple(item.reference() for item in attachments)
            return self.draft_repository.queue_collecting(
                tenant_id,
                request_id,
                requester_person_id=requester_person_id,
                manifest=manifest,
                now=now,
            )

    def resolve_for_planning(
        self,
        request: ConsoleDraftRequest,
    ) -> tuple[ConsoleAttachment, ...]:
        with self._lock:
            current = self._request_owner(
                request.tenant_id,
                request.id,
                request.requester_person_id,
            )
            _require_planning_state(current)
            if current.attachment_manifest != request.attachment_manifest:
                raise AttachmentContextRejected("附件清单与当前草稿请求不一致")
            by_id = {
                item.attachment_id: item
                for item in self._ready_for_request(request.tenant_id, request.id)
            }
            return _ordered_manifest_records(
                request.attachment_manifest,
                by_id,
                expected_instance_id=f"console_draft_{request.id}",
            )

    def promote(
        self,
        request: ConsoleDraftRequest,
        *,
        instance_id: str,
        now: datetime,
    ) -> None:
        del now
        with self._lock:
            ids = {item.attachment_id for item in request.attachment_manifest}
            for attachment_id in ids:
                key = (request.tenant_id, attachment_id)
                item = self._items.get(key)
                if item is None or item.origin_request_id != request.id:
                    raise AttachmentContextRejected("附件提升清单不完整")
                if item.instance_id not in {None, instance_id}:
                    raise AttachmentContextRejected("附件已绑定到其他流程")
                self._items[key] = ConsoleAttachment(
                    **{**item.__dict__, "instance_id": instance_id}
                )

    def _request_owner(
        self,
        tenant_id: str,
        request_id: str,
        person_id: str,
    ) -> ConsoleDraftRequest:
        try:
            return self.draft_repository.get_for_owner(
                tenant_id,
                request_id,
                requester_person_id=person_id,
            )
        except ConsoleDraftNotFoundError as exc:
            raise ConsoleAttachmentNotFoundError(request_id) from exc

    def _ready_for_request(
        self,
        tenant_id: str,
        request_id: str,
    ) -> tuple[ConsoleAttachment, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.tenant_id == tenant_id
                    and item.origin_request_id == request_id
                    and item.status == "ready"
                ),
                key=lambda item: (item.created_at, item.attachment_id),
            )
        )

    def _retained_for_request(
        self,
        tenant_id: str,
        request_id: str,
    ) -> tuple[ConsoleAttachment, ...]:
        return tuple(
            item
            for item in self._items.values()
            if item.tenant_id == tenant_id
            and item.origin_request_id == request_id
        )


class PostgresConsoleAttachmentRepository:
    """Tenant-bound metadata repository with atomic manifest freezing."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def authorize_collecting(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_console_draft_requests
                WHERE tenant_id = %s AND id = %s AND requester_person_id = %s
                """,
                (tenant_id, request_id, requester_person_id),
            ).fetchone()
        if row is None:
            raise ConsoleAttachmentNotFoundError(request_id)
        _require_collecting(_request_from_row(row))

    def create_for_request(self, attachment: ConsoleAttachment) -> ConsoleAttachment:
        with self.connection_factory() as connection:
            with connection.transaction():
                request = connection.execute(
                    """
                    SELECT * FROM workflow_console_draft_requests
                    WHERE tenant_id = %s AND id = %s
                      AND requester_person_id = %s
                    FOR UPDATE
                    """,
                    (
                        attachment.tenant_id,
                        attachment.origin_request_id,
                        attachment.uploader_person_id,
                    ),
                ).fetchone()
                if request is None:
                    raise ConsoleAttachmentNotFoundError(attachment.origin_request_id)
                _require_collecting(_request_from_row(request))
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (attachment.tenant_id,),
                )
                request_totals = connection.execute(
                    """
                    SELECT count(*) AS count, coalesce(sum(size_bytes), 0) AS total
                    FROM workflow_project_attachments
                    WHERE tenant_id = %s AND origin_request_id = %s
                    """,
                    (attachment.tenant_id, attachment.origin_request_id),
                ).fetchone()
                tenant_totals = connection.execute(
                    """
                    SELECT count(*) AS count, coalesce(sum(size_bytes), 0) AS total
                    FROM workflow_project_attachments
                    WHERE tenant_id = %s
                    """,
                    (attachment.tenant_id,),
                ).fetchone()
                _check_attachment_totals(
                    int(request_totals["count"]),
                    int(request_totals["total"]),
                    int(tenant_totals["count"]),
                    int(tenant_totals["total"]),
                    attachment.size_bytes,
                )
                row = connection.execute(
                    """
                    INSERT INTO workflow_project_attachments (
                        tenant_id, attachment_id, origin_request_id, instance_id,
                        uploader_person_id, display_filename, media_type,
                        size_bytes, content_sha256, object_key, status,
                        data_classification, model_egress_policy,
                        created_at, revoked_at
                    ) VALUES (
                        %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s,
                        'ready', 'internal', %s, %s, NULL
                    )
                    RETURNING *
                    """,
                    (
                        attachment.tenant_id,
                        attachment.attachment_id,
                        attachment.origin_request_id,
                        attachment.uploader_person_id,
                        attachment.display_filename,
                        attachment.media_type,
                        attachment.size_bytes,
                        attachment.content_sha256,
                        attachment.object_key,
                        attachment.model_egress_policy,
                        attachment.created_at,
                    ),
                ).fetchone()
        return _attachment_from_row(row)

    def list_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> tuple[ConsoleAttachment, ...]:
        with self.connection_factory() as connection:
            owner = connection.execute(
                """
                SELECT 1 FROM workflow_console_draft_requests
                WHERE tenant_id = %s AND id = %s AND requester_person_id = %s
                """,
                (tenant_id, request_id, requester_person_id),
            ).fetchone()
            if owner is None:
                raise ConsoleAttachmentNotFoundError(request_id)
            rows = connection.execute(
                """
                SELECT * FROM workflow_project_attachments
                WHERE tenant_id = %s AND origin_request_id = %s
                ORDER BY created_at, attachment_id
                """,
                (tenant_id, request_id),
            ).fetchall()
        return tuple(_attachment_from_row(row) for row in rows)

    def revoke_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        attachment_id: str,
        *,
        requester_person_id: str,
        now: datetime,
    ) -> ConsoleAttachment:
        with self.connection_factory() as connection:
            with connection.transaction():
                request = connection.execute(
                    """
                    SELECT * FROM workflow_console_draft_requests
                    WHERE tenant_id = %s AND id = %s
                      AND requester_person_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, request_id, requester_person_id),
                ).fetchone()
                if request is None:
                    raise ConsoleAttachmentNotFoundError(request_id)
                _require_collecting(_request_from_row(request))
                row = connection.execute(
                    """
                    UPDATE workflow_project_attachments
                    SET status = 'revoked', revoked_at = %s
                    WHERE tenant_id = %s AND attachment_id = %s
                      AND origin_request_id = %s AND status = 'ready'
                    RETURNING *
                    """,
                    (now, tenant_id, attachment_id, request_id),
                ).fetchone()
        if row is None:
            raise ConsoleAttachmentNotFoundError(attachment_id)
        return _attachment_from_row(row)

    def freeze_and_queue(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
        now: datetime,
    ) -> ConsoleDraftRequest:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT * FROM workflow_console_draft_requests
                    WHERE tenant_id = %s AND id = %s
                      AND requester_person_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, request_id, requester_person_id),
                ).fetchone()
                if row is None:
                    raise ConsoleAttachmentNotFoundError(request_id)
                request = _request_from_row(row)
                _require_collecting(request)
                attachment_rows = connection.execute(
                    """
                    SELECT * FROM workflow_project_attachments
                    WHERE tenant_id = %s AND origin_request_id = %s
                      AND status = 'ready'
                    ORDER BY created_at, attachment_id
                    """,
                    (tenant_id, request_id),
                ).fetchall()
                attachments = tuple(
                    _attachment_from_row(item) for item in attachment_rows
                )
                if any(item.model_egress_policy != "allow" for item in attachments):
                    raise ConsoleAttachmentConflictError(
                        "egress_denied",
                        "当前部署未允许将内部附件发送给规划模型。",
                    )
                manifest = tuple(item.reference() for item in attachments)
                updated = connection.execute(
                    """
                    UPDATE workflow_console_draft_requests
                    SET status = 'pending', attachment_manifest = %s,
                        available_at = %s, updated_at = %s
                    WHERE tenant_id = %s AND id = %s AND status = 'collecting'
                    RETURNING *
                    """,
                    (
                        Jsonb([item.snapshot_value() for item in manifest]),
                        now,
                        now,
                        tenant_id,
                        request_id,
                    ),
                ).fetchone()
        if updated is None:
            raise ConsoleAttachmentConflictError(
                "draft_not_collecting",
                "草稿请求已开始生成，附件清单不能再修改。",
            )
        return _request_from_row(updated)

    def resolve_for_planning(
        self,
        request: ConsoleDraftRequest,
    ) -> tuple[ConsoleAttachment, ...]:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_console_draft_requests
                WHERE tenant_id = %s AND id = %s AND requester_person_id = %s
                """,
                (request.tenant_id, request.id, request.requester_person_id),
            ).fetchone()
            if row is None:
                raise AttachmentContextRejected("附件所属草稿请求不存在")
            current = _request_from_row(row)
            _require_planning_state(current)
            if current.attachment_manifest != request.attachment_manifest:
                raise AttachmentContextRejected("附件清单与当前草稿请求不一致")
            ids = [item.attachment_id for item in request.attachment_manifest]
            rows = connection.execute(
                """
                SELECT * FROM workflow_project_attachments
                WHERE tenant_id = %s AND origin_request_id = %s
                  AND attachment_id = ANY(%s)
                """,
                (request.tenant_id, request.id, ids),
            ).fetchall()
        by_id = {
            item.attachment_id: item
            for item in (_attachment_from_row(row) for row in rows)
        }
        return _ordered_manifest_records(
            request.attachment_manifest,
            by_id,
            expected_instance_id=f"console_draft_{request.id}",
        )

    def promote(
        self,
        request: ConsoleDraftRequest,
        *,
        instance_id: str,
        now: datetime,
    ) -> None:
        del now
        ids = [item.attachment_id for item in request.attachment_manifest]
        if not ids:
            return
        with self.connection_factory() as connection:
            with connection.transaction():
                conflict = connection.execute(
                    """
                    SELECT 1 FROM workflow_project_attachments
                    WHERE tenant_id = %s AND origin_request_id = %s
                      AND attachment_id = ANY(%s)
                      AND (status <> 'ready' OR instance_id IS DISTINCT FROM %s
                           AND instance_id IS NOT NULL)
                    LIMIT 1
                    """,
                    (request.tenant_id, request.id, ids, instance_id),
                ).fetchone()
                if conflict is not None:
                    raise AttachmentContextRejected("附件无法提升到当前流程")
                rows = connection.execute(
                    """
                    UPDATE workflow_project_attachments
                    SET instance_id = %s
                    WHERE tenant_id = %s AND origin_request_id = %s
                      AND attachment_id = ANY(%s) AND status = 'ready'
                      AND (instance_id IS NULL OR instance_id = %s)
                    RETURNING attachment_id
                    """,
                    (instance_id, request.tenant_id, request.id, ids, instance_id),
                ).fetchall()
        if {str(row["attachment_id"]) for row in rows} != set(ids):
            raise AttachmentContextRejected("附件提升清单不完整")


class ConsoleAttachmentService:
    """Owner-authorized attachment mutations and explicit generation start."""

    def __init__(
        self,
        repository: ConsoleAttachmentRepository,
        blob_store: AttachmentBlobStore,
        *,
        model_egress_policy: str = "deny",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if model_egress_policy not in {"allow", "deny"}:
            raise ValueError("attachment model egress policy is invalid")
        self.repository = repository
        self.blob_store = blob_store
        self.model_egress_policy = model_egress_policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def planning_enabled(self) -> bool:
        return self.model_egress_policy == "allow"

    def upload(
        self,
        principal: ConsolePrincipal,
        request_id: str,
        *,
        display_filename: Any,
        media_type: Any,
        content: Any,
    ) -> Mapping[str, Any]:
        if not self.planning_enabled:
            raise ConsoleAttachmentConflictError(
                "attachment_planning_unavailable",
                "当前部署未启用项目资料规划。",
            )
        self.repository.authorize_collecting(
            principal.tenant_id,
            request_id,
            requester_person_id=principal.person_id,
        )
        filename = _validated_filename(display_filename)
        if media_type not in _MEDIA_TYPES:
            raise ConsoleAttachmentConflictError(
                "unsupported_media_type",
                "附件只支持纯文本或 Markdown。",
            )
        if not isinstance(content, str):
            raise ConsoleAttachmentConflictError(
                "invalid_utf8",
                "附件正文必须是 UTF-8 文本。",
            )
        encoded = content.encode("utf-8")
        if not content.strip():
            raise ConsoleAttachmentConflictError(
                "empty_attachment",
                "附件正文不能为空。",
            )
        if len(encoded) > MAX_ATTACHMENT_BYTES:
            raise ConsoleAttachmentConflictError(
                "attachment_too_large",
                f"单个附件不能超过 {MAX_ATTACHMENT_BYTES} 字节。",
            )
        now = _utc(self.clock())
        attachment_id = secrets.token_hex(16)
        tenant_prefix = hashlib.sha256(principal.tenant_id.encode("utf-8")).hexdigest()[:16]
        object_key = f"{tenant_prefix}/{attachment_id}"
        attachment = ConsoleAttachment(
            tenant_id=principal.tenant_id,
            attachment_id=attachment_id,
            origin_request_id=request_id,
            instance_id=None,
            uploader_person_id=principal.person_id,
            display_filename=filename,
            media_type=media_type,
            size_bytes=len(encoded),
            content_sha256=sha256_hex(encoded),
            object_key=object_key,
            model_egress_policy=self.model_egress_policy,
            created_at=now,
        )
        self.blob_store.put(object_key, encoded)
        try:
            stored = self.repository.create_for_request(attachment)
        except BaseException:
            self.blob_store.delete(object_key)
            raise
        return {"attachment": _public_attachment(stored)}

    def list(
        self,
        principal: ConsolePrincipal,
        request_id: str,
    ) -> Mapping[str, Any]:
        items = self.repository.list_for_owner(
            principal.tenant_id,
            request_id,
            requester_person_id=principal.person_id,
        )
        return {
            "attachments": [_public_attachment(item) for item in items],
            "total": len(items),
        }

    def revoke(
        self,
        principal: ConsolePrincipal,
        request_id: str,
        attachment_id: str,
    ) -> Mapping[str, Any]:
        item = self.repository.revoke_for_owner(
            principal.tenant_id,
            request_id,
            _validated_attachment_id(attachment_id),
            requester_person_id=principal.person_id,
            now=_utc(self.clock()),
        )
        return {"attachment": _public_attachment(item)}

    def generate(
        self,
        principal: ConsolePrincipal,
        request_id: str,
    ) -> Mapping[str, Any]:
        if not self.planning_enabled:
            self.repository.authorize_collecting(
                principal.tenant_id,
                request_id,
                requester_person_id=principal.person_id,
            )
            ready = tuple(
                item
                for item in self.repository.list_for_owner(
                    principal.tenant_id,
                    request_id,
                    requester_person_id=principal.person_id,
                )
                if item.status == "ready"
            )
            if ready:
                raise ConsoleAttachmentConflictError(
                    "egress_denied",
                    "当前部署未允许将内部附件发送给规划模型。",
                )
        request = self.repository.freeze_and_queue(
            principal.tenant_id,
            request_id,
            requester_person_id=principal.person_id,
            now=_utc(self.clock()),
        )
        return {"request": {"id": request.id, "status": "queued"}}


class PlanningContextService:
    """Build a fail-closed planning bundle from one frozen manifest."""

    def __init__(
        self,
        repository: ConsoleAttachmentRepository,
        blob_store: AttachmentBlobStore,
        *,
        model_egress_policy: str = "deny",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if model_egress_policy not in {"allow", "deny"}:
            raise ValueError("planning context model egress policy is invalid")
        self.repository = repository
        self.blob_store = blob_store
        self.model_egress_policy = model_egress_policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def build_for_planning(
        self,
        request: ConsoleDraftRequest,
    ) -> ContextBundle | None:
        if not request.attachment_manifest:
            return None
        if self.model_egress_policy != "allow":
            raise AttachmentContextRejected("当前 Worker 未允许附件模型外发")
        attachments = self.repository.resolve_for_planning(request)
        sources = []
        references = []
        chunks = []
        total_chars = 0
        for order, attachment in enumerate(attachments):
            if attachment.status != "ready" or attachment.revoked_at is not None:
                raise AttachmentContextRejected("附件已撤销或不可用")
            if attachment.data_classification != "internal":
                raise AttachmentContextRejected("附件分级不受支持")
            if attachment.model_egress_policy != "allow":
                raise AttachmentContextRejected("附件模型外发未获授权")
            try:
                content = self.blob_store.get(attachment.object_key)
            except (FileNotFoundError, ValueError) as exc:
                raise AttachmentContextRejected("附件正文不可用") from exc
            if len(content) != attachment.size_bytes:
                raise AttachmentContextRejected("附件长度校验失败")
            if sha256_hex(content) != attachment.content_sha256:
                raise AttachmentContextRejected("附件完整性校验失败")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AttachmentContextRejected("附件不是有效 UTF-8 文本") from exc
            total_chars += len(text)
            if total_chars > MAX_CONTEXT_BUNDLE_CHARS:
                raise AttachmentContextRejected("附件上下文超过规划字符预算")
            reference = attachment.reference()
            references.append(reference)
            sources.append(
                SourceRef(
                    source_id=reference.source_id,
                    kind="attachment",
                    label=reference.display_filename,
                    content_sha256=reference.content_sha256,
                )
            )
            chunks.append(
                ContextChunk(
                    source_id=reference.source_id,
                    order=order,
                    text=text,
                )
            )
        now = _utc(self.clock())
        return ContextBundle(
            tenant_id=request.tenant_id,
            scope_kind="console_draft_request",
            scope_id=request.id,
            purpose="planning",
            actor_person_id=request.requester_person_id,
            sources=tuple(sources),
            attachments=tuple(references),
            chunks=tuple(chunks),
            created_at=now,
            expires_at=now + CONTEXT_BUNDLE_TTL,
        )

    def promote(
        self,
        request: ConsoleDraftRequest,
        *,
        instance_id: str,
    ) -> None:
        self.repository.promote(
            request,
            instance_id=instance_id,
            now=_utc(self.clock()),
        )


def _public_attachment(item: ConsoleAttachment) -> dict[str, Any]:
    return {
        "id": item.attachment_id,
        "display_filename": item.display_filename,
        "media_type": item.media_type,
        "size_bytes": item.size_bytes,
        "content_sha256": item.content_sha256,
        "status": item.status,
        "data_classification": item.data_classification,
        "model_egress_policy": item.model_egress_policy,
        "created_at": item.created_at.isoformat(),
        "revoked_at": item.revoked_at.isoformat() if item.revoked_at else None,
    }


def _attachment_from_row(row: Mapping[str, Any]) -> ConsoleAttachment:
    return ConsoleAttachment(
        tenant_id=str(row["tenant_id"]),
        attachment_id=str(row["attachment_id"]),
        origin_request_id=str(row["origin_request_id"]),
        instance_id=row.get("instance_id"),
        uploader_person_id=str(row["uploader_person_id"]),
        display_filename=str(row["display_filename"]),
        media_type=str(row["media_type"]),
        size_bytes=int(row["size_bytes"]),
        content_sha256=str(row["content_sha256"]),
        object_key=str(row["object_key"]),
        status=str(row["status"]),
        data_classification=str(row["data_classification"]),
        model_egress_policy=str(row["model_egress_policy"]),
        created_at=row["created_at"],
        revoked_at=row.get("revoked_at"),
    )


def _ordered_manifest_records(
    manifest: tuple[AttachmentRef, ...],
    by_id: Mapping[str, ConsoleAttachment],
    *,
    expected_instance_id: str,
) -> tuple[ConsoleAttachment, ...]:
    ordered = []
    for expected in manifest:
        item = by_id.get(expected.attachment_id)
        if item is None or item.status != "ready" or item.revoked_at is not None:
            raise AttachmentContextRejected("冻结清单中的附件不可用")
        if item.instance_id not in {None, expected_instance_id}:
            raise AttachmentContextRejected("附件已绑定到其他流程")
        if item.reference() != expected:
            raise AttachmentContextRejected("冻结清单中的附件元数据不一致")
        ordered.append(item)
    if len(ordered) != len(by_id):
        raise AttachmentContextRejected("附件清单包含未授权内容")
    return tuple(ordered)


def _require_collecting(request: ConsoleDraftRequest) -> None:
    if request.status != "collecting":
        raise ConsoleAttachmentConflictError(
            "draft_not_collecting",
            "草稿请求已开始生成，附件清单不能再修改。",
        )


def _require_planning_state(request: ConsoleDraftRequest) -> None:
    if request.status not in _ACTIVE_REQUEST_STATUSES:
        raise AttachmentContextRejected("草稿请求不处于可规划状态")


def _check_attachment_limits(
    retained_for_request: tuple[ConsoleAttachment, ...],
    retained_for_tenant: tuple[ConsoleAttachment, ...],
    incoming_size: int,
) -> None:
    _check_attachment_totals(
        len(retained_for_request),
        sum(item.size_bytes for item in retained_for_request),
        len(retained_for_tenant),
        sum(item.size_bytes for item in retained_for_tenant),
        incoming_size,
    )


def _check_attachment_totals(
    request_count: int,
    request_total: int,
    tenant_count: int,
    tenant_total: int,
    incoming_size: int,
) -> None:
    if request_count >= MAX_ATTACHMENTS_PER_REQUEST:
        raise ConsoleAttachmentConflictError(
            "too_many_attachments",
            f"每个草稿最多保留 {MAX_ATTACHMENTS_PER_REQUEST} 个附件对象。",
        )
    if request_total + incoming_size > MAX_ATTACHMENTS_TOTAL_BYTES:
        raise ConsoleAttachmentConflictError(
            "attachments_too_large",
            f"每个草稿保留的附件总量不能超过 {MAX_ATTACHMENTS_TOTAL_BYTES} 字节。",
        )
    if tenant_count >= MAX_RETAINED_ATTACHMENTS_PER_TENANT:
        raise ConsoleAttachmentConflictError(
            "tenant_attachment_quota_exceeded",
            "当前租户保留的附件对象数量已达到上限。",
        )
    if tenant_total + incoming_size > MAX_RETAINED_ATTACHMENT_BYTES_PER_TENANT:
        raise ConsoleAttachmentConflictError(
            "tenant_attachment_quota_exceeded",
            "当前租户保留的附件总量已达到上限。",
        )


def _validated_filename(value: Any) -> str:
    if not isinstance(value, str):
        raise ConsoleAttachmentConflictError(
            "invalid_filename",
            "附件文件名无效。",
        )
    filename = value.strip()
    if (
        not filename
        or len(filename) > MAX_ATTACHMENT_FILENAME_CHARS
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
        or any(ord(character) < 32 for character in filename)
    ):
        raise ConsoleAttachmentConflictError(
            "invalid_filename",
            "附件文件名无效。",
        )
    return filename


def _validated_attachment_id(value: Any) -> str:
    if not isinstance(value, str) or _ATTACHMENT_ID.fullmatch(value) is None:
        raise ConsoleAttachmentNotFoundError("attachment")
    return value


def _validated_object_key(value: Any) -> str:
    if not isinstance(value, str) or _OBJECT_KEY.fullmatch(value) is None:
        raise ValueError("attachment object key is invalid")
    return value


__all__ = [
    "AttachmentBlobUnavailableError",
    "AttachmentBlobStore",
    "AttachmentContextRejected",
    "ConsoleAttachment",
    "ConsoleAttachmentConflictError",
    "ConsoleAttachmentNotFoundError",
    "ConsoleAttachmentService",
    "FilesystemAttachmentBlobStore",
    "InMemoryAttachmentBlobStore",
    "InMemoryConsoleAttachmentRepository",
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENT_UPLOAD_BODY_BYTES",
    "MAX_ATTACHMENTS_PER_REQUEST",
    "MAX_ATTACHMENTS_TOTAL_BYTES",
    "MAX_CONTEXT_BUNDLE_CHARS",
    "MAX_RETAINED_ATTACHMENT_BYTES_PER_TENANT",
    "MAX_RETAINED_ATTACHMENTS_PER_TENANT",
    "PlanningContextService",
    "PostgresConsoleAttachmentRepository",
]
