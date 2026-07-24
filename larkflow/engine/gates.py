"""就绪判定 + 门禁 + 打回（纯函数，无副作用，可单测）。"""
from __future__ import annotations

from ..model.node import is_gate, node_by_id


def ready_nodes(dag: list[dict], status: dict) -> list[dict]:
    """status==pending 且 deps 全 done 的节点。"""
    out = []
    for n in dag:
        if status.get(n["id"], "pending") != "pending":
            continue
        if all(status.get(d) == "done" for d in n.get("deps", [])):
            out.append(n)
    return out


def all_done(dag: list[dict], status: dict) -> bool:
    return all(status.get(n["id"]) == "done" for n in dag)


def gate_passes(node: dict, result: dict) -> bool:
    """gate 节点是否放行。约定：结果里的 passed 为真则放行。

    human gate：passed 来自人的裁决（卡片 通过 / 打回）。
    tool gate（approval_policy=auto，如格式检查）：passed 来自确定性机检结果。
    produce 节点不走这里。
    """
    return bool((result or {}).get("passed", False))


def stale_downstream(dag: list[dict], target: str) -> set[str]:
    """target 的全部传递下游（reverse-reachability）。

    打回时，target 及其已跑过的下游都要重置为 pending，
    否则 dispatch 把陈旧下游当 done、永不用修正后的上游产出重跑。
    """
    children: dict[str, list[str]] = {}
    for n in dag:
        for d in n.get("deps", []):
            children.setdefault(d, []).append(n["id"])
    out: set[str] = set()
    stack = list(children.get(target, []))
    while stack:
        x = stack.pop()
        if x in out:
            continue
        out.add(x)
        stack.extend(children.get(x, []))
    return out


def reopen_targets(node: dict, result: dict) -> list[str]:
    """gate 打回的目标组：运行时手选（ADR-014），缺省 = 它把关的直接上游。

    缺省来自 ADR-025「reopen 默认 = 把关的上游」；合法域校验（⊆ 传递祖先）在
    reopen_resets 里做，不信调用方。
    """
    picked = (result or {}).get("reopen")
    if picked:
        return list(picked)
    return list(node.get("deps", []))


def finish(dag: list[dict], nid: str, result: dict) -> dict:
    """节点收尾 → 返回 status/outputs 增量（合并回主 state）。

    放行 / 非 gate → 该节点 done。gate 不放行 → 只把**自己**标 failed。

    关键（修 A）：worker 只写自己的 status 键。并行扇出下各 worker 写不相交键，
    merge reducer 保持可交换、无丢写。真正的打回（重置 reopen 组 + 其下游）由 dispatch
    单点做（见 reopen_resets），dispatch 不扇出、无并发写者，杜绝「打回 pending 被兄弟
    done 覆盖」的静默竞争。
    """
    node = node_by_id(dag, nid)
    result = result or {}
    if is_gate(node) and not gate_passes(node, result):
        return {"outputs": {nid: result}, "status": {nid: "failed"}}
    return {"outputs": {nid: result}, "status": {nid: "done"}}


def reopen_resets(dag: list[dict], status: dict, outputs: dict | None = None) -> dict:
    """打回落地（dispatch 单点执行）：把每个 failed gate 的 reopen 组 + 其传递下游
    + gate 自身重置 pending（选择性重算，ADR-014）。单写者，无并发竞争。

    gate 自身必被重置，且 reopen 目标是它的祖先，故重置集必然覆盖 gate → 结构性终止。
    返回 status 增量 dict（node_id -> "pending"）；无 failed 节点则空。
    """
    outputs = outputs or {}
    resets: dict = {}
    for n in dag:
        if status.get(n["id"]) != "failed":
            continue
        for target in reopen_targets(n, outputs.get(n["id"])):
            for m in {target} | stale_downstream(dag, target) | {n["id"]}:
                resets[m] = "pending"
    return resets
