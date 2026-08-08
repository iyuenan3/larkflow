"""Owner workflow actions exposed by the central console."""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import count
import json

import pytest

from larkflow.workflow.console import (
    ConsolePrincipal,
    ConsoleReadService,
    ConsoleResourceNotFoundError,
    StaticConsoleAuthenticator,
)
from larkflow.workflow.console_actions import (
    ConsoleActionConflictError,
    ConsoleActionService,
)
from larkflow.workflow.console_http import ConsoleHttpApplication
from larkflow.workflow.model import InstanceSnapshot, NodeSpec
from larkflow.workflow.repository import InMemoryWorkflowRepository
from larkflow.workflow.service import WorkflowService


NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)
TENANT = "tenant_console_actions"
OWNER = "person_console_owner"
OTHER = "person_other_owner"
TOKEN = "console-action-token-with-at-least-thirty-two-characters"
ACTION_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Larkflow-Console-Action": "workflow-action-v1",
}


def _snapshot(owner: str = OWNER) -> InstanceSnapshot:
    return InstanceSnapshot(
        goal="Review a direct console action",
        nodes=(
            NodeSpec(
                "confirm_input",
                "Confirm input",
                owner,
                "human",
                work={
                    "objective": "Confirm input",
                    "inputs": [],
                    "outputs": [{"id": "confirmation", "type": "data"}],
                    "acceptance": ["Input is confirmed"],
                },
            ),
            NodeSpec(
                "draft_summary",
                "Draft summary",
                owner,
                "agent",
                deps=("confirm_input",),
                work={
                    "objective": "Draft summary",
                    "inputs": ["dependencies.confirm_input"],
                    "outputs": [{"id": "summary", "type": "data"}],
                    "acceptance": ["Summary exists"],
                },
            ),
        ),
    )


def _services():
    repository = InMemoryWorkflowRepository()
    identifiers = count(1)
    domain = WorkflowService(
        repository,
        clock=lambda: NOW,
        id_factory=lambda: f"console-action-id-{next(identifiers)}",
    )
    for instance_id, owner in (
        ("draft_owner", OWNER),
        ("restart_owner", OWNER),
        ("foreign_draft", OTHER),
    ):
        domain.create_draft(
            instance_id=instance_id,
            tenant_id=TENANT,
            owner_person_id=owner,
            actor_person_id=owner,
            snapshot=_snapshot(owner),
        )
    domain.confirm_draft(TENANT, "restart_owner", actor_person_id=OWNER)
    domain.dispatch_ready(TENANT, "restart_owner", max_automated=0)
    return repository, domain, ConsoleActionService(domain)


def _principal(person_id: str = OWNER) -> ConsolePrincipal:
    return ConsolePrincipal(tenant_id=TENANT, person_id=person_id)


def test_direct_confirm_pause_resume_and_replay_use_existing_domain_service():
    _repository, _domain, actions = _services()

    confirmed = actions.confirm_draft(_principal(), "draft_owner")
    replay = actions.confirm_draft(_principal(), "draft_owner")
    paused = actions.pause(_principal(), "draft_owner")
    pause_replay = actions.pause(_principal(), "draft_owner")
    resumed = actions.resume(_principal(), "draft_owner")

    assert confirmed["instance"]["status"] == "running"
    assert confirmed["already_applied"] is False
    assert replay["already_applied"] is True
    assert paused["instance"]["status"] == "paused"
    assert pause_replay["already_applied"] is True
    assert resumed["instance"]["status"] == "running"


def test_cancel_requires_version_bound_preview_and_replays_safely():
    _repository, _domain, actions = _services()
    actions.confirm_draft(_principal(), "draft_owner")

    preview = actions.preview_cancellation(_principal(), "draft_owner")
    version = preview["preview"]["expected_instance_version"]
    confirmed = actions.confirm_cancellation(
        _principal(),
        "draft_owner",
        version,
    )
    replay = actions.confirm_cancellation(
        _principal(),
        "draft_owner",
        version,
    )

    assert preview["stage"] == "preview"
    assert [item["key"] for item in preview["preview"]["affected_nodes"]] == [
        "confirm_input",
        "draft_summary",
    ]
    assert confirmed["instance"]["status"] == "canceled"
    assert confirmed["already_applied"] is False
    assert replay["already_applied"] is True


