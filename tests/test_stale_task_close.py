"""打回之后，把**旧轮次**那些还开着的飞书待办关掉。

现场（真栈第一条 e2e 之后清场时发现的）：`finalize` 被机检打回 3 次，于是飞书里躺着 4 条
「负责人定稿」，其中 2 条从头到尾没人点过、也永远不会有人点。`complete_task` 在
`io/lark_io.py` 有协议 / Mock / Cli 三处定义，**零调用点**，代码注释里早就写着
「重复的待办没有任何代码去关掉它，永远躺在人的待办列表里（实测）」。

真正的孤儿比「旧轮次已完成的单」更难受：**被打回卷进新一轮、但新一轮还没轮到派单**的
旁支节点（它得等上游返工完成）。那条旧单一直开着，人点「完成」只得到静默 no-op，任务通道
没有卡片那套「陈旧当场作废」的对称物（`_settle_card` 要回调 token，任务事件没有）。

三条硬约束：
  · **绝不关本轮那条**。`_sweep_tasks` 会把 `completed == True` 当成人的真实完成信号并
    resume，关错一条就等于引擎替人交了卷，破「完成必须来自显式信号」这条红线。
  · 关单是投影侧动作：失败只记 `provision_errors`，不抛、不影响 checkpointer 里的裁决。
  · 幂等走本地 `_once`，键里必须带**旧轮次号**，否则同一节点第二次被打回会被幂等吞掉。
"""
from __future__ import annotations

from larkflow.app import build_service
from larkflow.config import RoleResolver
from larkflow.io import FakeDeliverableStore
from larkflow.io.deliverable import Deliverable
from larkflow.io.events import CARD_ACTION, TASK_UPDATE

ROLES = RoleResolver({"甲": "ou_甲", "乙": "ou_乙", "丙": "ou_丙"})


# 机检打回：一个节点被反复退回，最典型的僵尸来源
AUTO = [
    {"id": "draft", "label": "定稿", "executor": "human", "role": "produce", "deps": [],
     "assignee_role": "甲", "signal": "task_complete", "deliverable": {"region": "whole"}},
    {"id": "check", "label": "机检", "executor": "tool", "role": "gate", "deps": ["draft"],
     "approval_policy": "auto", "reopen_budget": 5,
     "tool": {"kind": "expect_fields", "args": {"fields": ["期限"]}}},
    {"id": "close", "label": "收口", "executor": "tool", "role": "produce", "deps": ["check"],
     "tool": {"kind": "noop"}},
]

# 串行的人工链：打回 a 会把 s 一起卷进新一轮，而 s 要等 a 重做完才轮得到派单
CHAIN = [
    {"id": "a", "label": "甲写材料", "executor": "human", "role": "produce", "deps": [],
     "assignee_role": "甲", "signal": "task_complete", "deliverable": {"region": "whole"}},
    {"id": "s", "label": "乙补充", "executor": "human", "role": "produce", "deps": ["a"],
     "assignee_role": "乙", "signal": "task_complete", "deliverable": {"region": "whole"}},
    {"id": "g", "label": "丙审", "executor": "human", "role": "gate", "deps": ["s"],
     "assignee_role": "丙", "signal": "card_action", "approval_policy": "single"},
    {"id": "end", "label": "收口", "executor": "tool", "role": "produce", "deps": ["g"],
     "tool": {"kind": "noop"}},
]


def spy_on_closes(io):
    """单独记下**引擎主动关掉**的单。

    不能靠 `io.tasks[guid]["completed"]` 判断：Mock 的 `get_task` 读的就是 `complete_task`
    写的那个字典，于是「引擎关的」与「人点完成的」在断言里完全不可区分（写这份测试时
    差点踩进去）。
    """
    closed, real = [], io.complete_task

    def spy(task_guid, *, idem_key):
        closed.append(task_guid)
        return real(task_guid, idem_key=idem_key)

    io.complete_task = spy
    return closed


def task_of(io, summary, *, nth=0):
    hits = [t["guid"] for t in io.tasks.values() if t["summary"] == summary]
    return hits[nth]


def finish_task(svc, io, store, iid, node_label, text, *, nth=-1):
    """扮演人：把交付物写了，然后在飞书里点完成。"""
    p = next(x for x in svc.pending(iid) if x["label"] == node_label)
    store.overwrite(Deliverable.from_dict(p["deliverable"]), content=text)
    guid = [t["guid"] for t in io.tasks.values() if t["summary"] == node_label][nth]
    io.tasks[guid]["completed"] = True
    return svc.resume_from_event({"key": TASK_UPDATE, "event": {
        "task_guid": guid, "event_types": ["task_completed_update"]}})


