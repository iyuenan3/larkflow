"""放行策略：auto 短路（确定性机检，不挂人）/ single 走人；会签阈值明确未实现。

v1.0 的合同图末尾就是一道 auto 格式检查门（ARCHITECTURE〈首个工作流〉），
它必须能自动放行、也能自动打回上游重写。
"""
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from larkflow.config import RoleResolver
from larkflow.engine import Executors, build_graph
from larkflow.engine.support import UnsupportedInV1, assert_v1_supported
from larkflow.io import FakeDeliverableStore, MockLarkIO
from larkflow.llm import StubLLM
from larkflow.model.template import validate_template

# seed(tool) → draft(llm) → checks(auto 门·tool) → finalize(human 定稿)
DAG = [
    {"id": "seed", "label": "素材", "executor": "tool", "role": "produce", "deps": [],
     "deliverable": {"region": "whole"}},
    {"id": "draft", "label": "起草", "executor": "llm", "role": "produce", "deps": ["seed"],
     "prompt": "写一稿", "model_role": "writer", "deliverable": {"region": "whole"}},
    {"id": "checks", "label": "格式检查", "executor": "tool", "role": "gate", "deps": ["draft"],
     "approval_policy": "auto"},
    {"id": "finalize", "label": "定稿", "executor": "human", "role": "produce", "deps": ["checks"],
     "assignee_role": "负责人", "signal": "task_complete", "deliverable": {"region": "whole"}},
]


def run(checks_verdicts: list[bool]):
    """跑一遍图；checks 门按给定序列逐次放行 / 打回。返回 (state, 计数, io)。"""
    calls = {"checks": 0, "seed": 0}

    def seed(node, state, ex):
        calls["seed"] += 1
        return {"ok": True, "content": "素材正文"}

    def checks(node, state, ex):
        i = calls["checks"]
        calls["checks"] += 1
        return {"passed": checks_verdicts[i], "reason": "格式"}

    llm = StubLLM(completion="起草正文")
    io = MockLarkIO()
    ex = Executors(io=io, resolver=RoleResolver(), llm=llm,
                   deliverables=FakeDeliverableStore(),
                   tool_handlers={"seed": seed, "checks": checks})
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    graph = build_graph(ex, saver)
    cfg = {"configurable": {"thread_id": "auto-1"}, "recursion_limit": 50}
    graph.invoke({"dag": DAG, "status": {}, "outputs": {}, "meta": {"instance_id": "auto-1"}},
                 cfg, durability="sync")
    return graph.get_state(cfg), calls, io, llm


def test_template_is_valid():
    validate_template(DAG)
    assert_v1_supported(DAG)


def test_auto_gate_passes_without_touching_a_human():
    st, calls, io, _ = run([True])

    assert st.values["status"]["checks"] == "done"
    assert calls["checks"] == 1
    assert io.cards == {} and io.tasks == {}          # 自动门不发卡、不派单
    assert [i.value["node_id"] for i in st.interrupts] == ["finalize"]  # 只挂在人定稿


def test_auto_gate_reopens_upstream_and_reruns_it():
    """auto 打回 = 自动重算上游（v1.0 win 里「机检不过自动回去重写」那条）。"""
    st, calls, io, llm = run([False, True])

    assert calls["checks"] == 2
    assert len(llm.calls) == 2                        # draft 被重算一次
    assert calls["seed"] == 1                         # 旁支（未被打回的上游）不重跑
    assert st.values["status"]["checks"] == "done"


def test_cosign_policies_are_rejected_at_assembly_not_silently_degraded():
    for policy in ("any", "all", {"threshold": "反对 > 1/3"}):
        dag = [dict(n) for n in DAG]
        dag[2] = {**dag[2], "executor": "human", "approval_policy": policy,
                  "assignee_role": "法务", "signal": "card_action"}
        validate_template(dag)                        # schema 层放行（生成器照同一契约产图）
        with pytest.raises(UnsupportedInV1, match="v1.3"):
            assert_v1_supported(dag)                  # 运行前挡下，不按 single 静默降级


def test_v1_unsupported_features_are_listed_explicitly():
    with pytest.raises(UnsupportedInV1, match="vote"):
        assert_v1_supported([{**DAG[3], "vote": {"voters": ["a"], "primary": "a"}}])
    with pytest.raises(UnsupportedInV1, match="when"):
        assert_v1_supported([{**DAG[3], "when": {"draft": "A"}}])
    with pytest.raises(UnsupportedInV1, match="section"):
        assert_v1_supported([{**DAG[3], "deliverable": {"region": {"section": "第三条"}}}])
