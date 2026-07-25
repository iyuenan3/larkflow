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
# 引擎给 human-produce 备好的空容器里放的占位标记。auto 机检门据此判「人还没真写」，
# 所以它必须是**引擎与能力库共用的一个常量**，不能各写各的字样（改了措辞就静默失效）。
PLACEHOLDER_MARK = "【待填写】"
# 出厂 prompt 教 AI 写的占位是「【待确认：…】」。机检必须认全套，否则一份满是占位的空壳稿
# 会大摇大摆通过最后一道自动门（实测）。新增占位写法时**同时**改这里与模板 prompt。
PLACEHOLDER_MARKS = (PLACEHOLDER_MARK, "【待确认")


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


def upstream_handles(state: dict, node: dict) -> dict[str, Deliverable]:
    """本节点该消费 / 该审的上游交付物 handle。

    **透过不产交付物的节点看上游**：gate 只把关、不产出，若不透传，往图里插一道复核门
    就会悄悄切断下游的数据流（受控活图下这是常态）。每条路径在遇到第一个已登记 handle
    的节点处停止，按 deps 顺序、去重。
    """
    outputs = state.get("outputs") or {}
    dag_nodes = {n["id"]: n for n in (state.get("dag") or [])}
    deps_of = {nid: list(n.get("deps", [])) for nid, n in dag_nodes.items()}
    found: dict[str, Deliverable] = {}
    seen: set[str] = set()
    queue = list(node.get("deps", []))
    while queue:
        dep = queue.pop(0)
        if dep in seen:
            continue
        seen.add(dep)
        handle = prior_handle(outputs, dep)
        if handle is not None:
            found[dep] = handle
        elif _produces_nothing(dag_nodes.get(dep)):
            # 只透过**本来就不产交付物**的节点（gate / 纯动作节点）。若是一个声明了落点却
            # 没产出的 produce，那是缺陷不是透传，绝不能悄悄替换成它祖父的正文。
            queue.extend(deps_of.get(dep, []))
    return found


def _produces_nothing(node: dict | None) -> bool:
    return node is not None and node.get("deliverable") is None


def read_upstream(io: DeliverableIO, state: dict, node: dict) -> dict[str, str]:
    """下游消费：按 deps 从权威登记表取 handle 再 fetch 正文（ADR-016 consume）。"""
    return {dep: io.fetch(handle) for dep, handle in upstream_handles(state, node).items()}


def upstream_links(state: dict, node: dict) -> list[dict]:
    """给人看的上游交付物链接（审核人得先能打开要审的那份东西）。"""
    labels = {n["id"]: n.get("label", n["id"]) for n in (state.get("dag") or [])}
    return [{"node_id": dep, "label": labels.get(dep, dep), "url": handle.url}
            for dep, handle in upstream_handles(state, node).items()]
