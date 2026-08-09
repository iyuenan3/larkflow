"""Participant-scoped Console Human task and transfer tests."""
from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from larkflow.workflow.console import (
    ConsolePrincipal,
    ConsoleReadService,
    ConsoleResourceNotFoundError,
    StaticConsoleAuthenticator,
)
from larkflow.workflow.console_http import ConsoleHttpApplication
from larkflow.workflow.console_tasks import (
    ConsoleTaskNotFoundError,
    ConsoleTaskService,
)
from larkflow.workflow.directory import DirectoryPerson
from larkflow.workflow.model import InstanceSnapshot, NodeSpec, NodeStatus
from larkflow.workflow.repository import InMemoryWorkflowRepository
from larkflow.workflow.service import WorkflowService


NOW = datetime(2026, 8, 9, 4, 0, tzinfo=timezone.utc)
TENANT = "tenant_console_tasks"
OWNER = "person_instance_owner"
ASSIGNEE = "person_current_assignee"
NEW_ASSIGNEE = "person_new_assignee"
TOKEN = "console-task-token-with-at-least-thirty-two-characters"


class Directory:
    people = {
        OWNER: "流程发起人",
        ASSIGNEE: "当前负责人",
        NEW_ASSIGNEE: "新负责人",
    }

    def get_person(self, tenant_id: str, person_id: str) -> DirectoryPerson:
        assert tenant_id == TENANT
        return DirectoryPerson(
            person_id=person_id,
            active=person_id in self.people,
            name=self.people.get(person_id, ""),
        )

    def list_candidate_people(
        self,
        tenant_id: str,
        *,
        limit: int,
    ) -> tuple[DirectoryPerson, ...]:
        assert tenant_id == TENANT
        return tuple(
            DirectoryPerson(person_id=person_id, active=True, name=name)
            for person_id, name in list(self.people.items())[:limit]
        )


def snapshot(*, decision: bool = False) -> InstanceSnapshot:
    work = {
        "objective": "Write the verified release assessment",
        "inputs": ["instance_inputs.brief"],
        "outputs": [{"id": "content", "type": "data"}],
        "acceptance": ["Assessment is explicit", "Evidence is cited"],
    }
    nodes = []
    if decision:
        nodes.append(
            NodeSpec(
                "draft",
                "Prepare assessment",
                OWNER,
                "human",
                work={**work, "inputs": []},
            )
        )
        work["decision"] = {"kind": "accept_reject", "reject_target": "draft"}
    nodes.append(
        NodeSpec(
            "review",
            "Review assessment",
            ASSIGNEE,
            "human",
            deps=("draft",) if decision else (),
            work=work,
        )
    )
    return InstanceSnapshot(
        goal="Review a release assessment",
        inputs={"brief": "Release candidate 7 has passed deterministic checks."},
        nodes=tuple(nodes),
    )


def setup_waiting_task(*, decision: bool = False):
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(
        repository,
        directory=Directory(),
        clock=lambda: NOW,
    )
    service.create_draft(
        instance_id="instance_collaboration",
        tenant_id=TENANT,
        owner_person_id=OWNER,
        actor_person_id=OWNER,
        snapshot=snapshot(decision=decision),
    )
    service.confirm_draft(
        TENANT,
        "instance_collaboration",
        actor_person_id=OWNER,
    )
    activations = service.dispatch_ready(
        TENANT,
        "instance_collaboration",
        max_automated=0,
    )
    if decision:
        draft = activations[0]
        service.submit_human(
            TENANT,
            "instance_collaboration",
            "draft",
            actor_person_id=OWNER,
            attempt_no=draft.attempt_no,
            expected_node_version=draft.expected_node_version,
            result={"content": "Assessment ready for review."},
        )
        service.dispatch_ready(
            TENANT,
            "instance_collaboration",
            max_automated=0,
        )
    return repository, service, ConsoleTaskService(service)


def principal(person_id: str) -> ConsolePrincipal:
    return ConsolePrincipal(tenant_id=TENANT, person_id=person_id)


def test_assignee_can_see_bounded_task_without_owner_instance_visibility():
    repository, _, tasks = setup_waiting_task()

    listing = tasks.list_tasks(principal(ASSIGNEE))
    detail = tasks.get_task(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
    )

    assert listing["total"] == 1
    assert listing["tasks"][0]["instance_owner_relation"] == "collaborator"
    assert detail["task"]["work"]["objective"] == "Write the verified release assessment"
    assert detail["task"]["work"]["acceptance"] == [
        "Assessment is explicit",
        "Evidence is cited",
    ]
    assert detail["task"]["work"]["context"]["instance_inputs"] == {
        "brief": "Release candidate 7 has passed deterministic checks."
    }
    with pytest.raises(ConsoleResourceNotFoundError):
        ConsoleReadService(repository).get_instance(
            principal(ASSIGNEE),
            "instance_collaboration",
        )
    with pytest.raises(ConsoleTaskNotFoundError):
        tasks.get_task(principal(NEW_ASSIGNEE), "instance_collaboration", "review")


