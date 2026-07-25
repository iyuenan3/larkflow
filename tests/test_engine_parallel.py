"""并行扇出 + 打回竞争回归测试（修 A）。

钻石 DAG：T →(A, G)，G 是 auto 门（tool 机检，打回目标缺省 = 其直接上游 T），H join。
G 第一次不放行触发打回：T 重跑（gen 变），兄弟 A 必须也重跑、看到 T 的新产出。
旧实现（worker 写全下游 status + last-write-wins reducer）在节点顺序 [T,A,G,H] 下
会让 A 的 done 覆盖打回的 pending → A 不重跑、用陈旧 T 产出（静默损坏）。
修 A 后：worker 只写自己键、打回由 dispatch 单点做，A 必重跑。断言 A 两次看到的
generation = [1, 2]。
"""
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from larkflow.config import RoleResolver
from larkflow.engine import Executors, build_graph
from larkflow.io import FakeDeliverableStore, MockLarkIO
from larkflow.model.template import validate_template

# 节点顺序刻意 [T,A,G,H]：这正是旧实现让 A 陈旧 done 获胜的顺序（回归守卫）
DIAMOND = [
    {"id": "T", "label": "上游", "executor": "tool", "role": "produce", "deps": [],
     "deliverable": {"region": "whole"}},
    {"id": "A", "label": "兄弟", "executor": "llm", "role": "produce", "deps": ["T"],
     "prompt": "读上游写一段", "model_role": "writer", "deliverable": {"region": "whole"}},
    {"id": "G", "label": "机检门", "executor": "tool", "role": "gate", "deps": ["T"],
     "approval_policy": "auto"},
    {"id": "H", "label": "汇合", "executor": "human", "role": "produce", "deps": ["A", "G"],
     "assignee_role": "QA", "signal": "task_complete", "deliverable": {"region": "whole"}},
]


def test_diamond_reopen_reruns_sibling_against_fresh_upstream():
    validate_template(DIAMOND)  # 合法（三型齐全 / G 有可回退祖先 T / auto 门是 tool）

    runs = {"T": 0, "A_saw": [], "G_attempts": 0}

    def t_tool(node, state, ex):
        runs["T"] += 1
        return {"ok": True, "gen": runs["T"], "content": f"上游正文 v{runs['T']}"}

    def a_llm(node, state, ex):
        gen = (state["outputs"].get("T") or {}).get("gen")
        runs["A_saw"].append(gen)
        return {"ok": True, "saw_gen": gen, "content": f"兄弟读到 gen={gen}"}

    def g_gate(node, state, ex):
        runs["G_attempts"] += 1
        return {"passed": runs["G_attempts"] >= 2}  # 第一次失败，第二次通过

    ex = Executors(
        io=MockLarkIO(), resolver=RoleResolver(), deliverables=FakeDeliverableStore(),
        tool_handlers={"T": t_tool, "G": g_gate}, llm_handlers={"A": a_llm},
    )
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn); saver.setup()
    graph = build_graph(ex, saver)

    cfg = {"configurable": {"thread_id": "diamond-1"}, "recursion_limit": 50}
    graph.invoke({"dag": DIAMOND, "status": {}, "outputs": {}, "meta": {}}, cfg, durability="sync")

    # T 重跑一次(gen 2)，A 必须两次都跑、第二次看到新 gen
    assert runs["T"] == 2, runs
    assert runs["G_attempts"] == 2, runs
    assert runs["A_saw"] == [1, 2], runs["A_saw"]  # 关键：A 未被陈旧 done 卡住，重跑看到 gen 2

    st = graph.get_state(cfg)
    status = st.values["status"]
    assert status["T"] == "done" and status["A"] == "done" and status["G"] == "done"
    # H(human) 挂起等人
    assert [i.value["node_id"] for i in st.interrupts] == ["H"]
