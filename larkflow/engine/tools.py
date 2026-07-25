"""tool 节点的内置能力库：确定性动作由**配置**选取，不再按 node id 写 Python。

为什么必须这样（ADR-022 / ARCHITECTURE 禁改项）：模板生成是主路径，而 AI 只能生成 YAML、
生不出 Python。只要 tool 节点的行为得靠「按 node id 注册的函数」提供，任何新业务场景就都
必须先有人写代码，「节点契约恒为数据、执行器一行不改」这条红线就是空的。

于是 tool 节点写成：

    tool: {kind: format_check, args: {required: [价款, 期限]}}

kind 是**跨业务复用**的有限集合（这里全部实现），args 是业务参数、下沉 yaml。
按 node id 注册的 handler 保留为逃生舱（真正一次性的确定性代码），不再是唯一路径。

约定：能力函数签名 `fn(node, state, ex, args) -> dict`；produce 类返回 `content` 交由引擎
统一物化成交付物，gate 类必须返回 `passed`。
"""
from __future__ import annotations

import json
from typing import Callable

from .deliverables import PLACEHOLDER_MARK, prior_handle, read_upstream

ToolKind = Callable[[dict, dict, object, dict], dict]
TOOL_KINDS: dict[str, ToolKind] = {}


def tool_kind(name: str):
    def deco(fn: ToolKind) -> ToolKind:
        TOOL_KINDS[name] = fn
        return fn
    return deco


def _meta(state: dict) -> dict:
    return state.get("meta") or {}


def _target(state: dict, who: str | None) -> str | None:
    """通知对象：reporter = 发起人；ou_/oc_ 开头当作 id 直用；其余按 assignee_role 解析。"""
    if not who:
        return None
    if who == "reporter":
        return _meta(state).get("reporter")
    return who


# ---------- produce 类 ----------

@tool_kind("record")
def record(node: dict, state: dict, ex, args: dict) -> dict:
    """把项目要素落成一份登记类交付物（受理单 / 立项单 / 派单说明…）。

    args: fields=[要素键…]（省略 = 全部）、note=附加说明
    """
    inputs = _meta(state).get("inputs") or {}
    fields = args.get("fields") or list(inputs)
    lines = [f"# {node.get('label', node['id'])}", ""]
    lines += [f"- {k}：{inputs.get(k, '')}" for k in fields]
    if args.get("note"):
        lines += ["", str(args["note"])]
    return {"ok": True, "recorded": {k: inputs.get(k) for k in fields},
            "content": "\n".join(lines)}


@tool_kind("summarize_links")
def summarize_links(node: dict, state: dict, ex, args: dict) -> dict:
    """收口：读 **dag 拓扑**汇总全部已登记交付物的链接，可选通知某人。

    读拓扑而不是写死 node id 列表，故运行中新增的节点也会自动进汇总（受控活图下这是常态）。
    args: notify=reporter|ou_xxx、title=标题、text=通知正文
    """
    meta, outputs = _meta(state), state.get("outputs") or {}
    iid = meta.get("instance_id", "")
    title = args.get("title") or node.get("label", node["id"])
    lines = [f"# {title} {iid}".rstrip(), "", "## 交付物"]
    for n in state.get("dag") or []:
        if n["id"] == node["id"]:
            continue
        handle = prior_handle(outputs, n["id"])
        if handle is not None:
            lines.append(f"- {n.get('label', n['id'])}：{handle.url}")

    target = _target(state, args.get("notify"))
    if target and ex.io is not None:
        ex.io.notify(
            target=target,
            text=args.get("text") or f"{title} {iid} 已完成。",
            idem_key=f"{iid}:{node['id']}:notify",
        )
    return {"ok": True, "closed": True, "content": "\n".join(lines)}


@tool_kind("notify")
def notify(node: dict, state: dict, ex, args: dict) -> dict:
    """纯通知节点（不产交付物）。args: to=reporter|ou_xxx、text=正文"""
    meta = _meta(state)
    target = _target(state, args.get("to") or "reporter")
    text = args.get("text") or f"{node.get('label', node['id'])}（{meta.get('instance_id','')}）"
    if target and ex.io is not None:
        ex.io.notify(target=target, text=text,
                     idem_key=f"{meta.get('instance_id','')}:{node['id']}:notify")
    return {"ok": True, "notified": target}


@tool_kind("noop")
def noop(node: dict, state: dict, ex, args: dict) -> dict:
    """占位 / 里程碑节点：只解锁下游，不做事、不产交付物。"""
    return {"ok": True}


# ---------- gate 类 ----------

@tool_kind("format_check")
def format_check(node: dict, state: dict, ex, args: dict) -> dict:
    """auto 门：对上游交付物做确定性机检（要素齐全 + 无占位符残留 + 长度下限）。

    args: required=[必须出现的字样…]、forbid_placeholders=true|false、min_chars=int
    机检不过 → passed=False，引擎按选择性重算自动打回它把关的上游。
    """
    texts = read_upstream(ex.deliverables, state, node) if ex.deliverables else {}
    joined = "\n".join(texts.values())
    missing = [s for s in (args.get("required") or []) if s not in joined]
    marks = [PLACEHOLDER_MARK] if args.get("forbid_placeholders", True) else []
    left = [m for m in marks if m in joined]
    short = len(joined.strip()) < int(args.get("min_chars") or 1)

    passed = not missing and not left and not short
    reasons = []
    if missing:
        reasons.append(f"缺要素 {missing}")
    if left:
        reasons.append("占位符未填写")
    if short:
        reasons.append("正文过短")
    return {"passed": passed, "missing": missing, "placeholders": left,
            "comment": "机检通过" if passed else "；".join(reasons)}


@tool_kind("expect_fields")
def expect_fields(node: dict, state: dict, ex, args: dict) -> dict:
    """auto 门：检查上游节点的**结构化产出**里必须有哪些键（不看正文）。

    args: node=上游节点 id（省略 = 全部 deps）、fields=[键…]
    """
    outputs = state.get("outputs") or {}
    targets = [args["node"]] if args.get("node") else list(node.get("deps", []))
    fields = args.get("fields") or []
    missing = [f"{t}.{f}" for t in targets for f in fields
               if not (outputs.get(t) or {}).get(f)]
    return {"passed": not missing, "missing": missing,
            "comment": "字段齐全" if not missing else f"缺字段 {missing}"}


def describe_kinds() -> str:
    """给生成器 / 文档用的能力清单。"""
    return json.dumps(sorted(TOOL_KINDS), ensure_ascii=False)
