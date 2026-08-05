"""Safety and contract tests for natural-language workflow generation."""
from __future__ import annotations

import json

import pytest

from larkflow.workflow import (
    DraftDefinitionGenerator,
    DraftGenerationRejected,
    draft_wizard_form,
)


def valid_definition():
    return {
        "schema_version": "0.2",
        "goal": "Generate and review a summary",
        "inputs": {"brief": "Summarize the plan", "context": "No invented facts"},
        "nodes": [
            {
                "id": "draft_summary",
                "title": "Generate summary",
                "owner_role": "requester",
                "executor": "agent",
                "deps": [],
                "work": {
                    "objective": "Generate a summary",
                    "inputs": ["instance_inputs.brief"],
                    "outputs": [{"id": "content", "type": "text"}],
                    "acceptance": ["No facts are invented"],
                    "agent": {
                        "kind": "llm.generate",
                        "model_role": "default",
                        "instructions": "Write a concise summary.",
                    },
                },
            },
            {
                "id": "review_summary",
                "title": "Review summary",
                "owner_role": "collaborator",
                "executor": "human",
                "deps": ["draft_summary"],
                "work": {
                    "objective": "Review the generated summary",
                    "inputs": ["dependencies.draft_summary"],
                    "outputs": [{"id": "decision", "type": "data"}],
                    "acceptance": ["A human decision is recorded"],
                },
            },
        ],
    }


class Completion:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def complete(self, *, prompt, model_role):
        self.calls.append((prompt, model_role))
        return self.value


class SequenceCompletion:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def complete(self, *, prompt, model_role):
        self.calls.append((prompt, model_role))
        return next(self.values)


def test_generator_accepts_only_a_valid_bounded_inline_definition():
    completion = Completion(json.dumps(valid_definition()))
    generator = DraftDefinitionGenerator(completion)

    result = generator.generate(brief="Summarize the plan", context="No invented facts")

    assert result == valid_definition()
    assert completion.calls[0][1] == "default"
    assert "1 到 8 个节点" in completion.calls[0][0]
    assert "personal.readonly" in completion.calls[0][0]
    assert "不得反向引用或引用后续节点" in completion.calls[0][0]


def test_generator_repairs_one_invalid_dependency_candidate():
    invalid = valid_definition()
    invalid["nodes"][0]["work"]["inputs"] = ["dependencies.review_summary"]
    completion = SequenceCompletion(
        (json.dumps(invalid), json.dumps(valid_definition()))
    )

    result = DraftDefinitionGenerator(completion).generate(
        brief="Summarize the plan",
        context="No invented facts",
    )

    assert result == valid_definition()
    assert len(completion.calls) == 2
    assert "node references undeclared dependency" in completion.calls[1][0]
    assert "重新生成完整 JSON" in completion.calls[1][0]


def test_generator_rejects_after_one_failed_repair_attempt():
    invalid = valid_definition()
    invalid["nodes"][0]["work"]["inputs"] = ["dependencies.review_summary"]
    completion = SequenceCompletion((json.dumps(invalid), json.dumps(invalid)))

    with pytest.raises(DraftGenerationRejected, match="undeclared dependency"):
        DraftDefinitionGenerator(completion).generate(brief="brief", context="")

    assert len(completion.calls) == 2


def test_generator_preserves_server_owned_user_inputs_over_model_output():
    model_value = valid_definition()
    model_value["schema_version"] = "99"
    model_value["inputs"] = {"brief": "model replaced the request"}
    completion = Completion(json.dumps(model_value))

    result = DraftDefinitionGenerator(completion).generate(
        brief="Original request",
        context="Original constraint",
    )

    assert result["schema_version"] == "0.2"
    assert result["inputs"] == {
        "brief": "Original request",
        "context": "Original constraint",
    }


@pytest.mark.parametrize(
    "mutate, message",
    (
        (lambda value: json.dumps(value) + " trailing", "纯 JSON"),
        (
            lambda value: json.dumps(
                {**value, "nodes": value["nodes"] * 5}
            ),
            "1 到 8",
        ),
        (
            lambda value: json.dumps(
                {
                    **value,
                    "nodes": [
                        {**value["nodes"][0], "owner_role": "admin"},
                        value["nodes"][1],
                    ],
                }
            ),
            "未授权",
        ),
        (
            lambda value: json.dumps(
                {
                    **value,
                    "nodes": [
                        {
                            **value["nodes"][0],
                            "work": {
                                **value["nodes"][0]["work"],
                                "agent": {
                                    **value["nodes"][0]["work"]["agent"],
                                    "model_role": "private_route",
                                },
                            },
                        },
                        value["nodes"][1],
                    ],
                }
            ),
            "default",
        ),
        (
            lambda value: json.dumps({**value, "nodes": value["nodes"][:1]}),
            "Human",
        ),
    ),
)
def test_generator_rejects_unsafe_or_non_executable_model_output(mutate, message):
    completion = Completion(mutate(valid_definition()))

    with pytest.raises(DraftGenerationRejected, match=message):
        DraftDefinitionGenerator(completion).generate(brief="brief", context="")


def test_generator_rejects_prompt_data_over_the_card_contract_limit():
    completion = Completion(json.dumps(valid_definition()))

    with pytest.raises(DraftGenerationRejected, match="1000"):
        DraftDefinitionGenerator(completion).generate(
            brief="x" * 1001,
            context="",
        )

    assert completion.calls == []


def test_optional_context_may_be_omitted_by_the_feishu_form_callback():
    brief, context, collaborator = draft_wizard_form(
        json.dumps(
            {
                "draft_brief": "Generate a review flow",
                "role__collaborator": "person_reviewer",
            }
        )
    )

    assert brief == "Generate a review flow"
    assert context == ""
    assert collaborator == "person_reviewer"
