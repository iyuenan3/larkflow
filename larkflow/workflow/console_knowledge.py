"""Explicit, owner-bound enterprise knowledge selection for Console drafts."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import re
import secrets
from threading import RLock
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from larkflow.knowledge.contracts import (
    EnterpriseKnowledgeRef,
    EnterpriseKnowledgeSelection,
)
from larkflow.knowledge.repository import EnterpriseKnowledgeRepository

from .console import ConsolePrincipal
from .console_attachments import (
    ConsoleAttachmentConflictError,
    ConsoleAttachmentRepository,
)
from .console_drafts import (
    ConsoleDraftConflictError,
    ConsoleDraftNotFoundError,
    ConsoleDraftRequest,
    InMemoryConsoleDraftRepository,
    _request_from_row,
)


MAX_SELECTED_ENTERPRISE_SOURCES = 16
MAX_KNOWLEDGE_SELECTION_BODY_BYTES = 16_384
_SOURCE_ID = re.compile(r"^enterprise:[a-z][a-z0-9_.:-]{0,116}$")


class ConsoleKnowledgeSelectionNotFoundError(KeyError):
    """A request or source is not visible to the current principal."""


class ConsoleKnowledgeSelectionConflictError(RuntimeError):
    """A selection cannot be saved or frozen in the current state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConsoleKnowledgeSelectionRepository(Protocol):
    def list_candidates(self, tenant_id: str) -> tuple[EnterpriseKnowledgeRef, ...]:
        ...

    def get_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> ConsoleDraftRequest:
        ...

    def save_selection(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
        source_ids: tuple[str, ...],
        expected_version: int,
        now: datetime,
    ) -> ConsoleDraftRequest:
        ...

    def freeze_and_queue(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
        model_egress_policy: str,
        attachment_planning_enabled: bool,
        now: datetime,
    ) -> ConsoleDraftRequest:
        ...


class InMemoryConsoleKnowledgeSelectionRepository:
    """Deterministic selection repository for offline workflow tests."""

    def __init__(
        self,
        draft_repository: InMemoryConsoleDraftRepository,
        enterprise_repository: EnterpriseKnowledgeRepository,
        attachment_repository: ConsoleAttachmentRepository | None = None,
    ) -> None:
        self.draft_repository = draft_repository
        self.enterprise_repository = enterprise_repository
        self.attachment_repository = attachment_repository
        self._lock = RLock()

    def list_candidates(self, tenant_id: str) -> tuple[EnterpriseKnowledgeRef, ...]:
        return tuple(self.enterprise_repository.list_published(tenant_id))

    def get_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> ConsoleDraftRequest:
        try:
            return self.draft_repository.get_for_owner(
                tenant_id,
                request_id,
                requester_person_id=requester_person_id,
            )
        except ConsoleDraftNotFoundError as exc:
            raise ConsoleKnowledgeSelectionNotFoundError(request_id) from exc

    def save_selection(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
        source_ids: tuple[str, ...],
        expected_version: int,
        now: datetime,
    ) -> ConsoleDraftRequest:
        with self._lock, self.draft_repository._lock:
            request = self.get_for_owner(
                tenant_id,
                request_id,
                requester_person_id=requester_person_id,
            )
            _require_collecting(request)
            if request.enterprise_source_selection == source_ids:
                return request
            _require_visible_sources(
                source_ids,
                self.list_candidates(tenant_id),
                existing_source_ids=request.enterprise_source_selection,
            )
            if request.enterprise_selection_version != expected_version:
                raise ConsoleKnowledgeSelectionConflictError(
                    "selection_version_conflict",
                    "资料选择已在其他页面更新，请刷新后重试。",
                )
            updated = ConsoleDraftRequest(
                **{
                    **request.__dict__,
                    "enterprise_source_selection": source_ids,
                    "enterprise_selection_version": expected_version + 1,
                    "updated_at": now,
                }
            )
            self.draft_repository._items[(tenant_id, request_id)] = updated
            return updated

    def freeze_and_queue(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
        model_egress_policy: str,
        attachment_planning_enabled: bool,
        now: datetime,
    ) -> ConsoleDraftRequest:
        with self._lock:
            request = self.get_for_owner(
                tenant_id,
                request_id,
                requester_person_id=requester_person_id,
            )
            if request.status != "collecting":
                if request.status == "pending":
                    return request
                _require_collecting(request)
            refs = _resolve_selection(
                tenant_id,
                request.enterprise_source_selection,
                self.list_candidates(tenant_id),
            )
            if refs and model_egress_policy != "allow":
                raise ConsoleKnowledgeSelectionConflictError(
                    "knowledge_egress_denied",
                    "当前部署未允许将企业资料发送给规划模型。",
                )
            if refs:
                authorized = self.enterprise_repository.authorize_for_context(
                    tenant_id,
                    refs,
                )
                refs = tuple(item.ref for item in authorized)
            attachments = ()
            if self.attachment_repository is not None:
                records = self.attachment_repository.list_for_owner(
                    tenant_id,
                    request_id,
                    requester_person_id=requester_person_id,
                )
                ready = tuple(item for item in records if item.status == "ready")
                if ready and not attachment_planning_enabled:
                    raise ConsoleAttachmentConflictError(
                        "attachment_planning_unavailable",
                        "当前部署未启用项目资料规划。",
                    )
                if any(item.model_egress_policy != "allow" for item in ready):
                    raise ConsoleAttachmentConflictError(
                        "egress_denied",
                        "当前部署未允许将内部附件发送给规划模型。",
                    )
                attachments = tuple(item.reference() for item in ready)
            selection = (
                EnterpriseKnowledgeSelection(tenant_id=tenant_id, sources=refs)
                if refs
                else None
            )
            return self.draft_repository.queue_collecting_context(
                tenant_id,
                request_id,
                requester_person_id=requester_person_id,
                attachment_manifest=attachments,
                enterprise_manifest=refs,
                enterprise_fingerprint=(selection.fingerprint if selection else None),
                now=now,
            )


