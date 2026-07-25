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


class OrchestratorState(TypedDict):
    dag: list          # 节点数组（受控活图会改它；随 checkpoint 持久）
    status: Annotated[dict, merge]   # node_id -> pending | done | failed | blocked | skipped
    outputs: Annotated[dict, merge]  # node_id -> 节点产出 + 交付物 handle 权威登记（ADR-020）
    reopen_counts: Annotated[dict, add_counts]  # gate_id -> 已打回次数（预算，防无限重算）
    meta: dict         # instance_id / reporter / inputs / template_id 等
