"""Owner authorization and read-model tests for the central console."""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import count
import json

import pytest

from larkflow.workflow.console import (
    ConsolePrincipal,
    ConsoleReadService,
    ConsoleResourceNotFoundError,
    InvalidConsoleCredentialError,
    StaticConsoleAuthenticator,
)
from larkflow.workflow.console_http import (
    ConsoleHttpApplication,
    build_console_http_server,
)
from larkflow.workflow import console_http
from larkflow.workflow.model import InstanceSnapshot, NodeSpec
from larkflow.workflow.postgres import PostgresWorkflowRepository
from larkflow.workflow.repository import InMemoryWorkflowRepository
from larkflow.workflow.service import WorkflowService


NOW = datetime(2026, 8, 6, 7, 30, tzinfo=timezone.utc)
TENANT = "tenant_console"
OWNER = "person_owner_secret"
COLLABORATOR = "person_collaborator_secret"
TOKEN = "console-token-with-at-least-thirty-two-characters"


def _work(objective: str) -> dict:
    return {
        "objective": objective,
        "inputs": [],
        "outputs": [{"id": "content", "type": "data"}],
        "acceptance": ["Content exists"],
    }


def _snapshot() -> InstanceSnapshot:
    return InstanceSnapshot(
        goal="Review a release summary",
        nodes=(
            NodeSpec(
                "confirm_input",
                "Confirm input",
                OWNER,
                "human",
                work=_work("Confirm the input"),
            ),
            NodeSpec(
                "generate_summary",
                "Generate summary",
                OWNER,
                "agent",
                deps=("confirm_input",),
                work=_work("Generate a summary"),
            ),
            NodeSpec(
                "review_summary",
                "Review summary",
                COLLABORATOR,
                "human",
                deps=("generate_summary",),
                work=_work("Review the summary"),
            ),
        ),
    )


class TrackingRepository(InMemoryWorkflowRepository):
    def __init__(self) -> None:
        super().__init__()
        self.audit_reads = 0

    def recent_audit_log(
        self,
        tenant_id: str,
        instance_id: str,
        *,
        limit: int = 200,
    ):
        self.audit_reads += 1
        return super().recent_audit_log(
            tenant_id,
            instance_id,
            limit=limit,
        )


def _repository() -> TrackingRepository:
    repository = TrackingRepository()
    identifiers = count(1)
    service = WorkflowService(
        repository,
        clock=lambda: NOW,
        id_factory=lambda: f"console-test-id-{next(identifiers)}",
    )
    service.create_draft(
        instance_id="instance_owner",
        tenant_id=TENANT,
        owner_person_id=OWNER,
        actor_person_id=OWNER,
        snapshot=_snapshot(),
    )
    service.confirm_draft(
        TENANT,
        "instance_owner",
        actor_person_id=OWNER,
    )
    service.create_draft(
        instance_id="instance_foreign",
        tenant_id=TENANT,
        owner_person_id=COLLABORATOR,
        actor_person_id=COLLABORATOR,
        snapshot=_snapshot(),
    )
    service.confirm_draft(
        TENANT,
        "instance_foreign",
        actor_person_id=COLLABORATOR,
    )
    service.create_draft(
        instance_id="instance_owner",
        tenant_id="tenant_other",
        owner_person_id=OWNER,
        actor_person_id=OWNER,
        snapshot=_snapshot(),
    )
    service.confirm_draft(
        "tenant_other",
        "instance_owner",
        actor_person_id=OWNER,
    )
    return repository


def _principal(person_id: str = OWNER, tenant_id: str = TENANT) -> ConsolePrincipal:
    return ConsolePrincipal(tenant_id=tenant_id, person_id=person_id)


def _application(repository=None) -> ConsoleHttpApplication:
    return ConsoleHttpApplication(
        ConsoleReadService(repository or _repository()),
        StaticConsoleAuthenticator(TOKEN, _principal()),
    )