def test_cancel_rejects_a_preview_after_instance_state_changes():
    _repository, _domain, actions = _services()
    actions.confirm_draft(_principal(), "draft_owner")
    preview = actions.preview_cancellation(_principal(), "draft_owner")
    actions.pause(_principal(), "draft_owner")

    with pytest.raises(ConsoleActionConflictError) as caught:
        actions.confirm_cancellation(
            _principal(),
            "draft_owner",
            preview["preview"]["expected_instance_version"],
        )

    assert caught.value.code == "preview_stale"


def test_restart_uses_durable_preview_and_preserves_idempotent_replay():
    _repository, _domain, actions = _services()

    preview = actions.preview_restart(
        _principal(),
        "restart_owner",
        node_key="confirm_input",
    )
    confirmed = actions.confirm_restart(
        _principal(),
        preview["preview"]["id"],
    )
    replay = actions.confirm_restart(
        _principal(),
        preview["preview"]["id"],
    )

    assert preview["preview"]["scope"] == "node"
    assert [item["key"] for item in preview["preview"]["affected_nodes"]] == [
        "confirm_input",
        "draft_summary",
    ]
    assert confirmed["affected_nodes"][0]["attempt_no"] == 2
    assert confirmed["already_applied"] is False
    assert replay["already_applied"] is True


def test_action_service_hides_foreign_and_missing_resources_identically():
    _repository, _domain, actions = _services()

    for instance_id in ("foreign_draft", "missing_draft"):
        with pytest.raises(ConsoleResourceNotFoundError):
            actions.confirm_draft(_principal(), instance_id)


def test_http_workflow_actions_require_auth_header_and_reject_request_bodies():
    repository, domain, actions = _services()
    application = ConsoleHttpApplication(
        ConsoleReadService(repository),
        StaticConsoleAuthenticator(TOKEN, _principal()),
        action_service=actions,
    )
    path = "/console/api/v1/instances/draft_owner/confirm"

    assert application.handle("POST", path).status == 401
    assert application.handle(
        "POST",
        path,
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).status == 403
    assert application.handle(
        "POST",
        path,
        headers={**ACTION_HEADERS, "Content-Length": "2"},
    ).status == 400
    assert application.handle(
        "POST",
        path + "?unexpected=true",
        headers=ACTION_HEADERS,
    ).status == 400

    confirmed = application.handle("POST", path, headers=ACTION_HEADERS)
    replay = application.handle("POST", path, headers=ACTION_HEADERS)
    assert confirmed.status == replay.status == 200
    assert json.loads(confirmed.body)["instance"]["status"] == "running"
    assert json.loads(replay.body)["already_applied"] is True

    full_preview = application.handle(
        "POST",
        "/console/api/v1/instances/restart_owner/restart-preview",
        headers=ACTION_HEADERS,
    )
    assert full_preview.status == 201
    preview_id = json.loads(full_preview.body)["preview"]["id"]
    restarted = application.handle(
        "POST",
        f"/console/api/v1/restart-previews/{preview_id}/confirm",
        headers=ACTION_HEADERS,
    )
    assert restarted.status == 200
    assert json.loads(restarted.body)["action"] == "restart"

    foreign = application.handle(
        "POST",
        "/console/api/v1/instances/foreign_draft/confirm",
        headers=ACTION_HEADERS,
    )
    missing = application.handle(
        "POST",
        "/console/api/v1/instances/missing_draft/confirm",
        headers=ACTION_HEADERS,
    )
    assert foreign.status == missing.status == 404
    assert foreign.body == missing.body


def test_feishu_workflow_actions_require_the_exact_public_origin():
    repository, _domain, actions = _services()

    class FeishuAuthenticator:
        mode = "feishu"

        def authenticate(self, _headers):
            return _principal()

        def authenticate_context(self, _headers):
            from larkflow.workflow.console import ConsoleAuthentication

            return ConsoleAuthentication(principal=_principal(), session_id="session")

    class OAuthBoundary:
        public_base_url = "https://workspace.example.test"

    application = ConsoleHttpApplication(
        ConsoleReadService(repository),
        FeishuAuthenticator(),
        oauth=OAuthBoundary(),
        action_service=actions,
    )
    path = "/console/api/v1/instances/draft_owner/confirm"
    headers = {"X-Larkflow-Console-Action": "workflow-action-v1"}

    assert application.handle("POST", path, headers=headers).status == 403
    assert application.handle(
        "POST",
        path,
        headers={**headers, "Origin": "https://workspace.example.test.evil"},
    ).status == 403
    accepted = application.handle(
        "POST",
        path,
        headers={**headers, "Origin": "https://workspace.example.test"},
    )
    assert accepted.status == 200
