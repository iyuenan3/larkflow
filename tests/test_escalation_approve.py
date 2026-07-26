"""escalation 的一键同意 / 拒绝（ADR-023 ③ 那半边，v1 一直缺的死局）。

现场：`_escalate` 只往权威 state 写申请，全仓**没有任何 approve / reject 通道**，记录的
`status` 硬编码 `"pending"` 且 reducer 只追加不覆盖，于是一笔申请写下去后**物理上不可能**
再变成别的状态。而 v0.5.0 把卡上默认打回目标改成「只剔 denied、保留要走审批的」之后，
**默认那颗「打回」按钮天然带着跨界目标**，一点就落进 escalation。于是默认路径上的人会收到
「已提交申请、等人拍板」，而那个「拍板」的按钮不存在。

这和 ADR-029 的 `blocked` 死局是同一个病换了个地方复发：机制把人送进一个状态，却没给出口。

四条设计取舍（都在这份测试里钉住）：

  · **OR 语义照 ADR-023 原文**（「任一方同意即执行」，ADR 自称轻量版），不改成逐目标收齐。
    代价记在 ADR 的 Tradeoff 里：多目标时 A 目标的负责人一个人就能同意掉牵连 B 的整笔。
  · **禁自批**。`approvers_for` = owner 令牌 ∪ 目标节点主负责人，而申请人可能正好是后者
    （他打回自己的活，但重算集牵连了第三个人）。不禁的话，权限层被自己绕开。
    owner 恒在审批人集合里、且 owner 走不到 escalation 这条路，所以禁自批不会造成无人可批。
  · **追加型 channel 不能 UPDATE**（红线：只改未来不改历史），所以「同意」不是把 status
    改掉，而是**追加一条裁决记录**，状态改为**派生**。这顺带修掉「旧记录 status 恒为
    pending」那条挂了很久的 finding。
  · **先执行、后记账**（ADR-034）。中间崩掉的话：打回已落地 → 门进了新一轮 → 那笔申请按
    `attempt` 自然作废，不会被二次同意。反过来（先记后执行）崩掉就是「显示已批准、其实没发生」，
    不可恢复。
"""
from __future__ import annotations

import pytest

from larkflow.app import build_service
from larkflow.io import FakeDeliverableStore
from larkflow.io.events import CARD_ACTION, TASK_UPDATE
from support import CountingLLM


# ---------- 拓扑：乙 打回甲的活会把还在等的丙一起卷进返工 ----------

CROSS = [
    {"id": "a", "label": "甲写材料", "executor": "human", "role": "produce", "deps": [],
     "assignee_role": "甲", "signal": "task_complete", "deliverable": {"region": "whole"}},
    {"id": "b", "label": "AI 整合", "executor": "llm", "role": "produce", "deps": ["a"],
     "prompt": "整合", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "g", "label": "乙审", "executor": "human", "role": "gate", "deps": ["b"],
     "assignee_role": "乙", "signal": "card_action", "approval_policy": "single"},
    {"id": "side", "label": "丙审", "executor": "human", "role": "gate", "deps": ["b"],
     "assignee_role": "丙", "signal": "card_action", "approval_policy": "single"},
    {"id": "tail", "label": "收口", "executor": "tool", "role": "produce", "deps": ["g", "side"],
     "tool": {"kind": "noop"}},
]

# 同一个人（甲）既写 a 又把 g 这道门，于是他自己就在 a 的审批人里：自批的构造现场。
SELF = [
    {"id": "a", "label": "甲写材料", "executor": "human", "role": "produce", "deps": [],
     "assignee_role": "甲", "signal": "task_complete", "deliverable": {"region": "whole"}},
    {"id": "b", "label": "AI 整合", "executor": "llm", "role": "produce", "deps": ["a"],
     "prompt": "整合", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "g", "label": "甲复核", "executor": "human", "role": "gate", "deps": ["b"],
     "assignee_role": "甲", "signal": "card_action", "approval_policy": "single"},
    {"id": "side", "label": "丙审", "executor": "human", "role": "gate", "deps": ["b"],
     "assignee_role": "丙", "signal": "card_action", "approval_policy": "single"},
    {"id": "tail", "label": "收口", "executor": "tool", "role": "produce", "deps": ["g", "side"],
     "tool": {"kind": "noop"}},
]


