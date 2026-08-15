"""Safety and contract tests for natural-language workflow generation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from larkflow.planning.context import (
    AttachmentRef,
    ContextBundle,
    ContextChunk,
    SourceRef,
    sha256_hex,
)
from larkflow.workflow import (
    DraftCapabilityUnavailable,
    DraftDefinitionGenerator,
    DraftGenerationRejected,
    draft_wizard_form,
)


NOW = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)


def valid_definition():
    return {
        "schema_version": "0.2",
        "goal": "Generate and review a summary",
        "inputs": {"brief": "Summarize the plan", "context": "No invented facts"},
        "nodes": [
            {
                "id": "confirm_requirements",
                "title": "Confirm requirements",
                "owner_role": "requester",
                "executor": "human",
                "deps": [],
                "work": {
                    "objective": "Complete and confirm the requested inputs",
                    "inputs": ["instance_inputs.brief", "instance_inputs.context"],
                    "outputs": [
                        {
                            "id": "requirements",
                            "type": "long_text",
                            "label": "Confirmed requirements",
                            "required": True,
                        }
                    ],
                    "acceptance": ["Required inputs are explicit"],
                },
            },
            {
                "id": "draft_summary",
                "title": "Generate summary",
                "owner_role": "requester",
                "executor": "agent",
                "deps": ["confirm_requirements"],
                "work": {
                    "objective": "Generate a summary",
                    "inputs": ["dependencies.confirm_requirements"],
                    "outputs": [{"id": "content", "type": "text", "label": "Summary", "required": True}],
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
                    "outputs": [{"id": "decision", "type": "decision", "label": "Review decision", "required": True}],
                    "acceptance": ["A human decision is recorded"],
                    "decision": {
                        "kind": "accept_reject",
                        "reject_target": "draft_summary",
                    },
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


def attachment_context(text: str) -> ContextBundle:
    raw = text.encode("utf-8")
    digest = sha256_hex(raw)
    return ContextBundle(
        tenant_id="tenant_planning",
        scope_kind="console_draft_request",
        scope_id="request_planning",
        purpose="planning",
        actor_person_id="person_requester",
        sources=(
            SourceRef(
                source_id="attachment.trip_brief",
                kind="attachment",
                label="trip-brief.md",
                content_sha256=digest,
            ),
        ),
        attachments=(
            AttachmentRef(
                attachment_id="attachment_trip_brief",
                source_id="attachment.trip_brief",
                display_filename="trip-brief.md",
                media_type="text/markdown",
                size_bytes=len(raw),
                content_sha256=digest,
            ),
        ),
        chunks=(ContextChunk("attachment.trip_brief", 0, text),),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )


def no_web_trip_definition():
    value = valid_definition()
    value["goal"] = "Generate and review an eight-day trip manual"
    value["nodes"][0]["work"]["outputs"] = [
        {"id": "origin", "type": "text", "label": "出发地", "required": True},
        {"id": "start_date", "type": "date", "label": "出行日期", "required": True},
        {"id": "travelers", "type": "integer", "label": "出行人数", "required": True},
        {"id": "budget", "type": "money", "label": "预算", "required": True},
    ]
    return value


def test_generator_accepts_only_a_valid_bounded_inline_definition():
    completion = Completion(json.dumps(valid_definition()))
    generator = DraftDefinitionGenerator(completion)

    result = generator.generate(brief="Summarize the plan", context="No invented facts")

    assert result == valid_definition()
    assert completion.calls[0][1] == "default"
    assert "1 到 8 个节点" in completion.calls[0][0]
    assert "personal.readonly" in completion.calls[0][0]
    assert "不得反向引用或引用后续节点" in completion.calls[0][0]
    assert '"kind":"accept_reject"' in completion.calls[0][0]
    assert "只有 requester 可以修改流程 DAG" in completion.calls[0][0]
    assert "开发和验证状态不能表述为已经生产上线" in completion.calls[0][0]


def test_generator_repairs_one_invalid_dependency_candidate():
    invalid = valid_definition()
    invalid["nodes"][1]["work"]["inputs"] = ["dependencies.review_summary"]
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
    invalid["nodes"][1]["work"]["inputs"] = ["dependencies.review_summary"]
    completion = SequenceCompletion((json.dumps(invalid), json.dumps(invalid)))

    with pytest.raises(DraftGenerationRejected, match="undeclared dependency"):
        DraftDefinitionGenerator(completion).generate(brief="brief", context="")

    assert len(completion.calls) == 2


def test_generator_repairs_agent_flow_without_a_terminal_human_decision():
    invalid = valid_definition()
    invalid["nodes"][2]["work"].pop("decision")
    completion = SequenceCompletion(
        (json.dumps(invalid), json.dumps(valid_definition()))
    )

    result = DraftDefinitionGenerator(completion).generate(
        brief="Summarize the plan",
        context="No invented facts",
    )

    assert result == valid_definition()
    assert len(completion.calls) == 2
    assert "接受或退回" in completion.calls[1][0]


def test_under_specified_trip_cannot_skip_requirements_and_research_deliverables():
    shallow = {
        "schema_version": "0.2",
        "goal": "Generate a Suzhou itinerary",
        "inputs": {},
        "nodes": [
            {
                "id": "confirm_request",
                "title": "Confirm request",
                "owner_role": "requester",
                "executor": "human",
                "deps": [],
                "work": {
                    "objective": "Confirm the request",
                    "inputs": ["instance_inputs.brief"],
                    "outputs": [{"id": "confirmation", "type": "boolean", "label": "Confirmed", "required": True}],
                    "acceptance": ["The request is confirmed"],
                },
            },
            {
                "id": "draft_itinerary",
                "title": "Generate itinerary",
                "owner_role": "requester",
                "executor": "agent",
                "deps": ["confirm_request"],
                "work": {
                    "objective": "Generate a Suzhou itinerary",
                    "inputs": ["instance_inputs.brief", "dependencies.confirm_request"],
                    "outputs": [{"id": "content", "type": "text", "label": "Itinerary", "required": True}],
                    "acceptance": ["An itinerary exists"],
                    "agent": {"kind": "llm.generate", "model_role": "default", "instructions": "Write the itinerary"},
                },
            },
            {
                "id": "review_itinerary",
                "title": "Review itinerary",
                "owner_role": "requester",
                "executor": "human",
                "deps": ["draft_itinerary"],
                "work": {
                    "objective": "Review the itinerary",
                    "inputs": ["dependencies.draft_itinerary"],
                    "outputs": [{"id": "decision", "type": "decision", "label": "Decision", "required": True}],
                    "acceptance": ["A decision is recorded"],
                    "decision": {"kind": "accept_reject", "reject_target": "draft_itinerary"},
                },
            },
        ],
    }
    rich = valid_definition()
    rich["goal"] = "Plan a source-grounded Suzhou trip"
    rich["nodes"][0]["work"]["outputs"] = [
        {"id": "origin", "type": "text", "label": "Origin", "required": True},
        {"id": "start_date", "type": "date", "label": "Start date", "required": True},
        {"id": "travelers", "type": "integer", "label": "Travelers", "required": True},
        {"id": "budget", "type": "money", "label": "Budget", "required": True},
    ]
    research_nodes = []
    for node_id, title, label in (
        ("research_attractions", "Research attractions", "Attraction evidence"),
        ("research_transport", "Research transport", "Transport evidence"),
        ("research_lodging", "Research lodging", "Lodging evidence"),
    ):
        research_nodes.append(
            {
                "id": node_id,
                "title": title,
                "owner_role": "requester",
                "executor": "tool",
                "deps": ["confirm_requirements"],
                "work": {
                    "objective": title,
                    "inputs": ["dependencies.confirm_requirements"],
                    "outputs": [
                        {"id": "content", "type": "text", "label": label, "required": True},
                        {"id": "sources", "type": "string_list", "label": "Source URLs", "required": True},
                    ],
                    "acceptance": ["Evidence and source links are recorded"],
                    "tool": {
                        "kind": "web.search",
                        "args": {
                            "model_role": "default",
                            "instructions": title,
                        },
                    },
                },
            }
        )
    rich["nodes"] = [
        rich["nodes"][0],
        *research_nodes,
        {
            **rich["nodes"][1],
            "id": "draft_itinerary",
            "title": "Generate itinerary",
            "deps": [
                "confirm_requirements",
                "research_attractions",
                "research_transport",
                "research_lodging",
            ],
            "work": {
                **rich["nodes"][1]["work"],
                "objective": "Synthesize an itinerary from confirmed requirements and research",
                "inputs": [
                    "dependencies.confirm_requirements",
                    "dependencies.research_attractions",
                    "dependencies.research_transport",
                    "dependencies.research_lodging",
                ],
            },
        },
        {
            **rich["nodes"][2],
            "id": "review_itinerary",
            "title": "Review itinerary",
            "deps": ["draft_itinerary"],
            "work": {
                **rich["nodes"][2]["work"],
                "inputs": ["dependencies.draft_itinerary"],
                "decision": {"kind": "accept_reject", "reject_target": "draft_itinerary"},
            },
        },
    ]
    completion = SequenceCompletion((json.dumps(shallow), json.dumps(rich)))

    result = DraftDefinitionGenerator(completion, allow_web_search=True).generate(
        brief="我要去苏州旅游，帮我创建一个项目来规划行程",
        context="",
    )

    assert result["nodes"][0]["id"] == "confirm_requirements"
    assert {item["id"] for item in result["nodes"][0]["work"]["outputs"]} == {
        "origin",
        "start_date",
        "travelers",
        "budget",
    }
    assert [item["id"] for item in result["nodes"][1:4]] == [
        "research_attractions",
        "research_transport",
        "research_lodging",
    ]
    assert len(completion.calls) == 2
    assert "旅游规划必须先收集必填需求" in completion.calls[1][0]
    assert "日期、人数、预算" in completion.calls[0][0]
    assert '"kind":"web.search"' in completion.calls[0][0]


def test_trip_generation_is_rejected_when_controlled_search_is_disabled():
    completion = Completion(json.dumps(valid_definition()))

    with pytest.raises(DraftCapabilityUnavailable, match="URL 引用"):
        DraftDefinitionGenerator(completion).generate(
            brief="我要去苏州旅游，帮我创建一个项目来规划行程",
            context="",
        )

    assert completion.calls == []


def test_complete_attachment_and_explicit_no_web_allow_a_tool_free_trip_flow():
    definition = no_web_trip_definition()
    completion = Completion(json.dumps(definition, ensure_ascii=False))
    bundle = attachment_context(
        "2026年9月1日从上海出发，2人，预算12000元。"
        "新疆8日行程包含喀纳斯等景点游玩资料，所有交通段和路线资料均已给出。"
    )

    result = DraftDefinitionGenerator(completion).generate(
        brief="根据附件生成新疆8日旅行执行手册，不要联网，附件为唯一来源",
        context="",
        context_bundle=bundle,
    )

    assert [node["executor"] for node in result["nodes"]] == [
        "human",
        "agent",
        "human",
    ]
    assert len(completion.calls) == 1
    assert "不可信来源资料" in completion.calls[0][0]


def test_no_web_trip_rejects_missing_or_incomplete_attachment_evidence():
    completion = Completion(json.dumps(no_web_trip_definition()))

    with pytest.raises(DraftGenerationRejected, match="没有提供.*附件"):
        DraftDefinitionGenerator(completion).generate(
            brief="生成新疆旅行手册，不要联网",
            context="",
        )
    assert completion.calls == []

    incomplete = attachment_context("新疆景点资料已经整理，但日期和预算尚未确认。")
    with pytest.raises(DraftGenerationRejected, match="附件资料不足.*出发地"):
        DraftDefinitionGenerator(completion).generate(
            brief="生成新疆旅行手册，不要联网",
            context="",
            context_bundle=incomplete,
        )
    assert completion.calls == []


def test_attachment_travel_intent_cannot_be_hidden_by_renaming_the_manual():
    completion = Completion(json.dumps(no_web_trip_definition()))
    bundle = attachment_context("新疆旅游路线和景点资料，但缺少日期、人数、预算和交通。")

    with pytest.raises(DraftCapabilityUnavailable, match="URL 引用"):
        DraftDefinitionGenerator(completion).generate(
            brief="请创建一份执行手册",
            context="",
            context_bundle=bundle,
        )

    assert completion.calls == []


def test_generator_rejects_decision_whose_rework_target_is_not_an_agent():
    invalid = valid_definition()
    invalid["nodes"].insert(
        2,
        {
            "id": "human_context",
            "title": "Add context",
            "owner_role": "requester",
            "executor": "human",
            "deps": ["draft_summary"],
            "work": {
                "objective": "Add review context",
                "inputs": ["dependencies.draft_summary"],
                "outputs": [{"id": "context", "type": "text", "label": "Review context", "required": True}],
                "acceptance": ["Context is recorded"],
            },
        },
    )
    invalid["nodes"][3]["deps"] = ["human_context"]
    invalid["nodes"][3]["work"]["inputs"] = ["dependencies.human_context"]
    invalid["nodes"][3]["work"]["decision"]["reject_target"] = "human_context"
    completion = Completion(json.dumps(invalid))

    with pytest.raises(DraftGenerationRejected, match="上游 Agent"):
        DraftDefinitionGenerator(completion).generate(brief="brief", context="")


def test_generator_keeps_a_human_only_workflow_as_an_ordinary_task():
    value = valid_definition()
    value["nodes"] = [
        {
            "id": "confirm_scope",
            "title": "Confirm scope",
            "owner_role": "requester",
            "executor": "human",
            "deps": [],
            "work": {
                "objective": "Confirm the requested scope",
                "inputs": ["instance_inputs.brief"],
                "outputs": [{"id": "confirmation", "type": "long_text", "label": "Confirmed scope", "required": True}],
                "acceptance": ["Scope is explicit"],
            },
        }
    ]

    result = DraftDefinitionGenerator(Completion(json.dumps(value))).generate(
        brief="Confirm scope",
        context="",
    )

    assert "decision" not in result["nodes"][0]["work"]


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
                        *value["nodes"][1:],
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
                        value["nodes"][0],
                        {
                            **value["nodes"][1],
                            "work": {
                                **value["nodes"][1]["work"],
                                "agent": {
                                    **value["nodes"][1]["work"]["agent"],
                                    "model_role": "private_route",
                                },
                            },
                        },
                        value["nodes"][2],
                    ],
                }
            ),
            "default",
        ),
        (
            lambda value: json.dumps(
                {
                    **value,
                    "nodes": [
                        {
                            **value["nodes"][1],
                            "deps": [],
                            "work": {
                                **value["nodes"][1]["work"],
                                "inputs": ["instance_inputs.brief"],
                            },
                        },
                        value["nodes"][2],
                    ],
                }
            ),
            "Agent",
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
