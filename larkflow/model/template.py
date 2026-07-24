"""模板加载 + 校验：YAML → dag(list[dict])。

校验对策展模板与未来生成图共用同一把尺（ADR-010 / ADR-022），落地 CONVENTIONS
「模板生成护栏」①..⑤ + v1 字段级护栏：

  护栏①  三型齐全（tool / llm / human 各有落点）
  护栏②  每道 gate 须有可回退的传递祖先（否则打回无处可回）
  护栏③  放行 / 裁决落到人：approval_policy=auto 的 gate 只能是 tool（确定性机检），
          其余 policy 的 gate 只能是 human；**llm 绝不自动放行**
  护栏④  human 节点 ≥1 负责人；多人节点（vote）须 1 主负责人 ∈ voters
  护栏⑤  条件分支守卫 when 须引用本节点的传递祖先决策节点（否则永不可求值）
  字段级 produce 须 deliverable（且 gate 不得有）/ llm 须 prompt + model_role /
          gate 须 approval_policy / human 须 signal
  结构级 id 不重复、deps 不悬挂、deps 无环（回边是运行时语义，不进 deps）

不做行为、不碰运行时：打回目标合法域（reopen ⊆ 传递祖先）在**运行时**校验（ADR-014）。
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from .node import (
    APPROVAL_POLICIES,
    EXECUTORS,
    LEGACY_FIELDS,
    REGIONS,
    ROLES,
    SIGNALS,
    deps_ancestors,
    is_gate,
    is_produce,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class TemplateError(ValueError):
    """模板不合法（结构 / 护栏）。"""


def load_template(name_or_path: str) -> list[dict]:
    """按名字（templates/<name>.yaml）或路径加载模板，返回校验过的 dag。

    返回深拷贝：dag 会被 seed 进 state 且受控活图会改它，调用方之间不得共享可变对象。
    """
    path = Path(name_or_path)
    if not path.exists():
        path = TEMPLATES_DIR / f"{name_or_path}.yaml"
    if not path.exists():
        raise TemplateError(f"模板文件不存在: {name_or_path}")
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or "nodes" not in spec:
        raise TemplateError(f"模板缺少 nodes: {path}")
    dag = copy.deepcopy(spec["nodes"])
    validate_template(dag)
    return dag


def validate_template(dag: list[dict]) -> None:
    if not isinstance(dag, list) or not dag:
        raise TemplateError("模板 nodes 必须是非空数组")

    ids = _validate_shape(dag)
    _assert_acyclic(dag)

    # 护栏①：三型齐全
    missing = set(EXECUTORS) - {n["executor"] for n in dag}
    if missing:
        raise TemplateError(f"护栏①失败：缺少 executor {missing}（三型须各有落点）")

    for n in dag:
        _validate_node(dag, n, ids)


def _validate_shape(dag: list[dict]) -> set[str]:
    ids: set[str] = set()
    for n in dag:
        for field in ("id", "label", "executor", "role", "deps"):
            if field not in n:
                raise TemplateError(f"节点缺字段 {field}: {n}")
        nid = n["id"]
        if nid in ids:
            raise TemplateError(f"节点 id 重复: {nid}")
        ids.add(nid)
        legacy = [f for f in LEGACY_FIELDS if f in n]
        if legacy:
            raise TemplateError(
                f"{nid} 残留 seg-1 旧契约字段 {legacy}（type→executor / on_fail→运行时 reopen / gate→role）"
            )
        if n["executor"] not in EXECUTORS:
            raise TemplateError(f"{nid} 非法 executor={n['executor']}，须 ∈ {EXECUTORS}")
        if n["role"] not in ROLES:
            raise TemplateError(f"{nid} 非法 role={n['role']}，须 ∈ {ROLES}")
        if not isinstance(n["deps"], list):
            raise TemplateError(f"{nid} deps 必须是数组")

    for n in dag:
        for d in n["deps"]:
            if d not in ids:
                raise TemplateError(f"{n['id']} 依赖不存在的节点: {d}")
    return ids


def _validate_node(dag: list[dict], n: dict, ids: set[str]) -> None:
    nid, executor = n["id"], n["executor"]
    ancestors = deps_ancestors(dag, nid)

    if is_produce(n):
        _validate_deliverable(nid, n.get("deliverable"))
    else:  # gate
        if "deliverable" in n:
            raise TemplateError(f"{nid} 是 gate，不得声明 deliverable（把关不产出交付物）")
        _validate_policy(nid, n.get("approval_policy"))
        # 护栏②：每道 gate 须有可回退的传递祖先
        if not ancestors:
            raise TemplateError(f"护栏②失败：gate 节点 {nid} 无传递祖先（打回无处可回）")
        # 护栏③：放行 / 裁决落到人；llm 绝不自动放行
        wanted = "tool" if n["approval_policy"] == "auto" else "human"
        if executor != wanted:
            raise TemplateError(
                f"护栏③失败：{nid} approval_policy={n['approval_policy']} 须由 {wanted} 执行"
                f"（得到 {executor}；auto=确定性机检 bypass，其余=人拍板，llm 绝不自动放行）"
            )

    if executor == "llm":
        for field in ("prompt", "model_role"):
            if not n.get(field):
                raise TemplateError(f"{nid} 是 llm 节点，须声明 {field}")

    if executor == "human":
        if n.get("signal") not in SIGNALS:
            raise TemplateError(
                f"{nid} 是 human 节点，须声明 signal ∈ {SIGNALS}，得到 {n.get('signal')}"
            )
        # 护栏④：≥1 负责人
        if not n.get("assignee_role") and not (n.get("vote") or {}).get("voters"):
            raise TemplateError(f"护栏④失败：human 节点 {nid} 须 ≥1 负责人（assignee_role 或 vote.voters）")

    _validate_vote(nid, n.get("vote"))
    _validate_when(nid, n.get("when"), ids, ancestors)


def _validate_deliverable(nid: str, deliverable) -> None:
    if not isinstance(deliverable, dict):
        raise TemplateError(f"{nid} 是 produce 节点，须声明 deliverable（交付物落点）")
    region = deliverable.get("region")
    if isinstance(region, dict):
        if not region.get("section"):
            raise TemplateError(f"{nid} deliverable.region 若为对象须含 section 选择器")
        return
    if region not in REGIONS:
        raise TemplateError(f"{nid} 非法 deliverable.region={region}，须 ∈ {REGIONS} 或 {{section: ...}}")


def _validate_policy(nid: str, policy) -> None:
    if isinstance(policy, dict):
        if not policy.get("threshold"):
            raise TemplateError(f"{nid} approval_policy 若为对象须含 threshold 表达式")
        return
    if policy not in APPROVAL_POLICIES:
        raise TemplateError(
            f"{nid} 是 gate，须声明 approval_policy ∈ {APPROVAL_POLICIES} 或 {{threshold: expr}}，得到 {policy}"
        )


def _validate_vote(nid: str, vote) -> None:
    """多人节点（v1.3 runtime，schema 层 v1 就得认，见 ADR-025）。"""
    if vote is None:
        return
    if not isinstance(vote, dict):
        raise TemplateError(f"{nid} vote 必须是对象")
    voters = vote.get("voters") or []
    if not isinstance(voters, list) or not voters:
        raise TemplateError(f"护栏④失败：多人节点 {nid} 的 vote.voters 须非空")
    # 护栏④：多人节点须 1 主负责人（手动打回权主体，ADR-023）
    if vote.get("primary") not in voters:
        raise TemplateError(
            f"护栏④失败：多人节点 {nid} 须有 1 名主负责人 vote.primary ∈ voters，得到 {vote.get('primary')}"
        )


def _validate_when(nid: str, when, ids: set[str], ancestors: set[str]) -> None:
    """条件分支守卫（v1.3 runtime，schema 层 v1 就得认，见 ADR-025）。

    只校验「守卫可求值」这条结构必要条件：决策节点须是本节点的传递祖先。
    「决策取值域被守卫全覆盖或留默认支」需决策节点声明取值域，字段待 v1.3 定（见 SPEC 待填）。
    """
    if when is None:
        return
    if not isinstance(when, dict) or not when:
        raise TemplateError(f"{nid} when 必须是非空对象 {{决策节点: 值}}")
    for decision in when:
        if decision not in ids:
            raise TemplateError(f"护栏⑤失败：{nid} 的 when 引用不存在的决策节点 {decision}")
        if decision not in ancestors:
            raise TemplateError(
                f"护栏⑤失败：{nid} 的 when 引用 {decision} 不是其传递祖先（守卫永不可求值）"
            )


def _assert_acyclic(dag: list[dict]) -> None:
    deps = {n["id"]: list(n["deps"]) for n in dag}
    state: dict[str, int] = {}  # 0=visiting, 1=done

    def visit(nid: str, stack: tuple[str, ...]) -> None:
        if state.get(nid) == 1:
            return
        if state.get(nid) == 0:
            raise TemplateError(f"deps 存在环: {' → '.join(stack + (nid,))}")
        state[nid] = 0
        for d in deps[nid]:
            visit(d, stack + (nid,))
        state[nid] = 1

    for nid in deps:
        visit(nid, ())
