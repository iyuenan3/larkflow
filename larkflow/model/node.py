"""节点契约（数据）。

一张模板 = 节点数组，节点恒为数据 dict：
    id / label / type(tool|llm|human) / role / gate / deps
本段补白（SPEC 已写「回边到指定上游」，这里给它字段名）：
    on_fail: 带 gate 节点的回边目标节点 id
    signal:  human 节点完成信号（task_complete | card_action）

契约保持「数据」是关键：路线 2（AI 生成图）= 加 AI 作者节点 + 人审门，
执行器一行不改（ADR-003 / ADR-010）。故这里只做校验，不引入行为。
"""
from __future__ import annotations

NODE_TYPES = ("tool", "llm", "human")
SIGNALS = ("task_complete", "card_action")
NO_GATE = ("-", "", None)


def has_gate(node: dict) -> bool:
    return node.get("gate") not in NO_GATE


def node_by_id(dag: list[dict], nid: str) -> dict:
    for n in dag:
        if n["id"] == nid:
            return n
    raise KeyError(f"节点不存在: {nid}")
