"""编排器 state（禁改项：只放执行游标 + in-flight scratch，业务真相源 = checkpointer）。

status / outputs 用 Annotated reducer：一个 super-step 里 dispatch 用 Send 扇出多个
worker 并发写，无 reducer 会 InvalidUpdateError（LangGraph 硬约束）。

不变量（修 A 后成立）：每个 worker 只写**自己**节点的 status/outputs 键，故并行扇出下
各写不相交，merge 对不相交键可交换、无丢写。唯一会写「他人」键的是回边重置，但它由
dispatch 单点执行（reopen_resets），dispatch 不扇出、无并发写者，因此不存在「回边
pending 被兄弟 done 覆盖」的竞争。
"""
from __future__ import annotations

from typing import Annotated, TypedDict


def merge(a: dict, b: dict) -> dict:
    """两个 dict 浅合并（后者覆盖）。键互不相交时可交换。"""
    return {**(a or {}), **(b or {})}


def add_counts(a: dict, b: dict) -> dict:
    """按键累加。只有 dispatch（单写者）写它，故不存在并发累加竞争。"""
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = out.get(k, 0) + v
    return out


def extend_lists(a: dict, b: dict) -> dict:
    """按键追加列表。**只追加、绝不覆盖**（审计记录是历史，只改未来不改历史）。

    只有 service.unblock（人显式触发，持实例锁）写它，无并发追加竞争；
    与 add_counts 同理，保值写回绝不能带它，否则每推进一拍就重复追加一条假记录。
    """
    out = {k: list(v) for k, v in (a or {}).items()}
    for k, v in (b or {}).items():
        out.setdefault(k, []).extend(v)
    return out


class OrchestratorState(TypedDict):
    dag: list          # 节点数组（受控活图会改它；随 checkpoint 持久）
    status: Annotated[dict, merge]   # node_id -> pending | done | failed | blocked | skipped
    outputs: Annotated[dict, merge]  # node_id -> 节点产出 + 交付物 handle 权威登记（ADR-020）
    reopen_counts: Annotated[dict, add_counts]  # gate_id -> 已打回次数（预算，防无限重算）
    attempts: Annotated[dict, add_counts]       # node_id -> 第几轮（派单幂等键的一部分）
    unblocks: Annotated[dict, extend_lists]     # node_id -> 人解除 blocked 的审计记录（追加）
    escalations: Annotated[dict, extend_lists]  # gate_id -> 跨界打回的审批申请 + 裁决（ADR-023 ③，追加）
    edits: Annotated[dict, extend_lists]        # "log" -> 受控活图的改图审计（ADR-013，追加）
    meta: dict         # instance_id / reporter / inputs / template_id 等
