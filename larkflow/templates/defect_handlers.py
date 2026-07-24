"""缺陷流的 tool / llm 节点行为（注入引擎，引擎本身不认识具体模板）。

human 节点（fix / triage_review / reproduce / qa_verify）不在这里：它们只 interrupt，
飞书任务 / 卡由驱动层在 __interrupt__ 后建（规避 resume 重跑副作用）。

produce handler 返回 `content` 即交由引擎统一物化成交付物（handle 登记进 outputs，
ADR-020）；tool/llm 节点内的其他写动作须幂等（idem_key），容忍 super-step 重跑。
"""
from __future__ import annotations

from ..engine.executors import Executors


def intake(node: dict, state: dict, ex: Executors) -> dict:
    """受理登记：把缺陷要素落成登记单（交付物）。"""
    meta = state.get("meta", {})
    bug = meta.get("bug", {})
    lines = [f"# 缺陷登记 {meta.get('instance_id', '')}",
             f"- 标题：{bug.get('title', '')}",
             f"- 描述：{bug.get('detail', '')}",
             f"- 上报人：{meta.get('reporter', '')}"]
    return {"ok": True, "bug_id": meta.get("instance_id"), "title": bug.get("title"),
            "content": "\n".join(lines)}


def triage_ai(node: dict, state: dict, ex: Executors) -> dict:
    """AI 分诊：定级 / 定类 / 建议负责人（结构化产出 + 一份分诊结论交付物）。"""
    bug = state.get("meta", {}).get("bug", {})
    triage = ex.llm.triage(bug) if ex.llm else {}
    lines = ["# 分诊结论"] + [f"- {k}：{v}" for k, v in triage.items()]
    return {"ok": True, "triage": triage, "content": "\n".join(lines)}


def assign(node: dict, state: dict, ex: Executors) -> dict:
    """派单：定开发负责人。driver 建 fix 任务时优先用这里的 owner 当 assignee。"""
    triage = (state.get("outputs", {}).get("triage_ai") or {}).get("triage", {})
    owner_role = triage.get("proposed_owner") or "开发"
    owner = ex.resolver.resolve("开发", state)
    return {"ok": True, "owner": owner, "owner_role": owner_role,
            "content": f"# 派单\n- 负责人：{owner}（{owner_role}）"}


def close(node: dict, state: dict, ex: Executors) -> dict:
    """收口：通知上报人（幂等）+ 落一份收口小结。真飞书可再归档文档 / 关任务。"""
    meta = state.get("meta", {})
    reporter = meta.get("reporter")
    iid = meta.get("instance_id")
    if reporter:
        ex.io.notify(
            target=reporter,
            text=f"缺陷 {iid} 已验证通过并收口。",
            idem_key=f"{iid}:close:notify",
        )
    return {"ok": True, "closed": True, "content": f"# 收口\n缺陷 {iid} 已验证通过并归档。"}


DEFECT_TOOL_HANDLERS = {"intake": intake, "assign": assign, "close": close}
DEFECT_LLM_HANDLERS = {"triage_ai": triage_ai}
