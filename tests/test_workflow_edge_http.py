"""Versioned JSON boundary tests for the Edge proof."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from larkflow.workflow.edge import (
    EdgeControlService,
    InMemoryEdgeStore,
    PERSONAL_READONLY_CAPABILITY,
)
from larkflow.workflow.edge_http import EdgeHttpApplication
from larkflow.workflow.model import InstanceSnapshot, NodeSpec
from larkflow.workflow.repository import InMemoryWorkflowRepository
from larkflow.workflow.runner import NodeRunner
from larkflow.workflow.service import WorkflowService


NOW = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


def build_application(*, max_body_bytes: int = 256_000):
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(
        repository,
        runner=NodeRunner(
            claim_ttl=timedelta(minutes=5),
            token_factory=lambda: "claim_http",
        ),
        clock=clock,
    )
    workflow.create_draft(
        instance_id="instance_http",
        tenant_id="tenant_http",
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec(
                    "local_review",
                    "Local review",
                    "person_owner",
                    "agent",
                    work={
                        "objective": "Review local context",
                        "inputs": [],
                        "outputs": [{"id": "content", "type": "text"}],
                        "acceptance": ["A review exists"],
                        "agent": {
                            "kind": PERSONAL_READONLY_CAPABILITY,
                            "instructions": "Read only",
                        },
                    },
                ),
            )
        ),
    )
    workflow.confirm_draft(
        "tenant_http",
        "instance_http",
        actor_person_id="person_owner",
    )
    ids = (f"http_id_{index}" for index in range(20))
    secrets = (f"http_secret_{index}" for index in range(20))
    edge = EdgeControlService(
        InMemoryEdgeStore(),
        workflow,
        repository,
        clock=clock,
        id_factory=lambda: next(ids),
        secret_factory=lambda: next(secrets),
    )
    application = EdgeHttpApplication(edge, max_body_bytes=max_body_bytes)
    return clock, workflow, edge, application


def encode(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def pair(edge, application):
    grant = edge.issue_pairing(
        tenant_id="tenant_http",
        person_id="person_owner",
        actor_person_id="person_owner",
    )
    response = application.handle(
        "POST",
        "/edge/v1/devices/pair",
        body=encode(
            {
                "code": grant.code,
                "name": "HTTP Mac",
                "capabilities": [PERSONAL_READONLY_CAPABILITY],
            }
        ),
    )
    assert response.status == 201
    assert response.body is not None
    return response.body["credential"]


def test_http_pair_claim_and_complete_round_trip():
    _, workflow, edge, application = build_application()
    credential = pair(edge, application)
    headers = {"Authorization": f"Bearer {credential}"}

    claimed = application.handle(
        "POST",
        "/edge/v1/leases/claim",
        headers=headers,
        body=encode({"wait_seconds": 0}),
    )
    assert claimed.status == 200
    assert claimed.body is not None
    lease = claimed.body["lease"]
    assert lease["node_key"] == "local_review"
    assert lease["work"]["agent"]["kind"] == PERSONAL_READONLY_CAPABILITY

    completed = application.handle(
        "POST",
        "/edge/v1/leases/complete",
        headers=headers,
        body=encode(
            {
                "instance_id": lease["instance_id"],
                "node_key": lease["node_key"],
                "attempt_no": lease["attempt_no"],
                "expected_node_version": lease["expected_node_version"],
                "claim_token": lease["claim_token"],
                "result": {"content": "HTTP result"},
            }
        ),
    )
    assert completed.status == 200
    assert workflow.get("tenant_http", "instance_http").current_attempt(
        "local_review"
    ).result["content"] == "HTTP result"


def test_http_requires_device_auth_and_hides_stale_claim_details():
    _, _, edge, application = build_application()
    credential = pair(edge, application)

    missing = application.handle(
        "POST",
        "/edge/v1/leases/claim",
        body=encode({}),
    )
    assert missing.status == 401
    assert missing.body["error"]["code"] == "invalid_device_credential"

    headers = {"authorization": f"Bearer {credential}"}
    claimed = application.handle(
        "POST",
        "/edge/v1/leases/claim",
        headers=headers,
        body=encode({}),
    )
    lease = claimed.body["lease"]
    stale = application.handle(
        "POST",
        "/edge/v1/leases/complete",
        headers=headers,
        body=encode(
            {
                "instance_id": lease["instance_id"],
                "node_key": lease["node_key"],
                "attempt_no": lease["attempt_no"],
                "expected_node_version": lease["expected_node_version"],
                "claim_token": "wrong-secret",
                "result": {"content": "spoofed"},
            }
        ),
    )
    assert stale.status == 409
    assert stale.body == {
        "error": {
            "code": "stale_lease",
            "message": "execution lease is no longer current",
        }
    }


def test_http_rejects_oversized_or_non_object_bodies_before_domain_calls():
    _, _, _, application = build_application(max_body_bytes=20)

    oversized = application.handle(
        "POST",
        "/edge/v1/devices/pair",
        body=b"x" * 21,
    )
    assert oversized.status == 413
    invalid = application.handle(
        "POST",
        "/edge/v1/devices/pair",
        body=encode([]),
    )
    assert invalid.status == 400

    non_finite = application.handle(
        "POST",
        "/edge/v1/devices/pair",
        body=b'{"code": NaN}',
    )
    assert non_finite.status == 400
    assert non_finite.body["error"]["code"] == "invalid_request"


def test_unknown_routes_and_methods_are_closed():
    _, _, _, application = build_application()
    assert application.handle("GET", "/edge/v1/leases/claim").status == 405
    assert application.handle("POST", "/edge/v1/unknown").status == 401