def _json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_console_list_and_detail_are_owner_and_tenant_scoped():
    service = ConsoleReadService(_repository())

    listing = service.list_instances(_principal())
    detail = service.get_instance(_principal(), "instance_owner")

    assert [item["id"] for item in listing["instances"]] == ["instance_owner"]
    assert detail["instance"]["id"] == "instance_owner"
    assert [item["key"] for item in detail["nodes"]] == [
        "confirm_input",
        "generate_summary",
        "review_summary",
    ]
    assert [item["owner_relation"] for item in detail["nodes"]] == [
        "you",
        "you",
        "collaborator",
    ]
    assert [item["deps"] for item in detail["nodes"]] == [
        [],
        ["confirm_input"],
        ["generate_summary"],
    ]


def test_console_can_inspect_a_draft_before_runtime_nodes_exist():
    repository = TrackingRepository()
    WorkflowService(repository, clock=lambda: NOW).create_draft(
        instance_id="instance_draft",
        tenant_id=TENANT,
        owner_person_id=OWNER,
        actor_person_id=OWNER,
        snapshot=_snapshot(),
    )

    payload = ConsoleReadService(repository).get_instance(
        _principal(),
        "instance_draft",
    )

    assert payload["instance"]["status"] == "draft"
    assert {item["status"] for item in payload["nodes"]} == {"pending"}
    assert {item["current_attempt_no"] for item in payload["nodes"]} == {0}
    assert all(item["attempts"] == [] for item in payload["nodes"])


def test_non_owner_and_unknown_instance_have_the_same_not_found_boundary():
    repository = _repository()
    service = ConsoleReadService(repository)

    with pytest.raises(ConsoleResourceNotFoundError):
        service.get_instance(_principal(), "instance_foreign")
    with pytest.raises(ConsoleResourceNotFoundError):
        service.get_instance(_principal(), "instance_missing")

    assert repository.audit_reads == 0


def test_console_dto_excludes_credentials_raw_errors_and_identity_fields():
    repository = _repository()
    instance = repository.get(TENANT, "instance_owner")
    attempt = instance.attempts[("confirm_input", 1)]
    attempt.claimed_by = "worker_private_identity"
    attempt.claim_token = "claim_token_private"
    attempt.error_code = "temporary_failure"
    attempt.error_message = "private stack and credential detail"
    attempt.submitted_by_person_id = OWNER
    attempt.result = {"summary": "safe business result"}
    repository.save(instance, expected_version=instance.version)

    payload = ConsoleReadService(repository).get_instance(
        _principal(),
        "instance_owner",
    )
    encoded = json.dumps(payload, ensure_ascii=False)

    assert "safe business result" in encoded
    assert "worker_private_identity" not in encoded
    assert "claim_token_private" not in encoded
    assert "private stack and credential detail" not in encoded
    assert OWNER not in encoded
    assert COLLABORATOR not in encoded
    assert "audit-owner" not in encoded
    assert payload["nodes"][0]["attempts"][0]["has_error_detail"] is True


def test_console_truncates_oversized_results_and_bounds_audit_reads():
    repository = _repository()
    instance = repository.get(TENANT, "instance_owner")
    instance.attempts[("confirm_input", 1)].result = {"body": "x" * 2_000}
    repository.save(instance, expected_version=instance.version)
    service = ConsoleReadService(
        repository,
        max_audit_events=1,
        max_result_bytes=256,
    )

    payload = service.get_instance(_principal(), "instance_owner")
    result = payload["nodes"][0]["attempts"][0]["result"]

    assert result["_truncated"] is True
    assert result["original_bytes"] > 256
    assert len(payload["audit"]) == 1


def test_static_authenticator_requires_a_strong_exact_bearer_credential():
    with pytest.raises(ValueError):
        StaticConsoleAuthenticator("too-short", _principal())
    authenticator = StaticConsoleAuthenticator(TOKEN, _principal())

    assert authenticator.authenticate(
        {"authorization": f"Bearer {TOKEN}"}
    ) == _principal()
    for headers in ({}, {"Authorization": TOKEN}, {"Authorization": "Bearer wrong"}):
        with pytest.raises(InvalidConsoleCredentialError):
            authenticator.authenticate(headers)


