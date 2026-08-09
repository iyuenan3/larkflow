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
                "id": "generate_summary",
                "title": "Generate summary",
                "owner_role": "requester",
                "executor": "agent",
                "deps": [],
                "work": {
                    "objective": "Generate a grounded summary",
                    "inputs": ["instance_inputs.brief"],
                    "outputs": [{"id": "content", "type": "text"}],
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
                    "outputs": [{"id": "decision", "type": "data"}],
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
        COLLABORATOR,
    ]
    assert all(node.current_attempt_no == 0 for node in instance.nodes.values())

    result = ConsoleActionService(workflow_service).confirm_draft(
        principal(),
        instance.id,
    )

    assert result["action"] == "confirm_draft"
    assert result["instance"]["status"] == "running"


def test_generated_agent_flow_reaches_a_decision_card_not_an_ordinary_console_task():
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
    assert ConsoleTaskService(workflow_service).list_tasks(
        principal(COLLABORATOR)
    )["total"] == 0


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
