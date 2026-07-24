"""节点契约（数据）· v1 交付物流转。

一张模板 = 节点数组，节点恒为数据 dict（SPEC「模板节点契约」/ ADR-015）：
    id / label / executor(tool|llm|human) / role(produce|gate) / deps
    produce 专属: deliverable {container?, region}          # 交付物落点（ADR-016）
    llm    专属: prompt / model_role                        # 多角色路由（ADR-017）
    gate   专属: approval_policy(auto|single|any|all|{threshold})
    human  专属: assignee_role / signal(task_complete|card_action)
    多人节点  : vote {voters, primary, policy}              # runtime 落 v1.3（ADR-025）
    条件分支  : when {<决策节点 id>: 值}                     # runtime 落 v1.3（ADR-025）

两个正交维度（executor × role）自由组合，业务差异全下沉配置，引擎不为业务新增节点
类型。契约保持「数据」是关键：生成图（ADR-022）= 加 AI 作者 + 人审门，执行器一行不改。
故这里只做取值域定义与查询，不引入行为。

与 seg-1 旧契约的对应（SPEC「as-built vs v1 字段名」）：
    type → executor；旧 role（业务指派串）→ assignee_role；
    gate 字符串 + 静态 on_fail → role=="gate" + approval_policy + 运行时 reopen。
"""
from __future__ import annotations

EXECUTORS = ("tool", "llm", "human")
ROLES = ("produce", "gate")
SIGNALS = ("task_complete", "card_action")   # message 变体推迟（ADR-021）
APPROVAL_POLICIES = ("auto", "single", "any", "all")  # 另可 {"threshold": expr}（ADR-025）
REGIONS = ("whole",)                          # {"section": selector} 另判（v2，ADR-018）
LEGACY_FIELDS = ("type", "on_fail", "gate")   # seg-1 旧契约，留着必是没迁干净


def is_gate(node: dict) -> bool:
    """把关节点（放行或打回一组上游）。"""
    return node.get("role") == "gate"


def is_produce(node: dict) -> bool:
    """产出节点（往交付物上写）。"""
    return node.get("role") == "produce"


def is_auto_gate(node: dict) -> bool:
    """自动放行门（确定性机检，不挂人、不发卡；ADR-015 的 bypass）。"""
    return is_gate(node) and node.get("approval_policy") == "auto"


def node_by_id(dag: list[dict], nid: str) -> dict:
    for n in dag:
        if n["id"] == nid:
            return n
    raise KeyError(f"节点不存在: {nid}")


def deps_ancestors(dag: list[dict], nid: str) -> set[str]:
    """nid 的全部 deps 传递上游（祖先）。打回合法域 = 它（机制层，ADR-014）。"""
    deps = {n["id"]: list(n.get("deps", [])) for n in dag}
    seen: set[str] = set()
    stack = list(deps.get(nid, []))
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack.extend(deps.get(x, []))
    return seen
