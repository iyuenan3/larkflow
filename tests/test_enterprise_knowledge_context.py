from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from larkflow.agent_runtime import (
    AgentContextRequest,
    AgentRunResult,
    AgentRunRequest,
    CapabilityEnvelope,
    ENTERPRISE_KNOWLEDGE_INPUT,
)
from larkflow.agent_runtime.executor import AgentRuntimeExecutor
from larkflow.knowledge import (
    EnterpriseKnowledgeAuthorizationProof,
    EnterpriseKnowledgePublication,
    EnterpriseKnowledgeRef,
)
from larkflow.knowledge.blob import (
    InMemoryEnterpriseKnowledgeBlobStore,
    enterprise_knowledge_object_key,
)
from larkflow.knowledge.repository import InMemoryEnterpriseKnowledgeRepository
from larkflow.planning.context import (
    AttachmentRef,
    ContextBundle,
    ContextChunk,
    SourceRef,
)
from larkflow.workflow.knowledge_context import (
    CombinedAgentContextService,
    EnterpriseAgentContextService,
    EnterpriseKnowledgeContextRejected,
    EnterpriseKnowledgeContextService,
    merge_context_bundles,
)
from larkflow.workflow.console import ConsolePrincipal
from larkflow.workflow.console_drafts import (
    ConsoleDraftService,
    ConsoleDraftWorker,
    InMemoryConsoleDraftRepository,
)
from larkflow.workflow.draft_generation import DraftDefinitionGenerator
from larkflow.workflow.model import ExecutorKind
from larkflow.workflow.repository import InMemoryWorkflowRepository
from larkflow.workflow.runtime import ExecutionRequest
from larkflow.workflow.service import WorkflowService


NOW = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)


def _definition() -> dict:
    return {
        "schema_version": "0.2",
        "goal": "Use authorized policy facts",
        "inputs": {"brief": "Use policy", "context": ""},
        "nodes": [
            {
                "id": "confirm_scope",
                "title": "Confirm scope",
                "owner_role": "requester",
                "executor": "human",
                "deps": [],
                "work": {
                    "objective": "Confirm scope",
                    "inputs": ["instance_inputs.brief"],
                    "outputs": [
                        {
                            "id": "scope",
                            "type": "text",
                            "label": "Scope",
                            "required": True,
                        }
                    ],
                    "acceptance": ["Scope confirmed"],
                },
            },
            {
                "id": "draft",
                "title": "Draft",
                "owner_role": "requester",
                "executor": "agent",
                "deps": ["confirm_scope"],
                "work": {
                    "objective": "Draft with authorized facts",
                    "inputs": ["dependencies.confirm_scope"],
                    "outputs": [
                        {
                            "id": "content",
                            "type": "text",
                            "label": "Draft",
                            "required": True,
                        }
                    ],
                    "acceptance": ["Policy facts are used"],
                    "agent": {
                        "kind": "llm.generate",
                        "model_role": "default",
                        "instructions": "Draft the result.",
                    },
                },
            },
            {
                "id": "review",
                "title": "Review",
                "owner_role": "requester",
                "executor": "human",
                "deps": ["draft"],
                "work": {
                    "objective": "Review",
                    "inputs": ["dependencies.draft"],
                    "outputs": [
                        {
                            "id": "decision",
                            "type": "decision",
                            "label": "Decision",
                            "required": True,
                        }
                    ],
                    "acceptance": ["Human decides"],
                    "decision": {
                        "kind": "accept_reject",
                        "reject_target": "draft",
                    },
                },
            },
        ],
    }


class _Completion:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, *, prompt: str, model_role: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(_definition())


def _publish(
    content: str = "Synthetic enterprise policy facts.",
    *,
    tenant_id: str = "tenant-a",
    source_id: str = "enterprise:policy",
    egress_decision: str = "allow",
):
    encoded = content.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    proof = EnterpriseKnowledgeAuthorizationProof(
        tenant_id=tenant_id,
        source_id=source_id,
        version_id="v1",
        content_sha256=digest,
        authorized_by_person_id="admin-private",
        authorized_at=NOW,
    )
    publication = EnterpriseKnowledgePublication(
        ref=EnterpriseKnowledgeRef(
            tenant_id=tenant_id,
            source_id=source_id,
            version_id="v1",
            display_label="Synthetic policy",
            media_type="text/plain",
            size_bytes=len(encoded),
            content_sha256=digest,
            published_at=NOW,
            egress_decision=egress_decision,
            authorization_proof_id=proof.proof_id,
            authorization_fingerprint=proof.fingerprint,
        ),
        published_by_person_id="admin-private",
        authorization_proof=proof,
    )
    repository = InMemoryEnterpriseKnowledgeRepository()
    blobs = InMemoryEnterpriseKnowledgeBlobStore()
    repository.publish(publication)
    key = enterprise_knowledge_object_key(
        tenant_id=tenant_id,
        source_id=source_id,
        version_id="v1",
        content_sha256=digest,
    )
    blobs.put_if_absent(key, encoded)
    return repository, blobs, publication


