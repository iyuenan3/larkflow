"""Target Tool routing, content checks, and mixed template tests."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from larkflow.search import (
    OutboundFetchResult,
    SearchResult,
    SearchSource,
    SearchSourcesUnavailableError,
    SearchUsage,
)
from larkflow.workflow import (
    ContentCheckToolExecutor,
    DevelopmentToolExecutor,
    ExecutionRequest,
    ExecutorKind,
    InMemoryTemplateStore,
    InMemoryWorkflowRepository,
    InstanceStatus,
    LLMAgentExecutor,
    NodeRunner,
    NodeStatus,
    QualityVerdict,
    SourceClaimsCheckToolExecutor,
    SourceDecisionCheckToolExecutor,
    SourceEvidenceCheckToolExecutor,
    TargetRuntimeSettings,
    TemplateService,
    ToolExecutorRouter,
    WebSearchToolExecutor,
    WorkflowService,
    WorkflowWorker,
    validate_snapshot,
)
from larkflow.workflow.cli import _executors


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


class StaticCompletion:
    def complete(self, *, prompt: str, model_role: str) -> str:
        return "结论：输入完整，可以继续。下一步：请节点 Owner 复核摘要。"


class StaticWebSearch:
    def __init__(self, *, sources=None):
        self.sources = sources or ["https://example.com/official-guide"]
        self.calls = []

    def web_search(self, *, prompt: str, model_role: str):
        self.calls.append({"prompt": prompt, "model_role": model_role})
        return {
            "content": "景点开放信息与预约规则已核对。",
            "sources": self.sources,
        }


class UnavailableWebSearch:
    def __init__(self) -> None:
        self.calls = []

    def supports_web_search(self, model_role: str) -> bool:
        return False

    def web_search(self, *, prompt: str, model_role: str):
        self.calls.append({"prompt": prompt, "model_role": model_role})
        raise AssertionError("unavailable search route must not be called")


class StaticSearchProvider:
    def __init__(self, *, published_at: str | None = None) -> None:
        self.calls = []
        self.published_at = published_at

    def capability(self):
        class Capability:
            available = True

        return Capability()

    def search(self, *, query: str) -> SearchResult:
        self.calls.append(query)
        return SearchResult(
            provider="stub_search",
            query=query,
            sources=(
                SearchSource(
                    title="苏州博物馆参观须知",
                    snippet="开放与预约信息。",
                    source_url="https://www.szmuseum.com/guide",
                    published_at=self.published_at,
                    published_at_status=(
                        "known" if self.published_at is not None else "unknown"
                    ),
                ),
            ),
            usage=SearchUsage(result_count=1, time_cost_ms=12),
        )


class UnreachableSourceFetcher:
    def capability(self):
        return True, "stub_enabled"

    def check(self, *, url: str):
        return OutboundFetchResult(
            health="unreachable",
            reason="stub_unreachable",
            final_url=url,
        )


def request(
    *,
    kind: str = "content.check",
    args: dict | None = None,
    content: object = "结论：可以继续。下一步：人工复核。",
) -> ExecutionRequest:
    return ExecutionRequest(
        tenant_id="tenant_tools",
        instance_id="instance_tools",
        node_key="check_summary",
        attempt_id="attempt_tools",
        attempt_no=1,
        owner_person_id="person_owner",
        executor="tool",
        work={
            "objective": "检查摘要",
            "inputs": ["dependencies.draft_summary"],
            "outputs": [{"id": "verdict", "type": "data"}],
            "acceptance": ["返回检查证据"],
            "tool": {
                "kind": kind,
                "args": args
                or {
                    "source": "dependencies.draft_summary.content",
                    "required_terms": ["结论", "下一步"],
                    "min_chars": 10,
                    "max_chars": 100,
                },
            },
        },
        input_snapshot={
            "dependencies": {"draft_summary": {"content": content}},
        },
        expected_node_version=1,
        claim_token="claim-tools",
        claim_expires_at=NOW + timedelta(minutes=5),
    )


def web_search_request(*, extra_args: dict | None = None) -> ExecutionRequest:
    tool_args = {
        "model_role": "default",
        "instructions": "优先核对景点官方开放时间和预约规则",
    }
    tool_args.update(extra_args or {})
    return ExecutionRequest(
        tenant_id="tenant_tools",
        instance_id="instance_tools",
        node_key="research_attractions",
        attempt_id="attempt_search",
        attempt_no=1,
        owner_person_id="person_owner",
        executor="tool",
        work={
            "objective": "研究苏州景点开放与预约信息",
            "inputs": ["dependencies.confirm_requirements"],
            "outputs": [
                {"id": "content", "type": "text", "required": True},
                {"id": "sources", "type": "string_list", "required": True},
            ],
            "acceptance": ["关键结论有来源"],
            "tool": {
                "kind": "web.search",
                "args": tool_args,
            },
        },
        input_snapshot={
            "dependencies": {
                "confirm_requirements": {
                    "origin": "上海",
                    "start_date": "2026-08-20",
                    "travelers": 2,
                    "budget": 3000,
                }
            }
        },
        expected_node_version=1,
        claim_token="claim-search",
        claim_expires_at=NOW + timedelta(minutes=5),
    )


def source_evidence_request(
    *,
    claims=None,
    source_records=None,
    source_result_tool_kind: str = "web.search",
    source_executor: str = "tool",
    provenance_tool_kind: str | None = "web.search",
    include_provenance: bool = True,
) -> ExecutionRequest:
    records = source_records or [
        {
            "title": "苏州博物馆参观须知",
            "snippet": "苏州博物馆实行预约参观。",
            "source_url": "https://www.szmuseum.com/guide",
            "published_at": None,
            "published_at_status": "unknown",
            "url_status": "valid",
            "health": "unknown",
            "health_reason": "safe_outbound_fetcher_unavailable",
            "freshness": "unknown",
            "authority": "unknown",
            "authority_basis": "not_assessed",
            "support": "unknown",
        }
    ]
    claim_values = claims or [
        {
            "claim_id": "C1",
            "text": "参观需要预约",
            "source_url": "HTTPS://WWW.SZMUSEUM.COM:443/guide#notice",
            "supporting_excerpt": "实行预约参观",
        }
    ]
    return ExecutionRequest(
        tenant_id="tenant_tools",
        instance_id="instance_tools",
        node_key="check_search_evidence",
        attempt_id="attempt_search_evidence",
        attempt_no=1,
        owner_person_id="person_owner",
        executor="tool",
        work={
            "objective": "检查搜索结论的证据绑定",
            "inputs": [
                "dependencies.research.source_records",
                "dependencies.summary.claims",
            ],
            "outputs": [{"id": "verdict", "type": "data"}],
            "acceptance": ["每个 claim 绑定当前搜索来源原文片段"],
            "tool": {
                "kind": "source_evidence.check",
                "args": {
                    "claims": "dependencies.summary.claims",
                    "source_records": "dependencies.research.source_records",
                },
            },
        },
        input_snapshot={
            "dependencies": {
                "research": {
                    "tool_kind": source_result_tool_kind,
                    "sources": ["https://www.szmuseum.com/guide"],
                    "source_records": records,
                },
                "summary": {"claims": claim_values},
            },
            **(
                {
                    "dependency_provenance": {
                        "research": {
                            "node_key": "research",
                            "executor": source_executor,
                            "tool_kind": provenance_tool_kind,
                            "attempt_id": "attempt_research",
                            "attempt_no": 1,
                        },
                        "summary": {
                            "node_key": "summary",
                            "executor": "agent",
                            "tool_kind": None,
                            "attempt_id": "attempt_summary",
                            "attempt_no": 1,
                        },
                    }
                }
                if include_provenance
                else {}
            ),
        },
        expected_node_version=1,
        claim_token="claim-search-evidence",
        claim_expires_at=NOW + timedelta(minutes=5),
    )


def source_claims_request(
    *,
    document: dict | None = None,
    include_open_question: bool = True,
) -> ExecutionRequest:
    registry = {
        "source_url": "https://example.invalid/issues/42",
        "facts": [
            {"id": "F1", "text": "创建时校验配置"},
            {"id": "F2", "text": "校验失败时阻止保存"},
        ],
        "open_questions": (
            [{"id": "Q1", "text": "管理员能否绕过校验"}]
            if include_open_question
            else []
        ),
    }
    valid_document = {
        "problem": [
            {
                "text": "缺少创建时校验",
                "claim_type": "source_fact",
                "source_ids": ["F1"],
            }
        ],
        "target_users": [
            {
                "text": "流程管理员需要配置规则",
                "claim_type": "inference",
                "source_ids": ["F1"],
            }
        ],
        "functional_requirements": [
            {
                "text": "不合规提交不得保存",
                "claim_type": "source_fact",
                "source_ids": ["F2"],
            }
        ],
        "acceptance_criteria": [
            {
                "text": "创建时返回明确错误",
                "claim_type": "inference",
                "source_ids": ["F1", "F2"],
            }
        ],
        "risks": [
            {
                "text": "规则可能误判",
                "claim_type": "inference",
                "source_ids": ["F1"],
            }
        ],
        "open_questions": (
            [
                {
                    "text": "管理员能否绕过校验",
                    "claim_type": "open_question",
                    "source_ids": ["Q1"],
                }
            ]
            if include_open_question
            else []
        ),
        "source_url": registry["source_url"],
    }
    return ExecutionRequest(
        tenant_id="tenant_tools",
        instance_id="instance_source_claims",
        node_key="check_source_claims",
        attempt_id="attempt_source_claims",
        attempt_no=1,
        owner_person_id="person_owner",
        executor="tool",
        work={
            "objective": "检查来源归因",
            "inputs": [],
            "outputs": [{"id": "verdict", "type": "data"}],
            "acceptance": ["返回可审计证据"],
            "tool": {
                "kind": "source_claims.check",
                "args": {
                    "document": "dependencies.draft.source_claims",
                    "source_registry": "instance_inputs.source_registry",
                },
            },
        },
        input_snapshot={
            "instance_inputs": {"source_registry": registry},
            "dependencies": {
                "draft": {"source_claims": document or valid_document}
            },
        },
        expected_node_version=1,
        claim_token="claim-source-claims",
        claim_expires_at=NOW + timedelta(minutes=5),
    )


def source_decision_request(*, document: dict | None = None) -> ExecutionRequest:
    registry = {
        "source_url": "https://example.invalid/roadmap/1",
        "facts": [
            {"id": "F1", "text": "当前门槛是真实内部试用"},
            {"id": "F2", "text": "第二名管理员只在有明确需求时验证"},
        ],
        "open_questions": [
            {"id": "Q1", "text": "未来一周唯一优先级是什么"}
        ],
    }
    valid_document = {
        "priority": {
            "text": "完成一项真实内部工作",
            "source_ids": ["F1", "F2"],
        },
        "rationale": [
            {"text": "当前门槛要求真实使用", "source_ids": ["F1"]}
        ],
        "acceptance_criteria": [
            {"text": "Owner 形成明确决定", "source_ids": ["F1"]},
            {"text": "记录首次结果可用性", "source_ids": ["F1"]},
            {"text": "记录人工干预次数", "source_ids": ["F1"]},
        ],
        "not_now": [
            {
                "text": "暂不扩展管理员能力",
                "reconsider_when": "出现第二名管理员的明确需求",
                "source_ids": ["F2"],
            }
        ],
        "risks": [
            {"text": "单个样本不能证明稳定价值", "source_ids": ["F1"]}
        ],
        "answers": [
            {
                "question_id": "Q1",
                "text": "未来一周只完成真实内部工作",
                "source_ids": ["F1", "F2"],
            }
        ],
        "source_url": registry["source_url"],
    }
    return ExecutionRequest(
        tenant_id="tenant_tools",
        instance_id="instance_source_decision",
        node_key="check_source_decision",
        attempt_id="attempt_source_decision",
        attempt_no=1,
        owner_person_id="person_owner",
        executor="tool",
        work={
            "objective": "检查来源约束型决策",
            "inputs": [],
            "outputs": [{"id": "verdict", "type": "data"}],
            "acceptance": ["返回可审计证据"],
            "tool": {
                "kind": "source_decision.check",
                "args": {
                    "document": "dependencies.proposal.source_decision",
                    "source_registry": "instance_inputs.source_registry",
                },
            },
        },
        input_snapshot={
            "instance_inputs": {"source_registry": registry},
            "dependencies": {
                "proposal": {
                    "source_decision": valid_document if document is None else document
                }
            },
        },
        expected_node_version=1,
        claim_token="claim-source-decision",
        claim_expires_at=NOW + timedelta(minutes=5),
    )


def test_content_check_returns_a_structured_pass_quality_result():
    result = ContentCheckToolExecutor().execute(request())

    assert result.result["verdict"] == "pass"
    assert result.result["missing_terms"] == ()
    assert result.result["request_id"] == "tenant_tools:attempt_tools"
    assert result.quality_result is not None
    assert result.quality_result.verdict == QualityVerdict.PASS
    assert "全部 2 项" in result.quality_result.evidence


def test_source_claims_check_proves_structure_categories_and_coverage():
    result = SourceClaimsCheckToolExecutor().execute(source_claims_request())

    assert result.result["verdict"] == "pass"
    assert result.result["fact_coverage"] == {
        "used": 2,
        "total": 2,
        "missing": (),
    }
    assert result.result["question_coverage"] == {
        "used": 1,
        "total": 1,
        "missing": (),
    }
    assert result.quality_result is not None
    assert result.quality_result.verdict == QualityVerdict.PASS


def test_source_claims_check_does_not_force_an_unfounded_open_question():
    result = SourceClaimsCheckToolExecutor().execute(
        source_claims_request(include_open_question=False)
    )

    assert result.result["verdict"] == "pass"
    assert result.result["question_coverage"] == {
        "used": 0,
        "total": 0,
        "missing": (),
    }


def test_source_decision_check_requires_one_priority_and_answers_every_question():
    result = SourceDecisionCheckToolExecutor().execute(source_decision_request())

    assert result.result["verdict"] == "pass"
    assert result.result["fact_coverage"] == {
        "used": 2,
        "total": 2,
        "missing": (),
    }
    assert result.result["question_coverage"] == {
        "used": 1,
        "total": 1,
        "missing": (),
    }
    assert result.quality_result is not None
    assert result.quality_result.verdict == QualityVerdict.PASS


def test_source_decision_check_rejects_unanswered_questions_and_missing_triggers():
    request_with_valid_document = source_decision_request()
    valid = request_with_valid_document.input_snapshot["dependencies"]["proposal"][
        "source_decision"
    ]
    document = {key: value for key, value in valid.items()}
    document["answers"] = []
    document["not_now"] = [
        {
            "text": "暂不扩展管理员能力",
            "reconsider_when": " ",
            "source_ids": ["F2"],
        }
    ]

    result = SourceDecisionCheckToolExecutor().execute(
        source_decision_request(document=document)
    )

    assert result.result["verdict"] == "fail"
    assert "未回答决策问题：Q1" in result.result["evidence"]
    assert "reconsider_when 无效" in result.result["evidence"]


def test_source_decision_check_rejects_too_few_criteria_and_duplicate_answers():
    request_with_valid_document = source_decision_request()
    valid = request_with_valid_document.input_snapshot["dependencies"]["proposal"][
        "source_decision"
    ]
    document = {key: value for key, value in valid.items()}
    document["acceptance_criteria"] = list(valid["acceptance_criteria"][:2])
    document["answers"] = [valid["answers"][0], valid["answers"][0]]

    result = SourceDecisionCheckToolExecutor().execute(
        source_decision_request(document=document)
    )

    assert result.result["verdict"] == "fail"
    assert "acceptance_criteria 必须包含 3 到 5 项" in result.result["evidence"]
    assert "question_id 重复：Q1" in result.result["evidence"]


def test_source_claims_check_rejects_q_ids_disguised_as_facts():
    request_with_invalid_claim = source_claims_request()
    document = {
        key: value
        for key, value in request_with_invalid_claim.input_snapshot["dependencies"][
            "draft"
        ]["source_claims"].items()
    }
    document["open_questions"] = [
        {
            "text": "管理员能否绕过校验",
            "claim_type": "source_fact",
            "source_ids": ["Q1"],
        }
    ]

    result = SourceClaimsCheckToolExecutor().execute(
        source_claims_request(document=document)
    )

    assert result.result["verdict"] == "fail"
    assert result.quality_result is not None
    assert result.quality_result.verdict == QualityVerdict.FAIL
    assert "必须标记为 open_question" in result.result["evidence"]
    assert "非 F 编号" in result.result["evidence"]
    assert "语义复核" in result.quality_result.suggestion


def test_content_check_returns_fail_evidence_without_impersonating_the_owner():
    result = ContentCheckToolExecutor().execute(request(content="只有结论"))

    assert result.result["verdict"] == "fail"
    assert result.result["missing_terms"] == ("下一步",)
    assert result.quality_result is not None
    assert result.quality_result.verdict == QualityVerdict.FAIL
    assert "缺少必需内容" in result.quality_result.evidence
    assert "节点 Owner" in result.quality_result.suggestion


@pytest.mark.parametrize(
    "execution_request, message",
    [
        (request(args={"required_terms": []}), "source is required"),
        (
            request(
                args={
                    "source": "dependencies.missing.content",
                    "required_terms": [],
                }
            ),
            "was not found",
        ),
        (
            request(
                args={
                    "source": "dependencies.draft_summary",
                    "required_terms": [],
                }
            ),
            "resolve to text",
        ),
        (
            request(
                args={
                    "source": "dependencies.draft_summary.content",
                    "required_terms": [],
                    "min_chars": 5,
                    "max_chars": 4,
                }
            ),
            "cannot exceed",
        ),
    ],
)
def test_content_check_rejects_invalid_or_unbounded_contracts(
    execution_request,
    message,
):
    with pytest.raises(ValueError, match=message):
        ContentCheckToolExecutor().execute(execution_request)


def test_web_search_executor_preserves_research_content_and_sources():
    client = StaticWebSearch()
    result = WebSearchToolExecutor(client).execute(web_search_request())

    assert result.result["content"].endswith("景点开放信息与预约规则已核对。")
    assert "不证明页面内容" in result.result["content"]
    assert result.result["sources"] == ("https://example.com/official-guide",)
    assert result.result["provider"] == "openai_responses_web_search"
    assert result.result["query"] == "优先核对景点官方开放时间和预约规则"
    assert result.result["source_records"][0]["published_at_status"] == "unknown"
    assert result.result["usage"] == {}
    assert result.result["error"] is None
    assert result.result["tool_kind"] == "web.search"
    assert client.calls[0]["model_role"] == "default"
    assert "2026-08-20" in client.calls[0]["prompt"]
    assert "不预订、不购买" in client.calls[0]["prompt"]


def test_web_search_executor_preserves_typed_provider_evidence_without_synthesizing():
    provider = StaticSearchProvider()

    result = WebSearchToolExecutor(provider).execute(web_search_request()).result

    assert provider.calls == ["优先核对景点官方开放时间和预约规则"]
    assert result["provider"] == "stub_search"
    assert result["query"] == provider.calls[0]
    assert result["sources"] == ("https://www.szmuseum.com/guide",)
    assert result["source_records"][0]["title"] == "苏州博物馆参观须知"
    assert result["source_records"][0]["published_at_status"] == "unknown"
    assert result["source_records"][0]["url_status"] == "valid"
    assert result["source_records"][0]["health"] == "unknown"
    assert result["source_records"][0]["freshness"] == "unknown"
    assert result["source_records"][0]["authority"] == "unknown"
    assert result["source_records"][0]["support"] == "unknown"
    assert result["source_health"] == {
        "available": False,
        "reason": "safe_outbound_fetcher_unavailable",
    }
    assert result["source_quality_summary"] == {
        "total": 1,
        "reachable": 0,
        "unreachable": 0,
        "health_unknown": 1,
        "current": 0,
        "stale": 0,
        "freshness_unknown": 1,
        "support": "unknown",
    }
    assert "Human 复核" in result["evidence_boundary"]
    assert result["usage"]["result_count"] == 1
    assert result["error"] is None
    assert "发布时间：时间不明" in result["content"]


def test_web_search_executor_rejects_unsourced_or_provider_controlled_results():
    client = StaticWebSearch()
    client.sources = []
    with pytest.raises(ValueError, match="cited sources"):
        WebSearchToolExecutor(client).execute(web_search_request())

    with pytest.raises(ValueError, match="unsupported fields"):
        WebSearchToolExecutor(StaticWebSearch()).execute(
            web_search_request(extra_args={"api_key": "must-not-be-accepted"})
        )

    client.sources = ["https://user:password@example.com/source"]
    with pytest.raises(ValueError, match="cited sources"):
        WebSearchToolExecutor(client).execute(web_search_request())


def test_web_search_executor_preflights_provider_capability_without_remote_call():
    client = UnavailableWebSearch()

    with pytest.raises(RuntimeError, match="URL 引用"):
        WebSearchToolExecutor(client).execute(web_search_request())

    assert client.calls == []


def test_web_search_marks_stale_sources_only_with_an_explicit_policy():
    provider = StaticSearchProvider(published_at="2020-01-01")
    result = WebSearchToolExecutor(provider).execute(
        web_search_request(extra_args={"freshness_max_age_days": 30})
    ).result

    assert result["source_records"][0]["freshness"] == "stale"
    assert result["source_quality_summary"]["stale"] == 1


def test_web_search_result_budget_includes_the_visible_evidence_boundary():
    with pytest.raises(ValueError, match="result exceeds"):
        WebSearchToolExecutor(
            StaticSearchProvider(),
            max_result_chars=50,
        ).execute(web_search_request())


def test_web_search_fails_when_an_enabled_fetcher_observes_all_sources_unreachable():
    with pytest.raises(SearchSourcesUnavailableError) as error:
        WebSearchToolExecutor(
            StaticSearchProvider(),
            source_fetcher=UnreachableSourceFetcher(),
        ).execute(web_search_request())

    assert error.value.error_code == "search_sources_unreachable"


def test_source_evidence_check_binds_claims_to_the_current_search_attempt():
    result = SourceEvidenceCheckToolExecutor().execute(source_evidence_request())

    assert result.result["verdict"] == "pass"
    assert result.result["support"] == "supported"
    assert result.result["semantic_verification"] == "not_independently_verified"
    assert result.result["claim_support"] == (
        {
            "claim_id": "C1",
            "source_url": "https://www.szmuseum.com/guide",
            "supporting_excerpt": "实行预约参观",
            "support": "supported",
        },
    )


@pytest.mark.parametrize(
    ("claim_update", "message"),
    [
        (
            {"source_url": "https://other.example/guide"},
            "不属于当前搜索 Attempt",
        ),
        ({"supporting_excerpt": "免费且无需预约"}, "不是 provider 原文片段"),
    ],
)
def test_source_evidence_check_rejects_replaced_urls_and_forged_excerpts(
    claim_update,
    message,
):
    valid = source_evidence_request().input_snapshot["dependencies"]["summary"][
        "claims"
    ][0]
    result = SourceEvidenceCheckToolExecutor().execute(
        source_evidence_request(claims=[{**valid, **claim_update}])
    )

    assert result.result["verdict"] == "fail"
    assert result.result["support"] == "unsupported"
    assert message in result.result["evidence"]


def test_source_evidence_check_rejects_non_search_dependencies_and_budgets():
    with pytest.raises(ValueError, match="server provenance"):
        SourceEvidenceCheckToolExecutor().execute(
            source_evidence_request(provenance_tool_kind="content.check")
        )

    with pytest.raises(ValueError, match="exceeds"):
        SourceEvidenceCheckToolExecutor(max_source_chars=10).execute(
            source_evidence_request()
        )


def test_source_evidence_check_rejects_root_agent_forgery_and_missing_provenance():
    with pytest.raises(ValueError, match="server provenance"):
        SourceEvidenceCheckToolExecutor().execute(
            source_evidence_request(
                source_result_tool_kind="web.search",
                source_executor="agent",
                provenance_tool_kind=None,
            )
        )

    with pytest.raises(ValueError, match="server provenance"):
        SourceEvidenceCheckToolExecutor().execute(
            source_evidence_request(include_provenance=False)
        )


def test_source_evidence_check_rejects_nested_and_cross_dependency_forgery():
    original = source_evidence_request()
    records = original.input_snapshot["dependencies"]["research"]["source_records"]
    claims = original.input_snapshot["dependencies"]["summary"]["claims"]
    forged_snapshot = {
        "dependencies": {
            "research": original.input_snapshot["dependencies"]["research"],
            "summary": {
                "claims": claims,
                "copied_search": {
                    "tool_kind": "web.search",
                    "sources": ["https://www.szmuseum.com/guide"],
                    "source_records": records,
                },
            },
        },
        "dependency_provenance": original.input_snapshot["dependency_provenance"],
    }
    forged_work = {
        **original.work,
        "tool": {
            "kind": "source_evidence.check",
            "args": {
                "claims": "dependencies.summary.claims",
                "source_records": "dependencies.summary.copied_search.source_records",
            },
        },
    }

    with pytest.raises(ValueError, match="direct dependency"):
        SourceEvidenceCheckToolExecutor().execute(
            replace(original, input_snapshot=forged_snapshot, work=forged_work)
        )

    copied_snapshot = {
        "dependencies": {
            **forged_snapshot["dependencies"],
            "copied": {
                "tool_kind": "web.search",
                "sources": ["https://www.szmuseum.com/guide"],
                "source_records": records,
            },
        },
        "dependency_provenance": {
            **original.input_snapshot["dependency_provenance"],
            "copied": {
                "node_key": "copied",
                "executor": "agent",
                "tool_kind": None,
                "attempt_id": "attempt_copied",
                "attempt_no": 1,
            },
        },
    }
    copied_work = {
        **original.work,
        "tool": {
            "kind": "source_evidence.check",
            "args": {
                "claims": "dependencies.summary.claims",
                "source_records": "dependencies.copied.source_records",
            },
        },
    }
    with pytest.raises(ValueError, match="server provenance"):
        SourceEvidenceCheckToolExecutor().execute(
            replace(original, input_snapshot=copied_snapshot, work=copied_work)
        )


def test_tool_router_selects_one_explicit_adapter_and_rejects_unknown_kinds():
    router = ToolExecutorRouter(
        [
            ContentCheckToolExecutor(),
            SourceClaimsCheckToolExecutor(),
            SourceDecisionCheckToolExecutor(),
            SourceEvidenceCheckToolExecutor(),
            DevelopmentToolExecutor(sleep=lambda _: None),
            WebSearchToolExecutor(StaticWebSearch()),
        ]
    )

    assert router.accepts(executor=ExecutorKind.TOOL, work=request().work)
    assert router.execute(request()).result["verdict"] == "pass"
    assert router.execute(source_claims_request()).result["verdict"] == "pass"
    assert router.execute(source_decision_request()).result["verdict"] == "pass"
    assert router.execute(source_evidence_request()).result["verdict"] == "pass"
    assert router.execute(web_search_request()).result["sources"]
    with pytest.raises(ValueError, match="unsupported Tool contract"):
        router.execute(request(kind="unknown.tool"))


def test_runtime_can_enable_content_check_without_enabling_development_echo():
    settings = TargetRuntimeSettings.from_environ(
        {
            "LARKFLOW_TARGET_DSN": "postgresql:///test",
            "LARKFLOW_TARGET_TENANT": "tenant_tools",
            "LARKFLOW_TARGET_ENABLE_CONTENT_CHECK_EXECUTOR": "true",
            "LARKFLOW_TARGET_CONTENT_CHECK_MAX_CHARS": "1234",
        },
        worker_id="worker-tools",
    )

    registry = _executors(settings, environ={})

    assert tuple(registry) == (ExecutorKind.TOOL,)
    router = registry[ExecutorKind.TOOL]
    assert isinstance(router, ToolExecutorRouter)
    assert router.execute(request()).result["verdict"] == "pass"
    assert router.execute(source_decision_request()).result["verdict"] == "pass"


def test_runtime_can_enable_web_search_with_the_existing_llm_route():
    settings = TargetRuntimeSettings.from_environ(
        {
            "LARKFLOW_TARGET_DSN": "postgresql:///test",
            "LARKFLOW_TARGET_TENANT": "tenant_tools",
            "LARKFLOW_TARGET_ENABLE_WEB_SEARCH_EXECUTOR": "true",
            "LARKFLOW_TARGET_WEB_SEARCH_MAX_PROMPT_CHARS": "1234",
            "LARKFLOW_TARGET_WEB_SEARCH_MAX_RESULT_CHARS": "5678",
            "LLM_BASE_URL": "https://ark.example.invalid/api/v3",
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "test-model",
            "LLM_TIMEOUT": "20",
            "LLM_WEB_SEARCH_CAPABILITY": "responses_citations",
        },
        worker_id="worker-search",
    )

    registry = _executors(
        settings,
        environ={
            "LLM_BASE_URL": "https://ark.example.invalid/api/v3",
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "test-model",
            "LLM_TIMEOUT": "20",
            "LLM_WEB_SEARCH_CAPABILITY": "responses_citations",
        },
    )

    assert tuple(registry) == (ExecutorKind.TOOL,)
    router = registry[ExecutorKind.TOOL]
    assert router.accepts(
        executor=ExecutorKind.TOOL,
        work=web_search_request().work,
    )
    assert settings.web_search_max_prompt_chars == 1234
    assert settings.web_search_max_result_chars == 5678


def test_runtime_prefers_configured_doubao_search_without_an_llm_route():
    environment = {
        "LARKFLOW_TARGET_DSN": "postgresql:///test",
        "LARKFLOW_TARGET_TENANT": "tenant_tools",
        "LARKFLOW_TARGET_ENABLE_WEB_SEARCH_EXECUTOR": "true",
        "LARKFLOW_DOUBAO_SEARCH_API_KEY": "test-search-key",
    }
    settings = TargetRuntimeSettings.from_environ(
        environment,
        worker_id="worker-search",
    )

    registry = _executors(settings, environ=environment)

    router = registry[ExecutorKind.TOOL]
    assert router.accepts(
        executor=ExecutorKind.TOOL,
        work=web_search_request().work,
    )


def test_runtime_rejects_an_incomplete_doubao_search_configuration():
    environment = {
        "LARKFLOW_TARGET_DSN": "postgresql:///test",
        "LARKFLOW_TARGET_TENANT": "tenant_tools",
        "LARKFLOW_TARGET_ENABLE_WEB_SEARCH_EXECUTOR": "true",
        "LARKFLOW_DOUBAO_SEARCH_API_ID": "configured-without-key",
    }
    settings = TargetRuntimeSettings.from_environ(
        environment,
        worker_id="worker-search",
    )

    with pytest.raises(ValueError, match="configured incompletely"):
        _executors(settings, environ=environment)


def test_doubao_search_runtime_claim_covers_timeout_and_safety_margin():
    environment = {
        "LARKFLOW_TARGET_DSN": "postgresql:///test",
        "LARKFLOW_TARGET_TENANT": "tenant_tools",
        "LARKFLOW_TARGET_ENABLE_WEB_SEARCH_EXECUTOR": "true",
        "LARKFLOW_DOUBAO_SEARCH_API_KEY": "test-search-key",
        "LARKFLOW_TARGET_CLAIM_TTL_SECONDS": "60",
        "LARKFLOW_TARGET_AGENT_CLAIM_SAFETY_SECONDS": "30",
    }
    settings = TargetRuntimeSettings.from_environ(
        environment,
        worker_id="worker-search",
    )

    with pytest.raises(ValueError, match="Doubao Search timeout"):
        _executors(settings, environ=environment)


def test_packaged_human_agent_tool_human_template_matches_the_target_contract():
    path = (
        Path(__file__).parents[1]
        / "larkflow"
        / "templates"
        / "target_checked_agent_review.yaml"
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id="tenant_tools",
        actor_person_id="person_owner",
        document=document,
    )
    templates.enable(
        "tenant_tools",
        "target_checked_agent_review",
        actor_person_id="person_owner",
    )
    snapshot = templates.instantiate(
        "tenant_tools",
        "target_checked_agent_review",
        inputs={"brief": "验证混合执行器流程"},
        owner_bindings={"project_owner": "person_owner"},
    )
    validate_snapshot(snapshot)

    assert tuple(node.executor.value for node in snapshot.nodes) == (
        "human",
        "agent",
        "tool",
        "human",
    )
    assert snapshot.node("check_summary").work["tool"]["kind"] == "content.check"


def test_packaged_source_grounded_template_has_explicit_provenance_and_decision_gate():
    path = (
        Path(__file__).parents[1]
        / "larkflow"
        / "templates"
        / "source_grounded_review.yaml"
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id="tenant_tools",
        actor_person_id="person_owner",
        document=document,
    )
    templates.enable(
        "tenant_tools",
        "source_grounded_review",
        actor_person_id="person_owner",
    )
    snapshot = templates.instantiate(
        "tenant_tools",
        "source_grounded_review",
        inputs={
            "source_registry": {
                "source_url": "https://example.invalid/issues/42",
                "facts": [{"id": "F1", "text": "创建时校验"}],
                "open_questions": [{"id": "Q1", "text": "谁可绕过"}],
            }
        },
        owner_bindings={"project_owner": "person_owner"},
    )
    validate_snapshot(snapshot)

    assert tuple(node.executor.value for node in snapshot.nodes) == (
        "human",
        "agent",
        "tool",
        "human",
    )
    assert (
        snapshot.node("draft_engineering_brief").work["agent"]["result_format"]
        == "source_claims.v1"
    )
    assert (
        snapshot.node("check_source_claims").work["tool"]["kind"]
        == "source_claims.check"
    )
    assert snapshot.node("review_engineering_brief").work["decision"] == {
        "kind": "accept_reject",
        "reject_target": "draft_engineering_brief",
    }


def test_packaged_source_grounded_decision_answers_questions_before_human_gate():
    path = (
        Path(__file__).parents[1]
        / "larkflow"
        / "templates"
        / "source_grounded_decision.yaml"
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id="tenant_tools",
        actor_person_id="person_owner",
        document=document,
    )
    templates.enable(
        "tenant_tools",
        "source_grounded_decision",
        actor_person_id="person_owner",
    )
    snapshot = templates.instantiate(
        "tenant_tools",
        "source_grounded_decision",
        inputs={
            "source_registry": {
                "source_url": "https://example.invalid/roadmap/1",
                "facts": [{"id": "F1", "text": "先做真实内部工作"}],
                "open_questions": [{"id": "Q1", "text": "本周做什么"}],
            }
        },
        owner_bindings={"project_owner": "person_owner"},
    )
    validate_snapshot(snapshot)

    assert tuple(node.executor.value for node in snapshot.nodes) == (
        "human",
        "agent",
        "tool",
        "human",
    )
    assert (
        snapshot.node("propose_source_decision").work["agent"]["result_format"]
        == "source_decision.v1"
    )
    assert (
        snapshot.node("check_source_decision").work["tool"]["kind"]
        == "source_decision.check"
    )
    assert snapshot.node("review_source_decision").work["decision"] == {
        "kind": "accept_reject",
        "reject_target": "propose_source_decision",
    }


def test_mixed_template_runs_human_agent_tool_human_to_completion():
    path = (
        Path(__file__).parents[1]
        / "larkflow"
        / "templates"
        / "target_checked_agent_review.yaml"
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id="tenant_tools",
        actor_person_id="person_owner",
        document=document,
    )
    templates.enable(
        "tenant_tools",
        "target_checked_agent_review",
        actor_person_id="person_owner",
    )
    snapshot = templates.instantiate(
        "tenant_tools",
        "target_checked_agent_review",
        inputs={"brief": "验证混合执行器流程"},
        owner_bindings={"project_owner": "person_owner"},
    )

    repository = InMemoryWorkflowRepository()
    service = WorkflowService(
        repository,
        runner=NodeRunner(claim_ttl=timedelta(minutes=5)),
        clock=lambda: NOW,
    )
    service.create_draft(
        instance_id="mixed_instance",
        tenant_id="tenant_tools",
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot,
    )
    service.confirm_draft(
        "tenant_tools",
        "mixed_instance",
        actor_person_id="person_owner",
    )
    worker = WorkflowWorker(
        service,
        repository,
        tenant_id="tenant_tools",
        worker_id="worker-tools",
        executors={
            ExecutorKind.AGENT: LLMAgentExecutor(StaticCompletion()),
            ExecutorKind.TOOL: ToolExecutorRouter([ContentCheckToolExecutor()]),
        },
        clock=lambda: NOW,
    )

    first = worker.run_once()
    assert first.human_dispatched == 1
    current = service.get("tenant_tools", "mixed_instance")
    assert current.nodes["confirm_brief"].status == NodeStatus.WAITING_HUMAN
    confirmation = current.current_attempt("confirm_brief")
    service.submit_human(
        "tenant_tools",
        "mixed_instance",
        "confirm_brief",
        actor_person_id="person_owner",
        attempt_no=confirmation.attempt_no,
        expected_node_version=current.nodes["confirm_brief"].version,
            result={"confirmation": "已确认"},
    )

    assert worker.run_once().completed == 1
    assert worker.run_once().completed == 1
    final_dispatch = worker.run_once()
    assert final_dispatch.human_dispatched == 1

    current = service.get("tenant_tools", "mixed_instance")
    checked = current.current_attempt("check_summary")
    assert checked.result is not None
    assert checked.result["verdict"] == "pass"
    assert checked.quality_result is not None
    assert checked.quality_result.verdict == QualityVerdict.PASS
    final_attempt = current.current_attempt("review_summary")
    assert set(final_attempt.input_snapshot["dependencies"]) == {
        "draft_summary",
        "check_summary",
    }
    service.submit_human(
        "tenant_tools",
        "mixed_instance",
        "review_summary",
        actor_person_id="person_owner",
        attempt_no=final_attempt.attempt_no,
        expected_node_version=current.nodes["review_summary"].version,
        result={"review": "accepted"},
    )

    assert service.get("tenant_tools", "mixed_instance").status == InstanceStatus.DONE
