"""打回权限层（ADR-023）：纯函数、无副作用、可穷举测。

打回 = **机制层 ∩ 权限层**。机制层在 `gates.py`（回得回去吗：目标须 ⊆ gate 的传递祖先，
ADR-014）；这里管另一半：**你有资格让谁返工吗**。只有机制层的话，「防踢皮球」就是一句
空话，任何参与人都能把任意合法祖先踢回去让别人重做。

另有一把更基础的尺：**应答权**（`can_answer`）。打回与放行是同一张卡上的两个按钮，
只判打回那半边，就成了「让人返工要过三条规则、让交付物生效零校验」。谁点得动一个
节点的「通过 / 完成」，同样在这里算，同样不信前端（红线⑤）。

三条规则：
  ① **项目 owner** 可打回本项目任一祖先节点。
  ② **参与人**（人工节点 H 的主负责人）可打回 N，当且仅当 N ∈ 传递祖先(H)，且重算集
     （N ∪ N 的传递下游）里不牵连任何**别人的**人工节点（豁免见 `collateral_humans`）。
  ③ 其余的**跨界打回走 escalation**：审批人 = 项目 owner + 目标节点主负责人，任一方同意
     即执行（v1 只做申请 + 通知，一键同意等接真 dev app）。

## 身份的货币单位 = 令牌集合

`actor_roles` / `owner_roles` 都是**不透明字符串的集合**。令牌可以是模板里的
`assignee_role`（中文角色名），也可以是飞书 open_id，本模块一律不解释、只做集合运算。
这么定有两个硬理由：

  · 图里唯一能表达身份的东西是 `assignee_role`，「打回会连累谁」只能按角色算；
  · 事件给的却是 `operator_id`（open_id），而且一个人可能担多个角色，反向解析是**一对多**。

把「open_id → 他担的角色集合」这一步留在驱动层做一次（`RoleResolver.roles_of`），纯函数
这边就不必认识飞书、也不必造 open_id 才测得动，权限判定于是可以穷举。

`owner_roles` = 「持有即拥有 owner 全域打回权」的令牌集合。驱动层 v1 填 `{meta.reporter}`
（一个 open_id，**不是**角色，别混）。留成集合是给将来的联合发起人 / owner 角色留位。
"""
from __future__ import annotations

from ..model.node import deps_ancestors, node_by_id
from .gates import stale_downstream

# 同一道门最多能挂几笔待批的跨界打回申请。没有上界的话，手里有卡的人换一组目标就能再发一笔，
# 目标组有 2^|祖先| 种，于是申请队列与发起人的通知都能被刷爆，权威 state 还跟着无限长大。
MAX_PENDING_ESCALATIONS = 5


def is_human(node: dict) -> bool:
    """人工节点。**gate 也算**：人工审核门同样是「一个人的活」，漏掉它就漏掉了半张图。"""
    return (node or {}).get("executor") == "human"


def primary_owner(node: dict) -> str | None:
    """人工节点的**主负责人**（手动打回权的主体，ADR-023 ④）。

    多人节点取 `vote.primary`（v1.3 runtime，schema 层 v1 就认，故这里先认下来）；
    单人节点就是 `assignee_role`。非人工节点没有负责人。
    """
    if not is_human(node):
        return None
    vote = node.get("vote") or {}
    return vote.get("primary") or node.get("assignee_role")


def _node_or_none(dag: list[dict], nid) -> dict | None:
    try:
        return node_by_id(dag or [], nid)
    except (KeyError, TypeError):
        return None


def answerers(node: dict) -> set[str]:
    """这个人工节点的**应答人**令牌集合：谁点得动它的「通过 / 打回 / 完成」。

    单人节点 = `assignee_role`；多人节点 = 全体 `vote.voters`（含主负责人）。应答权比
    手动打回权宽：投票门里每个人都要投票，但只有主负责人能替这道门拍「打回谁」
    （ADR-023 ④，见 `primary_owner`）。非人工节点没有应答人。
    """
    if not is_human(node):
        return set()
    vote = node.get("vote") or {}
    out = {t for t in vote.get("voters") or () if t}
    for t in (vote.get("primary"), node.get("assignee_role")):
        if t:
            out.add(t)
    return out


def can_answer(dag: list[dict], *, actor_roles, node_id) -> bool:
    """actor 有没有资格替这个节点作答（放行 / 定稿完成都要过这一关）。

    为什么放行也必须在引擎侧判：真栈里卡片会被转发（法务把审核卡丢给助理），
    `assignee_role` 解析成群 `oc_` 时整个群都点得到，封套本身还是前端可自由构造的攻击面
    （红线⑤）。「这张卡发给了谁」不是判据，「他是不是这个节点的应答人」才是。

    这里**不认 owner**：打回是调度（owner 全域，规则 ①），放行是代签，谁的活谁签。
    owner 想跳过一道门有留痕的正路：受控活图改 / 删这个节点（ADR-013）。
    """
    who = answerers(_node_or_none(dag, node_id) or {})
    return bool(who) and bool(who & set(actor_roles or ()))


