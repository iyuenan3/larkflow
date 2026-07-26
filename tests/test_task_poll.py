"""对账时轮询在等的飞书任务（真跑第一条 e2e 时被一次 10 小时的静默丢事件逼出来的）。

现场：Mac 睡了一夜，`lark-cli` 的长连接静默死掉：进程全活、TCP 显示 ESTABLISHED、
`event status` 说 running、日志无任何异常，而 `RECEIVED 0`，10 小时 48 分一条事件没收到。
人在飞书里把「负责人定稿」点完成了，引擎**永远不知道**，实例就此停死。

关键的不对称（这才是要做轮询的理由，不是「多一层保险」）：

  · **卡片点击**：通道死了，用户当场看到红字「目标回调服务当前未在线」。失败得响，
    他会再点一次，事件本身不需要补。
  · **任务完成**：通道死了，用户看到任务变成已完成，**没有任何异常**。他以为交了，
    引擎以为还在等。双方都觉得自己是对的，谁都不会去查。

所以任务这条必须能被**捞回来**：对账时按关联表反查在等的任务，已完成就补一次 resume。
"""
from __future__ import annotations

from larkflow.app import build_service
from larkflow.config import RoleResolver

ROLES = RoleResolver({"负责人": "ou_owner"})


def graph() -> list[dict]:
    return [
        {"id": "a", "label": "AI 起草", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "fin", "label": "负责人定稿", "executor": "human", "role": "produce",
         "deps": ["a"], "assignee_role": "负责人", "signal": "task_complete",
         "deliverable": {"region": "whole"}},
        {"id": "end", "label": "收口", "executor": "tool", "role": "produce", "deps": ["fin"],
         "tool": {"kind": "noop"}},
    ]


def start():
    svc, io = build_service(graph(), resolver=ROLES)
    svc.start(instance_id="i1", reporter="ou_owner", inputs={})
    return svc, io


def the_task(io):
    return next(iter(io.tasks))


def test_a_completion_that_never_arrived_is_picked_up_by_reconcile():
    """这就是现场那一幕：人点完了、事件丢了、引擎还在等。"""
    svc, io = start()
    assert svc.status("i1").get("fin", "pending") == "pending"
    io.tasks[the_task(io)]["completed"] = True      # 人在飞书里点了完成，事件没送到
    out = svc.reconcile("i1")
    assert svc.status("i1").get("fin", "pending") == "done", out
    assert svc.finished("i1"), "补上之后整条链路要能自己走完"


def test_an_unfinished_task_is_left_alone():
    """没点完成就当没点完成。轮询绝不能替人做决定（红线：完成必须来自显式信号）。"""
    svc, io = start()
    svc.reconcile("i1")
    assert svc.status("i1").get("fin", "pending") == "pending"


def test_polling_survives_a_task_that_cannot_be_read():
    """飞书查不到 / 报错时，对账照常收尾，别让一条查询失败把整次对账带走。"""
    svc, io = start()

    def boom(guid):
        raise RuntimeError("飞书 500")

    io.get_task = boom
    out = svc.reconcile("i1")
    assert svc.status("i1").get("fin", "pending") == "pending"
    assert any("get_task" in e.get("error", "") for e in out.get("errors") or []), out


def test_it_only_looks_at_task_nodes_that_are_actually_waiting():
    """已经答复过的节点不许再查一遍：那是白调 API，而且会把已完成的活重推一次。"""
    svc, io = start()
    io.tasks[the_task(io)]["completed"] = True
    svc.reconcile("i1")
    asked = []
    real = io.get_task
    io.get_task = lambda g: (asked.append(g), real(g))[1]
    svc.reconcile("i1")
    assert asked == [], "全跑完了就不该再查任何任务"


def test_a_card_gate_is_never_polled():
    """卡片没有「状态」可查，而且它失败得响（用户当场看到红字），不需要补。"""
    svc, io = build_service([
        {"id": "a", "label": "稿", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "g", "label": "门", "executor": "human", "role": "gate", "deps": ["a"],
         "assignee_role": "负责人", "signal": "card_action", "approval_policy": "single"},
    ], resolver=ROLES)
    svc.start(instance_id="i2", reporter="ou_owner", inputs={})
    asked = []
    io.get_task = lambda g: asked.append(g)
    svc.reconcile("i2")
    assert asked == []
    assert svc.status("i2").get("g", "pending") == "pending", "绝不能因为查不到就把门放行"


def test_an_old_rounds_completion_never_resumes_the_current_round():
    """**每轮一条任务**：一个节点被打回 N 次就有 N+1 条飞书待办，旧的那几条永远停在
    「已完成」。按 node_id 去翻关联表，会拿第 1 轮那条早就完成的任务去 resume 第 3 轮，
    于是**每对账一次就白烧一轮打回预算**（真栈上实测撞到：两次重启把预算从 1 烧到 3，
    实例直奔 blocked）。

    正确的索引是派单幂等键 `{实例}:{节点}:{轮次}`，它天然只指向**本轮**那条。
    """
    svc, io = build_service([
        {"id": "fin", "label": "定稿", "executor": "human", "role": "produce", "deps": [],
         "assignee_role": "负责人", "signal": "task_complete", "deliverable": {"region": "whole"}},
        {"id": "chk", "label": "机检", "executor": "tool", "role": "gate", "deps": ["fin"],
         "approval_policy": "auto", "reopen_budget": 5,
         "tool": {"kind": "expect_fields", "args": {"fields": ["必填项"]}}},
    ], resolver=ROLES)
    svc.start(instance_id="i3", reporter="ou_owner", inputs={})
    first = the_task(io)
    io.tasks[first]["completed"] = True
    svc.reconcile("i3")                       # 第 1 轮交卷 → 机检打回 → 第 2 轮新任务
    assert svc.status("i3").get("chk") == "failed" or svc.status("i3").get("fin") == "pending"
    rounds = len(io.tasks)
    assert rounds >= 2, "打回之后应当有第 2 条任务"

    before = svc._values("i3").get("reopen_counts", {}).get("chk", 0)
    svc.reconcile("i3")                       # 再对账：第 2 轮还没人交卷
    after = svc._values("i3").get("reopen_counts", {}).get("chk", 0)
    assert after == before, "旧轮次那条已完成的任务不许把当前轮推下去"
    assert len(io.tasks) == rounds, "更不该因此再多派一条"