def redo_a(svc, io, store, iid, text="重做的材料"):
    """扮演甲：把 a 重写一版并在飞书点完成，于是这条链回到两道门挂着的现场。"""
    from larkflow.io.deliverable import Deliverable

    p = next(x for x in svc.pending(iid) if x["node_id"] == "a")
    store.overwrite(Deliverable.from_dict(p["deliverable"]), content=text)
    guid = [t["guid"] for t in io.tasks.values() if t["summary"] == "甲写材料"][-1]
    io.tasks[guid]["completed"] = True
    return svc.resume_from_event({"key": TASK_UPDATE, "event": {
        "task_guid": guid, "event_types": ["task_completed_update"]}})


def run_to_gates(dag, iid):
    """跑到两道人工门同时挂着的现场。"""
    llm = CountingLLM({"w": "正文"})
    store = FakeDeliverableStore()
    svc, io = build_service(dag, llm=llm, deliverables=store)
    svc.start(instance_id=iid, reporter="ou_owner", inputs={})
    redo_a(svc, io, store, iid, text="材料")
    assert {x["node_id"] for x in svc.pending(iid)} == {"g", "side"}
    return svc, io, llm, store


def click(svc, io, node_id, label, *, operator, **ov):
    return svc.resume_from_event({"key": CARD_ACTION, "operator_id": operator,
                                  "action_value": dict(io.button_value(node_id, label), **ov)})


def escalate(iid="e1", dag=CROSS, who="ou_乙", gate="g", targets=("a",), comment="材料不全"):
    svc, io, llm, store = run_to_gates(dag, iid)
    res = click(svc, io, gate, "打回", operator=who, reopen=list(targets), comment=comment)
    assert res.get("escalated") == list(targets), res
    return svc, io, llm, res


def tight_budget(iid, budget=1):
    """跑到「这道门的打回预算已经烧光、但仍挂着人」的现场。"""
    dag = [dict(n, reopen_budget=budget) if n["id"] == "g" else dict(n) for n in CROSS]
    svc, io, llm, store = run_to_gates(dag, iid)
    assert click(svc, io, "g", "打回", operator="ou_owner", reopen=["a"]).get("resumed")
    redo_a(svc, io, store, iid)          # 甲重做交卷，回到 g / side 挂着
    assert svc._values(iid).get("reopen_counts", {}).get("g") == budget
    return svc, io, llm, store


# ---------- 核心：同意真的把打回执行了 ----------

def test_an_approval_actually_executes_the_reopen():
    """这是整条通道存在的理由：在此之前，同意这个动作根本没有落点。"""
    svc, io, llm, _ = escalate("ap-1")
    assert svc.status("ap-1").get("a") == "done"

    out = svc.approve_escalation("ap-1", "g", by="ou_甲")

    assert out.get("approved") is True, out
    assert out["seq"] == 1 and out["by"] == "ou_甲"
    assert svc.status("ap-1")["a"] == "pending", "甲的活要真的退回重做"
    # 打回**真落地**的判据是整个重算集都进了新一轮，不是只把发起那个节点标一下。
    # （这里看不到 LLM 重跑：b 要等甲把 a 重做完才轮得到它。）
    assert set(out.get("reopened") or []) >= {"a", "b", "g"}, out
    assert io.tasks and any(t["summary"] == "甲写材料" and not t.get("completed")
                            for t in io.tasks.values()), "甲要收到新一轮的待办"


def test_an_approval_retires_the_request_and_frees_the_quota():
    """裁决过的申请不再是「待批」。配额跟着松绑，否则同意通道等于没有。"""
    svc, io, llm, _ = escalate("ap-2")
    assert len(svc.pending_escalations("ap-2", "g")) == 1

    svc.approve_escalation("ap-2", "g", by="ou_甲")

    assert svc.pending_escalations("ap-2", "g") == [], "拍过板的不许再算待批"
    assert svc.pending_escalations("ap-2") == {}


