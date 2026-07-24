"""通用 produce / gate 执行体单测（per-role，不再 per-node-id）。

引擎按 `executor × role + 配置`跑节点：llm 读 prompt/model_role、produce 末步物化交付物、
tool 走注入的确定性程序。per-id handler 保留为逃生舱（确定性程序天然是自定义代码）。
"""
import pytest

from larkflow.config import RoleResolver
from larkflow.engine.deliverables import prior_handle
from larkflow.engine.executors import Executors, ExecutorError
from larkflow.io import FakeDeliverableStore, MockLarkIO
from larkflow.llm import StubLLM


def build(**kw) -> Executors:
    kw.setdefault("io", MockLarkIO())
    kw.setdefault("resolver", RoleResolver())
    kw.setdefault("llm", StubLLM(completion="AI 起草的正文"))
    kw.setdefault("deliverables", FakeDeliverableStore())
    return Executors(**kw)


DRAFT = {"id": "draft", "label": "商务条款初稿", "executor": "llm", "role": "produce",
         "deps": ["seed"], "prompt": "照素材写商务条款", "model_role": "writer",
         "deliverable": {"region": "whole"}}


def state(outputs=None, meta=None) -> dict:
    return {"dag": [DRAFT], "outputs": outputs or {}, "meta": meta or {"instance_id": "wf-1"}}


# ---------- (llm, produce)：通用起草 ----------

def test_llm_produce_routes_by_model_role_and_materializes():
    ex = build()
    out = ex.run_llm(DRAFT, state())

    call = ex.llm.calls[-1]
    assert call["model_role"] == "writer"          # 按角色路由（ADR-017）
    assert "照素材写商务条款" in call["prompt"]     # 节点 prompt 进上下文
    handle = prior_handle({"draft": out}, "draft")
    assert ex.deliverables.fetch(handle) == "AI 起草的正文"


def test_llm_produce_feeds_upstream_deliverable_text_into_prompt():
    """下游 llm 消费上游正文 = 经 outputs[dep] 取 handle 再 fetch（ADR-016）。"""
    ex = build()
    seed = ex.deliverables.create(title="素材", content="甲方要求 30 天账期", idem_key="k")
    st = state({"seed": {"deliverable": seed.to_dict()}})

    ex.run_llm(DRAFT, st)

    assert "甲方要求 30 天账期" in ex.llm.calls[-1]["prompt"]


def test_llm_produce_rerun_overwrites_same_handle():
    """打回重算：handle 不变，飞书留版本（旁支复用的基础）。"""
    ex = build()
    first = ex.run_llm(DRAFT, state())
    ex.llm.completion = "改过的正文"
    second = ex.run_llm(DRAFT, state({"draft": first}))

    assert second["deliverable"] == first["deliverable"]
    assert len(ex.deliverables.docs) == 1
    assert ex.deliverables.versions(prior_handle({"draft": second}, "draft")) == [
        "AI 起草的正文", "改过的正文"]


def test_llm_gate_is_rejected_at_runtime():
    """红线：LLM 绝不自动放行（模板护栏③已拦；执行体再兜一道）。"""
    ex = build()
    with pytest.raises(ExecutorError, match="放行"):
        ex.run_llm(dict(DRAFT, role="gate", approval_policy="auto"), state())


# ---------- (tool, *)：确定性程序注入 ----------

def test_tool_produce_handler_content_gets_materialized():
    def handler(node, st, ex):
        return {"ok": True, "content": "登记正文"}

    ex = build(tool_handlers={"intake": handler})
    node = {"id": "intake", "label": "受理登记", "executor": "tool", "role": "produce",
            "deps": [], "deliverable": {"region": "whole"}}
    out = ex.run_tool(node, state())

    assert out["ok"] is True
    assert "content" not in out                      # content 落进交付物，不塞 state
    assert ex.deliverables.fetch(prior_handle({"intake": out}, "intake")) == "登记正文"


def test_tool_node_without_handler_raises_instead_of_silent_ok():
    """静默返回 {ok:True} 会让 gate 缺 passed → 无限打回；宁可炸。"""
    ex = build()
    node = {"id": "checks", "label": "格式检查", "executor": "tool", "role": "gate",
            "deps": ["draft"], "approval_policy": "auto"}
    with pytest.raises(ExecutorError, match="handler"):
        ex.run_tool(node, state())


def test_tool_gate_handler_must_return_passed():
    ex = build(tool_handlers={"checks": lambda n, s, e: {"ok": True}})
    node = {"id": "checks", "label": "格式检查", "executor": "tool", "role": "gate",
            "deps": ["draft"], "approval_policy": "auto"}
    with pytest.raises(ExecutorError, match="passed"):
        ex.run_tool(node, state())


def test_validate_coverage_catches_missing_tool_handler_before_run():
    ex = build(tool_handlers={})
    dag = [{"id": "checks", "label": "格式检查", "executor": "tool", "role": "gate",
            "deps": ["draft"], "approval_policy": "auto"}, DRAFT]
    with pytest.raises(ExecutorError, match="checks"):
        ex.validate_coverage(dag)


# ---------- (human, produce)：引擎先备好容器给人写 ----------

FINALIZE = {"id": "finalize", "label": "负责人定稿", "executor": "human", "role": "produce",
            "deps": [], "assignee_role": "负责人", "signal": "task_complete",
            "deliverable": {"region": "whole"}}


def test_human_produce_prepares_container_with_link():
    ex = build()
    prepared = ex.prepare_human(FINALIZE, state())
    handle = prior_handle({"finalize": prepared}, "finalize")
    assert handle is not None and handle.url                  # 对人是一条文档链接
    assert "负责人" in ex.deliverables.fetch(handle)          # 占位提示写给谁


def test_human_produce_replay_does_not_clobber_human_edits():
    """interrupt 前的代码在 resume 时会重跑：绝不能把人写的内容覆盖回占位。"""
    ex = build()
    prepared = ex.prepare_human(FINALIZE, state())
    handle = prior_handle({"finalize": prepared}, "finalize")
    ex.deliverables.overwrite(handle, content="人手写的定稿")

    again = ex.prepare_human(FINALIZE, state({"finalize": prepared}))

    assert again["deliverable"] == prepared["deliverable"]
    assert ex.deliverables.fetch(handle) == "人手写的定稿"
    assert len(ex.deliverables.docs) == 1


def test_human_gate_prepares_no_container():
    ex = build()
    gate = {"id": "review", "label": "复核", "executor": "human", "role": "gate",
            "deps": ["draft"], "assignee_role": "法务", "signal": "card_action",
            "approval_policy": "single"}
    assert ex.prepare_human(gate, state()) == {}
    assert ex.deliverables.docs == {}
