"""就绪判定 + 门禁 + 回边（纯函数，无副作用，可单测）。"""
from __future__ import annotations

from ..model.node import has_gate, node_by_id


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
    """带 gate 节点是否达标。约定：结果里的 passed 为真则达标。

    human 门禁节点：passed 来自人的裁决（卡片 通过/打回）。
    tool 门禁节点（如第二段 ci_test）：passed 来自工具结果。
    无 gate 节点不走这里。
    """
    return bool((result or {}).get("passed", False))


def stale_downstream(dag: list[dict], target: str) -> set[str]:
    """target 的全部传递下游（reverse-reachability）。

    门禁回边时，target 及其已跑过的下游都要重置为 pending，
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


def finish(dag: list[dict], nid: str, result: dict) -> dict:
    """节点收尾 → 返回 status/outputs 增量（合并回主 state）。

    达标 / 无门禁 → 该节点 done。
    门禁不达标 → 只把**自己**标 failed。

    关键（修 A）：worker 只写自己的 status 键。并行扇出下各 worker 写不相交键，
    merge reducer 保持可交换、无丢写。真正的回边（重置 on_fail + 其下游）由 dispatch
    单点做（见 reopen_resets），dispatch 不扇出、无并发写者，杜绝「回边 pending 被兄弟
    done 覆盖」的静默竞争。
    """
    node = node_by_id(dag, nid)
    result = result or {}
    if has_gate(node) and not gate_passes(node, result):
        return {"outputs": {nid: result}, "status": {nid: "failed"}}
    return {"outputs": {nid: result}, "status": {nid: "done"}}


def reopen_resets(dag: list[dict], status: dict) -> dict:
    """回边落地（dispatch 单点执行）：把每个 failed 门禁节点的 on_fail + 其传递下游
    + 门禁节点自身重置 pending。单写者，无并发竞争。

    返回 status 增量 dict（node_id -> "pending"）；无 failed 节点则空。
    """
    resets: dict = {}
    for n in dag:
        if status.get(n["id"]) == "failed":
            target = n["on_fail"]
            for m in {target} | stale_downstream(dag, target) | {n["id"]}:
                resets[m] = "pending"
    return resets
