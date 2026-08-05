"""Narrow Target Agent adapter and lease-budget tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from larkflow.workflow import (
    ExecutionRequest,
    ExecutorKind,
    InMemoryTemplateStore,
    LLMAgentExecutor,
    TemplateService,
    TargetRuntimeSettings,
    TargetDraftGenerationSettings,
    validate_snapshot,
)
from larkflow.workflow.cli import _draft_generator, _executors


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class RecordingCompletion:
    def __init__(self, content: str = "结论：可以继续。") -> None:
        self.content = content
        self.calls = []

    def complete(self, *, prompt: str, model_role: str) -> str:
        self.calls.append({"prompt": prompt, "model_role": model_role})
        return self.content


def request(*, kind: str = "llm.generate", instructions: str = "生成摘要"):
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
                "kind": kind,
                "model_role": "writer",
                "instructions": instructions,
            },
        },
        input_snapshot={
            "instance_inputs": {"brief": "发布检查"},
            "dependencies": {"confirm": {"approved": True}},
        },
        expected_node_version=2,
        claim_token="claim-agent",
        claim_expires_at=NOW + timedelta(minutes=5),
    )


def test_agent_executor_builds_auditable_prompt_and_structured_result():
    completion = RecordingCompletion()
    executor = LLMAgentExecutor(completion)

    result = executor.execute(request())

    assert result.result == {
        "content": "结论：可以继续。",
        "agent_kind": "llm.generate",
        "model_role": "writer",
        "request_id": "tenant_agent:attempt_agent",
    }
    assert completion.calls[0]["model_role"] == "writer"
    prompt = completion.calls[0]["prompt"]
    assert "发布检查" in prompt
    assert '"approved": true' in prompt
    assert "生成摘要" in prompt
    assert "tenant_agent:attempt_agent" not in prompt


@pytest.mark.parametrize(
    "execution_request, message",
    [
        (request(kind="unknown"), "unsupported Agent kind"),
        (request(instructions=""), "instructions are required"),
    ],
)
def test_agent_executor_rejects_work_outside_the_narrow_contract(
    execution_request,
    message,
):
    with pytest.raises(ValueError, match=message):
        LLMAgentExecutor(RecordingCompletion()).execute(execution_request)


def test_agent_executor_rejects_empty_and_unbounded_results():
    with pytest.raises(ValueError, match="empty result"):
        LLMAgentExecutor(RecordingCompletion("  ")).execute(request())

    with pytest.raises(ValueError, match="exceeds 3"):
        LLMAgentExecutor(
            RecordingCompletion("four"),
            max_result_chars=3,
        ).execute(request())


@pytest.mark.parametrize(
    "wrapped, expected",
    [
        ('{"content": "正文一"}', "正文一"),
        ('[{"id": "content", "type": "text", "text": "正文二"}]', "正文二"),
        (
            '```json\n[{"text": "第一段"}, {"content": "第二段"}]\n```',
            "第一段\n\n第二段",
        ),
    ],
)
def test_agent_executor_extracts_plain_text_from_common_structured_wrappers(
    wrapped,
    expected,
):
    result = LLMAgentExecutor(RecordingCompletion(wrapped)).execute(request())

    assert result.result["content"] == expected


def test_agent_executor_keeps_unrecognized_json_as_text():
    content = '{"decision": "continue"}'

    result = LLMAgentExecutor(RecordingCompletion(content)).execute(request())

    assert result.result["content"] == content


def test_runtime_assembles_agent_only_with_a_safe_lease_budget():
    environment = {
        "LLM_BASE_URL": "https://llm.example.invalid/v1",
        "LLM_API_KEY": "test-key",
        "LLM_MODEL": "test-model",
        "LLM_TIMEOUT": "20",
    }
    settings = TargetRuntimeSettings(
        dsn="postgresql:///test",
        tenant_id="tenant_agent",
        worker_id="worker_agent",
        claim_ttl=timedelta(seconds=60),
        enable_agent_executor=True,
        agent_claim_safety=timedelta(seconds=10),
    )

    registry = _executors(settings, environ=environment)

    assert tuple(registry) == (ExecutorKind.AGENT,)
    assert isinstance(registry[ExecutorKind.AGENT], LLMAgentExecutor)

    unsafe = TargetRuntimeSettings(
        dsn="postgresql:///test",
        tenant_id="tenant_agent",
        worker_id="worker_agent",
        claim_ttl=timedelta(seconds=30),
        enable_agent_executor=True,
        agent_claim_safety=timedelta(seconds=10),
    )
    with pytest.raises(ValueError, match="claim TTL"):
        _executors(unsafe, environ=environment)


def test_runtime_refuses_to_enable_agent_without_credentials():
    settings = TargetRuntimeSettings(
        dsn="postgresql:///test",
        tenant_id="tenant_agent",
        worker_id="worker_agent",
        enable_agent_executor=True,
    )

    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        _executors(settings, environ={})


def test_draft_generator_budgets_both_model_attempts():
    environment = {
        "LLM_BASE_URL": "https://llm.example.invalid/v1",
        "LLM_API_KEY": "test-key",
        "LLM_MODEL": "test-model",
        "LLM_TIMEOUT": "20",
    }
    safe = TargetDraftGenerationSettings(
        dsn="postgresql:///test",
        tenant_id="tenant_agent",
        worker_id="draft_worker",
        claim_ttl=timedelta(seconds=51),
        claim_safety=timedelta(seconds=10),
    )
    assert _draft_generator(safe, environ=environment) is not None

    unsafe = TargetDraftGenerationSettings(
        dsn="postgresql:///test",
        tenant_id="tenant_agent",
        worker_id="draft_worker",
        claim_ttl=timedelta(seconds=50),
        claim_safety=timedelta(seconds=10),
    )
    with pytest.raises(ValueError, match="two complete LLM route budgets"):
        _draft_generator(unsafe, environ=environment)


def test_packaged_human_agent_human_template_matches_the_target_contract():
    path = (
        Path(__file__).parents[1]
        / "larkflow"
        / "templates"
        / "target_agent_review.yaml"
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id="tenant_agent",
        actor_person_id="person_owner",
        document=document,
    )
    templates.enable(
        "tenant_agent",
        "target_agent_review",
        actor_person_id="person_owner",
    )
    snapshot = templates.instantiate(
        "tenant_agent",
        "target_agent_review",
        inputs={"brief": "验证正式模板入口"},
        owner_bindings={"project_owner": "person_owner"},
    )
    validate_snapshot(snapshot)

    assert tuple(node.executor.value for node in snapshot.nodes) == (
        "human",
        "agent",
        "human",
    )
    assert snapshot.node("draft_summary").work["agent"]["kind"] == "llm.generate"


def test_packaged_collaborative_template_splits_requester_and_reviewer():
    path = (
        Path(__file__).parents[1]
        / "larkflow"
        / "templates"
        / "collaborative_agent_review.yaml"
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id="tenant_agent",
        actor_person_id="person_owner",
        document=document,
    )
    templates.enable(
        "tenant_agent",
        "collaborative_agent_review",
        actor_person_id="person_owner",
    )
    snapshot = templates.instantiate(
        "tenant_agent",
        "collaborative_agent_review",
        inputs={"brief": "验证跨人员模板入口"},
        owner_bindings={
            "requester": "person_owner",
            "reviewer": "person_reviewer",
        },
    )
    validate_snapshot(snapshot)

    assert tuple(node.owner_person_id for node in snapshot.nodes) == (
        "person_owner",
        "person_owner",
        "person_reviewer",
    )
    assert tuple(node.executor.value for node in snapshot.nodes) == (
        "human",
        "agent",
        "human",
    )
