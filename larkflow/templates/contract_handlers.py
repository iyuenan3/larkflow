"""合同图的 tool 节点行为（确定性程序，注入引擎）。

- checks：auto 格式检查门。**不叫 AI 判、也不问人**，纯机检：必需要素齐 + 无占位符残留。
  不过则按选择性重算自动打回上游（默认 = 它把关的直接上游 finalize）。
- close：收口。汇总各交付物链接落一份小结 + 通知发起人（幂等）。
"""
from __future__ import annotations

from ..engine.deliverables import prior_handle, read_upstream
from ..engine.executors import Executors

REQUIRED_SECTIONS = ("价款", "期限", "违约", "争议")
PLACEHOLDER_MARKS = ("【待确认", "待 ", "TODO", "TBD")
SUMMARY_ORDER = ("biz_draft", "legal_draft", "merge", "finalize")


def format_checks(node: dict, state: dict, ex: Executors) -> dict:
    """机检定稿：必需条款要素齐全、无占位符残留。产出必须含 passed。"""
    text = "\n".join(read_upstream(ex.deliverables, state, node).values())
    missing = [s for s in REQUIRED_SECTIONS if s not in text]
    left = [m for m in PLACEHOLDER_MARKS if m in text]
    passed = not missing and not left and bool(text.strip())
    comment = "格式检查通过" if passed else f"缺要素 {missing}；占位符残留 {left}"
    return {"passed": passed, "missing": missing, "placeholders": left, "comment": comment}


def close(node: dict, state: dict, ex: Executors) -> dict:
    """收口：一份带全部交付物链接的小结 + 通知发起人。"""
    meta = state.get("meta", {})
    outputs = state.get("outputs", {})
    labels = {n["id"]: n.get("label", n["id"]) for n in state.get("dag", [])}

    lines = [f"# 合同项目收口 {meta.get('instance_id', '')}", "", "## 交付物"]
    for nid in SUMMARY_ORDER:
        handle = prior_handle(outputs, nid)
        if handle:
            lines.append(f"- {labels.get(nid, nid)}：{handle.url}")

    reporter = meta.get("reporter")
    if reporter:
        ex.io.notify(
            target=reporter,
            text=f"合同项目 {meta.get('instance_id', '')} 已定稿并通过格式检查。",
            idem_key=f"{meta.get('instance_id', '')}:close:notify",
        )
    return {"ok": True, "closed": True, "content": "\n".join(lines)}


CONTRACT_TOOL_HANDLERS = {"checks": format_checks, "close": close}
CONTRACT_LLM_HANDLERS: dict = {}   # 三个 llm 节点全走通用 produce 执行体
