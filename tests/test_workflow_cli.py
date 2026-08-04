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
from larkflow.workflow.cli import (
    _draft_preview_payload,
    _load_optional_mapping,
    build_parser,
)


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


def test_cli_template_commands_parse_formal_entrypoint_arguments():
    parser = build_parser()

    created = parser.parse_args(
        [
            "--dsn",
            "postgresql:///test",
            "--tenant",
            "tenant_1",
            "create-from-template",
            "brief_review",
            "--instance-id",
            "instance_1",
            "--owner",
            "person_owner",
            "--bindings",
            "bindings.yaml",
        ]
    )
    shown = parser.parse_args(["template-show", "brief_review", "--version", "2"])

    assert created.command == "create-from-template"
    assert created.inputs is None
    assert shown.version == 2
    assert _load_optional_mapping(None) == {}


def test_cli_exposes_projection_reconciliation_as_an_explicit_command():
    parsed = build_parser().parse_args(["reconcile-projections"])

    assert parsed.command == "reconcile-projections"


def test_cli_exposes_completion_reconciliation_as_an_explicit_command():
    parsed = build_parser().parse_args(["reconcile-completions"])

    assert parsed.command == "reconcile-completions"


def test_cli_exposes_one_instance_completion_repair():
    parsed = build_parser().parse_args(
        ["reconcile-instance-completion", "instance_1"]
    )

    assert parsed.command == "reconcile-instance-completion"
    assert parsed.instance_id == "instance_1"


def test_cli_exposes_isolated_interactive_worker_commands():
    parser = build_parser()

    once = parser.parse_args(["interact-once"])
    persistent = parser.parse_args(["interact"])

    assert once.command == "interact-once"
    assert persistent.command == "interact"


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


def test_draft_preview_payload_contains_the_materialized_contract():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=lambda: NOW)
    instance = service.create_draft(
        instance_id="instance_preview",
        tenant_id="tenant_1",
        owner_person_id="owner_1",
        actor_person_id="owner_1",
        snapshot=InstanceSnapshot(
            template_version_id="brief_review:1",
            locked=True,
            inputs={"brief": "Synthetic"},
            nodes=(NodeSpec("review", "Review", "owner_1", "human", work=work()),),
        ),
    )

    payload = _draft_preview_payload(instance)

    assert payload["template_version_id"] == "brief_review:1"
    assert payload["locked"] is True
    assert payload["inputs"] == {"brief": "Synthetic"}
    assert payload["nodes"][0]["work"]["objective"] == "Do the work"
