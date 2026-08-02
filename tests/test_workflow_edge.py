"""Personal Agent Edge proof tests at the central trust boundary."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest

from larkflow.workflow.edge import (
    DeviceRevokedError,
    EdgeControlService,
    InMemoryEdgeStore,
    InvalidPairingCodeError,
    PairingCodeUsedError,
    PERSONAL_READONLY_CAPABILITY,
    UnsupportedEdgeCapabilityError,
)
from larkflow.workflow.model import ExecutorKind, InstanceSnapshot, NodeSpec
from larkflow.workflow.repository import InMemoryWorkflowRepository
from larkflow.workflow.runner import NodeRunner, StaleAttemptError
from larkflow.workflow.runtime import WorkflowWorker
from larkflow.workflow.service import WorkflowService


NOW = datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)
TENANT = "tenant_edge"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def work(kind: str) -> dict:
    return {
        "objective": "Review the authorized local workspace",
        "inputs": [],
        "outputs": [{"id": "content", "type": "text"}],
        "acceptance": ["A review exists"],
        "agent": {
            "kind": kind,
            "instructions": "Read only and return a concise review",
        },
    }


def build_edge(
    *,
    owner_person_id: str = "person_owner",
    include_central_node: bool = False,
    max_result_bytes: int = 100_000,
):
    clock = Clock()
    repository = InMemoryWorkflowRepository()
    claim_tokens = iter(("claim_1", "claim_2", "claim_3"))
    workflow = WorkflowService(
        repository,
        runner=NodeRunner(
            claim_ttl=timedelta(minutes=5),
            token_factory=lambda: next(claim_tokens),
        ),
        clock=clock,
    )
    nodes = []
    if include_central_node:
        nodes.append(
            NodeSpec(
                "central_generate",
                "Central generate",
                owner_person_id,
                "agent",
                work=work("llm.generate"),
            )
        )
    nodes.append(
        NodeSpec(
            "local_review",
            "Local review",
            owner_person_id,
            "agent",
            work=work(PERSONAL_READONLY_CAPABILITY),
        )
    )
    workflow.create_draft(
        instance_id="instance_edge",
        tenant_id=TENANT,
        owner_person_id=owner_person_id,
        actor_person_id=owner_person_id,
        snapshot=InstanceSnapshot(nodes=tuple(nodes)),
    )
    workflow.confirm_draft(
        TENANT,
        "instance_edge",
        actor_person_id=owner_person_id,
    )
    ids = (f"id_{index}" for index in range(100))
    secrets = (f"secret_{index}" for index in range(100))
    store = InMemoryEdgeStore()
    edge = EdgeControlService(
        store,
        workflow,
        repository,
        clock=clock,
        id_factory=lambda: next(ids),
        secret_factory=lambda: next(secrets),
        max_result_bytes=max_result_bytes,
    )
    return clock, workflow, repository, store, edge


def pair(edge: EdgeControlService, *, person_id: str = "person_owner", name="Mac"):
    grant = edge.issue_pairing(
        tenant_id=TENANT,
        person_id=person_id,
        actor_person_id=person_id,
    )
    return edge.pair_device(
        grant.code,
        name=name,
        capabilities=(PERSONAL_READONLY_CAPABILITY,),
    )


def test_pairing_code_is_one_time_and_raw_secrets_are_not_stored():
    _, _, _, store, edge = build_edge()
    grant = edge.issue_pairing(
        tenant_id=TENANT,
        person_id="person_owner",
        actor_person_id="person_owner",
    )

    paired = edge.pair_device(
        grant.code,
        name="Owner Mac",
        capabilities=(PERSONAL_READONLY_CAPABILITY,),
    )

    assert paired.credential.startswith(f"{paired.device.id}.")
    assert grant.code not in paired.device.credential_hash
    assert paired.credential not in paired.device.credential_hash
    assert store.list_devices(TENANT) == (paired.device,)
    with pytest.raises(PairingCodeUsedError):
        edge.pair_device(
            grant.code,
            name="Replay",
            capabilities=(PERSONAL_READONLY_CAPABILITY,),
        )


def test_invalid_or_escalated_pairing_is_rejected():
    _, _, _, _, edge = build_edge()
    with pytest.raises(InvalidPairingCodeError):
        edge.pair_device(
            "not-a-code",
            name="Mac",
            capabilities=(PERSONAL_READONLY_CAPABILITY,),
        )

    grant = edge.issue_pairing(
        tenant_id=TENANT,
        person_id="person_owner",
        actor_person_id="person_owner",
    )
    with pytest.raises(UnsupportedEdgeCapabilityError):
        edge.pair_device(
            grant.code,
            name="Mac",
            capabilities=("llm.generate",),
        )


def test_device_claims_only_its_owner_and_explicit_personal_capability():
    _, _, _, _, edge = build_edge(include_central_node=True)
    other = pair(edge, person_id="person_other", name="Other Mac")
    assert edge.claim(other.credential) is None

    owner = pair(edge)
    lease = edge.claim(owner.credential)

    assert lease is not None
    assert lease.device_id == owner.device.id
    assert lease.request.node_key == "local_review"
    assert lease.request.work["agent"]["kind"] == PERSONAL_READONLY_CAPABILITY
    assert lease.request.owner_person_id == "person_owner"


def test_central_agent_worker_does_not_claim_a_personal_edge_node():
    clock, workflow, repository, _, _ = build_edge()

    class CentralOnlyExecutor:
        def accepts(self, *, executor, work):
            agent = work.get("agent")
            return (
                executor == ExecutorKind.AGENT
                and isinstance(agent, Mapping)
                and agent.get("kind") == "llm.generate"
            )

        def execute(self, _request):
            raise AssertionError("personal Edge work reached the central executor")

    report = WorkflowWorker(
        workflow,
        repository,
        tenant_id=TENANT,
        worker_id="central_worker",
        executors={ExecutorKind.AGENT: CentralOnlyExecutor()},
        clock=clock,
    ).run_once()

    assert report.automated_claimed == 0
    assert workflow.get(TENANT, "instance_edge").current_attempt(
        "local_review"
    ).claimed_by is None


def test_device_can_renew_and_complete_the_existing_attempt_claim():
    clock, workflow, repository, store, edge = build_edge()
    device = pair(edge)
    lease = edge.claim(device.credential)
    assert lease is not None
    original_expiry = lease.request.claim_expires_at

    clock.now += timedelta(minutes=2)
    renewed = edge.renew(
        device.credential,
        instance_id=lease.request.instance_id,
        node_key=lease.request.node_key,
        attempt_no=lease.request.attempt_no,
        expected_node_version=lease.request.expected_node_version,
        claim_token=lease.request.claim_token,
    )
    assert renewed > original_expiry

    edge.complete(
        device.credential,
        instance_id=lease.request.instance_id,
        node_key=lease.request.node_key,
        attempt_no=lease.request.attempt_no,
        expected_node_version=lease.request.expected_node_version,
        claim_token=lease.request.claim_token,
        result={"content": "Read-only review"},
    )
    finished = workflow.get(TENANT, "instance_edge")
    assert finished.current_attempt("local_review").result["content"] == (
        "Read-only review"
    )
    assert "node.claim_renewed" in {
        event.event_type
        for event in repository.audit_log(TENANT, "instance_edge")
    }
    assert [event.event_type for event in store.audit_log(TENANT)] == [
        "edge.pairing_issued",
        "edge.device_paired",
    ]


def test_clock_rollback_does_not_shorten_a_live_claim():
    clock, _, _, _, edge = build_edge()
    device = pair(edge)
    lease = edge.claim(device.credential)
    assert lease is not None
    clock.now -= timedelta(hours=1)

    renewed = edge.renew(
        device.credential,
        instance_id=lease.request.instance_id,
        node_key=lease.request.node_key,
        attempt_no=lease.request.attempt_no,
        expected_node_version=lease.request.expected_node_version,
        claim_token=lease.request.claim_token,
    )

    assert renewed == lease.request.claim_expires_at


def test_capability_collection_cannot_be_a_bare_string():
    _, workflow, repository, store, _ = build_edge()
    with pytest.raises(ValueError, match="must be a collection"):
        EdgeControlService(
            store,
            workflow,
            repository,
            supported_capabilities=PERSONAL_READONLY_CAPABILITY,
        )


def test_revocation_blocks_result_submission_and_leaves_claim_to_expire():
    _, workflow, _, _, edge = build_edge()
    device = pair(edge)
    lease = edge.claim(device.credential)
    assert lease is not None

    edge.revoke_device(
        tenant_id=TENANT,
        device_id=device.device.id,
        actor_person_id="person_owner",
        reason="lost device",
    )
    with pytest.raises(DeviceRevokedError):
        edge.complete(
            device.credential,
            instance_id=lease.request.instance_id,
            node_key=lease.request.node_key,
            attempt_no=lease.request.attempt_no,
            expected_node_version=lease.request.expected_node_version,
            claim_token=lease.request.claim_token,
            result={"content": "must be rejected"},
        )
    assert workflow.get(TENANT, "instance_edge").current_attempt(
        "local_review"
    ).result is None


def test_recovery_rejects_the_old_devices_late_result():
    clock, workflow, _, _, edge = build_edge()
    first_device = pair(edge, name="First Mac")
    second_device = pair(edge, name="Second Mac")
    first = edge.claim(first_device.credential)
    assert first is not None

    clock.now += timedelta(minutes=5)
    recovered = edge.claim(second_device.credential)
    assert recovered is not None
    assert recovered.request.attempt_id == first.request.attempt_id
    assert recovered.request.claim_token != first.request.claim_token

    with pytest.raises(StaleAttemptError):
        edge.complete(
            first_device.credential,
            instance_id=first.request.instance_id,
            node_key=first.request.node_key,
            attempt_no=first.request.attempt_no,
            expected_node_version=first.request.expected_node_version,
            claim_token=first.request.claim_token,
            result={"content": "late"},
        )
    edge.complete(
        second_device.credential,
        instance_id=recovered.request.instance_id,
        node_key=recovered.request.node_key,
        attempt_no=recovered.request.attempt_no,
        expected_node_version=recovered.request.expected_node_version,
        claim_token=recovered.request.claim_token,
        result={"content": "current"},
    )
    assert workflow.get(TENANT, "instance_edge").current_attempt(
        "local_review"
    ).result["content"] == "current"


def test_edge_result_has_a_server_side_size_limit():
    _, _, _, _, edge = build_edge(max_result_bytes=20)
    device = pair(edge)
    lease = edge.claim(device.credential)
    assert lease is not None

    with pytest.raises(ValueError, match="size limit"):
        edge.complete(
            device.credential,
            instance_id=lease.request.instance_id,
            node_key=lease.request.node_key,
            attempt_no=lease.request.attempt_no,
            expected_node_version=lease.request.expected_node_version,
            claim_token=lease.request.claim_token,
            result={"content": "x" * 100},
        )
