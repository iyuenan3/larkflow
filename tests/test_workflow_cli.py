"""Target CLI document and status projection tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from larkflow.workflow import (
    InMemoryWorkflowRepository,
    InstanceSnapshot,
    NodeRunner,
    NodeSpec,
    WorkflowService,
)
from larkflow.workflow.cli import _instance_payload, _load_mapping


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def work():
    return {
        "objective": "Do the work",
        "inputs": [],
        "outputs": [{"id": "result", "type": "data"}],
        "acceptance": ["The result exists"],
        "prompt": "Do it",
    }


def test_cli_document_loader_accepts_utf8_bom_yaml(tmp_path):
    source = tmp_path / "draft.yaml"
    source.write_text("\ufeffinstance_id: instance_1\nnodes: []\n", encoding="utf-8")

    assert _load_mapping(str(source)) == {
        "instance_id": "instance_1",
        "nodes": [],
    }


def test_status_projection_never_exposes_claim_token():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(
        repository,
        runner=NodeRunner(
            claim_ttl=timedelta(minutes=5),
            token_factory=lambda: "secret-claim-token",
        ),
        clock=lambda: NOW,
    )
    service.create_draft(
        instance_id="instance_1",
        tenant_id="tenant_1",
        owner_person_id="owner_1",
        actor_person_id="owner_1",
        snapshot=InstanceSnapshot(
            nodes=(NodeSpec("agent", "Agent", "owner_1", "agent", work=work()),)
        ),
    )
    service.confirm_draft("tenant_1", "instance_1", actor_person_id="owner_1")
    service.dispatch_due(
        "tenant_1",
        "instance_1",
        worker_id="worker_1",
    )

    payload = _instance_payload(service.get("tenant_1", "instance_1"))

    assert payload["nodes"][0]["attempt"]["claimed_by"] == "worker_1"
    assert "secret-claim-token" not in repr(payload)
    assert "claim_token" not in payload["nodes"][0]["attempt"]
