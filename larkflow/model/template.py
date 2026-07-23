"""模板加载 + 校验：YAML → dag(list[dict])。

校验落地 ADR-010 的护栏（对策展模板与未来生成图共用同一把尺）：
  ① 三型齐全（tool/llm/human 各有落点）
  ② 每道门禁必须配一条显式回边（on_fail，杜绝只有前向边的假流程）
  ③ deps 不悬挂、无环（deps 是依赖 DAG；回边是运行时环，不进 deps）
  ④ human 节点必须声明 signal；放行/裁决类门禁节点强制 human
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .node import NODE_TYPES, SIGNALS, has_gate

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class TemplateError(ValueError):
    """模板不合法（结构 / 护栏）。"""


def load_template(name_or_path: str) -> list[dict]:
    """按名字（templates/<name>.yaml）或路径加载模板，返回校验过的 dag。"""
    path = Path(name_or_path)
    if not path.exists():
        path = TEMPLATES_DIR / f"{name_or_path}.yaml"
    if not path.exists():
        raise TemplateError(f"模板文件不存在: {name_or_path}")
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or "nodes" not in spec:
        raise TemplateError(f"模板缺少 nodes: {path}")
    dag = spec["nodes"]
    validate_template(dag)
    return dag


def validate_template(dag: list[dict]) -> None:
    if not isinstance(dag, list) or not dag:
        raise TemplateError("模板 nodes 必须是非空数组")

    ids: set[str] = set()
    for n in dag:
        for field in ("id", "label", "type", "deps"):
            if field not in n:
                raise TemplateError(f"节点缺字段 {field}: {n}")
        nid = n["id"]
        if nid in ids:
            raise TemplateError(f"节点 id 重复: {nid}")
        ids.add(nid)
        if n["type"] not in NODE_TYPES:
            raise TemplateError(f"{nid} 非法 type={n['type']}，须 ∈ {NODE_TYPES}")
        if not isinstance(n["deps"], list):
            raise TemplateError(f"{nid} deps 必须是数组")

    # deps 不悬挂
    for n in dag:
        for d in n["deps"]:
            if d not in ids:
                raise TemplateError(f"{n['id']} 依赖不存在的节点: {d}")

    # deps 无环（依赖 DAG；回边是运行时语义，不在 deps 里）
    _assert_acyclic(dag)

    # 护栏 ①：三型齐全
    kinds = {n["type"] for n in dag}
    missing = set(NODE_TYPES) - kinds
    if missing:
        raise TemplateError(f"护栏①失败：缺少节点类型 {missing}（三型须各有落点）")

    for n in dag:
        nid = n["id"]
        if has_gate(n):
            # 护栏 ②：每道门禁配回边
            target = n.get("on_fail")
            if not target:
                raise TemplateError(f"护栏②失败：门禁节点 {nid} 缺 on_fail 回边目标")
            if target not in ids:
                raise TemplateError(f"{nid} on_fail 指向不存在的节点: {target}")
            if target == nid:
                raise TemplateError(f"{nid} on_fail 不能指向自身")
            # 护栏 ②b：on_fail 必须是门禁节点的 deps 传递上游（祖先）。否则门禁节点自身
            # 不在重置集里、门禁失败后不会被重置，dispatch 每步重选 → 非终止（GraphRecursionError）。
            if target not in _deps_ancestors(dag, nid):
                raise TemplateError(
                    f"护栏②b失败：{nid} 的 on_fail={target} 不是其 deps 传递上游（回边须指向祖先）"
                )
        # 护栏 ④：human 节点须声明 signal
        if n["type"] == "human":
            sig = n.get("signal")
            if sig not in SIGNALS:
                raise TemplateError(
                    f"护栏④失败：human 节点 {nid} 须声明 signal ∈ {SIGNALS}，得到 {sig}"
                )


def _deps_ancestors(dag: list[dict], nid: str) -> set[str]:
    """nid 的全部 deps 传递上游（祖先）。在 model 层内联，避免 model → engine 反向依赖。"""
    deps = {n["id"]: list(n["deps"]) for n in dag}
    seen: set[str] = set()
    stack = list(deps.get(nid, []))
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack.extend(deps.get(x, []))
    return seen


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