# ---------- 核心 ----------

def test_a_reopened_round_closes_the_todo_nobody_will_ever_click_again():
    """机检打回一次 → 第 1 轮那条待办当场关掉，人的列表里只留新的那条。"""
    store = FakeDeliverableStore()
    svc, io = build_service(AUTO, resolver=ROLES, deliverables=store)
    closed = spy_on_closes(io)
    svc.start(instance_id="st-1", reporter="ou_owner", inputs={})
    first = task_of(io, "定稿")

    finish_task(svc, io, store, "st-1", "定稿", "没有那个要素")   # 机检必打回

    assert svc.status("st-1")["draft"] == "pending", "先确认真被打回了"
    assert len([t for t in io.tasks.values() if t["summary"] == "定稿"]) == 2, "第 2 轮出了新单"
    assert first in closed, "第 1 轮那条必须被关掉，它再也不会有人点"


def test_the_round_that_is_actually_waiting_is_never_closed():
    """**这条最要命**：关错本轮那条 = `_sweep_tasks` 会把它读成「人交卷了」并推下去，
    等于引擎替人签了字（破「完成必须来自显式信号」红线）。"""
    store = FakeDeliverableStore()
    svc, io = build_service(AUTO, resolver=ROLES, deliverables=store)
    closed = spy_on_closes(io)
    svc.start(instance_id="st-2", reporter="ou_owner", inputs={})
    finish_task(svc, io, store, "st-2", "定稿", "没有那个要素")

    current = task_of(io, "定稿", nth=1)
    assert current not in closed, "本轮那条一根手指都不许碰"

    svc.reconcile("st-2")
    svc.reconcile("st-2")
    assert svc.status("st-2")["draft"] == "pending", "对账绝不能因此把人没做的活推下去"
    assert current not in closed


def test_the_orphan_on_a_side_branch_is_closed_right_away_not_when_it_is_redispatched():
    """真正的孤儿：乙 被卷进新一轮，但要等甲重做完才轮得到他重新派单。

    这中间他手里那条旧单一直开着，长得和能干的活一模一样，点了只有静默 no-op。
    所以关的时机必须是**打回那一刻**，不能等到给他派新单的时候。
    """
    store = FakeDeliverableStore()
    svc, io = build_service(CHAIN, resolver=ROLES, deliverables=store)
    closed = spy_on_closes(io)
    svc.start(instance_id="st-3", reporter="ou_owner", inputs={})
    finish_task(svc, io, store, "st-3", "甲写材料", "材料")
    finish_task(svc, io, store, "st-3", "乙补充", "补充")
    old_a, old_s = task_of(io, "甲写材料"), task_of(io, "乙补充")

    # owner 打回 a：a、s、g 全进新一轮，但 s 的上游 a 还没重做完
    out = svc.resume_from_event({"key": CARD_ACTION, "operator_id": "ou_owner",
                                 "action_value": dict(io.button_value("g", "打回"),
                                                      reopen=["a"], comment="重来")})
    assert out.get("resumed"), out
    assert svc.status("st-3")["s"] == "pending"
    assert [t["summary"] for t in io.tasks.values()].count("乙补充") == 1, \
        "先确认 s 的新单还没派出去（它在等 a）"

    assert old_s in closed, "乙 手里那条旧单必须当场关掉，别让他对着一条死单发呆"
    assert old_a in closed, "甲 的旧单同理"


def test_a_card_node_is_never_touched():
    """卡片没有「完成」这个动作，而且它已有 ADR-037 的「当场标失效」。"""
    store = FakeDeliverableStore()
    svc, io = build_service(CHAIN, resolver=ROLES, deliverables=store)
    closed = spy_on_closes(io)
    svc.start(instance_id="st-4", reporter="ou_owner", inputs={})
    finish_task(svc, io, store, "st-4", "甲写材料", "材料")
    finish_task(svc, io, store, "st-4", "乙补充", "补充")
    svc.resume_from_event({"key": CARD_ACTION, "operator_id": "ou_owner",
                           "action_value": dict(io.button_value("g", "打回"), reopen=["a"])})
    card_guids = {c.get("message_id") for c in io.cards.values()}
    assert not (set(closed) & card_guids)


# ---------- 幂等 / 容错 ----------

def test_closing_happens_exactly_once_per_round():
    """对账 / 重启会把 `_handle` 再跑一遍，不许每次都往飞书打一发关单。"""
    store = FakeDeliverableStore()
    svc, io = build_service(AUTO, resolver=ROLES, deliverables=store)
    closed = spy_on_closes(io)
    svc.start(instance_id="st-5", reporter="ou_owner", inputs={})
    finish_task(svc, io, store, "st-5", "定稿", "没有那个要素")
    once = len(closed)
    assert once == 1

    for _ in range(3):
        svc.reconcile("st-5")
    assert len(closed) == once, "同一轮的单只关一次"