def _agent_request(
    ref: EnterpriseKnowledgeRef,
    *,
    context_manifest: dict | None = None,
) -> AgentContextRequest:
    instance_inputs = {
        "enterprise_knowledge": [ref.snapshot_value()],
    }
    if context_manifest is not None:
        instance_inputs["context_manifest"] = context_manifest
    return AgentContextRequest(
        tenant_id=ref.tenant_id,
        instance_id="instance-a",
        node_key="draft",
        attempt_id="attempt-a",
        attempt_no=1,
        owner_person_id="person-owner",
        work_contract={"inputs": [ENTERPRISE_KNOWLEDGE_INPUT]},
        input_snapshot={"instance_inputs": instance_inputs},
    )


def test_planning_context_reads_authorized_body_without_leaking_storage() -> None:
    repository, blobs, publication = _publish()
    service = EnterpriseKnowledgeContextService(
        repository,
        blobs,
        model_egress_policy="allow",
        clock=lambda: NOW,
    )

    bundle = service.build_for_planning(
        tenant_id="tenant-a",
        request_id="request-a",
        actor_person_id="person-requester",
    )

    assert bundle is not None
    assert bundle.attachments == ()
    assert bundle.enterprise_knowledge == (publication.ref,)
    assert bundle.prompt_sources()[0]["content"] == (
        "Synthetic enterprise policy facts."
    )
    assert "Synthetic enterprise policy facts" not in repr(bundle)
    manifest = bundle.snapshot_manifest()
    encoded = str(manifest)
    assert "object_key" not in encoded
    assert "admin-private" not in encoded
    assert "Synthetic enterprise policy facts" not in encoded


def test_revoke_preserves_frozen_history_but_blocks_new_attempt_context() -> None:
    repository, blobs, publication = _publish()
    service = EnterpriseKnowledgeContextService(
        repository,
        blobs,
        model_egress_policy="allow",
        clock=lambda: NOW,
    )
    planning = service.build_for_planning(
        tenant_id="tenant-a",
        request_id="request-a",
        actor_person_id="person-requester",
    )
    assert planning is not None
    frozen = planning.snapshot_manifest()
    repository.revoke(
        "tenant-a",
        publication.ref.source_id,
        publication.ref.version_id,
        actor_person_id="admin-private",
        now=NOW + timedelta(minutes=1),
    )

    assert service.build_for_planning(
        tenant_id="tenant-a",
        request_id="request-b",
        actor_person_id="person-requester",
    ) is None
    with pytest.raises(EnterpriseKnowledgeContextRejected, match="已撤销"):
        service.build_for_agent(
            _agent_request(publication.ref),
            (publication.ref,),
            max_chars=1_000,
        )
    assert planning.snapshot_manifest() == frozen
    assert repository.list_audit(
        "tenant-a", publication.ref.source_id, publication.ref.version_id
    )


@pytest.mark.parametrize(
    ("service_policy", "source_policy", "message"),
    [
        ("deny", "allow", "Worker 未允许"),
        ("allow", "deny", "外发未获授权"),
    ],
)
def test_egress_policy_fails_closed(service_policy, source_policy, message) -> None:
    repository, blobs, _ = _publish(egress_decision=source_policy)
    service = EnterpriseKnowledgeContextService(
        repository,
        blobs,
        model_egress_policy=service_policy,
        clock=lambda: NOW,
    )

    with pytest.raises(EnterpriseKnowledgeContextRejected, match=message):
        service.build_for_planning(
            tenant_id="tenant-a",
            request_id="request-a",
            actor_person_id="person-requester",
        )


