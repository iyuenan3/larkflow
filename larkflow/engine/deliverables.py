"""produce 末步：把交付物物化到飞书，handle 登记进 outputs[node_id]（ADR-020）。

不变量（禁改）：
- `outputs[node_id]["deliverable"]` 是 handle 的**唯一权威登记表**（在 state 里 →
  仍 checkpointer 权威、飞书仍投影）。
- 首跑 create、重跑 overwrite（handle 不变）→ 打回后未重算的旁支跨 overwrite 仍读旧
  handle，这就是选择性重算「旁支复用旧产出」的实证基础（ADR-014 / ADR-016）。
- `deliverable.container` 只是活图 dag 里的**声明位**（写进既有容器时用），不回填、
  不当第二份权威：dag 不是 reducer channel，worker 并行写它会丢写。
"""
from __future__ import annotations

from ..io.deliverable import WHOLE, Deliverable, DeliverableIO

HANDLE_KEY = "deliverable"


def prior_handle(outputs: dict, node_id: str) -> Deliverable | None:
    """读权威登记表里该节点已登记的 handle（没有则 None）。"""
    reg = (outputs or {}).get(node_id) or {}
    raw = reg.get(HANDLE_KEY)
    return Deliverable.from_dict(raw) if raw else None


def declared_container(node: dict) -> Deliverable | None:
    """节点在活图里声明的既有容器（可选；无则首跑现建）。"""
    raw = (node.get(HANDLE_KEY) or {}).get("container")
    return Deliverable.from_dict(raw) if isinstance(raw, dict) else None


def materialize(io: DeliverableIO, node: dict, state: dict, *, content: str,
                title: str | None = None) -> dict:
    """物化交付物 → 返回该节点的产出（含 handle）。首跑 create、重跑 overwrite。"""
    node_id = node["id"]
    outputs = state.get("outputs") or {}
    meta = state.get("meta") or {}
    region = (node.get(HANDLE_KEY) or {}).get("region", WHOLE)

    handle = prior_handle(outputs, node_id) or declared_container(node)
    if handle is not None:
        handle = io.overwrite(handle, content=content)
    else:
        handle = io.create(
            title=title or node.get("label") or node_id,
            content=content,
            region=region,
            idem_key=f"{meta.get('instance_id', '')}:{node_id}:create",
        )
    return {"ok": True, HANDLE_KEY: handle.to_dict()}


def ensure_container(io: DeliverableIO, node: dict, state: dict, *, placeholder: str) -> dict:
    """备好交付物容器给人写：已登记 / 已声明则原样返回，**绝不覆盖**。

    human 节点 interrupt 之前的代码在 resume 时会重跑，覆盖会抹掉人写的内容。
    """
    node_id = node["id"]
    handle = prior_handle(state.get("outputs") or {}, node_id) or declared_container(node)
    if handle is None:
        meta = state.get("meta") or {}
        handle = io.create(
            title=node.get("label") or node_id,
            content=placeholder,
            region=(node.get(HANDLE_KEY) or {}).get("region", WHOLE),
            idem_key=f"{meta.get('instance_id', '')}:{node_id}:create",
        )
    return {HANDLE_KEY: handle.to_dict()}


def read_upstream(io: DeliverableIO, state: dict, node: dict) -> dict[str, str]:
    """下游消费：按 deps 从权威登记表取 handle 再 fetch 正文（ADR-016 consume）。

    未登记 handle 的上游（如纯 tool 记录节点）跳过，不报错。
    """
    outputs = state.get("outputs") or {}
    texts: dict[str, str] = {}
    for dep in node.get("deps", []):
        handle = prior_handle(outputs, dep)
        if handle is not None:
            texts[dep] = io.fetch(handle)
    return texts
