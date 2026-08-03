"""Behavioral tests for safe future-region graph editing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from larkflow.workflow import (
    AuthorizationError,
    GraphEditNotAllowedError,
    GraphEditPreviewExpiredError,
    InMemoryWorkflowRepository,
    InstanceSnapshot,
    InstanceStatus,
    NodeSpec,
    NodeStatus,
    StaleGraphEditPreviewError,
    WorkflowService,
)


NOW = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
TENANT = "tenant_edit"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def work(label: str) -> dict:
    return {
        "objective": f"Complete {label}",
        "inputs": [],
        "outputs": [{"id": "result", "type": "data"}],
        "acceptance": [f"{label} exists"],
    }


def snapshot(*, locked: bool = False) -> InstanceSnapshot:
    return InstanceSnapshot(
        goal="Edit only the future region",
        locked=locked,
        nodes=(
            NodeSpec("brief", "Confirm brief", "owner", "human", work=work("brief")),
            NodeSpec(
                "draft",
                "Draft summary",
                "owner",
                "agent",
                deps=("brief",),
                work=work("draft"),
            ),
            NodeSpec(
                "review",
                "Review summary",
                "owner",
                "human",
                deps=("draft",),
                work=work("review"),
            ),
        ),
    )


def build_service(*, locked: bool = False):
    repository = InMemoryWorkflowRepository()
    clock = Clock()
    identifiers = count(1)
    service = WorkflowService(
        repository,
        clock=clock,
        id_factory=lambda: f"id-{next(identifiers)}",
    )
    service.create_draft(
        instance_id="instance_edit",
        tenant_id=TENANT,
        owner_person_id="owner",
        actor_person_id="owner",
        snapshot=snapshot(locked=locked),
    )
    service.confirm_draft(TENANT, "instance_edit", actor_person_id="owner")
    activation = service.dispatch_ready(TENANT, "instance_edit")[0]
    service.submit_human(
        TENANT,
        "instance_edit",
        "brief",
        actor_person_id="owner",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        result={"confirmed": True},
    )
    return service, repository, clock


def edit_operations() -> list[dict]:
    return [
        {
            "op": "update_node",
            "node_key": "draft",
            "set": {
                "title": "Draft concise summary",
                "work": work("concise draft"),
            },
        },
        {"op": "remove_node", "node_key": "review"},
        {
            "op": "add_node",
            "node": {
                "key": "approve",
                "title": "Approve concise summary",
                "owner_person_id": "owner",
                "executor": "human",
                "deps": ["draft"],
                "work": work("approval"),
            },
        },
    ]


def test_graph_edit_preview_and_confirmation_preserve_frozen_history():
    service, repository, _ = build_service()
    before = service.get(TENANT, "instance_edit")

    preview = service.preview_graph_edit(
        TENANT,
        "instance_edit",
        edit_operations(),
        actor_person_id="owner",
    )

    unchanged = service.get(TENANT, "instance_edit")
    assert unchanged.version == before.version
    assert unchanged.graph_revision == 1
    assert tuple(node.key for node in unchanged.snapshot.nodes) == (
        "brief",
        "draft",
        "review",
    )
    assert preview.added_node_keys == ("approve",)
    assert preview.updated_node_keys == ("draft",)
    assert preview.removed_node_keys == ("review",)
    assert preview.proposed_graph_revision == 2

    confirmation = service.confirm_graph_edit(
        TENANT,
        preview.id,
        actor_person_id="owner",
    )
    edited = confirmation.instance
    assert confirmation.already_applied is False
    assert edited.graph_revision == 2
    assert edited.version == before.version + 1
    assert tuple(node.key for node in edited.snapshot.nodes) == (
        "brief",
        "draft",
        "approve",
    )
    assert edited.snapshot.node("draft").title == "Draft concise summary"
    assert edited.nodes["brief"] == before.nodes["brief"]
    assert edited.current_attempt("brief").result == {"confirmed": True}
    assert edited.nodes["draft"].status == NodeStatus.READY
    assert edited.current_attempt("draft").input_snapshot["work"]["objective"] == (
        "Complete concise draft"
    )
    assert edited.nodes["approve"].status == NodeStatus.PENDING
    assert ("review", 1) not in edited.attempts

    replay = service.confirm_graph_edit(
        TENANT,
        preview.id,
        actor_person_id="owner",
    )
    assert replay.already_applied is True
    assert replay.instance.version == edited.version
    assert len(
        [
            event
            for event in repository.audit_log(TENANT, "instance_edit")
            if event.event_type == "instance.graph_edited"
        ]
    ) == 1


def test_graph_edit_rejects_non_owner_locked_started_and_invalid_graphs():
    service, _, _ = build_service()
    with pytest.raises(AuthorizationError):
        service.preview_graph_edit(
            TENANT,
            "instance_edit",
            edit_operations(),
            actor_person_id="intruder",
        )

    activation = service.dispatch_ready(
        TENANT,
        "instance_edit",
        worker_id="worker",
    )[0]
    assert activation.node_key == "draft"
    with pytest.raises(GraphEditNotAllowedError, match="crossed the edit frontier"):
        service.preview_graph_edit(
            TENANT,
            "instance_edit",
            [
                {
                    "op": "update_node",
                    "node_key": "draft",
                    "set": {"title": "Too late"},
                }
            ],
            actor_person_id="owner",
        )

    service, _, _ = build_service()
    with pytest.raises(GraphEditNotAllowedError, match="cycle"):
        service.preview_graph_edit(
            TENANT,
            "instance_edit",
            [
                {
                    "op": "update_node",
                    "node_key": "draft",
                    "set": {"deps": ["review"]},
                }
            ],
            actor_person_id="owner",
        )

    locked, _, _ = build_service(locked=True)
    with pytest.raises(GraphEditNotAllowedError, match="locked"):
        locked.preview_graph_edit(
            TENANT,
            "instance_edit",
            edit_operations(),
            actor_person_id="owner",
        )


def test_graph_edit_preview_expires_and_becomes_stale_after_dispatch():
    service, _, clock = build_service()
    preview = service.preview_graph_edit(
        TENANT,
        "instance_edit",
        [
            {
                "op": "update_node",
                "node_key": "draft",
                "set": {"title": "Previewed title"},
            }
        ],
        actor_person_id="owner",
    )
    service.dispatch_ready(
        TENANT,
        "instance_edit",
        worker_id="worker",
    )
    with pytest.raises(StaleGraphEditPreviewError, match="changed"):
        service.confirm_graph_edit(
            TENANT,
            preview.id,
            actor_person_id="owner",
        )

    service, _, clock = build_service()
    preview = service.preview_graph_edit(
        TENANT,
        "instance_edit",
        [
            {
                "op": "update_node",
                "node_key": "draft",
                "set": {"title": "Expiring title"},
            }
        ],
        actor_person_id="owner",
    )
    clock.now += timedelta(minutes=16)
    with pytest.raises(GraphEditPreviewExpiredError):
        service.confirm_graph_edit(
            TENANT,
            preview.id,
            actor_person_id="owner",
        )


def test_removing_the_only_future_node_can_complete_the_instance():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=Clock())
    service.create_draft(
        instance_id="instance_short",
        tenant_id=TENANT,
        owner_person_id="owner",
        actor_person_id="owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec("brief", "Brief", "owner", "human", work=work("brief")),
                NodeSpec(
                    "optional",
                    "Optional follow-up",
                    "owner",
                    "human",
                    deps=("brief",),
                    work=work("optional"),
                ),
            )
        ),
    )
    service.confirm_draft(TENANT, "instance_short", actor_person_id="owner")
    activation = service.dispatch_ready(TENANT, "instance_short")[0]
    service.submit_human(
        TENANT,
        "instance_short",
        "brief",
        actor_person_id="owner",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        result={"done": True},
    )
    preview = service.preview_graph_edit(
        TENANT,
        "instance_short",
        [{"op": "remove_node", "node_key": "optional"}],
        actor_person_id="owner",
    )
    confirmation = service.confirm_graph_edit(
        TENANT,
        preview.id,
        actor_person_id="owner",
    )
    assert confirmation.instance.status == InstanceStatus.DONE
    assert tuple(confirmation.instance.nodes) == ("brief",)
    assert any(
        record.event.event_type == "instance.projection_completed_requested"
        for record in repository._outbox.values()
    )