def test_the_audit_log_keeps_the_request_and_derives_its_status():
    """审计只追加、一条不删：原申请必须原样留着，状态是**算**出来的，不是改出来的。"""
    svc, io, llm, _ = escalate("ap-3")
    svc.approve_escalation("ap-3", "g", by="ou_甲", comment="同意，材料确实缺")

    log = svc.escalations("ap-3", "g")
    req = [r for r in log if r.get("kind", "request") == "request"]
    assert len(req) == 1, "原申请不许被删改"
    assert req[0]["status"] == "pending", "落库时那一刻的字面值原样冻住（历史不可改）"
    assert req[0]["effective_status"] == "approved", "当下的真实状态是派生出来的"

    verdicts = [r for r in log if r.get("kind") == "verdict"]
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"] == "approved" and verdicts[0]["by"] == "ou_甲"
    assert verdicts[0]["ref"] == 1 and verdicts[0]["at"]
    assert verdicts[0]["comment"] == "同意，材料确实缺"


# ---------- 权限 ----------

def test_a_stranger_cannot_approve():
    svc, io, llm, _ = escalate("ap-4")
    before = dict(svc.status("ap-4"))

    out = svc.approve_escalation("ap-4", "g", by="ou_路人")

    assert out.get("rejected") == "unauthorized_approve", out
    assert svc.status("ap-4") == before, "越权不许动权威 state"
    assert len(svc.pending_escalations("ap-4", "g")) == 1, "申请照旧挂着"


def test_the_project_owner_can_always_approve():
    """owner 恒在审批人集合里，这条是「禁自批不会造成无人可批」的兜底证明。"""
    svc, io, llm, _ = escalate("ap-5")
    assert svc.approve_escalation("ap-5", "g", by="ou_owner").get("approved") is True


def test_the_requester_cannot_approve_their_own_request():
    """申请人正好在审批人集合里的现场：甲既写 a 又把 g 这道门。

    不禁的话，他自己提、自己批，ADR-023 那三条规则被完全绕开，而这条路径在真实图里
    一点都不刁钻（谁把关谁的上游，本来就常常是同一个人）。
    """
    svc, io, llm, res = escalate("ap-6", dag=SELF, who="ou_甲", gate="g")
    assert "甲" in svc.escalations("ap-6", "g")[0]["approvers"], "先确认他确实在审批人里"
    before = dict(svc.status("ap-6"))

    out = svc.approve_escalation("ap-6", "g", by="ou_甲")

    assert out.get("rejected") == "self_approve", out
    assert svc.status("ap-6") == before
    assert svc.approve_escalation("ap-6", "g", by="ou_owner").get("approved") is True, \
        "别人照样批得动：禁的是自批，不是这笔申请"


def test_an_approver_who_was_actually_notified_can_approve_even_if_the_role_stops_resolving():
    """审批人在记录里存的是**令牌**（角色名），判身份要靠 `roles_of` 反解求交。

    可反解会静默失败：自定义 resolver 没有 `roles_of`、角色映射改了、assignee 配成飞书群。
    只认令牌的话，这笔申请就**没人同意得了**，死局原样复发。故当时真通知到的那些
    open_id 是第二把尺：我们亲口告诉了他该他拍板，他就点得动。
    """
    svc, io, llm, _ = escalate("ap-7")
    assert svc.escalations("ap-7", "g")[0]["notified"] == ["ou_owner", "ou_甲"]

    svc.resolver.roles_of = lambda _open_id: set()      # 反解从此什么都认不出来

    assert svc.approve_escalation("ap-7", "g", by="ou_甲").get("approved") is True


def test_an_audit_trail_is_mandatory():
    """照 unblock 的先例：没有 `by` 就没有审计，审计是不变量。"""
    svc, io, llm, _ = escalate("ap-8")
    for bad in (None, "", "   "):
        assert svc.approve_escalation("ap-8", "g", by=bad).get("rejected") == "missing_audit"
    assert len(svc.pending_escalations("ap-8", "g")) == 1


# ---------- 幂等 / 陈旧 ----------

def test_approving_twice_does_not_reopen_twice():
    """双击、事件重放、CLI 手抖各来一次，不能烧掉两轮返工。"""
    svc, io, llm, _ = escalate("ap-9")
    first = svc.approve_escalation("ap-9", "g", by="ou_甲")
    after = llm.counts["w"]

    second = svc.approve_escalation("ap-9", "g", by="ou_甲", seq=first["seq"])

    assert second.get("rejected") == "already_settled", second
    assert llm.counts["w"] == after, "第二次不许再触发一轮重算"
    assert len([r for r in svc.escalations("ap-9", "g")
                if r.get("kind") == "verdict"]) == 1, "也不许留两条裁决"


