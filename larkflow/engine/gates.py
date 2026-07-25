"""就绪判定 + 门禁 + 打回（纯函数，无副作用，可单测）。"""
from __future__ import annotations

from ..model.node import deps_ancestors, is_gate, node_by_id


BLOCKED = "blocked"          # 终态：反复打回未见好转，等人介入（改图 / 改要素 / 手动解除）
DEFAULT_REOPEN_BUDGET = 3

# 人显式解除 blocked（service.unblock）时**追加**预算。两层上界都不能少：
#   MAX_UNBLOCK_GRANTS      同一节点最多被解除几次（人可以点几次）
#   MAX_GRANT_PER_UNBLOCK   单次最多追加多少预算（防一次 grant=10**9 把预算机制原地废掉）
# 少任何一层，「有限额度」都退化成「无限重算」，ADR-029 白做。
MAX_UNBLOCK_GRANTS = 3
MAX_GRANT_PER_UNBLOCK = DEFAULT_REOPEN_BUDGET


class ReopenError(RuntimeError):
    """打回目标越出合法域（机制层：每个目标须 ⊆ gate 的 deps 传递祖先，ADR-014）。"""


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

    缺省来自 ADR-025「reopen 默认 = 把关的上游」；合法域校验（⊆ 传递祖先）由
    illegal_reopen / reopen_resets 做，不信调用方。
    """
    picked = (result or {}).get("reopen")
    if picked:
        return list(picked)
    return list(node.get("deps", []))


def reopen_feedback(dag: list[dict], outputs: dict, node_id: str) -> list[dict]:
    """把「谁把我打回的、说了什么」查出来（纯函数）。

    重算的输入必须带上它：否则 llm 节点用同一份 prompt 重跑，真 LLM（temperature=0）会
    一字不差地再生成同一份稿；人节点也只会收到与上次逐字相同的派单卡。打回等于空转。
    """
    outputs = outputs or {}
    out = []
    for n in dag:
        if not is_gate(n):
            continue
        result = outputs.get(n["id"]) or {}
        if result.get("passed", True):
            continue
        if node_id in reopen_targets(n, result):
            out.append({"from": n["id"], "label": n.get("label", n["id"]),
                        "comment": result.get("comment"), "by": result.get("by")})
    return out


def reopen_candidates(dag: list[dict], gate_id: str) -> list[str]:
    """机制层合法的打回候选 = gate 的 deps 传递祖先（权限层过滤见 ADR-023）。"""
    return sorted(deps_ancestors(dag, gate_id))


def illegal_reopen(dag: list[dict], gate_id: str, targets) -> list[str]:
    """越出合法域的目标（空表示合法）。打回目标须是 gate 的传递祖先：

    ① 语义上「打回」只能回到把关范围内的上游；
    ② 结构上目标是祖先 ⇒ gate ∈ 目标的传递下游 ⇒ gate 必被重置 ⇒ 打回环必终止。
    """
    ancestors = deps_ancestors(dag, gate_id)
    return [t for t in (targets or []) if t not in ancestors]


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


def reopen_budget(node: dict) -> int:
    """一道门最多能把同一段活打回几次（可按节点配 `reopen_budget`）。

    没有预算的话，auto 机检门 + 产不出合格内容的上游 = 无限重算：单次 invoke 里
    super-step 一路涨到 recursion_limit 才崩，实例停在半截、谁也不知道发生了什么。
    这是通用产品的常态而非异常（AI 未必满足得了机检）。
    """
    raw = node.get("reopen_budget", DEFAULT_REOPEN_BUDGET)
    return max(1, int(raw))


def grants_used(grants: dict | None, nid: str) -> int:
    """这道门被人显式解除过几次（unblock 审计记录条数）。"""
    return len((grants or {}).get(nid) or [])


def granted_budget(grants: dict | None, nid: str) -> int:
    """人给这道门追加过多少打回预算（历次 unblock 之和）。"""
    return sum(int(r.get("grant", 0)) for r in (grants or {}).get(nid) or [])


def effective_reopen_budget(node: dict, grants: dict | None = None) -> int:
    """真实预算 = 节点配置 + 人追加的额度。

    追加是**加法**不是重置：`reopen_counts` 只增不减（历史不改），是预算被抬高了才让
    这道门又跑得动，故「一共重算过多少次」在审计里始终是真的。
    """
    return reopen_budget(node) + granted_budget(grants, node["id"])


def clamp_grant(grant) -> int:
    """单次追加额度收进 [1, MAX_GRANT_PER_UNBLOCK]。"""
    try:
        n = int(grant)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(n, MAX_GRANT_PER_UNBLOCK))


def unblock_resets(dag: list[dict], node_id: str, targets=None) -> dict:
    """人显式解除 blocked：把这道门放回执行前沿，可选连带解冻一组祖先（纯函数）。

    形状与 reopen_resets 一致（门自身 + 每个目标及其传递下游 → pending），差别只在
    触发者：这里是人显式点的，引擎绝不自动做（自动解除 = 把 ADR-029 消灭的无限重算放回来）。
    目标合法域（⊆ 传递祖先）由调用方先过 illegal_reopen，不信调用方给的值。
    """
    resets = {node_id: "pending"}
    for t in targets or []:
        for m in {t} | stale_downstream(dag, t) | {node_id}:
            resets[m] = "pending"
    return resets


def reopen_resets(dag: list[dict], status: dict, outputs: dict | None = None,
                  counts: dict | None = None, grants: dict | None = None) -> dict:
    """打回落地（dispatch 单点执行）：把每个 failed gate 的 reopen 组 + 其传递下游
    + gate 自身重置 pending（选择性重算，ADR-014）。单写者，无并发竞争。

    gate 自身必被重置，且 reopen 目标是它的祖先，故重置集必然覆盖 gate → 结构性终止。
    超出打回预算的门标 `blocked`（终态，需人介入）而不是继续重算。
    返回 status 增量 dict；无 failed 节点则空。
    """
    outputs, counts = outputs or {}, counts or {}
    resets: dict = {}
    for n in dag:
        nid = n["id"]
        if status.get(nid) != "failed":
            continue
        if counts.get(nid, 0) >= effective_reopen_budget(n, grants):
            resets[nid] = BLOCKED      # 反复打回不见好转：停下来叫人，别空转到崩
            continue
        targets = reopen_targets(n, outputs.get(nid))
        bad = set(illegal_reopen(dag, nid, targets))
        if bad:
            # 入口（service.resume）用引擎侧身份挡一道；到这里说明 state 里已有非法值。
            # **不能抛**：抛出去会让此后每一次推进都在同一处炸，实例永久砖化、pending() 还谎报
            # 无人等待（实测）。降级为「剔除非法目标」，全非法就把这道门标 blocked 叫人。
            targets = [t for t in targets if t not in bad]
            if not targets:
                resets[nid] = BLOCKED
                continue
        for target in targets:
            for m in {target} | stale_downstream(dag, target) | {nid}:
                resets[m] = "pending"
    return resets


def reopen_increments(dag: list[dict], status: dict, resets: dict) -> dict:
    """本轮真正执行了打回的门 → 计数 +1（与 resets 同一次 dispatch 写回）。"""
    return {n["id"]: 1 for n in dag
            if status.get(n["id"]) == "failed" and resets.get(n["id"]) == "pending"}


def attempt_increments(resets: dict) -> dict:
    """被重置为 pending 的节点 = 进入新一轮 → 轮次 +1。

    轮次是**派单幂等键的一部分**：同一轮无论推进多少拍、中断 id 换多少次，人手里都只有一张卡；
    真被打回了才发新卡。用中断 id 当幂等键会随每一拍 churn，导致无上限重复派单（实测）。
    """
    return {k: 1 for k, v in resets.items() if v == "pending"}


def total_reopen_budget(dag: list[dict], grants: dict | None = None) -> int:
    """全图打回预算上界（含人追加的额度）。递归 / 推进预算据它现算，别把追加额度漏掉。"""
    return sum(effective_reopen_budget(n, grants) for n in dag if is_gate(n))


def blocked_nodes(dag: list[dict], status: dict) -> list[str]:
    return [n["id"] for n in dag if status.get(n["id"]) == BLOCKED]
