"""Administrator session governance authorization and idempotency tests."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from larkflow.workflow.console import (
    ConsolePrincipal,
    ConsoleReadService,
    ConsoleResourceNotFoundError,
    StaticConsoleAuthenticator,
)
from larkflow.workflow.console_admin import (
    ConsoleAdminReadService,
    ConsoleAdminSnapshot,
)
from larkflow.workflow.console_admin_sessions import (
    AdminConsoleSession,
    AdminSessionRevocationConfirmation,
    AdminSessionRevocationEvent,
    AdminSessionRevocationPreview,
    ConsoleAdminSessionConflictError,
    ConsoleAdminSessionPreviewExpiredError,
    ConsoleAdminSessionPreviewStaleError,
    ConsoleAdminSessionService,
)
from larkflow.workflow.console_auth import (
    FeishuConsoleOAuthFlow,
    FeishuOAuthIdentity,
    InMemoryConsoleSessionAuthenticator,
    SESSION_COOKIE_NAME,
)
from larkflow.workflow.console_http import ConsoleHttpApplication
from larkflow.workflow.repository import InMemoryWorkflowRepository


NOW = datetime(2026, 8, 7, 13, 0, tzinfo=timezone.utc)
TENANT = "tenant_admin_sessions"
ADMIN = "ou_admin_private"
MEMBER = "ou_member_private"
TOKEN = "console-admin-token-with-at-least-thirty-two-characters"
CURRENT_SESSION = "1" * 32
TARGET_SESSION = "2" * 32
PREVIEW = "3" * 32
AUDIT = "4" * 32
PUBLIC_ORIGIN = "https://larkflow.example.test"


class FakeAdminRepository:
    def read_admin_snapshot(self, tenant_id, *, now):
        assert tenant_id == TENANT
        return ConsoleAdminSnapshot(
            instance_counts={},
            distinct_owners=0,
            active_sessions=2,
            active_session_people=2,
            sessions_expiring_within_hour=0,
            expired_sessions=0,
            queue_lanes=(),
            applied_migrations=(),
        )


class FakeSessionRepository:
    def __init__(self) -> None:
        self.sessions = {
            CURRENT_SESSION: AdminConsoleSession(
                id=CURRENT_SESSION,
                person_id=ADMIN,
                created_at=NOW - timedelta(minutes=5),
                expires_at=NOW + timedelta(hours=4),
            ),
            TARGET_SESSION: AdminConsoleSession(
                id=TARGET_SESSION,
                person_id=MEMBER,
                created_at=NOW - timedelta(minutes=10),
                expires_at=NOW + timedelta(hours=3),
            ),
        }
        self.previews: dict[str, AdminSessionRevocationPreview] = {}
        self.events: list[AdminSessionRevocationEvent] = []

    def list_active_sessions(self, tenant_id, *, now, limit):
        assert tenant_id == TENANT
        return tuple(
            session
            for session in self.sessions.values()
            if session.expires_at > now
        )[:limit]

    def list_recent_revocations(self, tenant_id, *, limit):
        assert tenant_id == TENANT
        return tuple(reversed(self.events[-limit:]))

    def create_revocation_preview(
        self,
        tenant_id,
        *,
        preview_id,
        actor_person_id,
        target_session_id,
        now,
        expires_at,
    ):
        assert tenant_id == TENANT
        target = self.sessions.get(target_session_id)
        if target is None or target.expires_at <= now:
            raise ConsoleResourceNotFoundError("console session")
        preview = AdminSessionRevocationPreview(
            id=preview_id,
            tenant_id=tenant_id,
            actor_person_id=actor_person_id,
            target_session_id=target.id,
            target_person_id=target.person_id,
            target_created_at=target.created_at,
            target_expires_at=target.expires_at,
            created_at=now,
            expires_at=expires_at,
        )
        self.previews[preview_id] = preview
        return preview

    def confirm_revocation(
        self,
        tenant_id,
        *,
        preview_id,
        actor_person_id,
        current_session_id,
        audit_id,
        now,
    ):
        assert tenant_id == TENANT
        preview = self.previews.get(preview_id)
        if preview is None or preview.actor_person_id != actor_person_id:
            raise ConsoleResourceNotFoundError("session revocation preview")
        if preview.consumed_at is not None:
            return AdminSessionRevocationConfirmation(preview, True)
        if preview.expires_at <= now:
            raise ConsoleAdminSessionPreviewExpiredError
        if preview.target_session_id == current_session_id:
            raise ConsoleAdminSessionConflictError
        target = self.sessions.get(preview.target_session_id)
        if (
            target is None
            or target.person_id != preview.target_person_id
            or target.created_at != preview.target_created_at
            or target.expires_at != preview.target_expires_at
            or target.expires_at <= now
        ):
            raise ConsoleAdminSessionPreviewStaleError
        del self.sessions[target.id]
        consumed = replace(preview, consumed_at=now, revoked_at=now)
        self.previews[preview_id] = consumed
        self.events.append(
            AdminSessionRevocationEvent(
                id=audit_id,
                actor_person_id=actor_person_id,
                target_person_id=target.person_id,
                target_session_id=target.id,
                occurred_at=now,
            )
        )
        return AdminSessionRevocationConfirmation(consumed, False)


class UnusedIdentityProvider:
    def exchange_code(self, code, *, code_verifier, redirect_uri):
        return FeishuOAuthIdentity("tenant-key", ADMIN)


def _services(*, token_factory=None):
    admin_service = ConsoleAdminReadService(
        FakeAdminRepository(),
        tenant_id=TENANT,
        allowed_person_ids=(ADMIN,),
        clock=lambda: NOW,
    )
    repository = FakeSessionRepository()
    values = iter((PREVIEW, AUDIT, "5" * 32))
    session_service = ConsoleAdminSessionService(
        repository,
        admin_service,
        clock=lambda: NOW,
        token_factory=token_factory or (lambda: next(values)),
    )
    return admin_service, session_service, repository


def test_session_list_is_tenant_scoped_sanitized_and_marks_current():
    _, service, repository = _services()
    repository.events.append(
        AdminSessionRevocationEvent(
            id=AUDIT,
            actor_person_id=ADMIN,
            target_person_id=MEMBER,
            target_session_id="5" * 32,
            occurred_at=NOW - timedelta(minutes=1),
        )
    )

    payload = service.list_sessions(
        ConsolePrincipal(TENANT, ADMIN),
        current_session_id=CURRENT_SESSION,
    )
    encoded = json.dumps(payload, ensure_ascii=False)

    assert payload["sessions"][0]["id"] == CURRENT_SESSION
    assert payload["sessions"][0]["current"] is True
    assert payload["sessions"][0]["revocable"] is False
    assert payload["sessions"][1]["relation"] == "member"
    assert payload["recent_revocations"][0]["actor_relation"] == "you"
    assert ADMIN not in encoded
    assert MEMBER not in encoded
    assert "person_id" not in encoded
    assert "credential" not in encoded


def test_revocation_requires_preview_refuses_current_and_is_idempotent():
    _, service, repository = _services()
    principal = ConsolePrincipal(TENANT, ADMIN)

    with pytest.raises(ConsoleAdminSessionConflictError):
        service.preview_revocation(
            principal,
            CURRENT_SESSION,
            current_session_id=CURRENT_SESSION,
        )

    preview = service.preview_revocation(
        principal,
        TARGET_SESSION,
        current_session_id=CURRENT_SESSION,
    )
    first = service.confirm_revocation(
        principal,
        preview["preview_id"],
        current_session_id=CURRENT_SESSION,
    )
    replay = service.confirm_revocation(
        principal,
        preview["preview_id"],
        current_session_id=CURRENT_SESSION,
    )

    assert preview["requires_confirmation"] is True
    assert first["status"] == "revoked"
    assert first["already_applied"] is False
    assert replay["already_applied"] is True
    assert TARGET_SESSION not in repository.sessions
    assert len(repository.events) == 1


def test_non_admin_session_governance_has_the_same_not_found_boundary():
    _, service, _ = _services()
    principal = ConsolePrincipal(TENANT, MEMBER)

    with pytest.raises(ConsoleResourceNotFoundError):
        service.list_sessions(principal, current_session_id=None)
    with pytest.raises(ConsoleResourceNotFoundError):
        service.preview_revocation(
            principal,
            TARGET_SESSION,
            current_session_id=None,
        )


def test_admin_http_write_requires_header_and_preserves_hidden_boundary():
    admin_service, session_service, _ = _services()
    application = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        StaticConsoleAuthenticator(
            TOKEN,
            ConsolePrincipal(TENANT, ADMIN),
        ),
        admin_service=admin_service,
        admin_session_service=session_service,
    )
    auth = {"Authorization": f"Bearer {TOKEN}"}
    action = {**auth, "X-Larkflow-Console-Action": "session-governance-v1"}

    listing = application.handle(
        "GET",
        "/console/api/v1/admin/sessions",
        headers=auth,
    )
    rejected = application.handle(
        "POST",
        f"/console/api/v1/admin/sessions/{TARGET_SESSION}/revoke-preview",
        headers=auth,
    )
    chunked = application.handle(
        "POST",
        f"/console/api/v1/admin/sessions/{TARGET_SESSION}/revoke-preview",
        headers={**action, "Transfer-Encoding": "chunked"},
    )
    preview = application.handle(
        "POST",
        f"/console/api/v1/admin/sessions/{TARGET_SESSION}/revoke-preview",
        headers=action,
    )
    confirm = application.handle(
        "POST",
        f"/console/api/v1/admin/session-revocations/{PREVIEW}/confirm",
        headers=action,
    )

    assert listing.status == 200
    assert rejected.status == 403
    assert json.loads(rejected.body)["error"]["code"] == "request_rejected"
    assert chunked.status == 400
    assert preview.status == 201
    assert confirm.status == 200

    member_admin = ConsoleAdminReadService(
        FakeAdminRepository(),
        tenant_id=TENANT,
        allowed_person_ids=(MEMBER,),
        clock=lambda: NOW,
    )
    member_sessions = ConsoleAdminSessionService(
        FakeSessionRepository(),
        member_admin,
        clock=lambda: NOW,
        token_factory=lambda: PREVIEW,
    )
    member_application = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        StaticConsoleAuthenticator(
            TOKEN,
            ConsolePrincipal(TENANT, ADMIN),
        ),
        admin_service=member_admin,
        admin_session_service=member_sessions,
    )
    hidden = member_application.handle(
        "POST",
        f"/console/api/v1/admin/sessions/{TARGET_SESSION}/revoke-preview",
        headers=action,
    )
    missing = member_application.handle(
        "POST",
        "/console/api/v1/admin/missing",
        headers=action,
    )
    assert hidden.status == missing.status == 404
    assert hidden.body == missing.body


def test_feishu_admin_write_requires_exact_same_origin():
    admin_service, session_service, _ = _services()
    sessions = InMemoryConsoleSessionAuthenticator(clock=lambda: NOW.timestamp())
    credential = sessions.issue(ConsolePrincipal(TENANT, ADMIN))
    current_session_id = sessions.authenticate_context(
        {"Cookie": f"{SESSION_COOKIE_NAME}={credential}"}
    ).session_id
    assert current_session_id is not None
    flow = FeishuConsoleOAuthFlow(
        app_id="cli_console_test",
        public_base_url=PUBLIC_ORIGIN,
        workflow_tenant_id=TENANT,
        allowed_feishu_tenant_key="tenant-key",
        identity_provider=UnusedIdentityProvider(),
        sessions=sessions,
        clock=lambda: NOW.timestamp(),
    )
    application = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        sessions,
        oauth=flow,
        admin_service=admin_service,
        admin_session_service=session_service,
    )
    headers = {
        "Cookie": f"{SESSION_COOKIE_NAME}={credential}",
        "X-Larkflow-Console-Action": "session-governance-v1",
    }
    path = f"/console/api/v1/admin/sessions/{TARGET_SESSION}/revoke-preview"

    assert application.handle("POST", path, headers=headers).status == 403
    assert application.handle(
        "POST",
        path,
        headers={**headers, "Origin": "https://attacker.example.test"},
    ).status == 403
    accepted = application.handle(
        "POST",
        path,
        headers={**headers, "Origin": PUBLIC_ORIGIN},
    )
    assert accepted.status == 201
