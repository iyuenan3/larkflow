"""第一段本地 e2e（证据）：假 bug 走完 8 节点，G5 验证门禁 reopen 打回 fix 一次，
再修好收口 + 通知上报人。全程 MockLarkIO + StubLLM，零外部依赖。

这就是 PRD 的第一个 win 的本地判定版：证「门禁真打回」+「流程真穿过」。
"""
from larkflow.app import build_defect_service
from larkflow.io.events import CARD_ACTION, TASK_UPDATE

NODES = ["intake", "triage_ai", "triage_review", "reproduce",
         "assign", "fix", "qa_verify", "close"]


def _card_event(io, node_id, label):
    """模拟点某节点最新一张卡的某按钮（action_value 自描述路由键）。"""
    return {"key": CARD_ACTION, "action_value": io.button_value(node_id, label), "operator_id": "ou_op"}


def _task_event(io):
    """模拟完成最近建的飞书任务（seg-1 只有 fix 走 task_complete）。"""
    guid = list(io.tasks.values())[-1]["guid"]
    return {"key": TASK_UPDATE, "event": {"task_guid": guid, "event_types": ["task_completed_update"]}}


def test_win_g5_reopen_once_then_close():
    svc, io = build_defect_service()
    iid = "wf-1"

    svc.start(instance_id=iid, reporter="ou_reporter", bug={"title": "登录崩溃", "detail": "点登录白屏"})
    # intake(tool) + triage_ai(llm) 自动跑完，挂在 triage_review(human·卡)
    assert svc.status(iid)["intake"] == "done"
    assert svc.status(iid)["triage_ai"] == "done"

    svc.resume_from_event(_card_event(io, "triage_review", "通过"))     # 分诊复核
    svc.resume_from_event(_card_event(io, "reproduce", "通过"))         # 复现确认
    svc.resume_from_event(_task_event(io))                              # 修复 #1 完成
    svc.resume_from_event(_card_event(io, "qa_verify", "打回"))         # G5 打回（reopen）
    svc.resume_from_event(_task_event(io))                              # 重修 #2 完成
    svc.resume_from_event(_card_event(io, "qa_verify", "通过"))         # G5 通过

    status = svc.status(iid)
    assert all(status.get(n) == "done" for n in NODES), status

    # G5 打回恰好一次 → fix 派了两张飞书任务
    fix_tasks = [t for t in io.tasks.values() if t["summary"] == "修复"]
    assert len(fix_tasks) == 2, io.tasks
    assert fix_tasks[0]["guid"] != fix_tasks[1]["guid"]  # reopen 出的是新单，不复用旧完成单

    # qa_verify 发了两张门禁卡（打回后重发）
    qa_cards = [c for c in io.cards.values()
                if any(b["action_value"]["node_id"] == "qa_verify" for b in c["buttons"])]
    assert len(qa_cards) == 2

    # 自动收口 + 通知上报人
    assert any(n["target"] == "ou_reporter" for n in io.notifications), io.notifications


def test_stale_resume_is_noop():
    """同一张卡重复点击（飞书 at-least-once）→ 第二次是陈旧中断，no-op。"""
    svc, io = build_defect_service()
    iid = "wf-2"
    svc.start(instance_id=iid, reporter="ou_r", bug={"title": "x"})

    ev = _card_event(io, "triage_review", "通过")
    first = svc.resume_from_event(ev)
    assert "resumed" in first
    second = svc.resume_from_event(ev)  # 重放同一事件
    assert second.get("skipped") == "stale"
