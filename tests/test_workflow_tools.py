"""Target Tool routing, content checks, and mixed template tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

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
    TargetRuntimeSettings,
    TemplateService,
    ToolExecutorRouter,
    WorkflowService,
    WorkflowWorker,
    validate_snapshot,
)
from larkflow.workflow.cli import _executors


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


class StaticCompletion:
    def complete(self, *, prompt: str, model_role: str) -> str:
        return "结论：输入完整，可以继续。下一步：请节点 Owner 复核摘要。"


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


def test_tool_router_selects_one_explicit_adapter_and_rejects_unknown_kinds():
    router = ToolExecutorRouter(
        [
            ContentCheckToolExecutor(),
            SourceClaimsCheckToolExecutor(),
            DevelopmentToolExecutor(sleep=lambda _: None),
        ]
    )

    assert router.accepts(executor=ExecutorKind.TOOL, work=request().work)
    assert router.execute(request()).result["verdict"] == "pass"
    assert router.execute(source_claims_request()).result["verdict"] == "pass"
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
        result={"confirmation": True},
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