class PostgresConsoleKnowledgeSelectionRepository:
    """Freeze source selection and attachment refs in one PostgreSQL transaction."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def list_candidates(self, tenant_id: str) -> tuple[EnterpriseKnowledgeRef, ...]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                _PUBLISHED_SQL + " ORDER BY v.source_id, v.version_id",
                (tenant_id,),
            ).fetchall()
        return tuple(_ref_from_row(row) for row in rows)

    def get_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> ConsoleDraftRequest:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_console_draft_requests
                WHERE tenant_id = %s AND id = %s AND requester_person_id = %s
                """,
                (tenant_id, request_id, requester_person_id),
            ).fetchone()
        if row is None:
            raise ConsoleKnowledgeSelectionNotFoundError(request_id)
        return _request_from_row(row)

    def save_selection(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
        source_ids: tuple[str, ...],
        expected_version: int,
        now: datetime,
    ) -> ConsoleDraftRequest:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT * FROM workflow_console_draft_requests
                    WHERE tenant_id = %s AND id = %s AND requester_person_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, request_id, requester_person_id),
                ).fetchone()
                if row is None:
                    raise ConsoleKnowledgeSelectionNotFoundError(request_id)
                request = _request_from_row(row)
                _require_collecting(request)
                if request.enterprise_source_selection == source_ids:
                    return request
                candidate_rows = connection.execute(
                    _PUBLISHED_SQL + " AND v.source_id = ANY(%s)",
                    (tenant_id, list(source_ids)),
                ).fetchall() if source_ids else []
                _require_visible_sources(
                    source_ids,
                    tuple(_ref_from_row(item) for item in candidate_rows),
                    existing_source_ids=request.enterprise_source_selection,
                )
                if request.enterprise_selection_version != expected_version:
                    raise ConsoleKnowledgeSelectionConflictError(
                        "selection_version_conflict",
                        "资料选择已在其他页面更新，请刷新后重试。",
                    )
                updated = connection.execute(
                    """
                    UPDATE workflow_console_draft_requests
                    SET enterprise_source_selection = %s,
                        enterprise_selection_version = enterprise_selection_version + 1,
                        updated_at = %s
                    WHERE tenant_id = %s AND id = %s
                      AND requester_person_id = %s AND status = 'collecting'
                      AND enterprise_selection_version = %s
                    RETURNING *
                    """,
                    (
                        Jsonb(list(source_ids)),
                        now,
                        tenant_id,
                        request_id,
                        requester_person_id,
                        expected_version,
                    ),
                ).fetchone()
        if updated is None:
            raise ConsoleKnowledgeSelectionConflictError(
                "selection_version_conflict",
                "资料选择已在其他页面更新，请刷新后重试。",
            )
        return _request_from_row(updated)

    def freeze_and_queue(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
        model_egress_policy: str,
        attachment_planning_enabled: bool,
        now: datetime,
    ) -> ConsoleDraftRequest:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT * FROM workflow_console_draft_requests
                    WHERE tenant_id = %s AND id = %s AND requester_person_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, request_id, requester_person_id),
                ).fetchone()
                if row is None:
                    raise ConsoleKnowledgeSelectionNotFoundError(request_id)
                request = _request_from_row(row)
                if request.status != "collecting":
                    if request.status == "pending":
                        return request
                    _require_collecting(request)
                source_ids = request.enterprise_source_selection
                if source_ids and model_egress_policy != "allow":
                    raise ConsoleKnowledgeSelectionConflictError(
                        "knowledge_egress_denied",
                        "当前部署未允许将企业资料发送给规划模型。",
                    )
                for source_id in sorted(source_ids):
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"enterprise-knowledge:{tenant_id}:{source_id}",),
                    )
                source_rows = connection.execute(
                    _PUBLISHED_SQL + " AND v.source_id = ANY(%s) FOR SHARE OF v",
                    (tenant_id, list(source_ids)),
                ).fetchall() if source_ids else []
                refs = _resolve_selection(
                    tenant_id,
                    source_ids,
                    tuple(_ref_from_row(item) for item in source_rows),
                )
                attachment_rows = connection.execute(
                    """
                    SELECT * FROM workflow_project_attachments
                    WHERE tenant_id = %s AND origin_request_id = %s
                      AND status = 'ready'
                    ORDER BY created_at, attachment_id
                    """,
                    (tenant_id, request_id),
                ).fetchall()
                if attachment_rows and not attachment_planning_enabled:
                    raise ConsoleAttachmentConflictError(
                        "attachment_planning_unavailable",
                        "当前部署未启用项目资料规划。",
                    )
                if any(item["model_egress_policy"] != "allow" for item in attachment_rows):
                    raise ConsoleAttachmentConflictError(
                        "egress_denied",
                        "当前部署未允许将内部附件发送给规划模型。",
                    )
                attachment_manifest = [_attachment_ref_value(item) for item in attachment_rows]
                selection = (
                    EnterpriseKnowledgeSelection(tenant_id=tenant_id, sources=refs)
                    if refs
                    else None
                )
                updated = connection.execute(
                    """
                    UPDATE workflow_console_draft_requests
                    SET status = 'pending', attachment_manifest = %s,
                        enterprise_knowledge_manifest = %s,
                        enterprise_selection_fingerprint = %s,
                        available_at = %s, updated_at = %s
                    WHERE tenant_id = %s AND id = %s AND status = 'collecting'
                      AND enterprise_selection_version = %s
                    RETURNING *
                    """,
                    (
                        Jsonb(attachment_manifest),
                        Jsonb([item.snapshot_value() for item in refs]),
                        selection.fingerprint if selection else None,
                        now,
                        now,
                        tenant_id,
                        request_id,
                        request.enterprise_selection_version,
                    ),
                ).fetchone()
        if updated is None:
            raise ConsoleKnowledgeSelectionConflictError(
                "selection_version_conflict",
                "资料选择已在其他页面更新，请刷新后重试。",
            )
        return _request_from_row(updated)