def test_console_http_assets_are_public_but_data_requires_authentication():
    application = _application()

    page = application.handle("GET", "/console/")
    script = application.handle("GET", "/console/app.js")
    styles = application.handle("GET", "/console/styles.css")
    missing_auth = application.handle("GET", "/console/api/v1/instances")
    authorized = application.handle(
        "GET",
        "/console/api/v1/instances?limit=10",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert page.status == 200
    assert page.content_type == "text/html; charset=utf-8"
    assert b"CENTRAL CONSOLE" in page.body
    assert script.status == 200
    assert b"innerHTML" not in script.body
    assert b"topologicalLayers" in script.body
    assert b"targetNode.deps" in script.body
    assert b"setGraphScale" in script.body
    assert b"fitGraph" in script.body
    assert b'addEventListener("pointerdown"' in script.body
    assert b'event.target.closest(".graph-node")' in script.body
    assert b'addEventListener("wheel"' in script.body
    assert b"graph-connector" not in script.body
    assert styles.status == 200
    assert b".dag-edge" in styles.body
    assert b".graph-controls" in styles.body
    assert b"graph-zoom-in" in page.body
    assert b"graph-fit" in page.body
    assert missing_auth.status == 401
    assert missing_auth.headers["WWW-Authenticate"] == "Bearer"
    assert authorized.status == 200
    assert [item["id"] for item in _json(authorized)["instances"]] == [
        "instance_owner"
    ]


def test_console_http_rejects_writes_bad_queries_and_resource_enumeration():
    application = _application()
    headers = {"Authorization": f"Bearer {TOKEN}"}

    assert application.handle("POST", "/console/api/v1/instances").status == 405
    assert application.handle(
        "GET",
        "/console/api/v1/instances?limit=0",
        headers=headers,
    ).status == 400
    assert application.handle(
        "GET",
        "/console/api/v1/instances?unexpected=true",
        headers=headers,
    ).status == 400
    assert application.handle(
        "GET",
        "/console/api/v1/instances#fragment",
        headers=headers,
    ).status == 400
    foreign = application.handle(
        "GET",
        "/console/api/v1/instances/instance_foreign",
        headers=headers,
    )
    missing = application.handle(
        "GET",
        "/console/api/v1/instances/instance_missing",
        headers=headers,
    )
    assert foreign.status == missing.status == 404
    assert foreign.body == missing.body


def test_console_server_refuses_non_loopback_bindings(monkeypatch):
    application = _application()

    with pytest.raises(ValueError, match="loopback"):
        build_console_http_server(application, host="0.0.0.0", port=8780)

    calls = []

    class FakeServer:
        daemon_threads = False

        def __init__(self, address, _handler):
            calls.append(address)

    monkeypatch.setattr(console_http, "ThreadingHTTPServer", FakeServer)
    server = build_console_http_server(application, host="127.0.0.1", port=0)

    assert calls == [("127.0.0.1", 0)]
    assert server.daemon_threads is True


def test_postgres_recent_audit_query_is_bounded_tenant_scoped_and_chronological():
    rows = [
        {
            "id": "audit-latest",
            "tenant_id": TENANT,
            "instance_id": "instance_owner",
            "node_key": "confirm_input",
            "attempt_no": 1,
            "event_type": "node.completed",
            "actor_person_id": OWNER,
            "source": "workflow_service",
            "correlation_id": "correlation-latest",
            "aggregate_version": 2,
            "payload": {"private": "not returned by console"},
            "occurred_at": datetime(2026, 8, 6, 7, 32, tzinfo=timezone.utc),
        },
        {
            "id": "audit-earliest",
            "tenant_id": TENANT,
            "instance_id": "instance_owner",
            "node_key": None,
            "attempt_no": None,
            "event_type": "instance.draft_created",
            "actor_person_id": OWNER,
            "source": "workflow_service",
            "correlation_id": "correlation-earliest",
            "aggregate_version": 0,
            "payload": {},
            "occurred_at": datetime(2026, 8, 6, 7, 30, tzinfo=timezone.utc),
        },
    ]
    calls = []

    class Cursor:
        def fetchall(self):
            return rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            calls.append((sql, params))
            return Cursor()

    repository = PostgresWorkflowRepository(Connection)

    events = repository.recent_audit_log(
        TENANT,
        "instance_owner",
        limit=2,
    )

    assert [event.id for event in events] == ["audit-earliest", "audit-latest"]
    assert calls[0][1] == (TENANT, "instance_owner", 2)
    assert "ORDER BY occurred_at DESC, id DESC" in calls[0][0]
    with pytest.raises(ValueError):
        repository.recent_audit_log(TENANT, "instance_owner", limit=501)
