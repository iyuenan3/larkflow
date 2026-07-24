"""缺陷流的 tool / llm 节点行为（注入引擎，引擎本身不认识具体模板）。

human 节点（triage_review / reproduce / fix / qa_verify）不在这里：它们只 interrupt，
飞书任务 / 卡由驱动层在 __interrupt__ 后建（规避 resume 重跑副作用）。

tool/llm 节点内的写动作幂等（idem_key），容忍崩溃恢复时 super-step 重跑。
"""
from __future__ import annotations

from ..engine.executors import Executors


def intake(node: dict, state: dict, ex: Executors) -> dict:
    """受理登记。真飞书可建跟踪文档 / 多维表格行；seg-1 记录即可。"""
    meta = state.get("meta", {})
    bug = meta.get("bug", {})
    return {"ok": True, "bug_id": meta.get("instance_id"), "title": bug.get("title")}


def triage_ai(node: dict, state: dict, ex: Executors) -> dict:
    """AI 分诊：定级 / 定类 / 建议负责人（走多角色 LLM 路由；本地用 stub）。"""
    bug = state.get("meta", {}).get("bug", {})
    triage = ex.llm.triage(bug) if ex.llm else {}
    return {"ok": True, "triage": triage}


def assign(node: dict, state: dict, ex: Executors) -> dict:
    """派单：定开发负责人。driver 建 fix 任务时优先用这里的 owner 当 assignee。"""
    triage = (state.get("outputs", {}).get("triage_ai") or {}).get("triage", {})
    owner_role = triage.get("proposed_owner") or "开发"
    owner = ex.resolver.resolve("开发", state)
    return {"ok": True, "owner": owner, "owner_role": owner_role}


def close(node: dict, state: dict, ex: Executors) -> dict:
    """收口：通知上报人（幂等）。真飞书可再归档文档 / 关任务。"""
    meta = state.get("meta", {})
    reporter = meta.get("reporter")
    iid = meta.get("instance_id")
    if reporter:
        ex.io.notify(
            target=reporter,
            text=f"缺陷 {iid} 已验证通过并收口。",
            idem_key=f"{iid}:close:notify",
        )
    return {"ok": True, "closed": True}


DEFECT_TOOL_HANDLERS = {"intake": intake, "assign": assign, "close": close}
DEFECT_LLM_HANDLERS = {"triage_ai": triage_ai}