def test_transfer_preserves_authored_owner_and_moves_runtime_authority():
    repository, service, tasks = setup_waiting_task()
    before = tasks.get_task(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
    )["task"]

    result = tasks.transfer(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
        attempt_no=before["node"]["attempt_no"],
        expected_node_version=before["node"]["version"],
        new_owner_person_id=NEW_ASSIGNEE,
    )

    instance = service.get(TENANT, "instance_collaboration")
    assert result["assigned_to"] == "collaborator"
    assert result["projection"] == {
        "kind": "feishu_task",
        "status": "queued",
        "message": "负责人已更换，飞书待办正在同步。",
    }
    assert instance.snapshot.node("review").owner_person_id == ASSIGNEE
    assert instance.nodes["review"].owner_person_id == NEW_ASSIGNEE
    assert tasks.list_tasks(principal(ASSIGNEE))["total"] == 0
    assert tasks.list_tasks(principal(NEW_ASSIGNEE))["total"] == 1
    audit = repository.recent_audit_log(TENANT, instance.id)
    transferred = [event for event in audit if event.event_type == "node.human_task_transferred"]
    assert len(transferred) == 1
    assert transferred[0].payload == {
        "from_owner_person_id": ASSIGNEE,
        "to_owner_person_id": NEW_ASSIGNEE,
        "authored_owner_preserved": True,
    }
    sync = [
        record.event
        for record in repository.outbox_records(TENANT)
        if record.event.payload.get("transfer_from_person_id") == ASSIGNEE
    ]
    assert len(sync) == 1
    assert sync[0].payload["node_key"] == "review"


def test_new_assignee_can_submit_and_old_assignee_cannot():
    _, service, tasks = setup_waiting_task()
    before = tasks.get_task(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
    )["task"]
    tasks.transfer(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
        attempt_no=before["node"]["attempt_no"],
        expected_node_version=before["node"]["version"],
        new_owner_person_id=NEW_ASSIGNEE,
    )
    current = tasks.get_task(
        principal(NEW_ASSIGNEE),
        "instance_collaboration",
        "review",
    )["task"]

    with pytest.raises(ConsoleTaskNotFoundError):
        tasks.submit(
            principal(ASSIGNEE),
            "instance_collaboration",
            "review",
            attempt_no=current["node"]["attempt_no"],
            expected_node_version=current["node"]["version"],
            content="Old owner result",
        )
    result = tasks.submit(
        principal(NEW_ASSIGNEE),
        "instance_collaboration",
        "review",
        attempt_no=current["node"]["attempt_no"],
        expected_node_version=current["node"]["version"],
        content="Release assessment accepted with evidence.",
    )

    assert result["node_status"] == NodeStatus.DONE.value
    assert service.get(TENANT, "instance_collaboration").current_attempt(
        "review"
    ).result == {"content": "Release assessment accepted with evidence."}


def test_decision_is_visible_as_bounded_work_without_transfer_authority():
    repository, _, tasks = setup_waiting_task(decision=True)

    listing = tasks.list_tasks(principal(ASSIGNEE))
    detail = tasks.get_task(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
    )["task"]

    assert listing["total"] == 1
    assert listing["tasks"][0]["kind"] == "decision"
    assert detail["kind"] == "decision"
    assert detail["actions"] == {
        "submit": False,
        "transfer": False,
        "accept": True,
        "reject": True,
    }
    assert detail["work"]["decision"] == {
        "kind": "accept_reject",
        "reject_target": "draft",
    }
    assert detail["work"]["context"]["dependencies"]["draft"] == {
        "content": "Assessment ready for review."
    }
    with pytest.raises(ConsoleTaskNotFoundError):
        tasks.get_task(principal(NEW_ASSIGNEE), "instance_collaboration", "review")
    assert not [
        event
        for event in repository.recent_audit_log(TENANT, "instance_collaboration")
        if event.event_type.startswith("node.human_decision_")
    ]


