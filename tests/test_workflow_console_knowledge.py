"""Explicit enterprise knowledge selection at the Console draft boundary."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from larkflow.knowledge import (
    EnterpriseKnowledgeAuthorizationProof,
    EnterpriseKnowledgePublication,
    EnterpriseKnowledgeRef,
)
from larkflow.knowledge.blob import InMemoryEnterpriseKnowledgeBlobStore
from larkflow.knowledge.repository import InMemoryEnterpriseKnowledgeRepository
from larkflow.workflow.console import (
    ConsolePrincipal,
    ConsoleReadService,
    StaticConsoleAuthenticator,
)
from larkflow.workflow.console_drafts import (
    ConsoleDraftService,
    InMemoryConsoleDraftRepository,
)
from larkflow.workflow.console_http import ConsoleHttpApplication
from larkflow.workflow.console_knowledge import (
    ConsoleKnowledgeSelectionConflictError,
    ConsoleKnowledgeSelectionNotFoundError,
    ConsoleKnowledgeSelectionService,
    InMemoryConsoleKnowledgeSelectionRepository,
    MAX_SELECTED_ENTERPRISE_SOURCES,
)
from larkflow.workflow.knowledge_context import (
    EnterpriseKnowledgeContextService,
    PlanningKnowledgeContextService,
)
from larkflow.workflow.repository import InMemoryWorkflowRepository


NOW = datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc)
TENANT = "tenant-a"
OWNER = "person-owner"
OTHER = "person-other"
REQUEST_ID = "a123456789abcdef0123456789abcdef"
TOKEN = "knowledge-selection-token-with-thirty-two-chars"


def _publication(
    source_id: str = "enterprise:policy",
    *,
    version_id: str = "v1",
    tenant_id: str = TENANT,
    egress: str = "allow",
) -> EnterpriseKnowledgePublication:
    body = f"Synthetic content for {source_id} {version_id}.".encode()
    digest = hashlib.sha256(body).hexdigest()
    proof = EnterpriseKnowledgeAuthorizationProof(
        tenant_id=tenant_id,
        source_id=source_id,
        version_id=version_id,
        content_sha256=digest,
        authorized_by_person_id="private-admin-id",
        authorized_at=NOW,
    )
    return EnterpriseKnowledgePublication(
        ref=EnterpriseKnowledgeRef(
            tenant_id=tenant_id,
            source_id=source_id,
            version_id=version_id,
            display_label=f"Policy {source_id}",
            media_type="text/plain",
            size_bytes=len(body),
            content_sha256=digest,
            published_at=NOW,
            egress_decision=egress,
            authorization_proof_id=proof.proof_id,
            authorization_fingerprint=proof.fingerprint,
        ),
        published_by_person_id="private-admin-id",
        authorization_proof=proof,
    )


def _fixture(*, policy: str = "allow"):
    drafts = InMemoryConsoleDraftRepository()
    knowledge = InMemoryEnterpriseKnowledgeRepository()
    publication = knowledge.publish(_publication())
    ConsoleDraftService(drafts, clock=lambda: NOW).create(
        ConsolePrincipal(TENANT, OWNER),
        request_id=REQUEST_ID,
        brief="Use explicitly selected policy material",
        context="Do not use unselected enterprise sources",
        collaborator_person_id=None,
        defer_generation=True,
    )
    repository = InMemoryConsoleKnowledgeSelectionRepository(drafts, knowledge)
    service = ConsoleKnowledgeSelectionService(
        repository,
        model_egress_policy=policy,
        clock=lambda: NOW,
    )
    return drafts, knowledge, publication, service


def _principal(person_id: str = OWNER, tenant_id: str = TENANT):
    return ConsolePrincipal(tenant_id, person_id)


def test_member_catalog_is_tenant_bound_and_does_not_leak_content_or_proof_body():
    _drafts, knowledge, publication, service = _fixture()
    knowledge.publish(_publication("enterprise:other", tenant_id="tenant-b"))

    payload = service.catalog(_principal())

    assert payload["total"] == 1
    assert payload["sources"][0] == {
        "source_id": publication.ref.source_id,
        "version_id": "v1",
        "display_label": publication.ref.display_label,
        "media_type": "text/plain",
        "size_bytes": publication.ref.size_bytes,
        "published_at": NOW.isoformat(),
        "data_classification": "internal",
        "egress_decision": "allow",
        "authorization_proof_id": publication.ref.authorization_proof_id,
        "selectable": True,
        "unavailable_reason": None,
    }
    encoded = json.dumps(payload)
    for forbidden in (
        "content_sha256",
        "authorization_fingerprint",
        "private-admin-id",
        "object_key",
        "tenant-a",
        "Synthetic content",
    ):
        assert forbidden not in encoded


def test_selection_is_owner_only_versioned_idempotent_and_bounded():
    _drafts, _knowledge, publication, service = _fixture()
    first = service.update(
        _principal(),
        REQUEST_ID,
        source_ids=[publication.ref.source_id],
        expected_version=0,
    )
    repeated = service.update(
        _principal(),
        REQUEST_ID,
        source_ids=[publication.ref.source_id],
        expected_version=0,
    )

    assert first["selection_version"] == 1
    assert repeated == first
    with pytest.raises(ConsoleKnowledgeSelectionNotFoundError):
        service.get(_principal(OTHER), REQUEST_ID)
    with pytest.raises(ConsoleKnowledgeSelectionNotFoundError):
        service.get(_principal(OWNER, "tenant-b"), REQUEST_ID)
    with pytest.raises(ConsoleKnowledgeSelectionConflictError) as duplicate:
        service.update(
            _principal(),
            REQUEST_ID,
            source_ids=[publication.ref.source_id, publication.ref.source_id],
            expected_version=1,
        )
    assert duplicate.value.code == "duplicate_knowledge_source"
    with pytest.raises(ValueError, match="at most"):
        service.update(
            _principal(),
            REQUEST_ID,
            source_ids=[f"enterprise:source{i}" for i in range(MAX_SELECTED_ENTERPRISE_SOURCES + 1)],
            expected_version=1,
        )


def test_competing_selection_updates_allow_only_one_new_value():
    _drafts, knowledge, first, service = _fixture()
    second = knowledge.publish(_publication("enterprise:operations"))

    def update(source_id: str):
        try:
            return service.update(
                _principal(),
                REQUEST_ID,
                source_ids=[source_id],
                expected_version=0,
            )["source_ids"]
        except ConsoleKnowledgeSelectionConflictError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(update, (first.ref.source_id, second.ref.source_id)))

    assert sum(item == "selection_version_conflict" for item in results) == 1
    assert sum(isinstance(item, list) for item in results) == 1


def test_generation_freezes_exact_refs_and_retry_does_not_select_new_version():
    drafts, knowledge, publication, service = _fixture()
    service.update(
        _principal(),
        REQUEST_ID,
        source_ids=[publication.ref.source_id],
        expected_version=0,
    )
    queued = service.generate(_principal(), REQUEST_ID)
    frozen = drafts.get_for_owner(TENANT, REQUEST_ID, requester_person_id=OWNER)

    assert queued["request"]["enterprise_knowledge_count"] == 1
    assert frozen.enterprise_knowledge_manifest == (publication.ref,)
    assert len(frozen.enterprise_selection_fingerprint or "") == 64
    knowledge.revoke(
        TENANT,
        publication.ref.source_id,
        "v1",
        actor_person_id="private-admin-id",
        now=NOW + timedelta(minutes=1),
    )
    knowledge.publish(_publication(version_id="v2"))

    assert service.generate(_principal(), REQUEST_ID) == queued
    assert drafts.get_for_owner(
        TENANT,
        REQUEST_ID,
        requester_person_id=OWNER,
    ).enterprise_knowledge_manifest == (publication.ref,)


def test_generation_fails_closed_when_selected_source_is_revoked_or_egress_denied():
    _drafts, knowledge, publication, service = _fixture()
    service.update(
        _principal(),
        REQUEST_ID,
        source_ids=[publication.ref.source_id],
        expected_version=0,
    )
    knowledge.revoke(
        TENANT,
        publication.ref.source_id,
        publication.ref.version_id,
        actor_person_id="private-admin-id",
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ConsoleKnowledgeSelectionNotFoundError):
        service.generate(_principal(), REQUEST_ID)

    _drafts, _knowledge, publication, denied = _fixture(policy="deny")
    denied.update(
        _principal(),
        REQUEST_ID,
        source_ids=[publication.ref.source_id],
        expected_version=0,
    )
    with pytest.raises(ConsoleKnowledgeSelectionConflictError) as rejected:
        denied.generate(_principal(), REQUEST_ID)
    assert rejected.value.code == "knowledge_egress_denied"


def test_empty_selection_does_not_auto_load_tenant_knowledge_or_feishu_identity():
    drafts, knowledge, publication, service = _fixture()
    service.generate(_principal(), REQUEST_ID)
    request = drafts.get_for_owner(TENANT, REQUEST_ID, requester_person_id=OWNER)
    blobs = InMemoryEnterpriseKnowledgeBlobStore()
    enterprise = EnterpriseKnowledgeContextService(
        knowledge,
        blobs,
        model_egress_policy="allow",
        clock=lambda: NOW,
    )
    planning = PlanningKnowledgeContextService(enterprise_service=enterprise)

    assert request.enterprise_knowledge_manifest == ()
    assert planning.build_for_planning(request) is None
    assert planning.build_for_identity(
        tenant_id=TENANT,
        request_id="feishu-wizard-request",
        actor_person_id=OWNER,
    ) is None
    assert publication.ref in knowledge.list_published(TENANT)


def test_http_selection_contract_and_static_ui_are_safe():
    drafts, _knowledge, publication, service = _fixture()
    application = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        StaticConsoleAuthenticator(TOKEN, _principal()),
        draft_service=ConsoleDraftService(drafts, clock=lambda: NOW),
        knowledge_selection_service=service,
    )
    auth = application.handle(
        "GET",
        "/console/api/v1/auth",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    catalog = application.handle(
        "GET",
        "/console/api/v1/knowledge",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    body = json.dumps(
        {"source_ids": [publication.ref.source_id], "expected_version": 0}
    ).encode()
    selected = application.handle(
        "POST",
        f"/console/api/v1/drafts/{REQUEST_ID}/knowledge-selection",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Larkflow-Console-Action": "knowledge-selection-v1",
        },
        body=body,
    )
    page = application.handle("GET", "/console/")
    script = application.handle("GET", "/console/app.js")

    assert json.loads(auth.body)["capabilities"]["enterprise_knowledge_selection"] is True
    assert catalog.status == 200
    assert selected.status == 200
    assert "content_sha256" not in catalog.body.decode()
    assert "authorization_fingerprint" not in catalog.body.decode()
    assert b"draft-knowledge-list" in page.body
    assert b"loadKnowledgeCatalog" in script.body
    assert b"knowledge-selection-v1" in script.body
    assert b"content_preview" not in script.body
