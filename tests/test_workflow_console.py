"""Owner authorization and read-model tests for the central console."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from itertools import count
import json

import pytest

from larkflow.workflow.events import AuditEvent
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
from larkflow.workflow.console_actions import ConsoleActionService
from larkflow.workflow import console_http
from larkflow.workflow.model import (
    AttemptStatus,
    InstanceSnapshot,
    InstanceStatus,
    NodeSpec,
    NodeStatus,
    WorkflowAttentionCandidate,
)
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
    service.dispatch_ready(TENANT, "instance_owner", max_automated=0)
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
    service.dispatch_ready(TENANT, "instance_foreign", max_automated=0)
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
    service.dispatch_ready("tenant_other", "instance_owner", max_automated=0)
    return repository


def _principal(person_id: str = OWNER, tenant_id: str = TENANT) -> ConsolePrincipal:
    return ConsolePrincipal(tenant_id=tenant_id, person_id=person_id)


def _application(repository=None) -> ConsoleHttpApplication:
    repository = repository or _repository()
    return ConsoleHttpApplication(
        ConsoleReadService(repository),
        StaticConsoleAuthenticator(TOKEN, _principal()),
        action_service=ConsoleActionService(WorkflowService(repository)),
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
    assert detail["insights"] == {
        "reworked_nodes": [],
        "latest_restart": None,
    }


def test_console_attention_only_includes_actionable_owner_work():
    service = ConsoleReadService(_repository())

    listing = service.list_instances(_principal())
    attention = listing["attention"]
    encoded = json.dumps(attention, ensure_ascii=False)

    assert attention["total"] == 1
    assert attention["counts"] == {
        "recover_failed": 0,
        "complete_human": 1,
        "resume_flow": 0,
        "confirm_draft": 0,
    }
    assert attention["items"][0] == {
        "id": "complete_human:instance_owner:confirm_input",
        "kind": "complete_human",
        "priority": 1,
        "instance_id": "instance_owner",
        "goal": "Review a release summary",
        "instance_status": "running",
        "title": "完成待办：Confirm input",
        "detail": "该 Human 节点正在等待你的输入或决定。",
        "occurred_at": NOW.isoformat(),
        "node": {"key": "confirm_input", "title": "Confirm input"},
        "action": None,
        "action_hint": "在飞书完成该节点对应的任务或决定卡。",
    }
    assert "instance_foreign" not in encoded
    assert OWNER not in encoded
    assert COLLABORATOR not in encoded


def test_console_attention_excludes_collaborator_human_work_from_owner_inbox():
    repository = _repository()
    instance = repository.get(TENANT, "instance_owner")
    instance.nodes["confirm_input"].owner_person_id = COLLABORATOR
    repository.save(instance, expected_version=instance.version)

    attention = ConsoleReadService(repository).list_instances(
        _principal()
    )["attention"]

    assert attention["total"] == 0
    assert attention["counts"]["complete_human"] == 0


def test_console_attention_uses_rework_target_and_safe_full_restart_fallback():
    rejected = WorkflowAttentionCandidate(
        instance_id="rejected_instance",
        goal="Revise a brief",
        instance_status=InstanceStatus.FAILED,
        created_at=NOW,
        node_key="review_summary",
        node_title="Review summary",
        node_status=NodeStatus.FAILED,
        node_owner_person_id=COLLABORATOR,
        node_occurred_at=NOW,
        reject_target="generate_summary",
    )
    first_failure = replace(
        rejected,
        instance_id="multiple_failures",
        node_key="generate_summary",
        node_title="Generate summary",
        reject_target=None,
    )
    second_failure = replace(
        first_failure,
        node_key="check_summary",
        node_title="Check summary",
    )

    items = ConsoleReadService._attention(
        (rejected, first_failure, second_failure),
        OWNER,
    )

    assert items[0]["action"] == {"kind": "restart", "scope": "instance"}
    actions = {item["instance_id"]: item["action"] for item in items}
    assert actions == {
        "multiple_failures": {"kind": "restart", "scope": "instance"},
        "rejected_instance": {
            "kind": "restart",
            "scope": "node",
            "node_key": "generate_summary",
        },
    }


def test_console_attention_prioritizes_failure_then_human_pause_and_draft():
    repository = TrackingRepository()
    identifiers = count(1)
    service = WorkflowService(
        repository,
        clock=lambda: NOW,
        id_factory=lambda: f"attention-test-id-{next(identifiers)}",
    )
    service.create_draft(
        instance_id="draft_attention",
        tenant_id=TENANT,
        owner_person_id=OWNER,
        actor_person_id=OWNER,
        snapshot=_snapshot(),
    )
    service.create_draft(
        instance_id="paused_attention",
        tenant_id=TENANT,
        owner_person_id=OWNER,
        actor_person_id=OWNER,
        snapshot=_snapshot(),
    )
    service.confirm_draft(TENANT, "paused_attention", actor_person_id=OWNER)
    service.dispatch_ready(TENANT, "paused_attention", max_automated=0)
    service.pause_instance(TENANT, "paused_attention", actor_person_id=OWNER)
    service.create_draft(
        instance_id="failed_attention",
        tenant_id=TENANT,
        owner_person_id=OWNER,
        actor_person_id=OWNER,
        snapshot=_snapshot(),
    )
    service.confirm_draft(TENANT, "failed_attention", actor_person_id=OWNER)
    failed = repository.get(TENANT, "failed_attention")
    failed.status = InstanceStatus.FAILED
    failed.nodes["confirm_input"].status = NodeStatus.FAILED
    failed.nodes["confirm_input"].completed_at = NOW
    failed.attempts[("confirm_input", 1)].status = AttemptStatus.FAILED
    failed.attempts[("confirm_input", 1)].error_message = "private failure detail"
    repository.save(failed, expected_version=failed.version)

    listing = ConsoleReadService(repository).list_instances(_principal())
    items = listing["attention"]["items"]
    kinds = [item["kind"] for item in items]
    encoded = json.dumps(items, ensure_ascii=False)

    assert kinds == [
        "recover_failed",
        "complete_human",
        "resume_flow",
        "confirm_draft",
    ]
    assert items[0]["action"] == {
        "kind": "restart",
        "scope": "node",
        "node_key": "confirm_input",
    }
    assert items[2]["action"] == {"kind": "resume"}
    assert items[3]["action"] == {"kind": "confirm_draft"}
    assert "private failure detail" not in encoded


def test_console_summarizes_reworked_nodes_and_latest_restart_without_raw_payload():
    repository = _repository()
    instance = repository.get(TENANT, "instance_owner")
    for node_key in ("generate_summary", "review_summary"):
        node = instance.nodes[node_key]
        node.current_attempt_no = 2
        instance.attempts[(node_key, 2)] = replace(
            instance.attempts[(node_key, 1)],
            id=f"{node.id}:attempt:2",
            attempt_no=2,
        )
    restart = AuditEvent(
        id="audit-restart-private",
        tenant_id=TENANT,
        instance_id=instance.id,
        event_type="instance.node_restarted",
        source="workflow_service",
        correlation_id="correlation-restart-private",
        aggregate_version=instance.version + 1,
        occurred_at=datetime(2026, 8, 6, 8, 53, 27, tzinfo=timezone.utc),
        actor_person_id=OWNER,
        node_key="generate_summary",
        attempt_no=2,
        payload={
            "affected_node_keys": (
                "generate_summary",
                "review_summary",
                "unknown_private_node",
            ),
            "private_reason": "must never leave the server DTO",
        },
    )
    repository.save(
        instance,
        expected_version=instance.version,
        audit_events=(restart,),
    )

    payload = ConsoleReadService(repository).get_instance(
        _principal(),
        "instance_owner",
    )
    insights = payload["insights"]
    encoded = json.dumps(payload, ensure_ascii=False)

    assert insights["reworked_nodes"] == [
        {
            "key": "generate_summary",
            "title": "Generate summary",
            "current_attempt_no": 2,
        },
        {
            "key": "review_summary",
            "title": "Review summary",
            "current_attempt_no": 2,
        },
    ]
    assert insights["latest_restart"] == {
        "event_type": "instance.node_restarted",
        "scope": "node",
        "occurred_at": "2026-08-06T08:53:27+00:00",
        "actor_relation": "you",
        "target_node": {
            "key": "generate_summary",
            "title": "Generate summary",
        },
        "attempt_no": 2,
        "affected_nodes": [
            {"key": "generate_summary", "title": "Generate summary"},
            {"key": "review_summary", "title": "Review summary"},
        ],
    }
    assert "private_reason" not in encoded
    assert "unknown_private_node" not in encoded


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
    failed_login_page = application.handle(
        "GET",
        "/console/?auth_error=login_failed",
    )
    denied_login_page = application.handle(
        "GET",
        "/console/?auth_error=access_denied",
    )
    script = application.handle("GET", "/console/app.js")
    canvas_script = application.handle("GET", "/console/canvas.js")
    canvas_styles = application.handle("GET", "/console/canvas.css")
    styles = application.handle("GET", "/console/styles.css")
    auth = application.handle("GET", "/console/api/v1/auth")
    missing_auth = application.handle("GET", "/console/api/v1/instances")
    authorized = application.handle(
        "GET",
        "/console/api/v1/instances?limit=10",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert page.status == 200
    assert failed_login_page.status == 200
    assert denied_login_page.status == 200
    assert page.content_type == "text/html; charset=utf-8"
    assert "LARKFLOW 工作台".encode() in page.body
    assert "我的工作台".encode() in page.body
    assert "使用飞书身份进入".encode() in page.body
    assert script.status == 200
    assert b"innerHTML" not in script.body
    assert b"ensureCanvasBundle" in script.body
    assert b"LarkflowCanvas.render" in script.body
    assert b"mountGraphCanvas" in script.body
    assert b"canvasExpanded" in script.body
    assert b"LarkflowCanvas.fit" in script.body
    assert b"topologicalLayers" not in script.body
    assert b"drawDagEdges" not in script.body
    assert b"renderInsights" in script.body
    assert b"renderAttention" in script.body
    assert b"runAttentionAction" in script.body
    assert b"confirmWorkflowActionPreview" in script.body
    assert b"renderDetailActions" in script.body
    assert b"openGraphEditor" in script.body
    assert b"createGraphEditPreview" in script.body
    assert b"confirmGraphEditPreview" in script.body
    assert b"previewNodeRestart" in script.body
    assert b"submitDraftRequest" in script.body
    assert b"pollDraftRequest" in script.body
    assert b"openDraftInstance" in script.body
    assert b"loadDraftAttachments" in script.body
    assert b"generateCurrentDraft" in script.body
    assert b"revokeDraftAttachment" in script.body
    assert b"loadAuthConfiguration" in script.body
    assert b"loadAdminOverview" in script.body
    assert b"renderAdminOverview" in script.body
    assert b"credentials: \"same-origin\"" in script.body
    assert b"sessionStorage.setItem(\"larkflow.console.token\"" in script.body
    assert b"larkflow.console.theme" in script.body
    assert b"prefers-color-scheme: dark" in script.body
    assert b"document.documentElement.dataset.theme" in script.body
    assert "正在生成预览".encode() in script.body
    assert "确认并启动".encode() in script.body
    assert "负责人已更换，飞书同步中".encode() in script.body
    assert "飞书待办随后同步".encode() in page.body
    assert "复制飞书命令".encode() not in script.body
    assert b"showOwnerSection" in script.body
    assert b"setDetailTab" in script.body
    assert b"renderOverviewNodes" in script.body
    assert b"renderWorkflowNextStep" in script.body
    assert b"renderWorkflowJourney" in script.body
    assert b"renderWorkflowResults" in script.body
    assert b"taskForInstance" in script.body
    assert b"reloadAfterTaskMutation" in script.body
    assert b'ownerSection: "attention"' in script.body
    assert b'workflowFilter: "open"' in script.body
    assert b'setDetailTab("overview")' in script.body
    assert b'&& state.instances.length === 0' not in script.body
    assert b'else if (!state.detail' not in script.body
    assert b'canvasLoadPromise' in script.body
    assert canvas_script.status == 200
    assert "受控流程画板".encode() in canvas_script.body
    assert "增加节点".encode() in canvas_script.body
    assert "编辑节点".encode() in canvas_script.body
    assert "断开选中连线".encode() in canvas_script.body
    assert "拖动节点端点可增加依赖".encode() in canvas_script.body
    assert "第 ".encode() in canvas_script.body
    assert "可修改流程".encode() in canvas_script.body
    assert "打回到此节点".encode() in canvas_script.body
    assert "恢复自动布局".encode() in canvas_script.body
    assert b"larkflow.canvas.layout.v1" in canvas_script.body
    assert b"elk.algorithm" in canvas_script.body
    assert b"NETWORK_SIMPLEX" in canvas_script.body
    assert b"LarkflowCanvas" in canvas_script.body
    assert b"process.env.NODE_ENV" not in canvas_script.body
    assert canvas_styles.status == 200
    assert b".lfc-node" in canvas_styles.body
    assert b".lfc-minimap" in canvas_styles.body
    assert b".react-flow" in canvas_styles.body
    assert b"width:24px!important" in canvas_styles.body
    assert styles.status == 200
    assert b".graph-fallback" in styles.body
    assert b'.detail-grid[data-canvas-expanded="true"]' in styles.body
    assert b"height: clamp(520px, 62vh, 720px)" in styles.body
    assert b".insight-grid" in styles.body
    assert b".draft-starter-options" in styles.body
    assert b".draft-advanced" in styles.body
    assert b".draft-attachment-panel" in styles.body
    assert b".workflow-next-step" in styles.body
    assert b".workflow-journey" in styles.body
    assert b".workflow-results-panel" in styles.body
    assert b".detail-advanced" in styles.body
    assert b"@media (max-width: 1400px)" in styles.body
    assert b"instance-insights" in page.body
    assert b"attention-center" in page.body
    assert b"attention-nav" in page.body
    assert b"workflow-library" in page.body
    assert b"draft-studio" in page.body
    assert b"draft-advanced" in page.body
    assert b"draft-attachments" in page.body
    assert b"draft-generate" in page.body
    assert page.body.count(b"data-draft-starter=") == 3
    assert "示例只会填入表单".encode() in page.body
    assert "默认隐藏已经结束的历史记录".encode() in page.body
    assert b"workflow-next-step" in page.body
    assert b"workflow-journey" in page.body
    assert b"workflow-results-panel" in page.body
    assert b"detail-advanced" in page.body
    assert b"graph-edit-dialog" in page.body
    assert b"graph-edit-dependency-list" in page.body
    assert b"human-task-deliverable-fields" in page.body
    assert b"graph-edit-before-list" in page.body
    assert "生成流程草稿".encode() in page.body
    assert "提交后只生成草稿，不会自动启动".encode() in page.body
    assert "不用复制飞书命令".encode() in page.body
    assert "接受并继续".encode() in page.body
    assert "退回修改".encode() in page.body
    assert b"submitHumanDecisionFromPage" in script.body
    assert b"/decision`" in script.body
    assert b"workflow-filters" in page.body

    task_link = application.handle(
        "GET",
        "/console/?action=task&instance=instance_owner&node=confirm_input",
    )
    assert task_link.status == 200
    assert application.handle(
        "GET",
        "/console/?action=task&instance=instance_owner&node=confirm_input&extra=1",
    ).status == 400
    assert b"detail-tabs" in page.body
    assert b"detail-tab-overview" not in page.body
    assert b"overview-nodes" in page.body
    assert b"detail-actions" in page.body
    assert page.body.count(b"theme-toggle") >= 2
    assert b'content="light dark"' in page.body
    assert b"admin-console" in page.body
    assert b"admin-view" in page.body
    assert b'/console/canvas.css' in page.body
    assert "受控流程运行画板".encode() in page.body
    assert "展开画板".encode() in page.body
    assert b':root[data-theme="light"]' in styles.body
    assert b"--font-body: 15px" in styles.body
    assert b".theme-toggle" in styles.body
    assert _json(auth) == {
        "mode": "static",
        "authenticated": False,
        "admin": False,
        "login_url": None,
        "logout_available": False,
        "capabilities": {"attachment_planning": False},
    }
    assert missing_auth.status == 401
    assert missing_auth.headers["WWW-Authenticate"] == "Bearer"
    assert authorized.status == 200
    assert [item["id"] for item in _json(authorized)["instances"]] == [
        "instance_owner"
    ]
    assert _json(authorized)["attention"]["total"] == 1


def test_console_http_rejects_writes_bad_queries_and_resource_enumeration():
    application = _application()
    headers = {"Authorization": f"Bearer {TOKEN}"}

    assert application.handle("POST", "/console/api/v1/instances").status == 405
    assert application.handle("GET", "/console/?unexpected=true").status == 400
    assert application.handle(
        "GET",
        "/console/?auth_error=unexpected",
    ).status == 400
    assert application.handle(
        "GET",
        "/console/?auth_error=login_failed&auth_error=access_denied",
    ).status == 400
    assert application.handle("GET", "/console/app.js?v=1").status == 400
    assert application.handle("GET", "/console/canvas.js?v=1").status == 400
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


def test_postgres_attention_query_is_bounded_and_owner_scoped():
    rows = [
        {
            "instance_id": "instance_owner",
            "goal": "Review a release summary",
            "instance_status": "failed",
            "created_at": NOW,
            "snapshot": {
                "nodes": [
                    {
                        "key": "review_summary",
                        "title": "Review summary",
                        "work": {
                            "decision": {
                                "kind": "accept_reject",
                                "reject_target": "generate_summary",
                            }
                        },
                    }
                ]
            },
            "node_key": "review_summary",
            "node_status": "failed",
            "node_executor": "human",
            "node_owner_person_id": OWNER,
            "node_occurred_at": NOW,
        }
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

    candidates = repository.list_attention_for_owner(
        TENANT,
        owner_person_id=OWNER,
        limit=30,
    )

    assert candidates[0].node_title == "Review summary"
    assert candidates[0].reject_target == "generate_summary"
    assert calls[0][1] == (TENANT, OWNER, 30, OWNER)
    assert "WITH owner_instances AS" in calls[0][0]
    assert "instance.tenant_id = %s" in calls[0][0]
    assert "instance.owner_person_id = %s" in calls[0][0]
    assert "node.owner_person_id = %s" in calls[0][0]
    assert "LIMIT %s" in calls[0][0]
    assert "node.status = 'failed'" in calls[0][0]
    with pytest.raises(ValueError):
        repository.list_attention_for_owner(
            TENANT,
            owner_person_id=OWNER,
            limit=101,
        )
