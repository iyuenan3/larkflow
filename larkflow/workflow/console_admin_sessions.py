"""Previewed, audited Console session revocation for tenant administrators."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import secrets
from typing import Any, Protocol

from .console import ConsolePrincipal, ConsoleResourceNotFoundError
from .console_admin import ConsoleAdminReadService


ConnectionFactory = Callable[[], Any]
_REFERENCE = re.compile(r"^[0-9a-f]{32}$")


class ConsoleAdminSessionConflictError(RuntimeError):
    """A session governance action conflicts with the current request."""


class ConsoleAdminSessionPreviewExpiredError(RuntimeError):
    """A durable revocation preview is no longer confirmable."""


class ConsoleAdminSessionPreviewStaleError(RuntimeError):
    """The target session changed after its preview was created."""


@dataclass(frozen=True)
class AdminConsoleSession:
    id: str
    person_id: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class AdminSessionRevocationEvent:
    id: str
    actor_person_id: str
    target_person_id: str
    target_session_id: str
    occurred_at: datetime


@dataclass(frozen=True)
class AdminSessionRevocationPreview:
    id: str
    tenant_id: str
    actor_person_id: str
    target_session_id: str
    target_person_id: str
    target_created_at: datetime
    target_expires_at: datetime
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class AdminSessionRevocationConfirmation:
    preview: AdminSessionRevocationPreview
    already_applied: bool


class ConsoleAdminSessionRepository(Protocol):
    def list_active_sessions(
        self,
        tenant_id: str,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[AdminConsoleSession, ...]:
        """List bounded active sessions for exactly one tenant."""

    def list_recent_revocations(
        self,
        tenant_id: str,
        *,
        limit: int,
    ) -> tuple[AdminSessionRevocationEvent, ...]:
        """List append-only session revocation audit for one tenant."""

    def create_revocation_preview(
        self,
        tenant_id: str,
        *,
        preview_id: str,
        actor_person_id: str,
        target_session_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> AdminSessionRevocationPreview:
        """Freeze one active session as a short-lived preview."""

    def confirm_revocation(
        self,
        tenant_id: str,
        *,
        preview_id: str,
        actor_person_id: str,
        current_session_id: str | None,
        audit_id: str,
        now: datetime,
    ) -> AdminSessionRevocationConfirmation:
        """Atomically revoke once or return the prior confirmation."""


class PostgresConsoleAdminSessionRepository:
    """Persist session governance previews and audit in PostgreSQL."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self.connection_factory = connection_factory

    def list_active_sessions(
        self,
        tenant_id: str,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[AdminConsoleSession, ...]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, person_id, created_at, expires_at
                FROM workflow_console_sessions
                WHERE tenant_id = %s AND expires_at > %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (tenant_id, now, limit),
            ).fetchall()
        return tuple(
            AdminConsoleSession(
                id=row["id"],
                person_id=row["person_id"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
            )
            for row in rows
        )

    def list_recent_revocations(
        self,
        tenant_id: str,
        *,
        limit: int,
    ) -> tuple[AdminSessionRevocationEvent, ...]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT id, actor_person_id, target_person_id,
                       target_session_id, occurred_at
                FROM workflow_console_session_events
                WHERE tenant_id = %s AND event_type = 'session.revoked'
                ORDER BY occurred_at DESC, id DESC
                LIMIT %s
                """,
                (tenant_id, limit),
            ).fetchall()
        return tuple(
            AdminSessionRevocationEvent(
                id=row["id"],
                actor_person_id=row["actor_person_id"],
                target_person_id=row["target_person_id"],
                target_session_id=row["target_session_id"],
                occurred_at=row["occurred_at"],
            )
            for row in rows
        )

    def create_revocation_preview(
        self,
        tenant_id: str,
        *,
        preview_id: str,
        actor_person_id: str,
        target_session_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> AdminSessionRevocationPreview:
        with self.connection_factory() as connection:
            with connection.transaction():
                target = connection.execute(
                    """
                    SELECT id, person_id, created_at, expires_at
                    FROM workflow_console_sessions
                    WHERE tenant_id = %s AND id = %s AND expires_at > %s
                    FOR UPDATE
                    """,
                    (tenant_id, target_session_id, now),
                ).fetchone()
                if target is None:
                    raise ConsoleResourceNotFoundError("console session")
                row = connection.execute(
                    """
                    INSERT INTO workflow_console_session_revocation_previews (
                        tenant_id, id, actor_person_id, target_session_id,
                        target_person_id, target_created_at, target_expires_at,
                        created_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        tenant_id,
                        preview_id,
                        actor_person_id,
                        target_session_id,
                        target["person_id"],
                        target["created_at"],
                        target["expires_at"],
                        now,
                        expires_at,
                    ),
                ).fetchone()
        if row is None:
            raise RuntimeError("session revocation preview was not created")
        return _preview_from_row(row)

    def confirm_revocation(
        self,
        tenant_id: str,
        *,
        preview_id: str,
        actor_person_id: str,
        current_session_id: str | None,
        audit_id: str,
        now: datetime,
    ) -> AdminSessionRevocationConfirmation:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT *
                    FROM workflow_console_session_revocation_previews
                    WHERE tenant_id = %s AND id = %s AND actor_person_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, preview_id, actor_person_id),
                ).fetchone()
                if row is None:
                    raise ConsoleResourceNotFoundError("session revocation preview")
                preview = _preview_from_row(row)
                if preview.consumed_at is not None:
                    return AdminSessionRevocationConfirmation(
                        preview=preview,
                        already_applied=True,
                    )
                if preview.expires_at <= now:
                    raise ConsoleAdminSessionPreviewExpiredError(
                        "session revocation preview expired"
                    )
                if (
                    current_session_id is not None
                    and secrets.compare_digest(
                        preview.target_session_id,
                        current_session_id,
                    )
                ):
                    raise ConsoleAdminSessionConflictError(
                        "the current session must be ended with logout"
                    )
                deleted = connection.execute(
                    """
                    DELETE FROM workflow_console_sessions
                    WHERE tenant_id = %s
                      AND id = %s
                      AND person_id = %s
                      AND created_at = %s
                      AND expires_at = %s
                      AND expires_at > %s
                    RETURNING id
                    """,
                    (
                        tenant_id,
                        preview.target_session_id,
                        preview.target_person_id,
                        preview.target_created_at,
                        preview.target_expires_at,
                        now,
                    ),
                ).fetchone()
                if deleted is None:
                    raise ConsoleAdminSessionPreviewStaleError(
                        "session changed after revocation preview"
                    )
                consumed = connection.execute(
                    """
                    UPDATE workflow_console_session_revocation_previews
                    SET consumed_at = %s, revoked_at = %s
                    WHERE tenant_id = %s AND id = %s AND consumed_at IS NULL
                    RETURNING *
                    """,
                    (now, now, tenant_id, preview_id),
                ).fetchone()
                if consumed is None:
                    raise RuntimeError("session revocation preview changed")
                connection.execute(
                    """
                    INSERT INTO workflow_console_session_events (
                        tenant_id, id, event_type, actor_person_id,
                        target_person_id, target_session_id, preview_id,
                        occurred_at
                    ) VALUES (%s, %s, 'session.revoked', %s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        audit_id,
                        actor_person_id,
                        preview.target_person_id,
                        preview.target_session_id,
                        preview_id,
                        now,
                    ),
                )
        return AdminSessionRevocationConfirmation(
            preview=_preview_from_row(consumed),
            already_applied=False,
        )


class ConsoleAdminSessionService:
    """Expose sanitized session governance through one admin authorizer."""

    def __init__(
        self,
        repository: ConsoleAdminSessionRepository,
        authorizer: ConsoleAdminReadService,
        *,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
        preview_ttl_seconds: int = 300,
    ) -> None:
        if preview_ttl_seconds < 60 or preview_ttl_seconds > 900:
            raise ValueError("preview TTL must be between 60 and 900 seconds")
        self.repository = repository
        self.authorizer = authorizer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))
        self.preview_ttl_seconds = preview_ttl_seconds

    def list_sessions(
        self,
        principal: ConsolePrincipal,
        *,
        current_session_id: str | None,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._authorize(principal)
        if limit < 1 or limit > 100:
            raise ValueError("session limit must be between 1 and 100")
        current_session_id = _optional_reference(current_session_id)
        now = self._now()
        sessions = self.repository.list_active_sessions(
            principal.tenant_id,
            now=now,
            limit=limit,
        )
        events = self.repository.list_recent_revocations(
            principal.tenant_id,
            limit=20,
        )
        safe_sessions = [
            self._session_payload(
                item,
                principal=principal,
                current_session_id=current_session_id,
                now=now,
            )
            for item in sessions
        ]
        safe_sessions.sort(key=lambda item: not item["current"])
        return {
            "scope": "current_tenant",
            "generated_at": now.isoformat(),
            "sessions": safe_sessions,
            "total": len(safe_sessions),
            "limit": limit,
            "recent_revocations": [
                {
                    "id": event.id,
                    "actor_relation": _relation(
                        event.actor_person_id,
                        principal.person_id,
                    ),
                    "target_relation": _relation(
                        event.target_person_id,
                        principal.person_id,
                    ),
                    "target_session_id": event.target_session_id,
                    "occurred_at": _aware_utc(event.occurred_at).isoformat(),
                }
                for event in events
            ],
        }

    def preview_revocation(
        self,
        principal: ConsolePrincipal,
        target_session_id: str,
        *,
        current_session_id: str | None,
    ) -> dict[str, Any]:
        self._authorize(principal)
        target_session_id = _reference(target_session_id)
        current_session_id = _optional_reference(current_session_id)
        if current_session_id is not None and secrets.compare_digest(
            target_session_id,
            current_session_id,
        ):
            raise ConsoleAdminSessionConflictError(
                "the current session must be ended with logout"
            )
        now = self._now()
        preview = self.repository.create_revocation_preview(
            principal.tenant_id,
            preview_id=self._new_reference(),
            actor_person_id=principal.person_id,
            target_session_id=target_session_id,
            now=now,
            expires_at=now + timedelta(seconds=self.preview_ttl_seconds),
        )
        return self._preview_payload(preview, principal)

    def confirm_revocation(
        self,
        principal: ConsolePrincipal,
        preview_id: str,
        *,
        current_session_id: str | None,
    ) -> dict[str, Any]:
        self._authorize(principal)
        preview_id = _reference(preview_id)
        current_session_id = _optional_reference(current_session_id)
        confirmation = self.repository.confirm_revocation(
            principal.tenant_id,
            preview_id=preview_id,
            actor_person_id=principal.person_id,
            current_session_id=current_session_id,
            audit_id=self._new_reference(),
            now=self._now(),
        )
        return {
            "preview_id": confirmation.preview.id,
            "status": "revoked",
            "already_applied": confirmation.already_applied,
            "revoked_at": _aware_utc(
                confirmation.preview.revoked_at
            ).isoformat(),
            "target": self._preview_target(confirmation.preview, principal),
        }

    def _authorize(self, principal: ConsolePrincipal) -> None:
        if not self.authorizer.is_admin(principal):
            raise ConsoleResourceNotFoundError("admin session governance")

    def _now(self) -> datetime:
        return _aware_utc(self._clock())

    def _new_reference(self) -> str:
        return _reference(self._token_factory())

    @staticmethod
    def _session_payload(
        session: AdminConsoleSession,
        *,
        principal: ConsolePrincipal,
        current_session_id: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        current = current_session_id is not None and secrets.compare_digest(
            session.id,
            current_session_id,
        )
        return {
            "id": session.id,
            "relation": _relation(session.person_id, principal.person_id),
            "current": current,
            "revocable": not current,
            "created_at": _aware_utc(session.created_at).isoformat(),
            "expires_at": _aware_utc(session.expires_at).isoformat(),
            "expiring_within_hour": session.expires_at <= now + timedelta(hours=1),
        }

    @staticmethod
    def _preview_target(
        preview: AdminSessionRevocationPreview,
        principal: ConsolePrincipal,
    ) -> dict[str, Any]:
        return {
            "id": preview.target_session_id,
            "relation": _relation(
                preview.target_person_id,
                principal.person_id,
            ),
            "created_at": _aware_utc(preview.target_created_at).isoformat(),
            "expires_at": _aware_utc(preview.target_expires_at).isoformat(),
        }

    def _preview_payload(
        self,
        preview: AdminSessionRevocationPreview,
        principal: ConsolePrincipal,
    ) -> dict[str, Any]:
        return {
            "preview_id": preview.id,
            "effect": "revoke_one_console_session",
            "requires_confirmation": True,
            "expires_at": _aware_utc(preview.expires_at).isoformat(),
            "target": self._preview_target(preview, principal),
        }


def _preview_from_row(row: dict[str, Any]) -> AdminSessionRevocationPreview:
    return AdminSessionRevocationPreview(
        id=row["id"],
        tenant_id=row["tenant_id"],
        actor_person_id=row["actor_person_id"],
        target_session_id=row["target_session_id"],
        target_person_id=row["target_person_id"],
        target_created_at=row["target_created_at"],
        target_expires_at=row["target_expires_at"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        consumed_at=row.get("consumed_at"),
        revoked_at=row.get("revoked_at"),
    )


def _relation(person_id: str, current_person_id: str) -> str:
    return "you" if secrets.compare_digest(person_id, current_person_id) else "member"


def _reference(value: str) -> str:
    value = str(value).strip()
    if _REFERENCE.fullmatch(value) is None:
        raise ValueError("session reference is invalid")
    return value


def _optional_reference(value: str | None) -> str | None:
    return None if value is None else _reference(value)


def _aware_utc(value: datetime | None) -> datetime:
    if value is None or value.tzinfo is None:
        raise ValueError("session governance timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