def test_a_second_reopen_closes_the_second_rounds_todo_too():
    """幂等键必须带轮次号：不带的话第二次打回会被幂等表整个吞掉。"""
    store = FakeDeliverableStore()
    svc, io = build_service(AUTO, resolver=ROLES, deliverables=store)
    closed = spy_on_closes(io)
    svc.start(instance_id="st-6", reporter="ou_owner", inputs={})
    finish_task(svc, io, store, "st-6", "定稿", "第一版，没那个要素")
    finish_task(svc, io, store, "st-6", "定稿", "第二版，还是没有")

    assert len(closed) == 2, f"两轮各关一条，实际 {closed}"
    assert len(set(closed)) == 2, "关的必须是两条不同的单"


def test_feishu_refusing_to_close_never_breaks_the_flow():
    """投影侧动作：飞书那一下失败了，打回照样算数（权威结论在 checkpointer）。"""
    store = FakeDeliverableStore()
    svc, io = build_service(AUTO, resolver=ROLES, deliverables=store)
    svc.start(instance_id="st-7", reporter="ou_owner", inputs={})

    def boom(task_guid, *, idem_key):
        raise RuntimeError("飞书 500")

    io.complete_task = boom
    finish_task(svc, io, store, "st-7", "定稿", "没有那个要素")

    assert svc.status("st-7")["draft"] == "pending", "打回照样落地"
    errs = " ".join(e.get("error", "") for e in svc.provision_errors.get("st-7") or [])
    assert "complete_task" in errs or "飞书 500" in errs, "但要留下痕迹，别静默"


def test_a_failed_close_is_retried_next_time():
    """失败不许被幂等表记成「做过了」，否则那条僵尸就永远关不掉了。"""
    store = FakeDeliverableStore()
    svc, io = build_service(AUTO, resolver=ROLES, deliverables=store)
    svc.start(instance_id="st-8", reporter="ou_owner", inputs={})

    def boom(task_guid, *, idem_key):
        raise RuntimeError("飞书 500")

    io.complete_task = boom
    finish_task(svc, io, store, "st-8", "定稿", "没有那个要素")

    closed = spy_on_closes(io)          # 飞书恢复了
    svc.reconcile("st-8")
    assert len(closed) == 1, "下一次对账要把它补上"


def test_finishing_normally_closes_nothing():
    """没有打回就没有旧轮次，一次多余的飞书调用都不该有。"""
    plain = [
        {"id": "draft", "label": "定稿", "executor": "human", "role": "produce", "deps": [],
         "assignee_role": "甲", "signal": "task_complete", "deliverable": {"region": "whole"}},
        {"id": "close", "label": "收口", "executor": "tool", "role": "produce", "deps": ["draft"],
         "tool": {"kind": "noop"}},
    ]
    store = FakeDeliverableStore()
    svc, io = build_service(plain, resolver=ROLES, deliverables=store)
    closed = spy_on_closes(io)
    svc.start(instance_id="st-9", reporter="ou_owner", inputs={})
    finish_task(svc, io, store, "st-9", "定稿", "一、期限：12 个月")

    assert svc.finished("st-9")
    assert closed == []


def test_a_persistent_failure_does_not_multiply_into_dozens_of_calls_per_advance():
    """关单失败时，一次推进只许**试一遍**，不许被泵循环放大。

    成功时挂在哪都一样（`_once` 记了键，后续全是本地幂等表查询）；**失败时不记键**，
    于是每一次 `_handle` 都会真去 spawn 一次 lark-cli。而 `_advance` 的泵在一次打回里
    可以跑很多拍（对抗 review 实测把 6 个目标放大成 81 次调用）。所以扫描挂在
    「一次推进一次」而不是「每拍一次」。
    """
    store = FakeDeliverableStore()
    svc, io = build_service(AUTO, resolver=ROLES, deliverables=store)
    svc.start(instance_id="st-10", reporter="ou_owner", inputs={})
    tries = []

    def boom(task_guid, *, idem_key):
        tries.append(task_guid)
        raise RuntimeError("飞书 500")

    io.complete_task = boom
    finish_task(svc, io, store, "st-10", "定稿", "没有那个要素")   # 一次打回 = 一次推进

    assert len(tries) == 1, f"一次推进只该试一遍，实际 {len(tries)} 次"
