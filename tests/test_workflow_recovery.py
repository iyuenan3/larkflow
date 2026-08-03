"""Recovery behavior for failed automated workflow Attempts."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from larkflow.workflow import (
    AttemptStatus,
    AuthorizationError,
    InMemoryWorkflowRepository,
    InstanceSnapshot,
    InstanceStatus,
    NodeRunner,
    NodeSpec,
    NodeStatus,
    RecoveryAction,
    StaleRecoveryError,
    WorkflowService,
)


NOW = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
TENANT = "tenant_recovery"


def build_failed_service():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(
        repository,
        runner=NodeRunner(token_factory=lambda: "claim-token"),
        clock=lambda: NOW,
    )
    service.create_draft(
        instance_id="instance_recovery",
        tenant_id=TENANT,
        owner_person_id="person_initiator",
        actor_person_id="person_initiator",
        snapshot=InstanceSnapshot(
            goal="Recover an Agent failure",
            nodes=(
                NodeSpec(
                    "draft",
                    "Generate draft",
                    "person_node_owner",
                    "agent",
                    work={
                        "objective": "Generate the draft",
                        "outputs": [{"id": "draft", "type": "data"}],
                        "acceptance": ["A draft exists"],
                    },
                ),
            ),
        ),
    )
    service.confirm_draft(
        TENANT,
        "instance_recovery",
        actor_person_id="person_initiator",
    )
    activation = service.dispatch_ready(
        TENANT,
        "instance_recovery",
        worker_id="agent_edge",
    )[0]
    failed = service.fail_automated(
        TENANT,
        "instance_recovery",
        "draft",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        claim_token=activation.claim_token or "",
        error_code="provider_timeout",
        error_message="private provider detail",
        worker_id="agent_edge",
    )
    return service, repository, failed


def recover(service, failed, action, *, actor="person_node_owner"):
    node = failed.nodes["draft"]
    return service.recover_failed_node(
        TENANT,
        failed.id,
        "draft",
        action,
        actor_person_id=actor,
        expected_instance_version=failed.version,
        expected_node_version=node.version,
        expected_attempt_no=node.current_attempt_no,
        correlation_id="recovery-card-event",
    )


def test_retry_creates_a_new_automated_attempt_and_preserves_failure():
    service, repository, failed = build_failed_service()

    recovered = recover(service, failed, RecoveryAction.RETRY)

    assert recovered.status == InstanceStatus.RUNNING
    assert recovered.nodes["draft"].status == NodeStatus.READY
    assert recovered.nodes["draft"].current_attempt_no == 2
    assert recovered.attempts[("draft", 1)].status == AttemptStatus.FAILED
    assert recovered.attempts[("draft", 1)].error_code == "provider_timeout"
    assert recovered.attempts[("draft", 2)].status == AttemptStatus.PENDING
    assert repository.audit_log(TENANT, failed.id)[-1].event_type == (
        "node.automated_retry_started"
    )


def test_human_takeover_creates_a_new_waiting_attempt_and_can_finish():
    service, repository, failed = build_failed_service()

    recovered = recover(service, failed, RecoveryAction.HUMAN_TAKEOVER)
    node = recovered.nodes["draft"]

    assert recovered.status == InstanceStatus.RUNNING
    assert node.executor.value == "agent"
    assert node.status == NodeStatus.WAITING_HUMAN
    assert node.current_attempt_no == 2
    assert recovered.attempts[("draft", 1)].status == AttemptStatus.FAILED
    assert recovered.current_attempt("draft").status == AttemptStatus.WAITING_HUMAN
    assert recovered.current_attempt("draft").input_snapshot["instance_inputs"] == {}
    assert repository.audit_log(TENANT, failed.id)[-1].event_type == (
        "node.human_takeover_started"
    )
    assert any(
        record.event.payload.get("recovery_action") == "human_takeover"
        for record in repository.outbox_records(TENANT)
    )

    completed = service.submit_human(
        TENANT,
        recovered.id,
        "draft",
        actor_person_id="person_node_owner",
        attempt_no=2,
        expected_node_version=node.version,
        result={"content": "Human replacement draft"},
    )

    assert completed.status == InstanceStatus.DONE
    assert completed.current_attempt("draft").submitted_by_person_id == (
        "person_node_owner"
    )


def test_recovery_reauthorizes_owner_and_rejects_stale_cards():
    service, _repository, failed = build_failed_service()

    with pytest.raises(AuthorizationError):
        recover(service, failed, RecoveryAction.HUMAN_TAKEOVER, actor="person_initiator")

    recover(service, failed, RecoveryAction.RETRY)
    with pytest.raises(StaleRecoveryError):
        recover(service, failed, RecoveryAction.HUMAN_TAKEOVER)


def test_restart_of_human_takeover_requests_old_task_completion():
    service, repository, failed = build_failed_service()
    takeover = recover(service, failed, RecoveryAction.HUMAN_TAKEOVER)
    preview = service.preview_node_restart(
        TENANT,
        takeover.id,
        "draft",
        actor_person_id="person_initiator",
    )

    service.confirm_restart(
        TENANT,
        preview.id,
        actor_person_id="person_initiator",
    )

    matching = [
        record.event
        for record in repository.outbox_records(TENANT)
        if record.event.payload.get("attempt_no") == 2
        and record.event.payload.get("status") == "canceled"
    ]
    assert len(matching) == 1
