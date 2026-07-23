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


class OrchestratorState(TypedDict):
    dag: list          # 模板节点数组（静态，seed 一次；随 checkpoint 持久）
    status: Annotated[dict, merge]   # node_id -> pending | running | done | failed
    outputs: Annotated[dict, merge]  # node_id -> 节点产出/交付物快照（scratch）
    meta: dict         # instance_id / reporter / bug / template_id 等
