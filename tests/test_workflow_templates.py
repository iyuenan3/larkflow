"""Template lifecycle, materialization, and draft-preview tests."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from importlib.resources import files
from itertools import count

import pytest
import yaml

from larkflow.workflow import (
    AuthorizationError,
    DuplicateTemplateContentError,
    InMemoryTemplateStore,
    InMemoryWorkflowRepository,
    InvalidTemplateTransitionError,
    TemplateService,
    TemplateStatus,
    TemplateValidationError,
    TransitionError,
    WorkflowService,
    inline_owner_roles,
    instantiate_inline_definition,
    parse_template_document,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def document(
    *,
    version: int = 1,
    locked: bool = False,
    objective: str = "Review the brief",
) -> dict:
    return {
        "schema_version": "0.2",
        "template": {
            "id": "brief_review",
            "version": version,
            "name": "Brief review",
            "status": "draft",
            "locked": locked,
        },
        "goal": "Review a supplied brief",
        "parameters": {
            "brief": {"type": "text", "required": True},
            "rounds": {"type": "integer", "default": 1},
        },
        "nodes": [
            {
                "id": "review",
                "title": "Review",
                "owner_role": "project_owner",
                "executor": "human",
                "deps": [],
                "work": {
                    "objective": objective,
                    "inputs": ["instance_inputs.brief"],
                    "outputs": [{"id": "decision", "type": "data"}],
                    "acceptance": ["A decision exists"],
                },
            }
        ],
    }


def service() -> tuple[TemplateService, InMemoryTemplateStore]:
    sequence = count(1)
    store = InMemoryTemplateStore()
    return (
        TemplateService(
            store,
            clock=lambda: NOW,
            id_factory=lambda: f"template_event_{next(sequence)}",
        ),
        store,
    )


def inline_document() -> dict:
    return {
        "goal": "Review one real product change",
        "inputs": {
            "brief": "Stop Edge expansion and close the central workflow MVP",
            "urgent": False,
        },
        "nodes": [
            {
                "id": "confirm_scope",
                "title": "Confirm change scope",
                "owner_role": "requester",
                "executor": "human",
                "deps": [],
                "work": {
                    "objective": "Confirm the proposed product change",
                    "inputs": ["instance_inputs.brief"],
                    "outputs": [{"id": "confirmation", "type": "data"}],
                    "acceptance": ["The scope is explicitly confirmed"],
                },
            },
            {
                "id": "review_change",
                "title": "Review product change",
                "owner_role": "reviewer",
                "executor": "agent",
                "deps": ["confirm_scope"],
                "work": {
                    "objective": "Summarize the product change and its risks",
                    "inputs": [
                        "instance_inputs.brief",
                        "dependencies.confirm_scope",
                    ],
                    "outputs": [{"id": "content", "type": "text"}],
                    "acceptance": ["The summary includes risks and next steps"],
                    "agent": {
                        "kind": "llm.generate",
                        "model_role": "default",
                        "instructions": "Summarize the confirmed change.",
                    },
                },
            },
        ],
    }


def test_inline_definition_materializes_a_non_template_snapshot():
    definition = inline_document()

    assert inline_owner_roles(definition) == ("requester", "reviewer")
    snapshot = instantiate_inline_definition(
        definition,
        owner_bindings={
            "requester": "person_owner",
            "reviewer": "person_reviewer",
        },
    )

    assert snapshot.template_version_id is None
    assert snapshot.locked is False
    assert snapshot.goal == "Review one real product change"
    assert dict(snapshot.inputs) == {
        "brief": "Stop Edge expansion and close the central workflow MVP",
        "urgent": False,
    }
    assert snapshot.node("confirm_scope").owner_person_id == "person_owner"
    assert snapshot.node("review_change").owner_person_id == "person_reviewer"


@pytest.mark.parametrize(
    "mutation, bindings, message",
    [
        (
            lambda value: value["nodes"][1]["work"]["agent"].__setitem__(
                "api_key", "secret"
            ),
            {"requester": "person_owner", "reviewer": "person_reviewer"},
            "provider configuration",
        ),
        (
            lambda value: value["inputs"].__setitem__("unsupported", None),
            {"requester": "person_owner", "reviewer": "person_reviewer"},
            "unsupported JSON value",
        ),
        (
            lambda value: None,
            {"requester": "person_owner"},
            "missing owner bindings",
        ),
        (
            lambda value: value["nodes"].extend(
                deepcopy(value["nodes"][0]) for _ in range(99)
            ),
            {"requester": "person_owner", "reviewer": "person_reviewer"},
            "exceeds 100 nodes",
        ),
        (
            lambda value: value["nodes"][1]["work"]["agent"].__setitem__(
                "kind", "personal.readonly"
            ),
            {"requester": "person_owner", "reviewer": "person_reviewer"},
            "cannot request Personal Agent Edge",
        ),
    ],
)
def test_inline_definition_rejects_unsafe_or_incomplete_values(
    mutation,
    bindings,
    message,
):
    definition = inline_document()
    mutation(definition)

    with pytest.raises(TemplateValidationError, match=message):
        instantiate_inline_definition(definition, owner_bindings=bindings)


def test_template_lifecycle_materializes_a_frozen_snapshot_and_audit_log():
    templates, store = service()

    template, version = templates.create_template(
        tenant_id="tenant_1",
        actor_person_id="person_owner",
        document=document(locked=True),
    )
    assert template.status == TemplateStatus.DRAFT
    assert template.version == 0
    assert version.id == "brief_review:1"
    assert len(version.content_hash) == 64

    enabled = templates.enable(
        "tenant_1",
        "brief_review",
        actor_person_id="person_owner",
    )
    snapshot = templates.instantiate(
        "tenant_1",
        "brief_review",
        inputs={"brief": "Use synthetic evidence"},
        owner_bindings={"project_owner": "person_owner"},
    )

    assert enabled.status == TemplateStatus.ENABLED
    assert enabled.version == 1
    assert snapshot.template_version_id == "brief_review:1"
    assert snapshot.locked is True
    assert dict(snapshot.inputs) == {"brief": "Use synthetic evidence", "rounds": 1}
    assert snapshot.node("review").owner_person_id == "person_owner"
    assert [event.event_type for event in store.template_audit_log(
        "tenant_1", "brief_review"
    )] == ["template.created", "template.enabled"]


def test_enabled_template_must_be_disabled_before_appending_a_version():
    templates, store = service()
    templates.create_template(
        tenant_id="tenant_1",
        actor_person_id="person_owner",
        document=document(),
    )
    templates.enable("tenant_1", "brief_review", actor_person_id="person_owner")

    with pytest.raises(InvalidTemplateTransitionError, match="draft or disabled"):
        templates.add_version(
            tenant_id="tenant_1",
            template_id="brief_review",
            actor_person_id="person_owner",
            document=document(version=2, objective="Review the revised brief"),
        )

    templates.disable("tenant_1", "brief_review", actor_person_id="person_owner")
    updated, version = templates.add_version(
        tenant_id="tenant_1",
        template_id="brief_review",
        actor_person_id="person_owner",
        document=document(version=2, objective="Review the revised brief"),
    )
    templates.enable("tenant_1", "brief_review", actor_person_id="person_owner")
    snapshot = templates.instantiate(
        "tenant_1",
        "brief_review",
        inputs={"brief": "Revised"},
        owner_bindings={"project_owner": "person_owner"},
    )

    assert updated.version == 3
    assert version.id == "brief_review:2"
    assert snapshot.template_version_id == "brief_review:2"
    assert [event.aggregate_version for event in store.template_audit_log(
        "tenant_1", "brief_review"
    )] == [0, 1, 2, 3, 4]


def test_new_version_must_change_the_complete_version_contract():
    templates, _ = service()
    templates.create_template(
        tenant_id="tenant_1",
        actor_person_id="person_owner",
        document=document(),
    )

    with pytest.raises(DuplicateTemplateContentError):
        templates.add_version(
            tenant_id="tenant_1",
            template_id="brief_review",
            actor_person_id="person_owner",
            document=document(version=2),
        )

    first = parse_template_document(document())
    second = parse_template_document(document(version=2, locked=True))
    assert first.content_hash != second.content_hash


@pytest.mark.parametrize(
    "inputs, bindings, message",
    [
        ({}, {"project_owner": "person_owner"}, "missing required"),
        ({"brief": 3}, {"project_owner": "person_owner"}, "does not match"),
        (
            {"brief": "ok", "unknown": True},
            {"project_owner": "person_owner"},
            "unknown template inputs",
        ),
        ({"brief": "ok"}, {}, "missing owner bindings"),
        (
            {"brief": "ok"},
            {"project_owner": "person_owner", "extra": "person_other"},
            "unknown owner bindings",
        ),
    ],
)
def test_instantiation_rejects_unbound_or_invalid_runtime_values(
    inputs,
    bindings,
    message,
):
    templates, _ = service()
    templates.create_template(
        tenant_id="tenant_1",
        actor_person_id="person_owner",
        document=document(),
    )
    templates.enable("tenant_1", "brief_review", actor_person_id="person_owner")

    with pytest.raises(TemplateValidationError, match=message):
        templates.instantiate(
            "tenant_1",
            "brief_review",
            inputs=inputs,
            owner_bindings=bindings,
        )


@pytest.mark.parametrize("mutation, message", [
    (lambda value: value["nodes"][0].__setitem__("owner_person_id", "person"),
     "owner_person_id"),
    (lambda value: value["nodes"][0]["work"]["inputs"].__setitem__(
        0, "instance_inputs.missing"
    ), "unknown parameter"),
    (lambda value: value["nodes"][0]["work"].__setitem__(
        "agent", {"kind": "llm.generate", "api_key": "secret"}
    ), "provider configuration"),
    (lambda value: value["nodes"][0].__setitem__(
        "retry", {"max_attempts": 2}
    ), "unsupported fields"),
])
def test_template_validation_rejects_runtime_identity_and_provider_config(
    mutation,
    message,
):
    invalid = deepcopy(document())
    mutation(invalid)

    with pytest.raises(TemplateValidationError, match=message):
        parse_template_document(invalid)


def test_draft_preview_is_owner_only_and_read_only():
    templates, _ = service()
    templates.create_template(
        tenant_id="tenant_1",
        actor_person_id="person_owner",
        document=document(),
    )
    templates.enable("tenant_1", "brief_review", actor_person_id="person_owner")
    snapshot = templates.instantiate(
        "tenant_1",
        "brief_review",
        inputs={"brief": "Preview"},
        owner_bindings={"project_owner": "person_owner"},
    )
    repository = InMemoryWorkflowRepository()
    workflows = WorkflowService(repository, clock=lambda: NOW)
    workflows.create_draft(
        instance_id="instance_1",
        tenant_id="tenant_1",
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot,
    )
    before = repository.audit_log("tenant_1", "instance_1")

    preview = workflows.preview_draft(
        "tenant_1",
        "instance_1",
        actor_person_id="person_owner",
    )
    assert preview.snapshot == snapshot
    assert repository.audit_log("tenant_1", "instance_1") == before
    with pytest.raises(AuthorizationError):
        workflows.preview_draft(
            "tenant_1",
            "instance_1",
            actor_person_id="person_other",
        )

    workflows.confirm_draft(
        "tenant_1",
        "instance_1",
        actor_person_id="person_owner",
    )
    with pytest.raises(TransitionError, match="not a draft"):
        workflows.preview_draft(
            "tenant_1",
            "instance_1",
            actor_person_id="person_owner",
        )


def test_packaged_personal_edge_template_is_a_human_agent_human_contract():
    resource = files("larkflow.templates").joinpath(
        "target_personal_edge_review.yaml"
    )
    value = yaml.safe_load(resource.read_text(encoding="utf-8"))
    templates, _ = service()
    templates.create_template(
        tenant_id="tenant_1",
        actor_person_id="person_owner",
        document=value,
    )
    templates.enable(
        "tenant_1",
        "target_personal_edge_review",
        actor_person_id="person_owner",
    )
    snapshot = templates.instantiate(
        "tenant_1",
        "target_personal_edge_review",
        inputs={"brief": "Review the local workspace"},
        owner_bindings={"project_owner": "person_owner"},
    )

    assert tuple(node.executor.value for node in snapshot.nodes) == (
        "human",
        "agent",
        "human",
    )
    assert snapshot.node("local_draft").work["agent"]["kind"] == (
        "personal.readonly"
    )
