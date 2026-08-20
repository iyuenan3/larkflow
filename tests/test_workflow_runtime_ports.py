"""Characterization tests for the Phase 0/1 runtime seams."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from larkflow.agent_runtime import AgentRunRequest, AgentRunResult
from larkflow.agent_runtime.completion import CompletionAgentRuntime
from larkflow.agent_runtime.executor import AgentRuntimeExecutor
from larkflow.knowledge import EnterpriseKnowledgeRef
from larkflow.search import SearchResult, SearchSource, SearchUsage
from larkflow.planning import PlannerRequest, PlannerResult
from larkflow.planning.bounded import BoundedPlannerRuntime
from larkflow.planning.context import (
    ContextBundle,
    ContextChunk,
    SourceRef,
    sha256_hex,
)
from larkflow.planning.contracts import to_mutable
from larkflow.planning.service import PlanningService
from larkflow.planning.travel import TravelTemplatePlannerRuntime
from larkflow.workflow import (
    DraftCapabilityUnavailable,
    DraftDefinitionGenerator,
    DraftGenerationRejected,
    ExecutionRequest,
    ExecutorKind,
    LLMAgentExecutor,
    WebSearchToolExecutor,
)
from larkflow.workflow.deliverables import MAX_DELIVERABLE_JSON_BYTES
from larkflow.workflow.serde import to_json_value


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def valid_definition() -> dict:
    return {
        "schema_version": "0.2",
        "goal": "Generate and review a summary",
        "inputs": {"brief": "Summarize", "context": "Use supplied facts"},
        "nodes": [
            {
                "id": "confirm_requirements",
                "title": "Confirm requirements",
                "owner_role": "requester",
                "executor": "human",
                "deps": [],
                "work": {
                    "objective": "Confirm inputs",
                    "inputs": [
                        "instance_inputs.brief",
                        "instance_inputs.context",
                    ],
                    "outputs": [
                        {
                            "id": "requirements",
                            "type": "long_text",
                            "label": "Requirements",
                            "required": True,
                        }
                    ],
                    "acceptance": ["Inputs are explicit"],
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
                    "outputs": [
                        {
                            "id": "content",
                            "type": "text",
                            "label": "Summary",
                            "required": True,
                        }
                    ],
                    "acceptance": ["Uses only confirmed inputs"],
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
                    "objective": "Review the summary",
                    "inputs": ["dependencies.draft_summary"],
                    "outputs": [
                        {
                            "id": "decision",
                            "type": "decision",
                            "label": "Decision",
                            "required": True,
                        }
                    ],
                    "acceptance": ["A human decides"],
                    "decision": {
                        "kind": "accept_reject",
                        "reject_target": "draft_summary",
                    },
                },
            },
        ],
    }


class SequenceCompletion:
    def __init__(self, values: list[str]) -> None:
        self.values = iter(values)
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, prompt: str, model_role: str) -> str:
        self.calls.append((prompt, model_role))
        return next(self.values)


def planner(values: list[str]) -> tuple[PlanningService, SequenceCompletion]:
    completion = SequenceCompletion(values)
    return (
        PlanningService(
            BoundedPlannerRuntime(DraftDefinitionGenerator(completion)),
        ),
        completion,
    )


def test_bounded_planner_matches_legacy_generation_and_one_repair() -> None:
    invalid = valid_definition()
    invalid["nodes"][1]["work"]["inputs"] = [
        "dependencies.review_summary"
    ]
    responses = [json.dumps(invalid), json.dumps(valid_definition())]
    legacy_client = SequenceCompletion(list(responses))
    legacy_repairs = 0

    def note_legacy_repair() -> None:
        nonlocal legacy_repairs
        legacy_repairs += 1

    legacy = DraftDefinitionGenerator(legacy_client).generate(
        brief="Summarize",
        context="Use supplied facts",
        on_repair=note_legacy_repair,
    )
    service, port_client = planner(list(responses))
    port_repairs = 0

    def note_port_repair() -> None:
        nonlocal port_repairs
        port_repairs += 1

    through_port = service.generate(
        tenant_id="tenant_planning",
        actor_person_id="person_requester",
        request_id="request_planning",
        brief="Summarize",
        context="Use supplied facts",
        on_repair=note_port_repair,
    )

    assert through_port == legacy == valid_definition()
    assert port_client.calls == legacy_client.calls
    assert len(port_client.calls) == 2
    assert port_repairs == legacy_repairs == 1


def test_bounded_planner_preserves_final_rejection_classification() -> None:
    invalid = valid_definition()
    invalid["nodes"][2]["work"].pop("decision")
    response = json.dumps(invalid)
    legacy_client = SequenceCompletion([response, response])
    port, port_client = planner([response, response])

    with pytest.raises(DraftGenerationRejected) as legacy_error:
        DraftDefinitionGenerator(legacy_client).generate(
            brief="Summarize",
            context="",
        )
    with pytest.raises(DraftGenerationRejected) as port_error:
        port.plan(
            PlannerRequest(
                tenant_id="tenant_planning",
                actor_person_id="person_requester",
                request_id="request_planning",
                brief="Summarize",
            )
        )

    assert str(port_error.value) == str(legacy_error.value)
    assert port_client.calls == legacy_client.calls
    assert len(port_client.calls) == 2


def test_bounded_planner_reports_minimal_runtime_metadata() -> None:
    service, _ = planner([json.dumps(valid_definition())])

    result = service.plan(
        PlannerRequest(
            tenant_id="tenant_planning",
            actor_person_id="person_requester",
            request_id="request_planning",
            brief="Summarize",
            context="Use supplied facts",
        )
    )

    assert result.runtime_metadata == {
        "runtime": "bounded",
        "adapter": "draft_definition_generator",
        "adapter_version": "1",
    }


class StaticPlannerRuntime:
    def __init__(self, candidate: dict) -> None:
        self.candidate = candidate
        self.requests: list[PlannerRequest] = []

    def plan(self, request, *, on_repair=None) -> PlannerResult:
        del on_repair
        self.requests.append(request)
        return PlannerResult(
            candidate=self.candidate,
            runtime_metadata={"runtime": "untrusted-test-adapter"},
        )


class FailingPlannerRuntime:
    def __init__(self) -> None:
        self.requests: list[PlannerRequest] = []

    def plan(self, request, *, on_repair=None) -> PlannerResult:
        del on_repair
        self.requests.append(request)
        raise AssertionError("fallback planner must not run")


def test_travel_template_planner_builds_controlled_search_dag_without_llm() -> None:
    fallback = FailingPlannerRuntime()
    service = PlanningService(
        TravelTemplatePlannerRuntime(
            fallback,
            allow_web_search=True,
        ),
        allow_web_search=True,
    )

    result = service.plan(
        PlannerRequest(
            tenant_id="tenant_planning",
            actor_person_id="person_requester",
            request_id="request_planning",
            brief=(
                "使用企业资料规划新疆8日旅行。出发地：上海；"
                "旅行开始日期：2026年9月10日；旅行结束日期：2026年9月17日；"
                "出行人数：2名员工；旅行总预算：20000元；允许联网核验公开信息"
            ),
            context="景点和交通必须分别搜索并保留来源。",
        )
    )

    nodes = list(result.candidate["nodes"])
    assert fallback.requests == []
    assert [node["executor"] for node in nodes] == [
        "human",
        "tool",
        "tool",
        "agent",
        "human",
    ]
    assert [node["work"]["tool"]["kind"] for node in nodes[1:3]] == [
        "web.search",
        "web.search",
    ]
    assert nodes[3]["deps"] == (
        "confirm_travel_requirements",
        "research_attractions",
        "research_transport",
    )
    assert result.runtime_metadata == {
        "runtime": "travel_template",
        "adapter": "deterministic_travel_v1",
        "adapter_version": "1",
        "model_calls": 0,
    }


def test_travel_template_binds_enterprise_manifest_only_to_agent_input() -> None:
    fallback = FailingPlannerRuntime()
    raw = "合成企业旅行政策，不包含凭据。"
    digest = sha256_hex(raw.encode("utf-8"))
    knowledge = EnterpriseKnowledgeRef(
        tenant_id="tenant_planning",
        source_id="enterprise:travel_policy",
        version_id="v1",
        display_label="旅行政策",
        media_type="text/markdown",
        size_bytes=len(raw.encode("utf-8")),
        content_sha256=digest,
        published_at=NOW,
        egress_decision="allow",
        authorization_proof_id="kp_" + "1" * 32,
        authorization_fingerprint="2" * 64,
    )
    bundle = ContextBundle(
        tenant_id="tenant_planning",
        scope_kind="console_draft_request",
        scope_id="request_planning",
        purpose="planning",
        actor_person_id="person_requester",
        sources=(
            SourceRef(
                source_id=knowledge.source_id,
                kind="enterprise_knowledge",
                label=knowledge.display_label,
                content_sha256=digest,
            ),
        ),
        chunks=(ContextChunk(knowledge.source_id, 0, raw),),
        enterprise_knowledge=(knowledge,),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    service = PlanningService(
        TravelTemplatePlannerRuntime(fallback, allow_web_search=True),
        allow_web_search=True,
    )

    result = service.plan(
        PlannerRequest(
            tenant_id="tenant_planning",
            actor_person_id="person_requester",
            request_id="request_planning",
            brief=(
                "为新疆8日旅行制定可执行规划。出发地：上海；"
                "旅行开始日期：2026年9月10日；旅行结束日期：2026年9月17日；"
                "出行人数：2名员工；旅行总预算：20000元；联网核验公开信息"
            ),
            context_bundle=bundle,
        )
    )

    candidate = to_mutable(result.candidate)
    agent = next(
        node for node in candidate["nodes"] if node["executor"] == "agent"
    )
    assert "instance_inputs.enterprise_knowledge" in agent["work"]["inputs"]
    assert candidate["inputs"]["enterprise_knowledge"] == [
        knowledge.snapshot_value()
    ]
    assert raw not in json.dumps(candidate, ensure_ascii=False)


def test_travel_template_planner_delegates_non_travel_requests() -> None:
    runtime = StaticPlannerRuntime(valid_definition())
    service = PlanningService(
        TravelTemplatePlannerRuntime(runtime, allow_web_search=True),
        allow_web_search=True,
    )

    result = service.plan(
        PlannerRequest(
            tenant_id="tenant_planning",
            actor_person_id="person_requester",
            request_id="request_planning",
            brief="Summarize the supplied project notes",
        )
    )

    assert len(runtime.requests) == 1
    assert result.runtime_metadata == {"runtime": "untrusted-test-adapter"}


def test_travel_template_rejects_missing_budget_before_fallback_or_model() -> None:
    fallback = FailingPlannerRuntime()
    service = PlanningService(
        TravelTemplatePlannerRuntime(fallback, allow_web_search=True),
        allow_web_search=True,
    )

    with pytest.raises(DraftGenerationRejected, match="必填需求：预算"):
        service.plan(
            PlannerRequest(
                tenant_id="tenant_planning",
                actor_person_id="person_requester",
                request_id="request_planning",
                brief=(
                    "规划新疆旅行。出发地：上海；旅行开始日期：2026年9月10日；"
                    "旅行结束日期：2026年9月17日；两名员工；预算未提供。"
                ),
            )
        )

    assert fallback.requests == []


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("tenant_id", ""),
        ("actor_person_id", "   "),
        ("request_id", ""),
    ],
)
def test_planner_request_rejects_empty_server_identity(
    field_name: str,
    value: str,
) -> None:
    values = {
        "tenant_id": "tenant_planning",
        "actor_person_id": "person_requester",
        "request_id": "request_planning",
        "brief": "Summarize",
    }
    values[field_name] = value

    with pytest.raises(ValueError, match=field_name):
        PlannerRequest(**values)


def _agent_only_definition() -> dict:
    definition = valid_definition()
    definition["nodes"] = [deepcopy(definition["nodes"][1])]
    return definition


def _missing_final_gate_definition() -> dict:
    definition = valid_definition()
    definition["nodes"][2]["work"].pop("decision")
    return definition


@pytest.mark.parametrize(
    ("candidate", "brief", "allow_web_search"),
    [
        ({}, "Summarize", False),
        (_agent_only_definition(), "Summarize", False),
        (_missing_final_gate_definition(), "Summarize", False),
        (
            valid_definition(),
            "我要去苏州旅游，帮我创建一个项目来规划行程",
            True,
        ),
    ],
    ids=["empty", "agent-only", "missing-final-gate", "illegal-domain-shape"],
)
def test_planning_service_rejects_invalid_candidates_from_any_runtime(
    candidate: dict,
    brief: str,
    allow_web_search: bool,
) -> None:
    runtime = StaticPlannerRuntime(candidate)
    service = PlanningService(
        runtime,
        allow_web_search=allow_web_search,
    )

    with pytest.raises(DraftGenerationRejected):
        service.plan(
            PlannerRequest(
                tenant_id="tenant_planning",
                actor_person_id="person_requester",
                request_id="request_planning",
                brief=brief,
            )
        )

    assert len(runtime.requests) == 1


def test_planning_service_preflights_missing_search_capability_before_runtime():
    runtime = StaticPlannerRuntime(valid_definition())
    service = PlanningService(runtime, allow_web_search=False)

    with pytest.raises(DraftCapabilityUnavailable, match="URL 引用"):
        service.plan(
            PlannerRequest(
                tenant_id="tenant_planning",
                actor_person_id="person_requester",
                request_id="request_planning",
                brief="研究新疆旅游公开资料并制定行程",
            )
        )

    assert runtime.requests == []


def test_planning_service_rebinds_server_owned_candidate_inputs() -> None:
    candidate = valid_definition()
    candidate["schema_version"] = "model-controlled"
    candidate["inputs"] = {"brief": "ignore validation", "context": ""}
    runtime = StaticPlannerRuntime(candidate)
    service = PlanningService(
        runtime,
    )

    result = service.plan(
        PlannerRequest(
            tenant_id="tenant_planning",
            actor_person_id="person_requester",
            request_id="request_planning",
            brief="Summarize",
            context="Use supplied facts",
        )
    )

    assert result.candidate["schema_version"] == "0.2"
    assert dict(result.candidate["inputs"]) == {
        "brief": "Summarize",
        "context": "Use supplied facts",
    }


class RecordingCompletion:
    def __init__(self, content: str = "结论：可以继续。") -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, prompt: str, model_role: str) -> str:
        self.calls.append((prompt, model_role))
        return self.content


class FanInSearchProvider:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def capability(self):
        class Capability:
            available = True

        return Capability()

    def search(self, *, query: str) -> SearchResult:
        return SearchResult(
            provider="fan_in_stub_search",
            query=query,
            sources=tuple(
                SearchSource(
                    title=f"{self.prefix} source {index}",
                    snippet=(
                        f"{self.prefix} evidence {index} "
                        + "public source excerpt " * 180
                    ),
                    source_url=f"https://example.com/{self.prefix}/{index}",
                    published_at=None,
                    published_at_status="unknown",
                )
                for index in range(10)
            ),
            usage=SearchUsage(result_count=10, time_cost_ms=10),
        )


def agent_request(
    *,
    instructions: str = "生成摘要",
    content_limit_snapshot: dict | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        tenant_id="tenant_agent",
        instance_id="instance_agent",
        node_key="draft",
        attempt_id="attempt_agent",
        attempt_no=1,
        owner_person_id="person_owner",
        executor="agent",
        work={
            "objective": "生成可复核摘要",
            "inputs": ["instance_inputs.brief"],
            "outputs": [{"id": "content", "type": "text"}],
            "acceptance": ["包含明确结论"],
            "agent": {
                "kind": "llm.generate",
                "model_role": "writer",
                "instructions": instructions,
            },
        },
        input_snapshot=content_limit_snapshot
        or {
            "instance_inputs": {"brief": "发布检查"},
            "dependencies": {"confirm": {"approved": True}},
        },
        expected_node_version=2,
        claim_token="secret-claim-token",
        claim_expires_at=NOW + timedelta(minutes=5),
    )


def completion_bridge(
    client: RecordingCompletion,
    *,
    max_prompt_chars: int = 100_000,
    max_result_chars: int = 12_000,
) -> AgentRuntimeExecutor:
    return AgentRuntimeExecutor(
        CompletionAgentRuntime(
            LLMAgentExecutor(
                client,
                max_prompt_chars=max_prompt_chars,
                max_result_chars=max_result_chars,
            )
        )
    )


def fan_in_search_request(node_key: str, instructions: str) -> ExecutionRequest:
    return ExecutionRequest(
        tenant_id="tenant_agent",
        instance_id="instance_agent",
        node_key=node_key,
        attempt_id=f"attempt_{node_key}",
        attempt_no=1,
        owner_person_id="person_owner",
        executor="tool",
        work={
            "objective": instructions,
            "inputs": ["dependencies.confirm"],
            "outputs": [
                {"id": "content", "type": "text", "required": True},
                {"id": "sources", "type": "string_list", "required": True},
            ],
            "acceptance": ["保留来源"],
            "tool": {
                "kind": "web.search",
                "args": {
                    "model_role": "default",
                    "instructions": instructions,
                },
            },
        },
        input_snapshot={"dependencies": {"confirm": {"approved": True}}},
        expected_node_version=2,
        claim_token=f"claim_{node_key}",
        claim_expires_at=NOW + timedelta(minutes=5),
    )


def test_completion_runtime_preserves_prompt_snapshot_and_result_shape() -> None:
    direct_client = RecordingCompletion()
    port_client = RecordingCompletion()
    direct = LLMAgentExecutor(direct_client).execute(agent_request())
    through_port = completion_bridge(port_client).execute(agent_request())

    assert through_port == direct
    assert port_client.calls == direct_client.calls
    assert through_port.result["request_id"] == "tenant_agent:attempt_agent"
    assert '"approved": true' in port_client.calls[0][0]


def test_completion_runtime_preserves_source_notice() -> None:
    snapshot = {
        "dependencies": {
            "research": {
                "tool_kind": "web.search",
                "content": "公开材料摘要",
                "sources": ["https://example.invalid/source"],
            }
        }
    }
    direct = LLMAgentExecutor(RecordingCompletion()).execute(
        agent_request(content_limit_snapshot=deepcopy(snapshot))
    )
    through_port = completion_bridge(RecordingCompletion()).execute(
        agent_request(content_limit_snapshot=deepcopy(snapshot))
    )

    assert through_port == direct
    assert through_port.result["content"].startswith(
        LLMAgentExecutor.WEB_RESEARCH_NOTICE
    )


def test_completion_runtime_accepts_two_maximal_search_results_and_enterprise_context():
    attraction = WebSearchToolExecutor(FanInSearchProvider("attractions")).execute(
        fan_in_search_request("research_attractions", "研究景点")
    ).result
    transport = WebSearchToolExecutor(FanInSearchProvider("transport")).execute(
        fan_in_search_request("research_transport", "研究交通")
    ).result
    for search_result in (attraction, transport):
        encoded = json.dumps(
            to_json_value(search_result),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        assert len(encoded) <= MAX_DELIVERABLE_JSON_BYTES
        assert len(search_result["sources"]) == 10

    material = "E" * 12_000
    digest = sha256_hex(material.encode("utf-8"))
    knowledge = EnterpriseKnowledgeRef(
        tenant_id="tenant_agent",
        source_id="enterprise:travel_policy",
        version_id="v1",
        display_label="旅行政策",
        media_type="text/plain",
        size_bytes=len(material.encode("utf-8")),
        content_sha256=digest,
        published_at=NOW,
        egress_decision="allow",
        authorization_proof_id="kp_" + "1" * 32,
        authorization_fingerprint="2" * 64,
    )
    bundle = ContextBundle(
        tenant_id="tenant_agent",
        scope_kind="workflow_instance",
        scope_id="instance_agent",
        purpose="agent_execution",
        actor_person_id="person_owner",
        node_key="draft",
        attempt_id="attempt_agent",
        sources=(
            SourceRef(
                source_id=knowledge.source_id,
                kind="enterprise_knowledge",
                label=knowledge.display_label,
                content_sha256=digest,
            ),
        ),
        chunks=(ContextChunk(knowledge.source_id, 0, material),),
        enterprise_knowledge=(knowledge,),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    client = RecordingCompletion()
    runtime = CompletionAgentRuntime(LLMAgentExecutor(client))

    result = runtime.run(
        AgentRunRequest(
            tenant_id="tenant_agent",
            instance_id="instance_agent",
            node_key="draft",
            attempt_id="attempt_agent",
            attempt_no=1,
            owner_person_id="person_owner",
            executor="agent",
            work_contract={
                "objective": "综合旅行方案",
                "inputs": [
                    "dependencies.confirm",
                    "dependencies.research_attractions",
                    "dependencies.research_transport",
                    "instance_inputs.enterprise_knowledge",
                ],
                "outputs": [
                    {"id": "content", "type": "text", "required": True}
                ],
                "acceptance": ["包含逐日行程和来源边界"],
                "agent": {
                    "kind": "llm.generate",
                    "model_role": "default",
                    "instructions": "综合输入生成完整方案",
                },
            },
            input_snapshot={
                "instance_inputs": {"brief": "新疆 8 日旅行"},
                "dependencies": {
                    "confirm": {"approved": True},
                    "research_attractions": attraction,
                    "research_transport": transport,
                },
            },
            context_bundle=bundle,
        )
    )

    assert result.deliverables["content"].endswith("结论：可以继续。")
    prompt = client.calls[0][0]
    assert 20_000 < len(prompt) <= 100_000
    assert "https://example.com/attractions/0" in prompt
    assert "https://example.com/transport/0" in prompt
    assert material in prompt


def test_completion_runtime_reports_minimal_runtime_metadata() -> None:
    execution_request = agent_request()
    runtime = CompletionAgentRuntime(LLMAgentExecutor(RecordingCompletion()))
    result = runtime.run(
        AgentRunRequest(
            tenant_id=execution_request.tenant_id,
            instance_id=execution_request.instance_id,
            node_key=execution_request.node_key,
            attempt_id=execution_request.attempt_id,
            attempt_no=execution_request.attempt_no,
            owner_person_id=execution_request.owner_person_id,
            executor=execution_request.executor.value,
            work_contract=execution_request.work,
            input_snapshot=execution_request.input_snapshot,
        )
    )

    assert result.runtime_metadata == {
        "runtime": "completion",
        "adapter": "llm_agent_executor",
        "adapter_version": "1",
    }


@pytest.mark.parametrize(
    "client_content,instructions,max_prompt,max_result",
    [
        ("result", "", 20_000, 12_000),
        ("  ", "generate", 20_000, 12_000),
        ("four", "generate", 20_000, 3),
        ("result", "generate", 10, 12_000),
    ],
)
def test_completion_runtime_preserves_error_classification(
    client_content: str,
    instructions: str,
    max_prompt: int,
    max_result: int,
) -> None:
    request = agent_request(instructions=instructions)
    direct_client = RecordingCompletion(client_content)
    port_client = RecordingCompletion(client_content)

    with pytest.raises(ValueError) as direct_error:
        LLMAgentExecutor(
            direct_client,
            max_prompt_chars=max_prompt,
            max_result_chars=max_result,
        ).execute(request)
    with pytest.raises(ValueError) as port_error:
        completion_bridge(
            port_client,
            max_prompt_chars=max_prompt,
            max_result_chars=max_result,
        ).execute(request)

    assert str(port_error.value) == str(direct_error.value)


class CapturingRuntime:
    def __init__(self) -> None:
        self.requests: list[AgentRunRequest] = []

    def accepts(self, *, executor: str, work_contract) -> bool:
        return executor == "agent"

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.requests.append(request)
        return AgentRunResult(deliverables={"content": "done"})


def test_agent_runtime_never_receives_worker_claim_fields() -> None:
    runtime = CapturingRuntime()
    result = AgentRuntimeExecutor(runtime).execute(agent_request())

    assert result.result == {"content": "done"}
    assert len(runtime.requests) == 1
    runtime_request = runtime.requests[0]
    assert runtime_request.idempotency_key == "tenant_agent:attempt_agent"
    assert runtime_request.input_snapshot["dependencies"]["confirm"][
        "approved"
    ] is True
    assert "claim_token" not in AgentRunRequest.__dataclass_fields__
    assert "expected_node_version" not in AgentRunRequest.__dataclass_fields__
    assert "claim_expires_at" not in AgentRunRequest.__dataclass_fields__
    assert "secret-claim-token" not in repr(runtime_request)
    assert runtime_request.policy == {}


def test_new_runtime_ports_have_no_langgraph_dependency() -> None:
    root = Path(__file__).parents[1]
    files = [
        *sorted((root / "larkflow" / "planning").glob("*.py")),
        *sorted((root / "larkflow" / "agent_runtime").glob("*.py")),
        *sorted((root / "larkflow" / "workflow").glob("*.py")),
    ]

    for path in files:
        source = path.read_text(encoding="utf-8").lower()
        assert "from langgraph" not in source
        assert "import langgraph" not in source


def test_target_runtime_ports_import_when_langgraph_is_blocked() -> None:
    script = """
import builtins

real_import = builtins.__import__

def blocked_import(name, *args, **kwargs):
    if name == "langgraph" or name.startswith("langgraph."):
        raise AssertionError(f"unexpected LangGraph import: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
from larkflow.agent_runtime.completion import CompletionAgentRuntime
from larkflow.agent_runtime.executor import AgentRuntimeExecutor
from larkflow.planning.bounded import BoundedPlannerRuntime
from larkflow.planning.service import PlanningService
from larkflow.workflow.cli import _draft_generator, _executors
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_contract_packages_do_not_load_workflow_or_adapters() -> None:
    script = """
import builtins

real_import = builtins.__import__

def blocked_import(name, *args, **kwargs):
    if name == "larkflow.workflow" or name.startswith("larkflow.workflow."):
        raise AssertionError(f"unexpected workflow import: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
from larkflow.agent_runtime import AgentRunRequest, AgentRunResult, AgentRuntime
from larkflow.planning import DraftGenerator, PlannerRequest, PlannerResult, PlannerRuntime
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
