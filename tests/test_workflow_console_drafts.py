"""Controlled employee-workspace workflow draft tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from larkflow.workflow import (
    ConsoleDraftConflictError,
    ConsoleDraftNotFoundError,
    ConsoleDraftService,
    ConsoleDraftWorker,
    DirectoryPerson,
    DraftCapabilityUnavailable,
    DraftDefinitionGenerator,
    DraftGenerationRejected,
    InMemoryConsoleDraftRepository,
    InMemoryWorkflowRepository,
    InstanceStatus,
    WorkflowService,
    human_decision_card,
)
from larkflow.workflow.console import (
    ConsolePrincipal,
    ConsoleReadService,
    StaticConsoleAuthenticator,
)
from larkflow.workflow.console_actions import ConsoleActionService
from larkflow.workflow.console_http import ConsoleHttpApplication
from larkflow.workflow.console_tasks import ConsoleTaskService
from larkflow.workflow.repository import InstanceNotFoundError


TENANT = "tenant_console_draft"
OWNER = "person_owner"
COLLABORATOR = "person_collaborator"
REQUEST_ID = "0123456789abcdef0123456789abcdef"
TOKEN = "console-token-with-at-least-thirty-two-characters"
NOW = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)


def definition() -> dict:
    return {
        "schema_version": "0.2",
        "goal": "Generate and review a release summary",
        "inputs": {
            "brief": "Generate a release summary",
            "context": "Use only registered facts",
        },
        "nodes": [
            {
                "id": "confirm_requirements",
                "title": "Confirm requirements",
                "owner_role": "requester",
                "executor": "human",
                "deps": [],
                "work": {
                    "objective": "Complete and confirm the requested inputs",
                    "inputs": ["instance_inputs.brief", "instance_inputs.context"],
                    "outputs": [
                        {
                            "id": "requirements",
                            "type": "long_text",
                            "label": "Confirmed requirements",
                            "required": True,
                        }
                    ],
                    "acceptance": ["Required inputs are explicit"],
                },
            },
            {
                "id": "generate_summary",
                "title": "Generate summary",
                "owner_role": "requester",
                "executor": "agent",
                "deps": ["confirm_requirements"],
                "work": {
                    "objective": "Generate a grounded summary",
                    "inputs": ["dependencies.confirm_requirements"],
                    "outputs": [{"id": "content", "type": "text", "label": "Summary", "required": True}],
                    "acceptance": ["No unsupported facts"],
                    "agent": {
                        "kind": "llm.generate",
                        "model_role": "default",
                        "instructions": "Write a concise summary.",
                    },
                },
            },
            {
                "id": "review_summary",
                "title": "Review summary",
                "owner_role": "collaborator",
                "executor": "human",
                "deps": ["generate_summary"],
                "work": {
                    "objective": "Review the generated summary",
                    "inputs": ["dependencies.generate_summary"],
                    "outputs": [{"id": "decision", "type": "decision", "label": "Review decision", "required": True}],
                    "acceptance": ["A human decision is recorded"],
                    "decision": {
                        "kind": "accept_reject",
                        "reject_target": "generate_summary",
                    },
                },
            },
        ],
    }


class Completion:
    def __init__(self, value=None, error=None):
        self.value = json.dumps(definition()) if value is None else value
        self.error = error
        self.calls = 0

    def complete(self, *, prompt, model_role):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


class RecordingDraftGenerator:
    def __init__(self):
        self.calls = []

    def generate(
        self,
        *,
        tenant_id,
        actor_person_id,
        request_id,
        brief,
        context,
        on_repair=None,
    ):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "actor_person_id": actor_person_id,
                "request_id": request_id,
                "brief": brief,
                "context": context,
                "on_repair": on_repair,
            }
        )
        return definition()


class SearchUnavailableDraftGenerator:
    def generate(self, **kwargs):
        del kwargs
        raise DraftCapabilityUnavailable(
            "当前没有支持 URL 引用的联网搜索后端"
        )


class MissingTravelFieldsDraftGenerator:
    def generate(self, **kwargs):
        del kwargs
        raise DraftGenerationRejected(
            "旅游规划必须先收集必填需求：出行人数、预算"
        )


class CancelDuringGeneration:
    def __init__(self, drafts):
        self.drafts = drafts

    def generate(self, **kwargs):
        del kwargs
        self.drafts.cancel(principal(), REQUEST_ID)
        return definition()


class Directory:
    def __init__(self, people=(OWNER, COLLABORATOR)):
        self.people = set(people)

    def get_person(self, tenant_id, person_id):
        if tenant_id != TENANT or person_id not in self.people:
            raise KeyError(person_id)
        return DirectoryPerson(person_id=person_id, name=person_id, active=True)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def principal(person_id=OWNER) -> ConsolePrincipal:
    return ConsolePrincipal(TENANT, person_id)


def queued_service(*, directory=None, clock=None):
    repository = InMemoryConsoleDraftRepository()
    service = ConsoleDraftService(
        repository,
        directory,
        clock=clock or (lambda: NOW),
    )
    return repository, service


def create_request(service, *, request_id=REQUEST_ID, collaborator=COLLABORATOR):
    return service.create(
        principal(),
        request_id=request_id,
        brief="Generate a release summary",
        context="Use only registered facts",
        collaborator_person_id=collaborator,
    )


def test_console_draft_request_is_idempotent_owner_scoped_and_directory_checked():
    repository, service = queued_service(directory=Directory())

    created = create_request(service)
    replay = create_request(service)

    assert created == replay
    assert created["request"]["status"] == "queued"
    assert created["request"]["collaborator_relation"] == "collaborator"
    assert OWNER not in json.dumps(created)
    assert COLLABORATOR not in json.dumps(created)
    with pytest.raises(ConsoleDraftNotFoundError):
        service.get(principal("person_foreign"), REQUEST_ID)
    with pytest.raises(ConsoleDraftConflictError, match="编号"):
        service.create(
            principal(),
            request_id=REQUEST_ID,
            brief="A different request",
            context="",
            collaborator_person_id=COLLABORATOR,
        )
    with pytest.raises(ConsoleDraftConflictError, match="协作者"):
        service.create(
            principal(),
            request_id="1123456789abcdef0123456789abcdef",
            brief="Another request",
            context="",
            collaborator_person_id="person_missing",
        )
    assert repository.list_for_owner(
        TENANT,
        requester_person_id=OWNER,
        limit=10,
    )[0].brief == "Generate a release summary"


def test_console_draft_without_collaborator_keeps_all_roles_with_requester():
    _repository, service = queued_service()

    payload = create_request(service, collaborator=None)

    assert payload["request"]["collaborator_relation"] == "you"


def test_worker_freezes_candidate_creates_only_a_draft_then_existing_confirm_starts():
    draft_repository, drafts = queued_service(directory=Directory())
    create_request(drafts)
    workflow_repository = InMemoryWorkflowRepository()
    workflow_service = WorkflowService(workflow_repository, clock=lambda: NOW)
    completion = Completion()
    worker = ConsoleDraftWorker(
        draft_repository,
        workflow_service,
        DraftDefinitionGenerator(completion),
        tenant_id=TENANT,
        worker_id="draft-worker",
        clock=lambda: NOW,
    )

    report = worker.run_once()
    request = drafts.get(principal(), REQUEST_ID)["request"]
    instance = workflow_service.get(TENANT, request["instance_id"])

    assert report.claimed == report.processed == 1
    assert report.failed == report.rejected == 0
    assert completion.calls == 1
    assert request["status"] == "ready"
    assert instance.status == InstanceStatus.DRAFT
    assert instance.snapshot.inputs == {
        "brief": "Generate a release summary",
        "context": "Use only registered facts",
    }
    assert [item.owner_person_id for item in instance.snapshot.nodes] == [
        OWNER,
        OWNER,
        COLLABORATOR,
    ]
    assert all(node.current_attempt_no == 0 for node in instance.nodes.values())

    result = ConsoleActionService(workflow_service).confirm_draft(
        principal(),
        instance.id,
    )

    assert result["action"] == "confirm_draft"
    assert result["instance"]["status"] == "running"


def test_console_worker_passes_server_identity_to_planner_without_claim_state():
    draft_repository, drafts = queued_service(directory=Directory())
    create_request(drafts)
    generator = RecordingDraftGenerator()
    worker = ConsoleDraftWorker(
        draft_repository,
        WorkflowService(InMemoryWorkflowRepository(), clock=lambda: NOW),
        generator,
        tenant_id=TENANT,
        worker_id="draft-worker",
        clock=lambda: NOW,
    )

    assert worker.run_once().processed == 1
    assert len(generator.calls) == 1
    call = generator.calls[0]
    assert call["tenant_id"] == TENANT
    assert call["actor_person_id"] == OWNER
    assert call["request_id"] == REQUEST_ID
    assert call["brief"] == "Generate a release summary"
    assert call["context"] == "Use only registered facts"
    assert set(call) == {
        "tenant_id",
        "actor_person_id",
        "request_id",
        "brief",
        "context",
        "on_repair",
    }


def test_generated_agent_flow_reaches_one_decision_card_and_bounded_console_task():
    draft_repository, drafts = queued_service(directory=Directory())
    create_request(drafts)
    workflow_repository = InMemoryWorkflowRepository()
    workflow_service = WorkflowService(workflow_repository, clock=lambda: NOW)
    worker = ConsoleDraftWorker(
        draft_repository,
        workflow_service,
        DraftDefinitionGenerator(Completion()),
        tenant_id=TENANT,
        worker_id="draft-worker",
        clock=lambda: NOW,
    )
    worker.run_once()
    instance_id = drafts.get(principal(), REQUEST_ID)["request"]["instance_id"]
    ConsoleActionService(workflow_service).confirm_draft(
        principal(),
        instance_id,
    )
    confirmation = workflow_service.dispatch_due(
        TENANT,
        instance_id,
        worker_id="runtime-worker",
        max_automated=0,
    )[0]
    workflow_service.submit_human(
        TENANT,
        instance_id,
        "confirm_requirements",
        actor_person_id=OWNER,
        attempt_no=confirmation.attempt_no,
        expected_node_version=confirmation.expected_node_version,
        result={"requirements": "Release summary requirements confirmed"},
    )
    agent = workflow_service.dispatch_due(
        TENANT,
        instance_id,
        worker_id="runtime-worker",
        max_automated=1,
    )[0]
    workflow_service.complete_automated(
        TENANT,
        instance_id,
        "generate_summary",
        attempt_no=agent.attempt_no,
        expected_node_version=agent.expected_node_version,
        claim_token=agent.claim_token or "",
        worker_id="runtime-worker",
        result={"content": "Grounded release summary"},
    )
    decision = workflow_service.dispatch_due(
        TENANT,
        instance_id,
        worker_id="runtime-worker",
    )[0]

    assert decision.node_key == "review_summary"
    instance = workflow_service.get(TENANT, instance_id)
    assert instance.snapshot.node("review_summary").work["decision"] == {
        "kind": "accept_reject",
        "reject_target": "generate_summary",
    }
    card = human_decision_card(instance, "review_summary", decision.attempt_no)
    rendered_card = json.dumps(card, ensure_ascii=False)
    assert "接受" in rendered_card
    assert "填写意见并退回" in rendered_card
    listing = ConsoleTaskService(workflow_service).list_tasks(
        principal(COLLABORATOR)
    )
    assert listing["total"] == 1
    assert listing["tasks"][0]["kind"] == "decision"


def test_persisted_candidate_is_reused_after_worker_lease_expiry_without_llm_call():
    clock = Clock()
    draft_repository, drafts = queued_service(directory=Directory(), clock=clock)
    create_request(drafts)
    first_claim = draft_repository.claim(
        TENANT,
        worker_id="worker-one",
        now=clock.now,
        limit=1,
        claim_ttl=timedelta(seconds=10),
    )[0]
    draft_repository.save_candidate(
        TENANT,
        REQUEST_ID,
        claim_token=first_claim.claim_token,
        definition=definition(),
        now=clock.now,
    )
    clock.now += timedelta(seconds=11)
    completion = Completion(error=AssertionError("LLM must not run"))
    workflows = WorkflowService(InMemoryWorkflowRepository(), clock=clock)
    worker = ConsoleDraftWorker(
        draft_repository,
        workflows,
        DraftDefinitionGenerator(completion),
        tenant_id=TENANT,
        worker_id="worker-two",
        clock=clock,
        claim_ttl=timedelta(seconds=10),
    )

    report = worker.run_once()

    assert report.processed == 1
    assert completion.calls == 0
    assert drafts.get(principal(), REQUEST_ID)["request"]["status"] == "ready"


def test_invalid_candidate_is_terminal_and_does_not_create_an_instance():
    draft_repository, drafts = queued_service(directory=Directory())
    create_request(drafts)
    workflows = InMemoryWorkflowRepository()
    generator = Completion(value="not-json")
    worker = ConsoleDraftWorker(
        draft_repository,
        WorkflowService(workflows, clock=lambda: NOW),
        DraftDefinitionGenerator(generator),
        tenant_id=TENANT,
        worker_id="worker",
        clock=lambda: NOW,
    )

    report = worker.run_once()

    assert report.rejected == 1
    assert generator.calls == 2
    assert drafts.get(principal(), REQUEST_ID)["request"]["status"] == "rejected"
    with pytest.raises(InstanceNotFoundError):
        workflows.get(TENANT, f"console_draft_{REQUEST_ID}")


def test_search_capability_rejection_returns_actionable_public_guidance():
    draft_repository, drafts = queued_service(directory=Directory())
    create_request(drafts)
    workflows = InMemoryWorkflowRepository()
    worker = ConsoleDraftWorker(
        draft_repository,
        WorkflowService(workflows, clock=lambda: NOW),
        SearchUnavailableDraftGenerator(),
        tenant_id=TENANT,
        worker_id="worker",
        clock=lambda: NOW,
    )

    report = worker.run_once()
    payload = drafts.get(principal(), REQUEST_ID)["request"]

    assert report.rejected == 1
    assert payload["status"] == "rejected"
    assert "URL 引用" in payload["message"]
    assert "上传完整资料" in payload["message"]
    assert "DraftCapabilityUnavailable" not in json.dumps(
        payload,
        ensure_ascii=False,
    )


def test_missing_fields_are_safe_structured_and_actionable_in_public_dto():
    draft_repository, drafts = queued_service(directory=Directory())
    create_request(drafts)
    worker = ConsoleDraftWorker(
        draft_repository,
        WorkflowService(InMemoryWorkflowRepository(), clock=lambda: NOW),
        MissingTravelFieldsDraftGenerator(),
        tenant_id=TENANT,
        worker_id="worker",
        clock=lambda: NOW,
    )

    assert worker.run_once().rejected == 1
    payload = drafts.get(principal(), REQUEST_ID)["request"]

    assert payload["status"] == "rejected"
    assert payload["message"] == "还缺少：出行人数、预算。补充后可重新生成。"
    assert payload["actionable_error"] == {
        "code": "missing_required_fields",
        "fields": ["出行人数", "预算"],
    }
    assert "DraftGenerationRejected" not in json.dumps(payload, ensure_ascii=False)


def test_infrastructure_failure_retries_then_reaches_bounded_terminal_state():
    clock = Clock()
    draft_repository, drafts = queued_service(directory=Directory(), clock=clock)
    create_request(drafts)
    completion = Completion(error=RuntimeError("provider unavailable"))
    worker = ConsoleDraftWorker(
        draft_repository,
        WorkflowService(InMemoryWorkflowRepository(), clock=clock),
        DraftDefinitionGenerator(completion),
        tenant_id=TENANT,
        worker_id="worker",
        clock=clock,
        retry_base=timedelta(seconds=1),
        retry_max=timedelta(seconds=1),
        max_attempts=2,
    )

    first = worker.run_once()
    assert first.failed == 1
    assert drafts.get(principal(), REQUEST_ID)["request"]["status"] == "retrying"
    clock.now += timedelta(seconds=1)
    second = worker.run_once()

    assert second.failed == 1
    assert drafts.get(principal(), REQUEST_ID)["request"]["status"] == "failed"
    assert worker.run_once().claimed == 0


def test_retry_progress_exposes_bounded_attempt_without_internal_error():
    clock = Clock()
    draft_repository, drafts = queued_service(directory=Directory(), clock=clock)
    create_request(drafts)
    worker = ConsoleDraftWorker(
        draft_repository,
        WorkflowService(InMemoryWorkflowRepository(), clock=clock),
        Completion(error=RuntimeError("private provider detail")),
        tenant_id=TENANT,
        worker_id="worker",
        clock=clock,
        retry_base=timedelta(seconds=1),
        retry_max=timedelta(seconds=1),
        max_attempts=2,
    )

    worker.run_once()
    payload = drafts.get(principal(), REQUEST_ID)["request"]

    assert payload["status"] == "retrying"
    assert payload["attempt"] == {"current": 1, "max": 2}
    assert payload["can_cancel"] is True
    assert "1/2" in payload["message"]
    assert "provider" not in json.dumps(payload)


def test_owner_cancel_is_durable_idempotent_and_late_generation_is_discarded():
    draft_repository, drafts = queued_service(directory=Directory())
    create_request(drafts)
    workflows = InMemoryWorkflowRepository()
    worker = ConsoleDraftWorker(
        draft_repository,
        WorkflowService(workflows, clock=lambda: NOW),
        CancelDuringGeneration(drafts),
        tenant_id=TENANT,
        worker_id="worker",
        clock=lambda: NOW,
    )

    report = worker.run_once()
    first = drafts.get(principal(), REQUEST_ID)["request"]
    replay = drafts.cancel(principal(), REQUEST_ID)["request"]

    assert report.canceled == 1
    assert report.processed == report.failed == report.rejected == 0
    assert first == replay
    assert first["status"] == "canceled"
    assert first["can_cancel"] is False
    assert "已取消" in first["message"]
    with pytest.raises(InstanceNotFoundError):
        workflows.get(TENANT, f"console_draft_{REQUEST_ID}")
    stored = draft_repository.get_for_owner(
        TENANT,
        REQUEST_ID,
        requester_person_id=OWNER,
    )
    assert stored.canceled_at == NOW
    assert stored.canceled_by_person_id == OWNER


def test_cancel_is_owner_only_and_ready_requests_cannot_be_rewritten():
    repository, drafts = queued_service(directory=Directory())
    create_request(drafts)
    with pytest.raises(ConsoleDraftNotFoundError):
        drafts.cancel(principal("person_foreign"), REQUEST_ID)
    drafts.cancel(principal(), REQUEST_ID)
    assert repository.claim(
        TENANT,
        worker_id="worker",
        now=NOW,
        limit=1,
        claim_ttl=timedelta(minutes=1),
    ) == ()


def test_console_http_creates_and_reads_only_the_authenticated_owners_request():
    draft_repository, draft_service = queued_service(directory=Directory())
    workflows = InMemoryWorkflowRepository()
    application = ConsoleHttpApplication(
        ConsoleReadService(workflows),
        StaticConsoleAuthenticator(TOKEN, principal()),
        draft_service=draft_service,
    )
    document = json.dumps(
        {
            "request_id": REQUEST_ID,
            "brief": "Generate a release summary",
            "context": "Use only registered facts",
            "collaborator_person_id": COLLABORATOR,
        }
    ).encode()
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Content-Length": str(len(document)),
        "X-Larkflow-Console-Action": "workflow-action-v1",
    }

    created = application.handle(
        "POST",
        "/console/api/v1/drafts",
        headers=headers,
        body=document,
    )
    read = application.handle(
        "GET",
        f"/console/api/v1/drafts/{REQUEST_ID}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    listing = application.handle(
        "GET",
        "/console/api/v1/drafts?limit=10",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert created.status == 202
    assert read.status == 200
    assert listing.status == 200
    assert json.loads(created.body)["request"]["status"] == "queued"
    assert json.loads(read.body) == json.loads(created.body)
    assert json.loads(listing.body)["total"] == 1
    assert draft_repository.get_for_owner(
        TENANT,
        REQUEST_ID,
        requester_person_id=OWNER,
    ).brief == "Generate a release summary"


def test_console_http_owner_can_cancel_without_exposing_identity():
    _repository, draft_service = queued_service(directory=Directory())
    create_request(draft_service)
    application = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        StaticConsoleAuthenticator(TOKEN, principal()),
        draft_service=draft_service,
    )

    canceled = application.handle(
        "POST",
        f"/console/api/v1/drafts/{REQUEST_ID}/cancel",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Length": "0",
            "X-Larkflow-Console-Action": "workflow-action-v1",
        },
        body=b"",
    )

    assert canceled.status == 200
    payload = json.loads(canceled.body)["request"]
    assert payload["status"] == "canceled"
    assert payload["can_cancel"] is False
    assert OWNER not in canceled.body.decode()


def test_console_http_cancel_hides_request_from_non_owner_and_other_tenant():
    _repository, draft_service = queued_service(directory=Directory())
    create_request(draft_service)
    cancel_path = f"/console/api/v1/drafts/{REQUEST_ID}/cancel"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Length": "0",
        "X-Larkflow-Console-Action": "workflow-action-v1",
    }

    for foreign_principal in (
        ConsolePrincipal(TENANT, "person_foreign"),
        ConsolePrincipal("tenant_foreign", OWNER),
    ):
        application = ConsoleHttpApplication(
            ConsoleReadService(InMemoryWorkflowRepository()),
            StaticConsoleAuthenticator(TOKEN, foreign_principal),
            draft_service=draft_service,
        )

        response = application.handle(
            "POST",
            cancel_path,
            headers=headers,
            body=b"",
        )

        assert response.status == 404

    assert draft_service.get(principal(), REQUEST_ID)["request"]["status"] == "queued"


@pytest.mark.parametrize(
    "path, headers, body, expected",
    (
        (
            "/console/api/v1/drafts?unexpected=true",
            {},
            b"{}",
            400,
        ),
        (
            "/console/api/v1/drafts",
            {"Content-Type": "application/json", "Content-Length": "2"},
            b"{}",
            403,
        ),
        (
            "/console/api/v1/drafts",
            {
                "Content-Type": "text/plain",
                "Content-Length": "2",
                "X-Larkflow-Console-Action": "workflow-action-v1",
            },
            b"{}",
            400,
        ),
    ),
)
def test_console_http_rejects_invalid_draft_write_envelopes(path, headers, body, expected):
    _repository, draft_service = queued_service(directory=Directory())
    application = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        StaticConsoleAuthenticator(TOKEN, principal()),
        draft_service=draft_service,
    )
    response = application.handle(
        "POST",
        path,
        headers={"Authorization": f"Bearer {TOKEN}", **headers},
        body=body,
    )

    assert response.status == expected