def recompute_set(dag: list[dict], target: str) -> set[str]:
    """打回 target 会让谁返工 = target ∪ 它的传递下游（与 gates.reopen_resets 同一口径）。"""
    return {target} | stale_downstream(dag or [], target)


def has_standing(dag: list[dict], from_node, actor_roles) -> bool:
    """actor 是不是「站在 from_node 上」的参与人（= 这道门的主负责人）。

    不是的话他既不能直接打回、也没有申请权：那不是「跨界」，是「没资格」。真栈里卡片只投给
    负责人，但卡片可被转发、封套可被伪造，所以这一判必须在引擎侧做（红线：不信前端）。
    """
    owner = primary_owner(_node_or_none(dag, from_node))
    return bool(owner) and owner in set(actor_roles or ())


def collateral_humans(dag: list[dict], *, actor_roles, from_node, target) -> list[str]:
    """打回 target 会连累到的**别人的**人工节点（空 = 不算踢皮球）。

    四类豁免，每一类都对应一个「不豁免就把产品做死」的日常场景：
      · **target 自己**：打回的对象本来就是被点名返工的那一个。串行图由此精确退化成
        ADR-023 括号里那句「最多回到上一个人工节点」（再往上一格就会把它拖下水，不豁免）。
      · **H = from_node**：他自己判的打回，他自己重来天经地义。
      · **H 的传递下游**：H 只要打回任何东西，自己必被重置、下游必然跟着重来，
        这是他行使打回权的固有代价，不算额外连累。
      · **actor 自己担的人工节点**：单人项目（所有人工节点同一个人）与一人多角色不得被
        误伤，他连累的是他自己。
    """
    actor = set(actor_roles or ())
    exempt = {target, from_node} | stale_downstream(dag or [], from_node)
    out = []
    for nid in recompute_set(dag, target):
        if nid in exempt:
            continue
        node = _node_or_none(dag, nid)
        if not is_human(node):
            continue
        if primary_owner(node) in actor:      # 他自己的活，不算被别人踢皮球
            continue
        out.append(nid)
    return sorted(out)


def approvers_for(dag: list[dict], *, owner_roles, target) -> list[str]:
    """跨界打回的审批人 = 项目 owner + 目标节点主负责人（多人节点取主负责人，ADR-023 ③）。

    返回的仍是**令牌**：owner 令牌通常是 open_id，负责人令牌是角色名。驱动层负责把角色
    过 RoleResolver 解析成 open_id 再发通知（不解析的话真栈会把中文名当 open_id 发出去）。
    """
    out = set(owner_roles or ())
    who = primary_owner(_node_or_none(dag, target))
    if who:
        out.add(who)
    return sorted(out)


def allowed_reopen(dag: list[dict], *, actor_roles, owner_roles, from_node) -> list[str]:
    """actor 站在 from_node 上，**无需任何审批**就能直接打回的目标（已 ∩ 机制层合法域）。

    候选集 = 机制层的 `deps_ancestors(from_node)`，与 `gates.reopen_candidates` 同一口径。
    审核卡 / 画布据此过滤，别给人一个点了必被拒的目标。
    """
    actor, owner = set(actor_roles or ()), set(owner_roles or ())
    candidates = sorted(deps_ancestors(dag or [], from_node))
    if actor & owner:                                   # ① owner 全域（与参与人身份取并集）
        return candidates
    if not has_standing(dag, from_node, actor):         # 陌生人：零权限
        return []
    return [t for t in candidates
            if not collateral_humans(dag, actor_roles=actor, from_node=from_node, target=t)]


def reopen_verdict(dag: list[dict], *, actor_roles, owner_roles, from_node, targets) -> dict:
    """对一组打回目标逐个裁定。

    返回 `{"allowed": [...], "needs_escalation": [{target, approvers, collateral}], "denied": [...]}`：
      · `denied`           机制层就不合法（非传递祖先），或 actor 根本没资格站在这道门上。
                           这两类都**不给** escalation：前者回不去，后者没有申请权。
      · `needs_escalation` 机制合法、actor 有资格，但会连累别人 → 走 ADR-023 ③。
      · `allowed`          可以当场执行。
    保持调用方给的顺序（前端展示顺序稳定），重复目标去重。
    """
    actor, owner = set(actor_roles or ()), set(owner_roles or ())
    legal = deps_ancestors(dag or [], from_node)
    direct = set(allowed_reopen(dag, actor_roles=actor, owner_roles=owner, from_node=from_node))
    standing = bool(actor & owner) or has_standing(dag, from_node, actor)

    out: dict = {"allowed": [], "needs_escalation": [], "denied": []}
    seen: set[str] = set()
    for t in targets or []:
        if t in seen:
            continue
        seen.add(t)
        if t not in legal or not standing:
            out["denied"].append(t)
        elif t in direct:
            out["allowed"].append(t)
        else:
            out["needs_escalation"].append({
                "target": t,
                "approvers": approvers_for(dag, owner_roles=owner, target=t),
                "collateral": collateral_humans(dag, actor_roles=actor,
                                                from_node=from_node, target=t),
            })
    return out
