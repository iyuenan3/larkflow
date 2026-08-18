"""Durable natural-language workflow drafts for the employee workspace."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import re
import secrets
from threading import RLock
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from larkflow.planning.contracts import DraftGenerator
from larkflow.planning.context import AttachmentRef, ContextBundle

from .console import ConsolePrincipal
from .directory import DirectoryValidationError
from .draft_generation import (
    DraftGenerationRejected,
    MAX_WIZARD_TEXT_CHARS,
)
from .repository import InstanceAlreadyExistsError, InstanceNotFoundError
from .service import WorkflowService
from .template_service import (
    TemplateValidationError,
    inline_owner_roles,
    instantiate_inline_definition,
)


ConnectionFactory = Callable[[], Any]
_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_TERMINAL_STATUSES = {"ready", "rejected", "exhausted"}


class ConsoleDraftNotFoundError(KeyError):
    """A draft request is absent or does not belong to this principal."""


class ConsoleDraftConflictError(RuntimeError):
    """A request identifier or durable claim no longer matches."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InvalidConsoleDraftClaimError(RuntimeError):
    """A worker attempted to settle a claim it no longer owns."""


@dataclass(frozen=True)
class ConsoleDraftRequest:
    id: str
    tenant_id: str
    requester_person_id: str
    collaborator_person_id: str
    brief: str
    context: str
    status: str
    attempt_count: int
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    definition: Mapping[str, Any] | None = None
    instance_id: str | None = None
    completed_at: datetime | None = None
    last_error: str | None = None
    generation_deferred: bool = False
    attachment_manifest: tuple[AttachmentRef, ...] = ()


@dataclass(frozen=True)
class ConsoleDraftClaim:
    request: ConsoleDraftRequest
    claim_token: str