def test_assignee_can_accept_decision_from_workbench():
    _, service, tasks = setup_waiting_task(decision=True)
    current = tasks.get_task(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
    )["task"]

    result = tasks.submit_decision(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
        attempt_no=current["node"]["attempt_no"],
        expected_instance_version=current["instance_version"],
        expected_node_version=current["node"]["version"],
        decision="accept",
        feedback=None,
    )

    assert result["action"] == "submit_human_decision"
    assert result["decision"] == "accept"
    assert result["instance_status"] == "done"
    assert service.get(TENANT, "instance_collaboration").current_attempt(
        "review"
    ).result == {"decision": "accepted"}
    assert tasks.list_tasks(principal(ASSIGNEE))["total"] == 0


def test_reject_decision_requires_feedback_and_preserves_it():
    _, service, tasks = setup_waiting_task(decision=True)
    current = tasks.get_task(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
    )["task"]
    binding = {
        "attempt_no": current["node"]["attempt_no"],
        "expected_instance_version": current["instance_version"],
        "expected_node_version": current["node"]["version"],
        "decision": "reject",
    }

    with pytest.raises(ValueError, match="必须填写具体意见"):
        tasks.submit_decision(
            principal(ASSIGNEE),
            "instance_collaboration",
            "review",
            feedback="",
            **binding,
        )
    result = tasks.submit_decision(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
        feedback="请补充回滚条件。",
        **binding,
    )

    assert result["decision"] == "reject"
    assert result["instance_status"] == "failed"
    assert service.get(TENANT, "instance_collaboration").current_attempt(
        "review"
    ).result == {
        "decision": "rejected",
        "feedback": "请补充回滚条件。",
    }


def test_http_task_routes_require_version_bound_json_and_hide_full_instance():
    repository, service, tasks = setup_waiting_task()
    app = ConsoleHttpApplication(
        ConsoleReadService(repository),
        StaticConsoleAuthenticator(TOKEN, principal(ASSIGNEE)),
        task_service=tasks,
    )
    auth = {"authorization": f"Bearer {TOKEN}"}

    assert app.handle(
        "GET",
        "/console/api/v1/instances/instance_collaboration",
        headers=auth,
    ).status == 404
    people = json.loads(app.handle(
        "GET",
        "/console/api/v1/people?limit=100",
        headers=auth,
    ).body)
    assert {item["name"] for item in people["people"]} == {"流程发起人", "新负责人"}
    task = tasks.get_task(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
    )["task"]
    body = json.dumps(
        {
            "attempt_no": task["node"]["attempt_no"],
            "expected_node_version": task["node"]["version"],
            "new_owner_person_id": NEW_ASSIGNEE,
        },
        separators=(",", ":"),
    ).encode()
    headers = {
        **auth,
        "content-type": "application/json",
        "content-length": str(len(body)),
        "x-larkflow-console-action": "workflow-action-v1",
    }

    response = app.handle(
        "POST",
        "/console/api/v1/tasks/instance_collaboration/nodes/review/transfer",
        headers=headers,
        body=body,
    )

    assert response.status == 200
    assert service.get(TENANT, "instance_collaboration").nodes[
        "review"
    ].owner_person_id == NEW_ASSIGNEE
    missing_header = dict(headers)
    missing_header.pop("x-larkflow-console-action")
    assert app.handle(
        "POST",
        "/console/api/v1/tasks/instance_collaboration/nodes/review/transfer",
        headers=missing_header,
        body=body,
    ).status == 403


def test_http_decision_route_is_version_bound_and_owner_scoped():
    repository, service, tasks = setup_waiting_task(decision=True)
    app = ConsoleHttpApplication(
        ConsoleReadService(repository),
        StaticConsoleAuthenticator(TOKEN, principal(ASSIGNEE)),
        task_service=tasks,
    )
    current = tasks.get_task(
        principal(ASSIGNEE),
        "instance_collaboration",
        "review",
    )["task"]
    body = json.dumps(
        {
            "attempt_no": current["node"]["attempt_no"],
            "expected_instance_version": current["instance_version"],
            "expected_node_version": current["node"]["version"],
            "decision": "accept",
            "feedback": None,
        },
        separators=(",", ":"),
    ).encode()
    headers = {
        "authorization": f"Bearer {TOKEN}",
        "content-type": "application/json",
        "content-length": str(len(body)),
        "x-larkflow-console-action": "workflow-action-v1",
    }

    response = app.handle(
        "POST",
        "/console/api/v1/tasks/instance_collaboration/nodes/review/decision",
        headers=headers,
        body=body,
    )

    assert response.status == 200
    assert json.loads(response.body)["decision"] == "accept"
    assert service.get(TENANT, "instance_collaboration").status.value == "done"
    assert app.handle(
        "POST",
        "/console/api/v1/tasks/instance_collaboration/nodes/review/decision",
        headers=headers,
        body=body,
    ).status == 404