def test_a_request_from_an_older_round_cannot_be_approved():
    """门已经进了新一轮 = 那笔申请针对的那一版早被重做过了，再批就是拿过期判定改真相源。"""
    svc, io, llm, _ = escalate("ap-10")
    seq = svc.escalations("ap-10", "g")[0]["seq"]
    # owner 先从另一条路把 a 打回，g 因此进入新一轮
    assert click(svc, io, "g", "打回", operator="ou_owner", reopen=["a"]).get("resumed")
    assert svc.pending_escalations("ap-10", "g") == [], "旧申请已随轮次作废"

    out = svc.approve_escalation("ap-10", "g", by="ou_甲", seq=seq)

    assert out.get("skipped") == "stale", out


def test_you_must_say_which_request_when_several_are_waiting():
    """一道门本轮可以挂多笔申请（换一组目标就能再提），省略 seq 时不许瞎猜。"""
    svc, io, llm, _ = escalate("ap-11")
    click(svc, io, "g", "打回", operator="ou_乙", reopen=["b"], comment="整合也不行")
    waiting = svc.pending_escalations("ap-11", "g")
    assert len(waiting) == 2

    out = svc.approve_escalation("ap-11", "g", by="ou_owner")

    assert out.get("rejected") == "ambiguous_escalation", out
    assert sorted(out.get("candidates") or []) == [1, 2]
    assert svc.approve_escalation("ap-11", "g", by="ou_owner", seq=2).get("approved") is True


def test_approving_something_that_was_never_filed():
    svc, io, llm, _ = escalate("ap-12")
    assert svc.approve_escalation("ap-12", "g", by="ou_owner",
                                  seq=99).get("rejected") == "no_such_escalation"
    assert svc.approve_escalation("ap-12", "side",
                                  by="ou_owner").get("rejected") == "no_such_escalation"


# ---------- 拒绝 ----------

def test_a_rejection_closes_the_request_without_reopening_anything():
    svc, io, llm, _ = escalate("rj-1")
    before_status, before_llm = dict(svc.status("rj-1")), llm.counts["w"]

    out = svc.reject_escalation("rj-1", "g", by="ou_甲", comment="材料没问题，别退")

    assert out.get("rejected_request") is True, out
    assert svc.status("rj-1") == before_status, "拒绝就是什么都不该发生"
    assert llm.counts["w"] == before_llm
    assert svc.pending_escalations("rj-1", "g") == []
    assert svc.escalations("rj-1", "g")[0]["effective_status"] == "rejected"


def test_a_rejected_request_cannot_then_be_approved():
    svc, io, llm, _ = escalate("rj-2")
    svc.reject_escalation("rj-2", "g", by="ou_甲", comment="不同意")
    assert svc.approve_escalation("rj-2", "g",
                                  by="ou_owner", seq=1).get("rejected") == "already_settled"


def test_rejection_obeys_the_same_permission_rules():
    svc, io, llm, _ = escalate("rj-3")
    assert svc.reject_escalation("rj-3", "g", by="ou_路人").get("rejected") == "unauthorized_approve"
    assert svc.reject_escalation("rj-3", "g", by=None).get("rejected") == "missing_audit"


# ---------- 谁被告知 ----------

def test_everyone_who_has_a_stake_is_told_what_happened():
    """申请人在等回音，被连累的人即将平白返工，两边都不能靠猜。"""
    svc, io, llm, _ = escalate("nt-1")
    io.notifications.clear()

    svc.approve_escalation("nt-1", "g", by="ou_甲")

    told = {n["target"] for n in io.notifications}
    assert "ou_乙" in told, "申请人要知道批下来了"
    assert "ou_丙" in told, "被连累的人要知道自己为什么突然又要重来"
    body = " ".join(n["text"] for n in io.notifications)
    assert "甲写材料" in body, "说人话用标签，不是节点 id"


def test_a_rejection_is_reported_back_to_the_requester():
    svc, io, llm, _ = escalate("nt-2")
    io.notifications.clear()
    svc.reject_escalation("nt-2", "g", by="ou_甲", comment="材料是齐的")
    told = {n["target"] for n in io.notifications}
    assert "ou_乙" in told
    assert "材料是齐的" in " ".join(n["text"] for n in io.notifications), "理由要带给申请人"


