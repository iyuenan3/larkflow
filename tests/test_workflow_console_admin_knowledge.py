"""Server-authorized immutable enterprise knowledge API tests."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import pytest

from larkflow.knowledge import (
    ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1,
    ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1,
)
from larkflow.knowledge.blob import InMemoryEnterpriseKnowledgeBlobStore
from larkflow.knowledge.repository import InMemoryEnterpriseKnowledgeRepository
from larkflow.workflow.console import (
    ConsoleAuthentication,
    ConsolePrincipal,
    ConsoleReadService,
    ConsoleResourceNotFoundError,
    StaticConsoleAuthenticator,
)
from larkflow.workflow.console_admin import (
    ConsoleAdminReadService,
    ConsoleAdminSnapshot,
)
from larkflow.workflow.console_admin_knowledge import (
    ConsoleAdminKnowledgeService,
)
from larkflow.workflow.console_http import ConsoleHttpApplication
from larkflow.workflow.console_http import _request_body_limit
from larkflow.workflow.repository import InMemoryWorkflowRepository


NOW = datetime(2026, 8, 19, 2, 0, tzinfo=timezone.utc)
TENANT = "tenant-admin-knowledge"
ADMIN = "ou_admin_private"
MEMBER = "ou_member_private"
TOKEN = "admin-knowledge-token-with-thirty-two-characters"
SOURCE_ID = "enterprise:release_policy"
VERSION_ID = "v1"
PUBLIC_ORIGIN = "https://console.example.test"


class EmptyAdminRepository:
    def read_admin_snapshot(self, tenant_id, *, now):
        return ConsoleAdminSnapshot(
            instance_counts={},
            distinct_owners=0,
            active_sessions=0,
            active_session_people=0,
            sessions_expiring_within_hour=0,
            expired_sessions=0,
            queue_lanes=(),
            applied_migrations=(),
        )


class FixedFeishuAuthenticator:
    mode = "feishu"

    def __init__(self, principal: ConsolePrincipal) -> None:
        self.principal = principal

    def authenticate(self, _headers):
        return self.principal

    def authenticate_context(self, _headers):
        return ConsoleAuthentication(self.principal, "1" * 32)


class PublicOriginOnly:
    public_base_url = PUBLIC_ORIGIN


def _services(*, principal_person_id: str = ADMIN, content_enabled: bool = True):
    authorizer = ConsoleAdminReadService(
        EmptyAdminRepository(),
        tenant_id=TENANT,
        allowed_person_ids=(ADMIN,),
        clock=lambda: NOW,
    )
    repository = InMemoryEnterpriseKnowledgeRepository()
    blob_store = InMemoryEnterpriseKnowledgeBlobStore()
    service = ConsoleAdminKnowledgeService(
        repository,
        authorizer,
        blob_store if content_enabled else None,
        clock=lambda: NOW,
    )
    principal = ConsolePrincipal(TENANT, principal_person_id)
    return authorizer, repository, blob_store, service, principal


def _publication_body(**overrides) -> bytes:
    content = "# 发布流程规范\n\n仅用于合成测试。"
    document = {
        "source_id": SOURCE_ID,
        "version_id": VERSION_ID,
        "display_label": "发布流程规范",
        "media_type": "text/markdown",
        "content": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "egress_decision": "deny",
        "authorization_statement": (
            ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1
        ),
        "authorization_policy_version": (
            ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1
        ),
    }
    document.update(overrides)
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _headers(body: bytes, *, action: str = "knowledge-governance-v1"):
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "X-Larkflow-Console-Action": action,
    }


def _app(*, principal_person_id: str = ADMIN) -> ConsoleHttpApplication:
    authorizer, _, _, service, principal = _services(
        principal_person_id=principal_person_id
    )
    return ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        StaticConsoleAuthenticator(TOKEN, principal),
        admin_service=authorizer,
        admin_knowledge_service=service,
    )


def test_service_keeps_tenant_actor_and_time_server_owned() -> None:
    _, repository, blob_store, service, principal = _services()
    content = "# 发布流程规范\n\n仅用于合成测试。"

    published = service.publish(
        principal,
        source_id=SOURCE_ID,
        version_id=VERSION_ID,
        display_label=" 发布流程规范 ",
        media_type="text/markdown",
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        egress_decision="deny",
        authorization_statement=ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1,
        authorization_policy_version=ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1,
    )
    listing = service.list_versions(principal)
    audit = service.audit(principal, SOURCE_ID, VERSION_ID)
    revoked = service.revoke(principal, SOURCE_ID, VERSION_ID)

    assert published["version"]["published_at"] == NOW.isoformat()
    assert published["version"]["display_label"] == "发布流程规范"
    assert listing["versions"][0]["status"] == "published"
    assert audit["events"][0]["actor_relation"] == "you"
    assert audit["events"][0]["snapshot"]["content_sha256"] == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()
    assert revoked["version"]["status"] == "revoked"
    encoded = json.dumps(
        {"published": published, "listing": listing, "audit": audit},
        ensure_ascii=False,
    )
    assert TENANT not in encoded
    assert ADMIN not in encoded
    assert "published_by_person_id" not in encoded
    assert "object_key" not in encoded
    assert "content\"" not in encoded
    assert ADMIN not in repr(stored := repository.list_versions(TENANT, limit=10)[0])
    assert stored.authorization_proof is not None
    assert blob_store.retained_usage() == (1, len(content.encode("utf-8")))
    assert stored.published_by_person_id == ADMIN


def test_non_admin_and_cross_tenant_are_hidden_before_repository_access() -> None:
    _, repository, _, service, _ = _services()
    content = "synthetic"

    for principal in (
        ConsolePrincipal(TENANT, MEMBER),
        ConsolePrincipal("other-tenant", ADMIN),
    ):
        with pytest.raises(ConsoleResourceNotFoundError):
            service.list_versions(principal)
        with pytest.raises(ConsoleResourceNotFoundError):
            service.publish(
                principal,
                source_id=SOURCE_ID,
                version_id=VERSION_ID,
                display_label="规范",
                media_type="text/plain",
                content=content,
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                egress_decision="deny",
                authorization_statement=(
                    ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1
                ),
                authorization_policy_version=(
                    ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1
                ),
            )

    assert repository.list_versions(TENANT, limit=10) == ()


def test_http_catalog_publish_list_audit_revoke_and_conflict() -> None:
    app = _app()
    body = _publication_body()
    headers = _headers(body)

    auth = json.loads(
        app.handle(
            "GET",
            "/console/api/v1/auth",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ).body
    )
    publish = app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers=headers,
        body=body,
    )
    conflict = app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers=_headers(_publication_body(version_id="v2")),
        body=_publication_body(version_id="v2"),
    )
    listing = app.handle(
        "GET",
        "/console/api/v1/admin/knowledge?limit=10",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    audit = app.handle(
        "GET",
        f"/console/api/v1/admin/knowledge/sources/{SOURCE_ID}/versions/{VERSION_ID}/audit",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    revoke = app.handle(
        "POST",
        f"/console/api/v1/admin/knowledge/sources/{SOURCE_ID}/versions/{VERSION_ID}/revoke",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Larkflow-Console-Action": "knowledge-governance-v1",
        },
    )

    assert auth["capabilities"]["enterprise_knowledge_catalog"] is True
    assert auth["capabilities"]["enterprise_knowledge_content_publication"] is True
    assert publish.status == 201
    assert conflict.status == 409
    assert json.loads(conflict.body)["error"]["code"] == "knowledge_conflict"
    assert json.loads(listing.body)["versions"][0]["source_id"] == SOURCE_ID
    assert json.loads(audit.body)["events"][0]["event_type"] == (
        "enterprise_knowledge.published"
    )
    assert json.loads(revoke.body)["version"]["status"] == "revoked"


def test_http_publication_rejects_unknown_identity_fields_and_wrong_action() -> None:
    app = _app()
    body = _publication_body(tenant_id="attacker-tenant")
    wrong_action = _publication_body()

    unknown = app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers=_headers(body),
        body=body,
    )
    rejected = app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers=_headers(wrong_action, action="session-governance-v1"),
        body=wrong_action,
    )

    assert unknown.status == 400
    assert rejected.status == 403


def test_http_non_admin_catalog_matches_unknown_route() -> None:
    app = _app(principal_person_id=MEMBER)
    auth_headers = {"Authorization": f"Bearer {TOKEN}"}
    body = _publication_body()

    auth = json.loads(
        app.handle("GET", "/console/api/v1/auth", headers=auth_headers).body
    )
    hidden_get = app.handle(
        "GET",
        "/console/api/v1/admin/knowledge",
        headers=auth_headers,
    )
    hidden_post = app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers=_headers(body),
        body=body,
    )
    missing = app.handle(
        "GET",
        "/console/api/v1/admin/missing",
        headers=auth_headers,
    )

    assert auth["capabilities"]["enterprise_knowledge_catalog"] is False
    assert hidden_get.status == hidden_post.status == missing.status == 404
    assert hidden_get.body == hidden_post.body == missing.body


def test_feishu_knowledge_write_requires_exact_origin() -> None:
    authorizer, _, _, service, principal = _services()
    app = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        FixedFeishuAuthenticator(principal),
        oauth=PublicOriginOnly(),  # type: ignore[arg-type]
        admin_service=authorizer,
        admin_knowledge_service=service,
    )
    body = _publication_body()
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "X-Larkflow-Console-Action": "knowledge-governance-v1",
    }

    assert app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers=headers,
        body=body,
    ).status == 403
    assert app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers={**headers, "Origin": "https://attacker.example.test"},
        body=body,
    ).status == 403
    assert app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers={**headers, "Origin": PUBLIC_ORIGIN},
        body=body,
    ).status == 201


def test_service_rejects_naive_clock_and_invalid_json_types() -> None:
    authorizer, _, _, _, principal = _services()
    service = ConsoleAdminKnowledgeService(
        InMemoryEnterpriseKnowledgeRepository(),
        authorizer,
        InMemoryEnterpriseKnowledgeBlobStore(),
        clock=lambda: datetime(2026, 8, 19, 2, 0),
    )
    content = "synthetic"

    with pytest.raises(ValueError, match="timezone-aware"):
        service.publish(
            principal,
            source_id=SOURCE_ID,
            version_id=VERSION_ID,
            display_label="规范",
            media_type="text/plain",
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            egress_decision="deny",
            authorization_statement=ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1,
            authorization_policy_version=ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1,
        )
    app = _app()
    body = _publication_body(content=True)
    response = app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers=_headers(body),
        body=body,
    )
    assert response.status == 400


def test_publication_requires_exact_authorization_and_matching_hash() -> None:
    app = _app()

    boolean = _publication_body(authorization_statement=True)
    wrong_hash = _publication_body(content_sha256="a" * 64)

    assert app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers=_headers(boolean),
        body=boolean,
    ).status == 400
    mismatch = app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers=_headers(wrong_hash),
        body=wrong_hash,
    )
    assert mismatch.status == 400
    assert json.loads(mismatch.body)["error"]["code"] == "invalid_request"


def test_identical_publication_retry_reuses_version_and_blob() -> None:
    _, repository, blob_store, service, principal = _services()
    content = "synthetic tenant-wide policy"
    arguments = {
        "source_id": SOURCE_ID,
        "version_id": VERSION_ID,
        "display_label": "Synthetic policy",
        "media_type": "text/plain",
        "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "egress_decision": "allow",
        "authorization_statement": (
            ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1
        ),
        "authorization_policy_version": (
            ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1
        ),
    }

    first = service.publish(principal, **arguments)
    second = service.publish(principal, **arguments)

    assert second == first
    assert blob_store.retained_usage() == (1, len(content.encode()))
    assert len(repository.list_audit(TENANT, SOURCE_ID, VERSION_ID)) == 1


def test_disabled_content_store_rejects_before_publication() -> None:
    authorizer, repository, _, service, principal = _services(
        content_enabled=False
    )
    app = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        StaticConsoleAuthenticator(TOKEN, principal),
        admin_service=authorizer,
        admin_knowledge_service=service,
    )
    body = _publication_body()

    auth = json.loads(
        app.handle(
            "GET",
            "/console/api/v1/auth",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ).body
    )
    response = app.handle(
        "POST",
        "/console/api/v1/admin/knowledge/publications",
        headers=_headers(body),
        body=body,
    )

    assert auth["capabilities"]["enterprise_knowledge_catalog"] is True
    assert auth["capabilities"]["enterprise_knowledge_content_publication"] is False
    assert response.status == 503
    assert json.loads(response.body)["error"]["code"] == (
        "knowledge_content_unavailable"
    )
    assert repository.list_versions(TENANT, limit=10) == ()


def test_knowledge_publication_has_an_independent_body_budget() -> None:
    assert _request_body_limit(
        "/console/api/v1/admin/knowledge/publications"
    ) == 262_144
    assert _request_body_limit("/console/api/v1/admin/knowledge") == 65_536