def test_missing_wrong_hash_cross_tenant_and_budget_are_rejected() -> None:
    repository, _, publication = _publish(content="12345")
    missing = EnterpriseKnowledgeContextService(
        repository,
        InMemoryEnterpriseKnowledgeBlobStore(),
        model_egress_policy="allow",
        clock=lambda: NOW,
    )
    with pytest.raises(EnterpriseKnowledgeContextRejected, match="正文不可用"):
        missing.build_for_planning(
            tenant_id="tenant-a",
            request_id="request-a",
            actor_person_id="person-requester",
        )

    repository, blobs, publication = _publish(content="12345")
    limited = EnterpriseKnowledgeContextService(
        repository,
        blobs,
        model_egress_policy="allow",
        planning_max_chars=4,
        clock=lambda: NOW,
    )
    with pytest.raises(EnterpriseKnowledgeContextRejected, match="字符预算"):
        limited.build_for_planning(
            tenant_id="tenant-a",
            request_id="request-a",
            actor_person_id="person-requester",
        )

    with pytest.raises(EnterpriseKnowledgeContextRejected, match="跨越 tenant"):
        limited.build_for_agent(
            _agent_request(publication.ref),
            (
                EnterpriseKnowledgeRef(
                    **{**publication.ref.__dict__, "tenant_id": "tenant-b"}
                ),
            ),
            max_chars=10,
        )


def test_agent_context_and_capability_bind_exact_enterprise_scope() -> None:
    repository, blobs, publication = _publish()
    context_service = EnterpriseKnowledgeContextService(
        repository,
        blobs,
        model_egress_policy="allow",
        clock=lambda: NOW,
    )
    planning = context_service.build_for_planning(
        tenant_id="tenant-a",
        request_id="request-a",
        actor_person_id="person-requester",
    )
    assert planning is not None
    manifest = {
        key: value
        for key, value in planning.snapshot_manifest().items()
        if key not in {"attachments", "enterprise_knowledge"}
    }
    manifest["source_kinds"] = "enterprise_knowledge"
    enterprise = EnterpriseAgentContextService(
        context_service,
        max_context_chars=1_000,
    )
    request = _agent_request(publication.ref, context_manifest=manifest)
    resolver = CombinedAgentContextService(
        enterprise_service=enterprise,
        max_context_chars=1_000,
    )
    bundle = resolver.resolve(request)
    assert bundle is not None
    envelope = CapabilityEnvelope(
        tenant_id="tenant-a",
        actor_person_id="person-owner",
        instance_id="instance-a",
        node_key="draft",
        attempt_id="attempt-a",
        attempt_no=1,
        allowed_capabilities=("context.read.enterprise_knowledge",),
        knowledge_scopes=("enterprise_knowledge",),
        data_classification="internal",
        egress_decision="allow",
        max_context_chars=1_000,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )

    run_request = AgentRunRequest(
        tenant_id="tenant-a",
        instance_id="instance-a",
        node_key="draft",
        attempt_id="attempt-a",
        attempt_no=1,
        owner_person_id="person-owner",
        executor="agent",
        work_contract=request.work_contract,
        input_snapshot=request.input_snapshot,
        context_bundle=bundle,
        capability_envelope=envelope,
    )

    assert run_request.context_bundle == bundle
    assert envelope.knowledge_scopes == ("enterprise_knowledge",)

    class Runtime:
        def __init__(self) -> None:
            self.requests = []

        def accepts(self, **_kwargs):
            return True

        def run(self, runtime_request):
            self.requests.append(runtime_request)
            return AgentRunResult(deliverables={"content": "safe result"})

    runtime = Runtime()
    result = AgentRuntimeExecutor(
        runtime,
        context_resolver=resolver,
        clock=lambda: NOW,
        max_context_chars=1_000,
    ).execute(
        ExecutionRequest(
            tenant_id="tenant-a",
            instance_id="instance-a",
            node_key="draft",
            attempt_id="attempt-a",
            attempt_no=1,
            owner_person_id="person-owner",
            executor="agent",
            work=request.work_contract,
            input_snapshot=request.input_snapshot,
            expected_node_version=1,
            claim_token="worker-only-secret",
            claim_expires_at=NOW + timedelta(minutes=5),
        )
    ).result
    evidence = result["_runtime_evidence"]
    assert evidence["capability_envelope"]["allowed_capabilities"] == (
        "context.read.enterprise_knowledge",
    )
    assert evidence["capability_envelope"]["knowledge_scopes"] == (
        "enterprise_knowledge",
    )
    assert "Synthetic enterprise policy facts" not in str(evidence)
    assert runtime.requests[0].context_bundle == bundle


