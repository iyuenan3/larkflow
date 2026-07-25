"""固定编排器图（Pregel 有环）：一张图解释数据驱动的领域 DAG。

禁改项：不 per-instance 现编译新图。编一次，模板当 state 数据 seed 进去。
形状（langgraph 1.2.9 核实）：
    START -> dispatch
    dispatch --conditional--> [Send(<type>_worker, payload) ...] 或 END
    <type>_worker -> dispatch          # 唯一真环边

对抗复核纠正的关键点：Send(node, payload) 把 payload 当作该 worker 的完整输入
state（不并入主 channel、不进 kwargs）。故 worker 从 payload 里读 node_id / dag，
需要的上下文全部打进 payload；返回值经 reducer 合并回主 state。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from ..model.node import is_gate, node_by_id
from .deliverables import upstream_links
from .executors import Executors
from .gates import (
    attempt_increments,
    finish,
    ready_nodes,
    reopen_candidates,
    reopen_feedback,
    reopen_increments,
    reopen_resets,
)
from .state import OrchestratorState

_WORKER = {"tool": "tool_worker", "llm": "llm_worker", "human": "human_worker"}


def build_graph(executors: Executors, checkpointer):
    """编译固定编排器图。executors 注入 tool/llm 行为；human 走 interrupt。"""

    def dispatch(state: OrchestratorState) -> dict:
        # 打回单点执行：把 failed gate 的 reopen 组 + 下游重置 pending（修 A：单写者无竞争）。
        # 随后 route 在重置后的 status 上算 ready。dispatch 不扇出，故此写无并发。
        status = state.get("status", {})
        resets = reopen_resets(state["dag"], status, state.get("outputs", {}),
                               state.get("reopen_counts", {}), state.get("unblocks", {}))
        if not resets:
            return {}
        return {"status": resets,
                "reopen_counts": reopen_increments(state["dag"], status, resets),
                "attempts": attempt_increments(resets)}

    def route(state: OrchestratorState):
        ready = ready_nodes(state["dag"], state.get("status", {}))
        if not ready:
            return END  # 无就绪节点（全 done 或阻塞）→ 收尾
        payload_base = {
            "dag": state["dag"],
            "meta": state.get("meta", {}),
            "outputs": state.get("outputs", {}),
        }
        return [
            Send(_WORKER[n["executor"]], {"node_id": n["id"], **payload_base})
            for n in ready
        ]

    def tool_worker(state: dict) -> dict:
        nid, dag = state["node_id"], state["dag"]
        node = node_by_id(dag, nid)
        return finish(dag, nid, executors.run_tool(node, state))

    def llm_worker(state: dict) -> dict:
        nid, dag = state["node_id"], state["dag"]
        node = node_by_id(dag, nid)
        return finish(dag, nid, executors.run_llm(node, state))

    def human_worker(state: dict) -> dict:
        nid, dag = state["node_id"], state["dag"]
        node = node_by_id(dag, nid)
        # produce 先备好交付物容器（人去飞书里写）；只创建不覆盖，resume 重跑安全。
        prepared = executors.prepare_human(node, state)
        handle = prepared.get("deliverable") or {}
        # 随后纯挂起：只把人要看的信息交出去；飞书任务/卡由驱动层在 __interrupt__ 后建。
        answer = interrupt(
            {
                "kind": "human_node",
                "node_id": nid,
                "label": node["label"],
                "role": node.get("role"),                    # produce | gate
                "assignee_role": node.get("assignee_role"),  # 派给谁
                "approval_policy": node.get("approval_policy"),
                "signal": node.get("signal"),
                "deliverable": handle or None,               # produce：人要写的那份交付物
                "deliverable_url": handle.get("url"),        # 对人 = 一条文档链接
                # gate 得先能打开「要审的那份东西」；produce 也常要参考上游
                "upstream": upstream_links(state, node),
                # 打回是运行时手选一组（ADR-014）：候选 = 机制合法域，默认 = 把关的直接上游
                "reopen_candidates": reopen_candidates(dag, nid) if is_gate(node) else None,
                "reopen_default": list(node.get("deps", [])) if is_gate(node) else None,
                # 被打回重做的人得知道「谁打回的、为什么」，否则重来一遍还是同一份东西
                "feedback": reopen_feedback(dag, state.get("outputs") or {}, nid),
            }
        )
        return finish(dag, nid, {**prepared, **(answer or {})})

    b = StateGraph(OrchestratorState)
    b.add_node("dispatch", dispatch)
    b.add_node("tool_worker", tool_worker)
    b.add_node("llm_worker", llm_worker)
    b.add_node("human_worker", human_worker)
    b.add_edge(START, "dispatch")
    b.add_conditional_edges("dispatch", route, ["tool_worker", "llm_worker", "human_worker", END])
    for w in ("tool_worker", "llm_worker", "human_worker"):
        b.add_edge(w, "dispatch")
    return b.compile(checkpointer=checkpointer)