def test_a_failed_notification_never_undoes_the_decision():
    """通知是投影侧动作，权威结论在 checkpointer（与 `_settle_card` 同一条纪律）。"""
    svc, io, llm, _ = escalate("nt-3")

    def boom(**kw):
        raise RuntimeError("飞书 500")

    io.notify = boom
    assert svc.approve_escalation("nt-3", "g", by="ou_甲").get("approved") is True
    assert svc.status("nt-3")["a"] == "pending"


# ---------- 与既有不变量的接缝 ----------

def test_the_approved_reopen_still_obeys_all_or_nothing():
    """同意执行的是**整组 targets**，不是其中跨界的那部分。"""
    svc, io, llm, _ = escalate("iv-1", targets=("a", "b"))
    assert svc.escalations("iv-1", "g")[0]["targets"] == ["a", "b"]

    out = svc.approve_escalation("iv-1", "g", by="ou_owner")

    assert set(out["reopened"]) >= {"a", "b"}, out
    assert svc.status("iv-1")["a"] == "pending" and svc.status("iv-1")["b"] == "pending"


def test_a_request_is_retired_once_the_gate_has_been_answered_anyway():
    """乙 提了申请，没等批下来就自己点了「通过」。那笔申请必须当场作废。

    申请不是裁决：`_escalate` 明说「你手里这张卡仍然有效」，所以这条路是**常态**，不是刁钻构造。
    不作废的话有两个后果：
      ① 驾驶舱一直显示「这道门在等人拍板」，而它早就过去了；
      ② 真有人去点了同意，引擎会试着 resume 一个已经不存在的中断，
         或者更糟：把一道已经放行的门重新掀开。

    轮次那把尺在这里不管用：点「通过」不会让 `attempts` 变化，所以必须另看门的状态。
    """
    svc, io, llm, _ = escalate("iv-2")
    assert len(svc.pending_escalations("iv-2", "g")) == 1

    assert click(svc, io, "g", "通过", operator="ou_乙").get("resumed")
    assert svc.status("iv-2")["g"] == "done"

    assert svc.pending_escalations("iv-2", "g") == [], "门都过去了，不该还挂着待批"
    assert svc.escalations("iv-2", "g")[0]["effective_status"] == "expired"
    out = svc.approve_escalation("iv-2", "g", by="ou_owner", seq=1)
    assert out.get("skipped") == "stale", out
    assert svc.status("iv-2")["a"] == "done", "绝不能把已经过去的门重新掀开"


# ---------- 对抗 review 坐实的三条回归（都实测复现过） ----------

def test_a_rejected_request_can_be_filed_again_and_the_requester_hears_back():
    """驳回**不等于**从此不许再提。

    去重判据原本自己写了一遍、拿记录里的 `status == "pending"` 当活性，而那个字面量是落库
    那一刻冻住的、永远 pending（追加型 channel 没有 UPDATE）。于是驳回之后申请人再点同一个
    打回：命中 duplicate 分支 → 卡不变 → `_ack_escalation` 的幂等键与上次逐字相同又被
    `_once` 吞掉 → **他一个字都收不到，也永远提不了这笔申请**，而审批人那边查无此事。
    """
    svc, io, llm, _ = escalate("rf-1")
    assert svc.reject_escalation("rf-1", "g", by="ou_甲", comment="不同意").get("rejected_request")
    assert svc.pending_escalations("rf-1", "g") == []
    io.notifications.clear()

    again = click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="材料不全")

    assert again.get("duplicate") is not True, again
    assert again["seq"] == 2, "是新的一笔，不是把旧的翻出来"
    assert len(svc.pending_escalations("rf-1", "g")) == 1, "审批人这边要能看见"
    assert any(n["target"] == "ou_乙" for n in io.notifications), "申请人必须收到回执"


def test_the_same_click_twice_in_a_row_is_still_deduped():
    """修去重的时候别把去重修没了：同一轮、同一人、同一组目标仍然只留一笔。"""
    svc, io, llm, _ = escalate("rf-2")
    again = click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="材料不全")
    assert again.get("duplicate") is True, again
    assert len(svc.pending_escalations("rf-2", "g")) == 1