def test_merge_is_deterministic_and_rejects_duplicate_or_over_budget_sources() -> None:
    repository, blobs, publication = _publish()
    enterprise = EnterpriseKnowledgeContextService(
        repository,
        blobs,
        model_egress_policy="allow",
        clock=lambda: NOW,
    ).build_for_planning(
        tenant_id="tenant-a",
        request_id="request-a",
        actor_person_id="person-requester",
    )
    assert enterprise is not None
    attachment = AttachmentRef(
        attachment_id="attachment-a",
        source_id="attachment:attachment-a",
        display_filename="project.txt",
        media_type="text/plain",
        size_bytes=7,
        content_sha256=hashlib.sha256(b"project").hexdigest(),
    )
    project = ContextBundle(
        tenant_id="tenant-a",
        scope_kind="console_draft_request",
        scope_id="request-a",
        purpose="planning",
        actor_person_id="person-requester",
        sources=(
            SourceRef(
                source_id=attachment.source_id,
                kind="attachment",
                label=attachment.display_filename,
                content_sha256=attachment.content_sha256,
            ),
        ),
        chunks=(
            ContextChunk(
                source_id=attachment.source_id,
                order=0,
                text="project",
            ),
        ),
        attachments=(attachment,),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    merged = merge_context_bundles(project, enterprise, max_chars=1_000)
    assert merged is not None
    assert [item.kind for item in merged.sources] == [
        "attachment",
        "enterprise_knowledge",
    ]
    assert merged.snapshot_manifest()["fingerprint"] == merged.fingerprint
    with pytest.raises(EnterpriseKnowledgeContextRejected, match="字符预算"):
        merge_context_bundles(project, enterprise, max_chars=5)

    duplicate_project = ContextBundle(
        **{
            **project.__dict__,
            "sources": (
                SourceRef(
                    source_id=publication.ref.source_id,
                    kind="attachment",
                    label="duplicate.txt",
                    content_sha256=attachment.content_sha256,
                ),
            ),
            "chunks": (
                ContextChunk(
                    source_id=publication.ref.source_id,
                    order=0,
                    text="project",
                ),
            ),
            "attachments": (
                AttachmentRef(
                    **{
                        **attachment.__dict__,
                        "source_id": publication.ref.source_id,
                    }
                ),
            ),
            "fingerprint": "",
        }
    )
    with pytest.raises(EnterpriseKnowledgeContextRejected, match="来源重复"):
        merge_context_bundles(duplicate_project, enterprise, max_chars=1_000)


def test_console_draft_freezes_enterprise_refs_and_binds_every_agent() -> None:
    repository, blobs, publication = _publish()
    context_service = EnterpriseKnowledgeContextService(
        repository,
        blobs,
        model_egress_policy="allow",
        clock=lambda: NOW,
    )
    from larkflow.workflow.knowledge_context import PlanningKnowledgeContextService

    drafts = InMemoryConsoleDraftRepository()
    principal = ConsolePrincipal("tenant-a", "person-requester")
    ConsoleDraftService(drafts, clock=lambda: NOW).create(
        principal,
        request_id="a123456789abcdef0123456789abcdef",
        brief="Create an internal policy summary",
        context="Use authorized shared material",
        collaborator_person_id=None,
    )
    workflows = InMemoryWorkflowRepository()
    completion = _Completion()
    worker = ConsoleDraftWorker(
        drafts,
        WorkflowService(workflows, clock=lambda: NOW),
        DraftDefinitionGenerator(completion),
        tenant_id="tenant-a",
        worker_id="knowledge-draft-worker",
        clock=lambda: NOW,
        context_service=PlanningKnowledgeContextService(
            enterprise_service=context_service,
        ),
    )

    assert worker.run_once().processed == 1
    instance = workflows.get(
        "tenant-a",
        "console_draft_a123456789abcdef0123456789abcdef",
    )
    assert instance.snapshot.inputs["enterprise_knowledge"] == (
        publication.ref.snapshot_value(),
    )
    assert "Synthetic enterprise policy facts" not in str(instance.snapshot.inputs)
    agent = next(
        node
        for node in instance.snapshot.nodes
        if node.executor == ExecutorKind.AGENT
    )
    assert ENTERPRISE_KNOWLEDGE_INPUT in agent.work["inputs"]
    assert "Synthetic enterprise policy facts" in completion.prompts[0]