class ConsoleDraftRepository(Protocol):
    def create(self, request: ConsoleDraftRequest) -> ConsoleDraftRequest:
        ...

    def get_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> ConsoleDraftRequest:
        ...

    def list_for_owner(
        self,
        tenant_id: str,
        *,
        requester_person_id: str,
        limit: int,
    ) -> tuple[ConsoleDraftRequest, ...]:
        ...

    def claim(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[ConsoleDraftClaim, ...]:
        ...

    def mark_repairing(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        now: datetime,
    ) -> None:
        ...

    def save_candidate(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        definition: Mapping[str, Any],
        now: datetime,
    ) -> ConsoleDraftRequest:
        ...

    def mark_ready(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        instance_id: str,
        now: datetime,
    ) -> None:
        ...

    def mark_rejected(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        error: str,
        now: datetime,
    ) -> None:
        ...

    def mark_failed(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        error: str,
        now: datetime,
        retry_at: datetime,
        exhausted: bool,
    ) -> None:
        ...


class InMemoryConsoleDraftRepository:
    """Small deterministic repository used by offline tests and local fixtures."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ConsoleDraftRequest] = {}
        self._claims: dict[tuple[str, str], tuple[str, datetime]] = {}
        self._lock = RLock()

    def create(self, request: ConsoleDraftRequest) -> ConsoleDraftRequest:
        key = (request.tenant_id, request.id)
        with self._lock:
            current = self._items.get(key)
            if current is None:
                self._items[key] = request
                return request
            if _same_request(current, request):
                return current
            raise ConsoleDraftConflictError(
                "request_id_conflict",
                "草稿请求编号已用于另一项输入，请重新提交。",
            )

    def get_for_owner(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
    ) -> ConsoleDraftRequest:
        with self._lock:
            item = self._items.get((tenant_id, request_id))
            if item is None or not secrets.compare_digest(
                item.requester_person_id,
                requester_person_id,
            ):
                raise ConsoleDraftNotFoundError(request_id)
            return item

    def list_for_owner(
        self,
        tenant_id: str,
        *,
        requester_person_id: str,
        limit: int,
    ) -> tuple[ConsoleDraftRequest, ...]:
        with self._lock:
            items = [
                item
                for item in self._items.values()
                if item.tenant_id == tenant_id
                and secrets.compare_digest(
                    item.requester_person_id,
                    requester_person_id,
                )
            ]
        items.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return tuple(items[:limit])

    def queue_collecting(
        self,
        tenant_id: str,
        request_id: str,
        *,
        requester_person_id: str,
        manifest: tuple[AttachmentRef, ...],
        now: datetime,
    ) -> ConsoleDraftRequest:
        with self._lock:
            item = self._items.get((tenant_id, request_id))
            if item is None or not secrets.compare_digest(
                item.requester_person_id,
                requester_person_id,
            ):
                raise ConsoleDraftNotFoundError(request_id)
            if item.status != "collecting":
                raise ConsoleDraftConflictError(
                    "draft_not_collecting",
                    "草稿请求已开始生成，附件清单不能再修改。",
                )
            updated = replace(
                item,
                status="pending",
                attachment_manifest=tuple(manifest),
                available_at=now,
                updated_at=now,
            )
            self._items[(tenant_id, request_id)] = updated
            return updated

    def claim(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[ConsoleDraftClaim, ...]:
        _validate_claim(worker_id, limit, claim_ttl)
        claimed = []
        with self._lock:
            candidates = sorted(
                (
                    item
                    for item in self._items.values()
                    if item.tenant_id == tenant_id
                    and _claimable(
                        item,
                        self._claims.get((tenant_id, item.id)),
                        now,
                    )
                ),
                key=lambda item: (item.available_at, item.created_at, item.id),
            )
            for item in candidates[:limit]:
                token = secrets.token_urlsafe(24)
                active_status = "creating" if item.definition is not None else "generating"
                updated = replace(
                    item,
                    status=active_status,
                    attempt_count=item.attempt_count + 1,
                    updated_at=now,
                )
                self._items[(tenant_id, item.id)] = updated
                self._claims[(tenant_id, item.id)] = (token, now + claim_ttl)
                claimed.append(ConsoleDraftClaim(updated, token))
        return tuple(claimed)

    def mark_repairing(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        now: datetime,
    ) -> None:
        with self._lock:
            item = self._claimed(tenant_id, request_id, claim_token)
            if item.status != "generating":
                raise InvalidConsoleDraftClaimError(request_id)
            self._items[(tenant_id, request_id)] = replace(
                item,
                status="repairing",
                updated_at=now,
            )

    def save_candidate(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        definition: Mapping[str, Any],
        now: datetime,
    ) -> ConsoleDraftRequest:
        with self._lock:
            item = self._claimed(tenant_id, request_id, claim_token)
            if item.status not in {"generating", "repairing"} or item.definition:
                raise InvalidConsoleDraftClaimError(request_id)
            updated = replace(
                item,
                status="creating",
                definition=dict(definition),
                updated_at=now,
            )
            self._items[(tenant_id, request_id)] = updated
            return updated

    def mark_ready(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        instance_id: str,
        now: datetime,
    ) -> None:
        self._settle(
            tenant_id,
            request_id,
            claim_token,
            status="ready",
            now=now,
            instance_id=instance_id,
        )

    def mark_rejected(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        error: str,
        now: datetime,
    ) -> None:
        self._settle(
            tenant_id,
            request_id,
            claim_token,
            status="rejected",
            now=now,
            last_error=error,
        )

    def mark_failed(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        error: str,
        now: datetime,
        retry_at: datetime,
        exhausted: bool,
    ) -> None:
        with self._lock:
            item = self._claimed(tenant_id, request_id, claim_token)
            status = "exhausted" if exhausted else "failed"
            updated = replace(
                item,
                status=status,
                available_at=retry_at,
                updated_at=now,
                completed_at=now if exhausted else None,
                last_error=error,
            )
            self._items[(tenant_id, request_id)] = updated
            self._claims.pop((tenant_id, request_id), None)

    def _claimed(
        self,
        tenant_id: str,
        request_id: str,
        claim_token: str,
    ) -> ConsoleDraftRequest:
        key = (tenant_id, request_id)
        claim = self._claims.get(key)
        if claim is None or not secrets.compare_digest(claim[0], claim_token):
            raise InvalidConsoleDraftClaimError(request_id)
        return self._items[key]

    def _settle(
        self,
        tenant_id: str,
        request_id: str,
        claim_token: str,
        *,
        status: str,
        now: datetime,
        instance_id: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self._lock:
            item = self._claimed(tenant_id, request_id, claim_token)
            self._items[(tenant_id, request_id)] = replace(
                item,
                status=status,
                instance_id=instance_id or item.instance_id,
                updated_at=now,
                completed_at=now,
                last_error=last_error,
            )
            self._claims.pop((tenant_id, request_id), None)


class PostgresConsoleDraftRepository:
    """Persist requests and lease slow generation through PostgreSQL."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self.connection_factory = connection_factory

    def create(self, request: ConsoleDraftRequest) -> ConsoleDraftRequest:
        with self.connection_factory() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO workflow_console_draft_requests (
                        tenant_id, id, requester_person_id,
                        collaborator_person_id, brief, context, status,
                        generation_deferred, attachment_manifest,
                        attempt_count, available_at, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        0, %s, %s, %s
                    ) ON CONFLICT (tenant_id, id) DO NOTHING
                    """,
                    (
                        request.tenant_id,
                        request.id,
                        request.requester_person_id,
                        request.collaborator_person_id,
                        request.brief,
                        request.context,
                        request.status,
                        request.generation_deferred,
                        Jsonb(
                            [
                                item.snapshot_value()
                                for item in request.attachment_manifest
                            ]
                        ),
                        request.available_at,
                        request.created_at,
                        request.updated_at,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM workflow_console_draft_requests
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (request.tenant_id, request.id),
                ).fetchone()
        current = _request_from_row(row)
        if not _same_request(current, request):
            raise ConsoleDraftConflictError(
                "request_id_conflict",
                "草稿请求编号已用于另一项输入，请重新提交。",
            )
        return current

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
                WHERE tenant_id = %s AND id = %s
                  AND requester_person_id = %s
                """,
                (tenant_id, request_id, requester_person_id),
            ).fetchone()
        if row is None:
            raise ConsoleDraftNotFoundError(request_id)
        return _request_from_row(row)

    def list_for_owner(
        self,
        tenant_id: str,
        *,
        requester_person_id: str,
        limit: int,
    ) -> tuple[ConsoleDraftRequest, ...]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_console_draft_requests
                WHERE tenant_id = %s AND requester_person_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (tenant_id, requester_person_id, limit),
            ).fetchall()
        return tuple(_request_from_row(row) for row in rows)

    def claim(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[ConsoleDraftClaim, ...]:
        _validate_claim(worker_id, limit, claim_ttl)
        token = secrets.token_urlsafe(24)
        expires_at = now + claim_ttl
        with self.connection_factory() as connection:
            with connection.transaction():
                rows = connection.execute(
                    """
                    WITH selected AS (
                        SELECT tenant_id, id
                        FROM workflow_console_draft_requests
                        WHERE tenant_id = %s AND (
                            (
                                status IN ('pending', 'failed')
                                AND available_at <= %s
                                AND (
                                    claim_token IS NULL
                                    OR claim_expires_at IS NULL
                                    OR claim_expires_at <= %s
                                )
                            ) OR (
                                status IN ('generating', 'repairing', 'creating')
                                AND claim_expires_at <= %s
                            )
                        )
                        ORDER BY available_at, created_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    ), updated AS (
                        UPDATE workflow_console_draft_requests AS request
                        SET status = CASE
                                WHEN request.definition IS NULL
                                THEN 'generating' ELSE 'creating'
                            END,
                            attempt_count = request.attempt_count + 1,
                            claimed_by = %s,
                            claim_token = %s,
                            claim_expires_at = %s,
                            updated_at = %s
                        FROM selected
                        WHERE request.tenant_id = selected.tenant_id
                          AND request.id = selected.id
                        RETURNING request.*
                    )
                    SELECT * FROM updated
                    ORDER BY available_at, created_at, id
                    """,
                    (
                        tenant_id,
                        now,
                        now,
                        now,
                        limit,
                        worker_id,
                        token,
                        expires_at,
                        now,
                    ),
                ).fetchall()
        return tuple(
            ConsoleDraftClaim(_request_from_row(row), token) for row in rows
        )

    def mark_repairing(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        now: datetime,
    ) -> None:
        self._settle_claim(
            """
            UPDATE workflow_console_draft_requests
            SET status = 'repairing', updated_at = %s
            WHERE tenant_id = %s AND id = %s
              AND status = 'generating' AND claim_token = %s
            RETURNING id
            """,
            (now, tenant_id, request_id, claim_token),
            request_id,
        )

    def save_candidate(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        definition: Mapping[str, Any],
        now: datetime,
    ) -> ConsoleDraftRequest:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    UPDATE workflow_console_draft_requests
                    SET status = 'creating', definition = %s, updated_at = %s
                    WHERE tenant_id = %s AND id = %s
                      AND status IN ('generating', 'repairing')
                      AND definition IS NULL AND claim_token = %s
                    RETURNING *
                    """,
                    (
                        Jsonb(dict(definition)),
                        now,
                        tenant_id,
                        request_id,
                        claim_token,
                    ),
                ).fetchone()
        if row is None:
            raise InvalidConsoleDraftClaimError(request_id)
        return _request_from_row(row)

    def mark_ready(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        instance_id: str,
        now: datetime,
    ) -> None:
        self._settle_claim(
            """
            UPDATE workflow_console_draft_requests
            SET status = 'ready', instance_id = %s, completed_at = %s,
                updated_at = %s, last_error = NULL,
                claimed_by = NULL, claim_token = NULL, claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status = 'creating' AND definition IS NOT NULL
              AND claim_token = %s
            RETURNING id
            """,
            (
                instance_id,
                now,
                now,
                tenant_id,
                request_id,
                claim_token,
            ),
            request_id,
        )

    def mark_rejected(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        error: str,
        now: datetime,
    ) -> None:
        self._settle_claim(
            """
            UPDATE workflow_console_draft_requests
            SET status = 'rejected', completed_at = %s, updated_at = %s,
                last_error = %s,
                claimed_by = NULL, claim_token = NULL, claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status IN ('generating', 'repairing', 'creating')
              AND claim_token = %s
            RETURNING id
            """,
            (now, now, error, tenant_id, request_id, claim_token),
            request_id,
        )

    def mark_failed(
        self,
        tenant_id: str,
        request_id: str,
        *,
        claim_token: str,
        error: str,
        now: datetime,
        retry_at: datetime,
        exhausted: bool,
    ) -> None:
        status = "exhausted" if exhausted else "failed"
        self._settle_claim(
            """
            UPDATE workflow_console_draft_requests
            SET status = %s, available_at = %s, updated_at = %s,
                completed_at = CASE WHEN %s THEN %s ELSE NULL END,
                last_error = %s,
                claimed_by = NULL, claim_token = NULL, claim_expires_at = NULL
            WHERE tenant_id = %s AND id = %s
              AND status IN ('generating', 'repairing', 'creating')
              AND claim_token = %s
            RETURNING id
            """,
            (
                status,
                retry_at,
                now,
                exhausted,
                now,
                error,
                tenant_id,
                request_id,
                claim_token,
            ),
            request_id,
        )

    def _settle_claim(
        self,
        sql: str,
        parameters: tuple[Any, ...],
        request_id: str,
    ) -> None:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(sql, parameters).fetchone()
        if row is None:
            raise InvalidConsoleDraftClaimError(request_id)


class ConsoleDraftService:
    """Authorize workspace draft requests and expose only safe progress fields."""

    def __init__(
        self,
        repository: ConsoleDraftRepository,
        directory: Any = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.directory = directory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create(
        self,
        principal: ConsolePrincipal,
        *,
        request_id: str,
        brief: str,
        context: str,
        collaborator_person_id: str | None,
        defer_generation: bool = False,
    ) -> Mapping[str, Any]:
        request_id = _normalized_request_id(request_id)
        brief = _bounded_text(brief, required=True, field="brief")
        context = _bounded_text(context, required=False, field="context")
        collaborator = (collaborator_person_id or "").strip()
        if collaborator:
            self._validate_collaborator(principal, collaborator)
        else:
            collaborator = principal.person_id
        if not isinstance(defer_generation, bool):
            raise ValueError("defer_generation must be a boolean")
        now = _utc(self.clock())
        request = self.repository.create(
            ConsoleDraftRequest(
                id=request_id,
                tenant_id=principal.tenant_id,
                requester_person_id=principal.person_id,
                collaborator_person_id=collaborator,
                brief=brief,
                context=context,
                status="collecting" if defer_generation else "pending",
                attempt_count=0,
                available_at=now,
                created_at=now,
                updated_at=now,
                generation_deferred=defer_generation,
            )
        )
        return {"request": _public_request(request, include_brief=True)}

    def get(
        self,
        principal: ConsolePrincipal,
        request_id: str,
    ) -> Mapping[str, Any]:
        request = self.repository.get_for_owner(
            principal.tenant_id,
            _normalized_request_id(request_id),
            requester_person_id=principal.person_id,
        )
        return {"request": _public_request(request, include_brief=True)}

    def list(
        self,
        principal: ConsolePrincipal,
        *,
        limit: int = 10,
    ) -> Mapping[str, Any]:
        if limit < 1 or limit > 20:
            raise ValueError("draft request limit must be between 1 and 20")
        requests = self.repository.list_for_owner(
            principal.tenant_id,
            requester_person_id=principal.person_id,
            limit=limit,
        )
        return {
            "requests": [
                _public_request(item, include_brief=True) for item in requests
            ],
            "total": len(requests),
            "limit": limit,
        }

    def _validate_collaborator(
        self,
        principal: ConsolePrincipal,
        collaborator_person_id: str,
    ) -> None:
        getter = getattr(self.directory, "get_person", None)
        if not callable(getter):
            raise ConsoleDraftConflictError(
                "directory_unavailable",
                "企业成员目录暂时不可用，请稍后重试。",
            )
        try:
            person = getter(principal.tenant_id, collaborator_person_id)
        except (DirectoryValidationError, KeyError) as exc:
            raise ConsoleDraftConflictError(
                "collaborator_unavailable",
                "所选协作者当前不可用，请刷新成员列表后重试。",
            ) from exc
        if person.person_id != collaborator_person_id or not person.active:
            raise ConsoleDraftConflictError(
                "collaborator_unavailable",
                "所选协作者当前不可用，请刷新成员列表后重试。",
            )


@dataclass(frozen=True)
class ConsoleDraftWorkerReport:
    claimed: int = 0
    processed: int = 0
    rejected: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class ConsoleDraftWorker:
    """Generate, freeze and create one web-originated draft per durable claim."""

    def __init__(
        self,
        repository: ConsoleDraftRepository,
        service: WorkflowService,
        generator: DraftGenerator,
        *,
        tenant_id: str,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 1,
        claim_ttl: timedelta = timedelta(minutes=10),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
        max_attempts: int = 5,
        context_service: Any = None,
    ) -> None:
        _validate_claim(worker_id, claim_limit, claim_ttl)
        if not tenant_id.strip():
            raise ValueError("console draft worker tenant is required")
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("console draft retry delays are invalid")
        if max_attempts < 1:
            raise ValueError("console draft max_attempts must be positive")
        self.repository = repository
        self.service = service
        self.generator = generator
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.max_attempts = max_attempts
        self.context_service = context_service

    def run_once(self) -> ConsoleDraftWorkerReport:
        claims = self.repository.claim(
            self.tenant_id,
            worker_id=self.worker_id,
            now=_utc(self.clock()),
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
        )
        processed = rejected = failed = 0
        errors = []
        for claim in claims:
            try:
                self._apply(claim)
            except (DraftGenerationRejected, TemplateValidationError) as exc:
                rejected += 1
                self.repository.mark_rejected(
                    self.tenant_id,
                    claim.request.id,
                    claim_token=claim.claim_token,
                    error=f"{type(exc).__name__}: {exc}",
                    now=_utc(self.clock()),
                )
            except Exception as exc:
                failed += 1
                failed_at = _utc(self.clock())
                error = f"{claim.request.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.repository.mark_failed(
                    self.tenant_id,
                    claim.request.id,
                    claim_token=claim.claim_token,
                    error=error,
                    now=failed_at,
                    retry_at=failed_at
                    + _retry_delay(
                        claim.request.attempt_count,
                        self.retry_base,
                        self.retry_max,
                    ),
                    exhausted=claim.request.attempt_count >= self.max_attempts,
                )
            else:
                processed += 1
        return ConsoleDraftWorkerReport(
            claimed=len(claims),
            processed=processed,
            rejected=rejected,
            failed=failed,
            errors=tuple(errors),
        )

    def _apply(self, claim: ConsoleDraftClaim) -> None:
        request = claim.request
        context_bundle: ContextBundle | None = None
        if self.context_service is not None:
            context_bundle = self.context_service.build_for_planning(request)
        elif request.attachment_manifest:
            raise DraftGenerationRejected("附件上下文服务未配置")
        if request.attachment_manifest:
            if context_bundle is None:
                raise DraftGenerationRejected("附件上下文未能构建")
        definition = request.definition
        if definition is None:
            generation: dict[str, Any] = {
                "tenant_id": self.tenant_id,
                "actor_person_id": request.requester_person_id,
                "request_id": request.id,
                "brief": request.brief,
                "context": request.context,
                "on_repair": lambda: self.repository.mark_repairing(
                    self.tenant_id,
                    request.id,
                    claim_token=claim.claim_token,
                    now=_utc(self.clock()),
                ),
            }
            if context_bundle is not None:
                generation["context_bundle"] = context_bundle
            definition = self.generator.generate(
                **generation,
            )
            request = self.repository.save_candidate(
                self.tenant_id,
                request.id,
                claim_token=claim.claim_token,
                definition=definition,
                now=_utc(self.clock()),
            )
        available_bindings = {
            "requester": request.requester_person_id,
            "collaborator": request.collaborator_person_id,
        }
        roles = set(inline_owner_roles(definition))
        snapshot = instantiate_inline_definition(
            definition,
            owner_bindings={role: available_bindings[role] for role in roles},
        )
        if context_bundle is not None:
            inputs = dict(snapshot.inputs)
            manifest = context_bundle.snapshot_manifest()
            if manifest["attachments"]:
                inputs["project_attachments"] = manifest["attachments"]
            if manifest["enterprise_knowledge"]:
                inputs["enterprise_knowledge"] = manifest[
                    "enterprise_knowledge"
                ]
            inputs["context_manifest"] = {
                key: value
                for key, value in manifest.items()
                if key not in {"attachments", "enterprise_knowledge"}
            }
            inputs["context_manifest"]["source_kinds"] = ",".join(
                dict.fromkeys(item.kind for item in context_bundle.sources)
            )
            snapshot = replace(snapshot, inputs=inputs)
        instance_id = f"console_draft_{request.id}"
        try:
            instance = self.service.create_draft(
                instance_id=instance_id,
                tenant_id=self.tenant_id,
                owner_person_id=request.requester_person_id,
                actor_person_id=request.requester_person_id,
                snapshot=snapshot,
                correlation_id=request.id,
            )
        except InstanceAlreadyExistsError:
            try:
                instance = self.service.get(self.tenant_id, instance_id)
            except InstanceNotFoundError as exc:
                raise ConsoleDraftConflictError(
                    "existing_draft_unreadable",
                    "existing console draft cannot be read",
                ) from exc
            if (
                instance.owner_person_id != request.requester_person_id
                or instance.snapshot != snapshot
            ):
                raise ConsoleDraftConflictError(
                    "existing_draft_mismatch",
                    "request id is already bound to another draft",
                )
        if context_bundle is not None:
            self.context_service.promote(request, instance_id=instance.id)
        self.repository.mark_ready(
            self.tenant_id,
            request.id,
            claim_token=claim.claim_token,
            instance_id=instance.id,
            now=_utc(self.clock()),
        )


def _public_request(
    request: ConsoleDraftRequest,
    *,
    include_brief: bool,
) -> dict[str, Any]:
    status = {
        "collecting": "collecting",
        "pending": "queued",
        "generating": "generating",
        "repairing": "repairing",
        "creating": "preparing",
        "failed": "retrying",
        "ready": "ready",
        "rejected": "rejected",
        "exhausted": "failed",
    }[request.status]
    message = {
        "collecting": "可上传或撤销附件，确认后再开始生成",
        "queued": "已进入生成队列",
        "generating": "中央 Agent 正在生成候选流程",
        "repairing": "候选未通过校验，正在安全重生成",
        "preparing": "候选已通过校验，正在保存草稿",
        "retrying": "生成服务暂时不可用，系统将自动重试",
        "ready": "流程草稿已生成，等待你确认启动",
        "rejected": "当前描述未能生成安全草稿，请调整后重新提交",
        "failed": "生成服务多次失败，请稍后重新提交",
    }[status]
    if status == "rejected" and (request.last_error or "").startswith(
        "DraftCapabilityUnavailable:"
    ):
        message = (
            "当前没有支持 URL 引用的联网搜索后端。请上传完整资料并明确要求不联网，"
            "或联系管理员配置已验证的搜索后端后重试"
        )
    payload: dict[str, Any] = {
        "id": request.id,
        "status": status,
        "message": message,
        "collaborator_relation": (
            "you"
            if secrets.compare_digest(
                request.requester_person_id,
                request.collaborator_person_id,
            )
            else "collaborator"
        ),
        "instance_id": request.instance_id,
        "created_at": request.created_at.isoformat(),
        "updated_at": request.updated_at.isoformat(),
        "completed_at": (
            request.completed_at.isoformat()
            if request.completed_at is not None
            else None
        ),
        "attachment_count": len(request.attachment_manifest),
    }
    if include_brief:
        payload["brief"] = request.brief
    return payload


def _request_from_row(row: Mapping[str, Any] | None) -> ConsoleDraftRequest:
    if row is None:
        raise ConsoleDraftNotFoundError("draft request")
    definition = row.get("definition")
    raw_manifest = row.get("attachment_manifest") or []
    return ConsoleDraftRequest(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        requester_person_id=str(row["requester_person_id"]),
        collaborator_person_id=str(row["collaborator_person_id"]),
        brief=str(row["brief"]),
        context=str(row.get("context") or ""),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        available_at=row["available_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        definition=dict(definition) if isinstance(definition, Mapping) else None,
        instance_id=row.get("instance_id"),
        completed_at=row.get("completed_at"),
        last_error=row.get("last_error"),
        generation_deferred=bool(row.get("generation_deferred", False)),
        attachment_manifest=tuple(
            AttachmentRef(
                attachment_id=str(item["attachment_id"]),
                source_id=str(item["source_id"]),
                display_filename=str(item["display_filename"]),
                media_type=str(item["media_type"]),
                size_bytes=int(item["size_bytes"]),
                content_sha256=str(item["content_sha256"]),
                data_classification=str(item["data_classification"]),
                egress_decision=str(item["egress_decision"]),
            )
            for item in raw_manifest
        ),
    )


def _same_request(left: ConsoleDraftRequest, right: ConsoleDraftRequest) -> bool:
    return (
        left.tenant_id == right.tenant_id
        and left.id == right.id
        and left.requester_person_id == right.requester_person_id
        and left.collaborator_person_id == right.collaborator_person_id
        and left.brief == right.brief
        and left.context == right.context
        and left.generation_deferred == right.generation_deferred
    )


def _normalized_request_id(value: Any) -> str:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request_id must be 32 lowercase hexadecimal characters")
    return value


def _bounded_text(value: Any, *, required: bool, field: str) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{field} is required")
    if len(normalized) > MAX_WIZARD_TEXT_CHARS:
        raise ValueError(
            f"{field} exceeds {MAX_WIZARD_TEXT_CHARS} characters"
        )
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("console draft clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_claim(worker_id: str, limit: int, claim_ttl: timedelta) -> None:
    if not worker_id.strip():
        raise ValueError("console draft worker_id is required")
    if limit < 1 or limit > 100:
        raise ValueError("console draft claim limit must be between 1 and 100")
    if claim_ttl <= timedelta(0):
        raise ValueError("console draft claim_ttl must be positive")


def _claimable(
    item: ConsoleDraftRequest,
    claim: tuple[str, datetime] | None,
    now: datetime,
) -> bool:
    if item.status in _TERMINAL_STATUSES:
        return False
    if item.available_at > now:
        return False
    if claim is None:
        return item.status in {"pending", "failed"}
    return claim[1] <= now and item.status in {
        "generating",
        "repairing",
        "creating",
        "failed",
    }


def _retry_delay(
    attempt_count: int,
    base: timedelta,
    maximum: timedelta,
) -> timedelta:
    multiplier = 2 ** max(0, min(attempt_count - 1, 16))
    return min(maximum, base * multiplier)


__all__ = [
    "ConsoleDraftClaim",
    "ConsoleDraftConflictError",
    "ConsoleDraftNotFoundError",
    "ConsoleDraftRequest",
    "ConsoleDraftService",
    "ConsoleDraftWorker",
    "ConsoleDraftWorkerReport",
    "InMemoryConsoleDraftRepository",
    "InvalidConsoleDraftClaimError",
    "PostgresConsoleDraftRepository",
]