def test_approving_when_the_budget_is_gone_says_so_instead_of_claiming_success():
    """打回预算耗尽时，`reopen_resets` 把门标 blocked 并**一个节点都不重置**。

    此前这里照样回 `approved: True`，并且告诉申请人「X 已退回重做」、告诉旁支负责人
    「你也被卷进这一轮返工」，而两人的节点一动没动、谁也不会收到新单。整条流程停在
    blocked 等 unblock，只有发起人从 ADR-029 那条独立通知里知道。
    **批准是真的，落地不是**，这两件事必须分开报。
    """
    svc, io, llm, store = tight_budget("bd-1")
    click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="还是不行")
    io.notifications.clear()

    out = svc.approve_escalation("bd-1", "g", by="ou_甲")

    assert out.get("approved") is True and out.get("landed") is False, out
    assert out["reopened"] == []
    assert svc.status("bd-1")["a"] == "done", "什么都没退回"
    told = {n["target"]: n["text"] for n in io.notifications}
    assert "ou_乙" in told and "什么都没能退回" in told["ou_乙"], told
    assert "ou_丙" not in told, "没人被卷进返工，就别去惊动旁支负责人"


def test_a_blocked_gate_stops_showing_requests_as_waiting_for_a_verdict():
    """`blocked` 之后 `attempts` 一动不动，「轮次已过」那把尺不触发；门也没被答复。

    三条出局判据一条都不命中的后果：同轮没拍板的申请**永远显示待批**，而同意那边按
    「还有没有挂起中断」判、blocked 之后没有中断，于是每次同意都回 stale，只有 reject
    出得去。驾驶舱上是一笔批不动也退不掉的僵尸申请。
    """
    svc, io, llm, store = tight_budget("bd-2")
    click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="第一笔")
    click(svc, io, "g", "打回", operator="ou_乙", reopen=["b"], comment="第二笔")
    assert len(svc.pending_escalations("bd-2", "g")) == 2

    svc.approve_escalation("bd-2", "g", by="ou_甲", seq=1)      # 这一下把门打成 blocked

    assert svc.status("bd-2")["g"] == "blocked"
    assert svc.pending_escalations("bd-2", "g") == [], "门都 blocked 了，别再挂着待批"
    assert [r["effective_status"] for r in svc.escalations("bd-2", "g")
            if r.get("kind", "request") == "request"] == ["approved", "expired"]


# ---------- 补三处零覆盖（变异测试指出来的） ----------

def test_the_token_ruler_is_what_saves_a_request_nobody_could_be_notified_about():
    """`_can_approve` 的**第一把尺**（令牌求交）此前零覆盖：删掉它测试全绿。

    它不是冗余：通知全失败时 `notified` 是空的，第二把尺什么都认不出来，只剩令牌这把
    尺救得了这笔申请。没有它，一次飞书抖动就让申请永久无人可批，死局原样复发。
    """
    svc, io, llm, store = run_to_gates(CROSS, "cov-1")

    def boom(**kw):
        raise RuntimeError("飞书 500")

    io.send_card = boom          # 审批卡一张都发不出去（ADR-043 之后这才是通知审批人的路）
    click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="材料不全")
    rec = svc.escalations("cov-1", "g")[0]
    assert rec["notified"] == [] and rec["notify_failed"] == ["ou_owner", "ou_甲"]

    assert svc.approve_escalation("cov-1", "g", by="ou_甲").get("approved") is True
    assert svc.status("cov-1")["a"] == "pending"


def test_a_second_round_request_gets_its_own_verdict_notice():
    """整条「第二轮再提一笔、再批一次」的生命周期此前零覆盖。

    裁决通知的幂等键少写 `:{seq}` 也没有测试会红，而 `_tell` 走 `_once`（同一个键一辈子
    只发一次），于是申请人从第二笔起再也收不到自己申请的结果。
    """
    svc, io, llm, store = run_to_gates(CROSS, "rd-1")
    click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="材料不全")
    assert svc.approve_escalation("rd-1", "g", by="ou_甲").get("approved") is True

    redo_a(svc, io, store, "rd-1")                       # 甲重做交卷，g 进第 1 轮
    second = click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="还是不行")
    assert second["seq"] == 2, second
    io.notifications.clear()

    assert svc.approve_escalation("rd-1", "g", by="ou_甲").get("approved") is True
    assert any(n["target"] == "ou_乙" and "第 2 笔" in n["text"] for n in io.notifications), \
        "第二笔的裁决通知被第一笔的幂等键吞掉了"
