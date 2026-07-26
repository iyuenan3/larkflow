"""受控活图：运行中改图的合法变更（ADR-013）。

冻结线 = 执行前沿：done / running / failed 的节点冻结，**只有 pending 节点可增删改**。
「只改未来、不改历史」：已完成节点的产出冻在 checkpointer 里就是权威。

这里只做**纯粹的 ops 应用 + 冻结线校验**；图级不变量（仍是 DAG / deps 不悬挂 / 护栏）
交给 validate_template，运行时能力边界交给 assert_v1_supported，二者由驱动层在写回前串起来。

ops 报文（引擎侧最小形态；前端命令 schema + 乐观并发 + 鉴权见 SPEC 待填）：
    {"op": "add_node",    "node": {...v1 节点...}}
    {"op": "remove_node", "id": "x"}
    {"op": "update_node", "id": "x", "set": {"deps": [...], "prompt": "..."}}
"""
from __future__ import annotations

import copy

ADD, REMOVE, UPDATE = "add_node", "remove_node", "update_node"
OPS = (ADD, REMOVE, UPDATE)


class GraphEditError(ValueError):
    """改图命令不合法（越过冻结线 / 报文错）。"""


def apply_ops(dag: list[dict], status: dict, ops: list[dict]) -> list[dict]:
    """按 ops 产出新 dag（不改入参）。越过冻结线即拒。"""
    if not ops:
        raise GraphEditError("ops 为空")
    new = copy.deepcopy(dag)
    index = {n["id"]: n for n in new}

    for op in ops:
        kind = op.get("op")
        if kind == ADD:
            node = copy.deepcopy(op.get("node") or {})
            nid = node.get("id")
            if not nid:
                raise GraphEditError(f"add_node 缺 node.id: {op}")
            if nid in index:
                raise GraphEditError(f"add_node 的 id 已存在: {nid}")
            new.append(node)
            index[nid] = node

        elif kind == REMOVE:
            nid = _require_id(op, index)
            _assert_editable(nid, status, "删除")
            new = [n for n in new if n["id"] != nid]
            index.pop(nid)

        elif kind == UPDATE:
            nid = _require_id(op, index)
            _assert_editable(nid, status, "修改")
            changes = op.get("set") or {}
            if not isinstance(changes, dict) or not changes:
                raise GraphEditError(f"update_node 缺 set: {op}")
            if "id" in changes:
                raise GraphEditError("update_node 不得改 id（改 id = 删+增）")
            index[nid].update(copy.deepcopy(changes))

        else:
            raise GraphEditError(f"未知 op={kind}，须 ∈ {OPS}")

    return new


def _require_id(op: dict, index: dict) -> str:
    nid = op.get("id")
    if not nid:
        raise GraphEditError(f"{op.get('op')} 缺 id: {op}")
    if nid not in index:
        raise GraphEditError(f"{op.get('op')} 指向不存在的节点: {nid}")
    return nid


def _assert_editable(nid: str, status: dict, what: str) -> None:
    st = (status or {}).get(nid, "pending")
    if st != "pending":
        raise GraphEditError(
            f"只改未来不改历史：{nid} 状态 {st}，已过冻结线，不可{what}（受控活图只动 pending 子图）"
        )
