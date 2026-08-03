"""Persistence and transactional outbox tests for the target workflow."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from larkflow.workflow import (
    AuditEvent,
    InMemoryWorkflowRepository,
    InstanceNotFoundError,
    InstanceSnapshot,
    InvalidOutboxClaimError,
    NodeRunner,
    NodeSpec,
    OutboxEvent,
    OutboxStatus,
    WorkflowInstance,
    WorkflowService,
    available_migrations,
)
from larkflow.workflow.serde import snapshot_from_dict, snapshot_to_dict


NOW = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


def snapshot() -> InstanceSnapshot:
    return InstanceSnapshot(
        goal="publish",
        inputs={"brief": {"title": "Launch"}},
        nodes=(
            NodeSpec(
                "approve_brief",
                "Approve brief",
                "person_owner",
                "human",
                work={
                    "objective": "Approve the brief",
                    "inputs": [{"ref": "instance.inputs.brief"}],
                    "outputs": [{"id": "approval", "type": "decision"}],
                    "acceptance": ["A decision exists"],
                },
            ),
            NodeSpec(
                "publish",
                "Publish",
                "person_owner",
                "tool",
                deps=("approve_brief",),
                work={
                    "objective": "Publish the approved brief",
                    "inputs": [{"ref": "nodes.approve_brief.outputs.approval"}],
                    "outputs": [{"id": "document", "type": "document"}],
                    "acceptance": ["A document URL exists"],
                    "tool": {"kind": "document.publish", "args": {}},
                },
            ),
        ),
    )


def instance(tenant_id: str = "tenant_1") -> WorkflowInstance:
    return WorkflowInstance(
        id="instance_1",
        tenant_id=tenant_id,
        owner_person_id="person_owner",
        snapshot=snapshot(),
        created_at=NOW,
    )


def test_snapshot_json_round_trip_preserves_runtime_contract():
    original = snapshot()
    restored = snapshot_from_dict(snapshot_to_dict(original))

    assert restored == original
    with pytest.raises(TypeError):
        restored.inputs["brief"]["title"] = "Changed"  # type: ignore[index]


def test_packaged_migration_contains_required_tables_and_guards():
    migrations = available_migrations()

    assert [version for version, _ in migrations] == [
        "0001_workflow",
        "0002_runtime_claim_owner",
        "0003_inbound_task_events",
        "0004_inbox_verification",
        "0005_template_lifecycle",
        "0006_inbox_verification_exhaustion",
        "0007_edge_devices",
        "0008_im_commands",
        "0009_owner_instance_list",
        "0010_restart_previews",
        "0011_restart_scope",
        "0012_graph_edit_previews",
        "0013_im_command_mentions",
        "0014_role_binding_cards",
    ]
    sql = migrations[0][1]
    for table in (
        "workflow_templates",
        "workflow_template_versions",
        "workflow_instances",
        "workflow_node_instances",
        "workflow_node_attempts",
        "workflow_projections",
        "workflow_audit_events",
        "workflow_outbox_events",
    ):
        assert f"CREATE TABLE {table}" in sql
    assert "workflow_template_versions_immutable" in sql
    assert "workflow_audit_events_append_only" in sql
    assert "ADD COLUMN claimed_by" in migrations[1][1]
    assert "CREATE TABLE workflow_inbox_events" in migrations[2][1]
    assert "workflow_projection_external_identity_idx" in migrations[2][1]
    assert "ADD COLUMN verified_payload" in migrations[3][1]
    assert "ADD COLUMN version" in migrations[4][1]
    assert "CREATE TABLE workflow_template_events" in migrations[4][1]
    assert "workflow_template_events_append_only" in migrations[4][1]
    assert "'exhausted'" in migrations[5][1]
    assert "CREATE TABLE workflow_edge_pairing_tickets" in migrations[6][1]
    assert "CREATE TABLE workflow_edge_devices" in migrations[6][1]
    assert "CREATE TABLE workflow_edge_events" in migrations[6][1]
    assert "workflow_edge_events_append_only" in migrations[6][1]
    assert "CREATE TABLE workflow_im_commands" in migrations[7][1]
    assert "workflow_im_command_claimable_idx" in migrations[7][1]
    assert "workflow_im_reply_claimable_idx" in migrations[7][1]
    assert "workflow_instances_owner_recent_idx" in migrations[8][1]
    assert "CREATE TABLE workflow_restart_previews" in migrations[9][1]
    assert "workflow_restart_previews_open_idx" in migrations[9][1]
    assert "ADD COLUMN scope" in migrations[10][1]
    assert "workflow_restart_previews_scope_check" in migrations[10][1]
    assert "CREATE TABLE workflow_graph_edit_previews" in migrations[11][1]
    assert "workflow_graph_edit_previews_open_idx" in migrations[11][1]
    assert "ADD COLUMN mentions" in migrations[12][1]
    assert "workflow_im_commands_mentions_array" in migrations[12][1]
    assert "CREATE TABLE workflow_role_binding_actions" in migrations[13][1]
    assert "reply_kind IN ('text', 'role_binding_card')" in migrations[13][1]


def test_in_memory_repository_is_tenant_scoped():
    repository = InMemoryWorkflowRepository()
    repository.add(instance("tenant_1"))
    repository.add(instance("tenant_2"))

    assert repository.get("tenant_1", "instance_1").tenant_id == "tenant_1"
    assert repository.get("tenant_2", "instance_1").tenant_id == "tenant_2"
    with pytest.raises(InstanceNotFoundError):
        repository.get("tenant_missing", "instance_1")


def test_service_persists_audit_and_outbox_with_state_changes():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(
        repository,
        runner=NodeRunner(token_factory=lambda: "node-claim"),
        clock=lambda: NOW,
    )
    service.create_draft(
        instance_id="instance_1",
        tenant_id="tenant_1",
        owner_person_id="person_owner",
        snapshot=snapshot(),
        actor_person_id="person_owner",
        correlation_id="correlation-create",
    )
    service.confirm_draft(
        "tenant_1",
        "instance_1",
        actor_person_id="person_owner",
        correlation_id="correlation-confirm",
    )
    activation = service.dispatch_ready(
        "tenant_1",
        "instance_1",
        correlation_id="correlation-dispatch",
    )[0]

    audit = repository.audit_log("tenant_1", "instance_1")
    assert [event.event_type for event in audit] == [
        "instance.draft_created",
        "instance.confirmed",
        "node.activated",
    ]
    assert audit[-1].node_key == "approve_brief"
    records = repository.outbox_records("tenant_1")
    assert [record.event.event_type for record in records] == [
        "node.projection_create_requested",
        "node.projection_create_requested",
        "node.projection_sync_requested",
    ]
    assert activation.claim_token is None


def test_failed_command_does_not_append_audit_or_outbox():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=lambda: NOW)
    service.create_draft(
        instance_id="instance_1",
        tenant_id="tenant_1",
        owner_person_id="person_owner",
        snapshot=snapshot(),
        actor_person_id="person_owner",
    )
    before_audit = repository.audit_log("tenant_1", "instance_1")
    before_outbox = repository.outbox_records("tenant_1")

    with pytest.raises(PermissionError):
        service.confirm_draft(
            "tenant_1",
            "instance_1",
            actor_person_id="person_other",
        )

    assert repository.audit_log("tenant_1", "instance_1") == before_audit
    assert repository.outbox_records("tenant_1") == before_outbox


def test_outbox_claim_retry_expiry_and_completion():
    repository = InMemoryWorkflowRepository()
    aggregate = instance()
    audit = AuditEvent(
        id="audit_1",
        tenant_id="tenant_1",
        instance_id="instance_1",
        event_type="instance.draft_created",
        source="test",
        correlation_id="correlation_1",
        aggregate_version=0,
        occurred_at=NOW,
    )
    event = OutboxEvent(
        id="outbox_1",
        tenant_id="tenant_1",
        aggregate_type="instance",
        aggregate_id="instance_1",
        aggregate_version=0,
        event_type="instance.created",
        payload={"instance_id": "instance_1"},
        created_at=NOW,
        available_at=NOW,
    )
    repository.add(aggregate, audit_events=(audit,), outbox_events=(event,))

    first = repository.claim_outbox(
        "tenant_1",
        worker_id="worker_1",
        now=NOW,
        claim_ttl=timedelta(minutes=1),
    )[0]
    assert first.attempt_count == 1
    with pytest.raises(InvalidOutboxClaimError):
        repository.mark_outbox_published(
            "tenant_1", "outbox_1", claim_token="wrong", now=NOW
        )

    retry_at = NOW + timedelta(minutes=5)
    repository.mark_outbox_failed(
        "tenant_1",
        "outbox_1",
        claim_token=first.claim_token,
        error="temporary",
        retry_at=retry_at,
    )
    assert repository.claim_outbox("tenant_1", worker_id="worker_2", now=NOW) == ()

    second = repository.claim_outbox(
        "tenant_1", worker_id="worker_2", now=retry_at
    )[0]
    assert second.attempt_count == 2
    expired = repository.claim_outbox(
        "tenant_1",
        worker_id="worker_3",
        now=second.claim_expires_at,
    )[0]
    assert expired.attempt_count == 3
    repository.mark_outbox_published(
        "tenant_1",
        "outbox_1",
        claim_token=expired.claim_token,
        now=expired.claim_expires_at,
    )
    record = repository.outbox_records("tenant_1")[0]
    assert record.status == OutboxStatus.PUBLISHED
    assert repository.claim_outbox(
        "tenant_1", worker_id="worker_4", now=expired.claim_expires_at
    ) == ()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"worker_id": "", "now": NOW}, "worker_id"),
        ({"worker_id": "worker", "now": NOW, "limit": 0}, "limit"),
        (
            {"worker_id": "worker", "now": NOW, "claim_ttl": timedelta(0)},
            "claim_ttl",
        ),
    ],
)
def test_outbox_claim_rejects_invalid_lease_parameters(kwargs, message):
    repository = InMemoryWorkflowRepository()

    with pytest.raises(ValueError, match=message):
        repository.claim_outbox("tenant_1", **kwargs)