class ConsoleKnowledgeSelectionService:
    """Expose safe metadata and freeze only an explicit owner selection."""

    def __init__(
        self,
        repository: ConsoleKnowledgeSelectionRepository,
        *,
        model_egress_policy: str = "deny",
        attachment_planning_enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if model_egress_policy not in {"allow", "deny"}:
            raise ValueError("enterprise knowledge model egress policy is invalid")
        self.repository = repository
        self.model_egress_policy = model_egress_policy
        self.attachment_planning_enabled = attachment_planning_enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def selection_enabled(self) -> bool:
        return True

    def catalog(self, principal: ConsolePrincipal) -> Mapping[str, Any]:
        items = self.repository.list_candidates(principal.tenant_id)
        return {
            "sources": [self._public_candidate(item) for item in items],
            "total": len(items),
            "selection_limit": MAX_SELECTED_ENTERPRISE_SOURCES,
        }

    def get(self, principal: ConsolePrincipal, request_id: str) -> Mapping[str, Any]:
        request = self.repository.get_for_owner(
            principal.tenant_id,
            request_id,
            requester_person_id=principal.person_id,
        )
        _require_collecting(request)
        candidates = {
            item.source_id: item
            for item in self.repository.list_candidates(principal.tenant_id)
        }
        return _selection_payload(request, candidates, self._public_candidate)

    def update(
        self,
        principal: ConsolePrincipal,
        request_id: str,
        *,
        source_ids: Any,
        expected_version: Any,
    ) -> Mapping[str, Any]:
        normalized = _normalized_source_ids(source_ids)
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise ValueError("expected_version must be a non-negative integer")
        if expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        request = self.repository.save_selection(
            principal.tenant_id,
            request_id,
            requester_person_id=principal.person_id,
            source_ids=normalized,
            expected_version=expected_version,
            now=_utc(self.clock()),
        )
        candidates = {
            item.source_id: item
            for item in self.repository.list_candidates(principal.tenant_id)
        }
        return _selection_payload(request, candidates, self._public_candidate)

    def generate(
        self,
        principal: ConsolePrincipal,
        request_id: str,
    ) -> Mapping[str, Any]:
        request = self.repository.freeze_and_queue(
            principal.tenant_id,
            request_id,
            requester_person_id=principal.person_id,
            model_egress_policy=self.model_egress_policy,
            attachment_planning_enabled=self.attachment_planning_enabled,
            now=_utc(self.clock()),
        )
        return {
            "request": {
                "id": request.id,
                "status": "queued",
                "enterprise_knowledge_count": len(
                    request.enterprise_knowledge_manifest
                ),
            }
        }

    def _public_candidate(self, ref: EnterpriseKnowledgeRef) -> dict[str, Any]:
        selectable = (
            self.model_egress_policy == "allow"
            and ref.content_authorized
            and ref.egress_decision == "allow"
        )
        reason = None
        if not selectable:
            reason = (
                "当前部署未允许企业资料模型外发"
                if self.model_egress_policy != "allow"
                else "该资料未获得模型外发授权"
            )
        return {
            "source_id": ref.source_id,
            "version_id": ref.version_id,
            "display_label": ref.display_label,
            "media_type": ref.media_type,
            "size_bytes": ref.size_bytes,
            "published_at": ref.published_at.isoformat(),
            "data_classification": ref.data_classification,
            "egress_decision": ref.egress_decision,
            "authorization_proof_id": ref.authorization_proof_id,
            "selectable": selectable,
            "unavailable_reason": reason,
        }


_PUBLISHED_SQL = """
    SELECT v.*, a.proof_id, a.proof_fingerprint
    FROM workflow_enterprise_knowledge_versions AS v
    LEFT JOIN workflow_enterprise_knowledge_authorizations AS a
      ON a.tenant_id = v.tenant_id
     AND a.source_id = v.source_id
     AND a.version_id = v.version_id
    WHERE v.tenant_id = %s AND v.status = 'published'
"""


def _ref_from_row(row: Mapping[str, Any]) -> EnterpriseKnowledgeRef:
    return EnterpriseKnowledgeRef(
        tenant_id=str(row["tenant_id"]),
        source_id=str(row["source_id"]),
        version_id=str(row["version_id"]),
        display_label=str(row["display_label"]),
        media_type=str(row["media_type"]),
        size_bytes=int(row["size_bytes"]),
        content_sha256=str(row["content_sha256"]),
        published_at=row["published_at"],
        data_classification=str(row["data_classification"]),
        egress_decision=str(row["model_egress_policy"]),
        authorization_proof_id=(str(row["proof_id"]) if row.get("proof_id") else None),
        authorization_fingerprint=(
            str(row["proof_fingerprint"]) if row.get("proof_fingerprint") else None
        ),
    )


def _attachment_ref_value(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "attachment_id": str(row["attachment_id"]),
        "source_id": f"attachment:{row['attachment_id']}",
        "display_filename": str(row["display_filename"]),
        "media_type": str(row["media_type"]),
        "size_bytes": int(row["size_bytes"]),
        "content_sha256": str(row["content_sha256"]),
        "data_classification": str(row["data_classification"]),
        "egress_decision": str(row["model_egress_policy"]),
    }


def _normalized_source_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("source_ids must be an array")
    if len(value) > MAX_SELECTED_ENTERPRISE_SOURCES:
        raise ValueError(
            f"source_ids accepts at most {MAX_SELECTED_ENTERPRISE_SOURCES} items"
        )
    normalized = []
    for item in value:
        if not isinstance(item, str) or _SOURCE_ID.fullmatch(item) is None:
            raise ValueError("source_ids contains an invalid source ID")
        normalized.append(item)
    if len(set(normalized)) != len(normalized):
        raise ConsoleKnowledgeSelectionConflictError(
            "duplicate_knowledge_source",
            "企业资料选择不能包含重复来源。",
        )
    return tuple(normalized)


def _require_visible_sources(
    source_ids: tuple[str, ...],
    candidates: tuple[EnterpriseKnowledgeRef, ...],
    *,
    existing_source_ids: tuple[str, ...] = (),
) -> None:
    visible = {item.source_id for item in candidates}
    existing = set(existing_source_ids)
    if any(
        source_id not in visible and source_id not in existing
        for source_id in source_ids
    ):
        raise ConsoleKnowledgeSelectionNotFoundError("knowledge source")


def _resolve_selection(
    tenant_id: str,
    source_ids: tuple[str, ...],
    candidates: tuple[EnterpriseKnowledgeRef, ...],
) -> tuple[EnterpriseKnowledgeRef, ...]:
    _require_visible_sources(source_ids, candidates)
    by_source = {item.source_id: item for item in candidates}
    refs = tuple(by_source[source_id] for source_id in source_ids)
    if any(
        ref.tenant_id != tenant_id
        or not ref.content_authorized
        or ref.data_classification != "internal"
        or ref.egress_decision != "allow"
        for ref in refs
    ):
        raise ConsoleKnowledgeSelectionConflictError(
            "knowledge_source_unavailable",
            "所选企业资料当前不可用于模型规划，请刷新后重试。",
        )
    return tuple(sorted(refs, key=lambda item: (item.source_id, item.version_id)))


def _require_collecting(request: ConsoleDraftRequest) -> None:
    if request.status != "collecting":
        raise ConsoleKnowledgeSelectionConflictError(
            "draft_not_collecting",
            "草稿请求已开始生成，企业资料选择不能再修改。",
        )


def _selection_payload(
    request: ConsoleDraftRequest,
    candidates: Mapping[str, EnterpriseKnowledgeRef],
    serializer: Callable[[EnterpriseKnowledgeRef], dict[str, Any]],
) -> dict[str, Any]:
    selected = [
        serializer(candidates[source_id])
        for source_id in request.enterprise_source_selection
        if source_id in candidates
    ]
    unavailable_selected = [
        {
            "source_id": source_id,
            "selectable": False,
            "unavailable_reason": "资料已撤销或不再可用，请取消选择后保存。",
        }
        for source_id in request.enterprise_source_selection
        if source_id not in candidates
    ]
    return {
        "request_id": request.id,
        "source_ids": list(request.enterprise_source_selection),
        "selection_version": request.enterprise_selection_version,
        "selected": selected,
        "unavailable_selected": unavailable_selected,
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("knowledge selection clock must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "ConsoleKnowledgeSelectionConflictError",
    "ConsoleKnowledgeSelectionNotFoundError",
    "ConsoleKnowledgeSelectionService",
    "InMemoryConsoleKnowledgeSelectionRepository",
    "MAX_KNOWLEDGE_SELECTION_BODY_BYTES",
    "MAX_SELECTED_ENTERPRISE_SOURCES",
    "PostgresConsoleKnowledgeSelectionRepository",
]
