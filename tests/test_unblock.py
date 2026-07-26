"""`blocked` 终态的解除通道（ADR-029 的恢复路径）。

ADR-029 让超打回预算的门进 `blocked` 并通知发起人「可改要素 / 改图后重试」，但这条路
在代码里原本不存在：blocked 门自己过不了冻结线（只有 pending 可改）、它的上游是 done
也过不了、`reopen_resets` 每次都把它重新算成 blocked。于是实例永久停死。

这里钉死解除通道的每一条不变量：人显式触发 / 必审计 / 额度有上界 / 回到 pending 后
一切照常规走 / reopen 目标过引擎侧合法域 / 第二次卡死照样有人被通知。
"""
import pytest

from larkflow.app import build_service
from larkflow.engine.gates import (
    BLOCKED,
    MAX_UNBLOCK_GRANTS,
    effective_reopen_budget,
    reopen_resets,
    unblock_resets,
)
from larkflow.io import FakeDeliverableStore
from larkflow.io.deliverable import Deliverable
from larkflow.io.events import CARD_ACTION, TASK_UPDATE
from support import CountingLLM, card_target

# 人写稿 → 机检 → 收口。机检要素写死，人写不出来就会一路打到 blocked。
HUMAN_LOOP = [
    {"id": "draft", "label": "起草", "executor": "human", "role": "produce", "deps": [],
     "assignee_role": "起草人", "signal": "task_complete", "deliverable": {"region": "whole"}},
    {"id": "check", "label": "机检", "executor": "tool", "role": "gate", "deps": ["draft"],
     "approval_policy": "auto", "reopen_budget": 1,
     "tool": {"kind": "format_check", "args": {"required": ["价款"]}}},
    {"id": "close", "label": "收口", "executor": "tool", "role": "produce", "deps": ["check"],
     "deliverable": {"region": "whole"}, "tool": {"kind": "summarize_links", "args": {}}},
]

# AI 写稿 + 永不通过的机检；旁边一个跟它毫无关系的人一直挂着（并行分支不许被误伤）
AUTO_LOOP = [
    {"id": "draft", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
     "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "chk", "label": "机检", "executor": "tool", "role": "gate", "deps": ["draft"],
     "approval_policy": "auto", "reopen_budget": 1,
     "tool": {"kind": "format_check", "args": {"required": ["永不出现"]}}},
    {"id": "idle", "label": "别人的活", "executor": "human", "role": "produce", "deps": [],
     "assignee_role": "路人", "signal": "task_complete", "deliverable": {"region": "whole"}},
]

# 人工门被人反复打回，也会打光预算
HUMAN_GATE = [
    {"id": "draft", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
     "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "g", "label": "老板审", "executor": "human", "role": "gate", "deps": ["draft"],
     "assignee_role": "老板", "signal": "card_action", "approval_policy": "single",
     "reopen_budget": 1},
]

# 两道门共享上游：一道打光预算停死时，另一道还挂在别人手上
TWO_GATES = [
    {"id": "draft", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
     "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "g1", "label": "财务审", "executor": "human", "role": "gate", "deps": ["draft"],
     "assignee_role": "财务", "signal": "card_action", "approval_policy": "single",
     "reopen_budget": 1},
    {"id": "g2", "label": "法务审", "executor": "human", "role": "gate", "deps": ["draft"],
     "assignee_role": "法务", "signal": "card_action", "approval_policy": "single",
     "reopen_budget": 5},
    {"id": "tail", "label": "收口", "executor": "tool", "role": "produce", "deps": ["g1", "g2"],
     "deliverable": {"region": "whole"}, "tool": {"kind": "noop"}},
]


def human_loop(iid="ub-1"):
    store = FakeDeliverableStore()
    svc, io = build_service(HUMAN_LOOP, deliverables=store)
    svc.start(instance_id=iid, reporter="ou_owner", inputs={})
    return svc, io, store


def auto_loop(iid="ub-a"):
    llm = CountingLLM({"w": "怎么写都过不了"})
    svc, io = build_service(AUTO_LOOP, llm=llm, deliverables=FakeDeliverableStore())
    svc.start(instance_id=iid, reporter="ou_owner", inputs={})
    return svc, io, llm


def write_and_finish(svc, io, store, iid, node_id, text):
    """模拟人在飞书文档里写好内容并点「完成」，走与真栈相同的事件入口。"""
    p = next(p for p in svc.pending(iid) if p["node_id"] == node_id)
    store.overwrite(Deliverable.from_dict(p["deliverable"]), content=text)
    guid = next(t["guid"] for t in reversed(list(io.tasks.values()))
                if t["summary"] == p["label"])
    return svc.resume_from_event({"key": TASK_UPDATE, "event": {
        "task_guid": guid, "event_types": ["task_completed_update"]}})


def click(svc, io, node_id, label, *, operator=None, **ov):
    """operator 默认 = 收到这张卡的人（打回权限层 ADR-023 据此判身份）。"""
    return svc.resume_from_event({
        "key": CARD_ACTION, "operator_id": operator or card_target(io, node_id),
        "action_value": dict(io.button_value(node_id, label), **ov)})


def blocked_notes(io, label):
    return [n for n in io.notifications if label in n["text"] and "等人介入" in n["text"]]


# ---------- 端到端：解除后真能跑完 ----------

def test_unblock_puts_the_gate_back_on_the_frontier_and_the_project_can_finish():
    """打到 blocked → 人解除 → 门回 pending → 受控活图能改它 → 上游改好后真跑完。

    「改图后重试」这条 ADR-029 承诺的路径，靠的正是「解除后回 pending」：门自己回到
    冻结线以内，于是常规的 edit_graph 就够了，不需要在冻结线上开后门。
    """
    svc, io, store = human_loop()
    write_and_finish(svc, io, store, "ub-1", "draft", "随便写点，没有那个要素")
    write_and_finish(svc, io, store, "ub-1", "draft", "还是没有")

    assert svc.status("ub-1")["check"] == BLOCKED
    assert len(blocked_notes(io, "机检")) == 1

    res = svc.unblock("ub-1", "check", by="ou_owner", reason="机检要素配错了", reopen=["draft"])
    assert res["unblocked"] == "check" and res["granted"] == 1
    assert svc.status("ub-1")["check"] == "pending"        # 回到执行前沿，不再是终态
    assert {p["node_id"] for p in svc.pending("ub-1")} == {"draft"}
    # 人明确点名要重做哪一段，就不该再烧一次打回预算（那是门自己判打回时才计的）
    assert svc._values("ub-1")["reopen_counts"]["check"] == 1
    # 再点一次「解除」（前端双击 / 运维重放）不能再花掉一份额度
    again = svc.unblock("ub-1", "check", by="ou_owner", reason="手抖又点了一次")
    assert again["rejected"] == "not_blocked"
    assert len(svc.unblock_log("ub-1", "check")) == 1

    # 门回了 pending，于是它自己也能被受控活图改（改前 blocked 是改不动的）
    svc.edit_graph("ub-1", [{"op": "update_node", "id": "check", "set": {
        "tool": {"kind": "format_check", "args": {"required": ["期限"]}}}}], by="ou_owner", reason="测试改图")

    write_and_finish(svc, io, store, "ub-1", "draft", "一、期限：12 个月。")

    status = svc.status("ub-1")
    assert status == {"draft": "done", "check": "done", "close": "done"}, status
    assert svc.blocked("ub-1") == []


def test_a_blocked_gate_cannot_be_edited_before_it_is_unblocked():
    """冻结线一寸都不许放宽：blocked 门本身不可改，解除通道是唯一出口。"""
    from larkflow.engine.livegraph import GraphEditError

    svc, io, store = human_loop("ub-frozen")
    write_and_finish(svc, io, store, "ub-frozen", "draft", "不合格")
    write_and_finish(svc, io, store, "ub-frozen", "draft", "还是不合格")
    assert svc.status("ub-frozen")["check"] == BLOCKED

    with pytest.raises(GraphEditError, match="冻结线"):
        svc.edit_graph("ub-frozen", [{"op": "update_node", "id": "check",
                                      "set": {"label": "改个名"}}], by="ou_owner", reason="测试改图")


# ---------- 合法域：不信调用方给的目标 ----------

def test_unblock_rejects_reopen_targets_outside_the_gates_ancestors():
    """`reopen` 是调用方给的，必须过引擎权威侧的合法域校验（⊆ 传递祖先）。"""
    svc, io, llm = auto_loop("ub-ill")
    before_counts = dict(svc._values("ub-ill")["reopen_counts"])
    before_attempts = dict(svc._values("ub-ill")["attempts"])

    res = svc.unblock("ub-ill", "chk", by="ou_owner", reason="再试一次", reopen=["idle"])

    assert res["rejected"] == "illegal_reopen" and res["illegal"] == ["idle"]
    # 拒绝要干净：状态、预算计数、轮次、审计记录一个都不许动
    assert svc.status("ub-ill")["chk"] == BLOCKED
    assert svc._values("ub-ill")["reopen_counts"] == before_counts
    assert svc._values("ub-ill")["attempts"] == before_attempts
    assert svc.unblock_log("ub-ill") == {}
    assert llm.counts["w"] == 2


def test_unblock_accepts_a_legal_ancestor_and_recomputes_it():
    svc, io, llm = auto_loop("ub-ok")
    assert llm.counts["w"] == 2

    svc.unblock("ub-ok", "chk", by="ou_owner", reason="上游要素补齐了", reopen=["draft"])

    assert llm.counts["w"] > 2                      # 指定的祖先真的重算了


# ---------- 额度：有限且自身有上界 ----------

def test_unblock_grants_are_capped_and_the_owner_is_told_when_they_run_out():
    """grant 是追加预算不是重置计数；追加次数本身还得有硬上界，否则等于没有预算。"""
    svc, io, llm = auto_loop("ub-cap")

    for i in range(MAX_UNBLOCK_GRANTS):
        res = svc.unblock("ub-cap", "chk", by="ou_owner", reason=f"第 {i+1} 次试")
        assert res["unblocked"] == "chk", res
        assert res["grants_left"] == MAX_UNBLOCK_GRANTS - i - 1
        assert svc.status("ub-cap")["chk"] == BLOCKED   # 还是过不了，又停下了

    res = svc.unblock("ub-cap", "chk", by="ou_owner", reason="再来一次")
    assert res["rejected"] == "unblock_exhausted"
    assert res["grants_used"] == MAX_UNBLOCK_GRANTS
    assert any("解除额度" in n["text"] for n in io.notifications)   # 拒绝了也得有人知道
    assert svc.status("ub-cap")["chk"] == BLOCKED


def test_a_single_grant_cannot_hand_out_an_unbounded_budget():
    """单次 grant 也要有上界：否则 grant=10**9 就把预算机制原地废掉。"""
    svc, io, llm = auto_loop("ub-big")
    res = svc.unblock("ub-big", "chk", by="ou_owner", reason="给多点", grant=10 ** 9)
    assert res["granted"] < 10 ** 9
    assert res["requested"] == 10 ** 9
    assert svc.status("ub-big")["chk"] == BLOCKED      # 有界 → 还是会停下来


def test_unblock_extends_the_budget_instead_of_wiping_the_counter():
    svc, io, llm = auto_loop("ub-ext")
    assert svc._values("ub-ext")["reopen_counts"]["chk"] == 1

    svc.unblock("ub-ext", "chk", by="ou_owner", reason="再试")

    # 计数只增不减（历史不改），是预算被追高了才让它又跑了一轮
    assert svc._values("ub-ext")["reopen_counts"]["chk"] == 2


# ---------- 审计 ----------

def test_unblock_is_audited_and_keeps_every_earlier_attempt():
    """谁 / 何时 / 为什么 / 给了多少，全落权威 state；历史尝试一条不许被覆盖。"""
    svc, io, store = human_loop("ub-aud")
    write_and_finish(svc, io, store, "ub-aud", "draft", "第一稿，不合格")
    write_and_finish(svc, io, store, "ub-aud", "draft", "第二稿，还是不合格")
    handle_before = svc.outputs("ub-aud")["draft"]["deliverable"]
    attempts_before = dict(svc._values("ub-aud")["attempts"])

    svc.unblock("ub-aud", "check", by="ou_boss", reason="标准定错了", grant=2, reopen=["draft"])

    log = svc.unblock_log("ub-aud", "check")
    assert len(log) == 1
    rec = log[0]
    assert rec["by"] == "ou_boss" and rec["reason"] == "标准定错了"
    assert rec["grant"] == 2 and rec["reopen"] == ["draft"] and rec["at"]

    # 历史：轮次只增不减、交付物 handle 不变、两稿都还在（飞书原生版本 = 投影侧证据）
    now = svc._values("ub-aud")["attempts"]
    assert all(now[k] >= v for k, v in attempts_before.items())
    assert now["draft"] > attempts_before["draft"]
    assert svc.outputs("ub-aud")["draft"]["deliverable"] == handle_before
    versions = store.versions(Deliverable.from_dict(handle_before))
    assert "第一稿，不合格" in versions and "第二稿，还是不合格" in versions

    # 第二次解除追加一条新记录，绝不覆盖第一条
    write_and_finish(svc, io, store, "ub-aud", "draft", "第三稿，仍不合格")
    write_and_finish(svc, io, store, "ub-aud", "draft", "第四稿，仍不合格")
    write_and_finish(svc, io, store, "ub-aud", "draft", "第五稿，仍不合格")
    assert svc.status("ub-aud")["check"] == BLOCKED
    svc.unblock("ub-aud", "check", by="ou_owner", reason="再给一次", reopen=["draft"])
    log = svc.unblock_log("ub-aud", "check")
    assert [r["by"] for r in log] == ["ou_boss", "ou_owner"]
    assert log[0]["reason"] == "标准定错了"


# ---------- 拒绝路径 ----------

def test_unblock_refuses_a_node_that_is_not_blocked():
    svc, io, llm = auto_loop("ub-no")
    res = svc.unblock("ub-no", "idle", by="ou_owner", reason="想推一把")
    assert res["rejected"] == "not_blocked" and res["status"] == "pending"
    assert svc.unblock_log("ub-no") == {}


def test_unblock_refuses_unknown_instances_and_nodes_without_raising():
    svc, io, llm = auto_loop("ub-404")
    assert svc.unblock("没这个实例", "chk", by="o", reason="r")["rejected"] == "no_such_instance"
    assert svc.unblock("ub-404", "没这个节点", by="o", reason="r")["rejected"] == "no_such_node"


def test_unblock_demands_who_and_why():
    """审计是不变量：说不清谁点的、为什么，就不给解除。"""
    svc, io, llm = auto_loop("ub-audit")
    assert svc.unblock("ub-audit", "chk", by="", reason="r")["rejected"] == "missing_audit"
    assert svc.unblock("ub-audit", "chk", by="ou_o", reason="  ")["rejected"] == "missing_audit"
    assert svc.status("ub-audit")["chk"] == BLOCKED


# ---------- 通知：第二次卡死也得有人知道 ----------

def test_the_owner_is_notified_again_when_the_gate_blocks_a_second_time():
    """blocked 通知的幂等键含解除次数：否则第二次卡死被幂等吞掉，项目静静躺死。"""
    svc, io, llm = auto_loop("ub-note")
    assert len(blocked_notes(io, "机检")) == 1

    svc.unblock("ub-note", "chk", by="ou_owner", reason="再试")

    assert svc.status("ub-note")["chk"] == BLOCKED
    assert len(blocked_notes(io, "机检")) == 2


# ---------- 并行：不许误伤别的分支 ----------

def test_unblocking_one_gate_does_not_disturb_a_sibling_waiting_on_someone_else():
    """旁边那个人手里的待办必须原样有效：不重复派单，也不因为换了中断 id 就失效。"""
    svc, io, llm = auto_loop("ub-par")
    idle_tasks = [t for t in io.tasks.values() if t["summary"] == "别人的活"]
    assert len(idle_tasks) == 1

    svc.unblock("ub-par", "chk", by="ou_owner", reason="再试")

    assert len([t for t in io.tasks.values() if t["summary"] == "别人的活"]) == 1
    assert [p["node_id"] for p in svc.pending("ub-par")] == ["idle"]
    # 他手里那张旧待办仍然能用（推进换了中断 id，靠 remap 续上）
    res = svc.resume_from_event({"key": TASK_UPDATE, "event": {
        "task_guid": idle_tasks[0]["guid"], "event_types": ["task_completed_update"]}})
    assert "resumed" in res
    assert svc.status("ub-par")["idle"] == "done"


def test_unblocking_a_human_gate_sends_the_reviewer_a_fresh_card():
    """解除 = 新一轮：人必须收到新卡，旧卡必须失效（否则会把对旧版的裁决记成放行新版）。"""
    llm = CountingLLM({"w": "正文"})
    svc, io = build_service(HUMAN_GATE, llm=llm, deliverables=FakeDeliverableStore())
    svc.start(instance_id="ub-hg", reporter="ou_owner", inputs={})

    click(svc, io, "g", "打回", comment="不行")          # 第 1 次打回：预算内
    stale_card = io.button_value("g", "通过")            # 第 2 轮那张（解除后应失效）
    click(svc, io, "g", "打回", comment="还是不行")      # 打光预算 → blocked
    assert svc.status("ub-hg")["g"] == BLOCKED
    assert len([c for c in io.cards.values()
                if c["buttons"][0]["action_value"]["node_id"] == "g"]) == 2

    svc.unblock("ub-hg", "g", by="ou_owner", reason="老板说再看一眼")

    cards = [c for c in io.cards.values() if c["buttons"][0]["action_value"]["node_id"] == "g"]
    assert len(cards) == 3, "解除后老板没收到新卡"
    assert svc.resume_from_event({"key": CARD_ACTION, "action_value": stale_card,
                                  "operator_id": "ou_op"}).get("skipped") == "stale"
    assert svc.status("ub-hg")["g"] != "done"

    assert "resumed" in click(svc, io, "g", "通过")      # 用新卡放行，实例走完
    assert svc.status("ub-hg")["g"] == "done"


def test_unblocking_with_reopen_gives_the_bystander_gate_a_fresh_card():
    """解除时连带解冻上游，会把还在等的旁观者一起卷进新一轮：他必须收到新卡。

    不给的话就是最坏的情况：法务从未见过新一版，引擎却在权威 state 里记下「法务放行
    新版」（他手里的旧卡被重绑到了新一轮的中断上）。这与打回卷入旁观者是同一个坑，
    只是触发者从 gate 换成了人显式解除，故它同样不许走「重绑旧中断」那条路。
    """
    llm = CountingLLM({"w": "正文"})
    svc, io = build_service(TWO_GATES, llm=llm, deliverables=FakeDeliverableStore())
    svc.start(instance_id="ub-by", reporter="ou_owner", inputs={})

    # 由 owner 开这两枪：财务自己打回共享上游会连累法务，得走 escalation（ADR-023 ②③，
    # 见 test_permissions）。这里要的是「g1 打光预算停死」这个现场，与谁开枪无关。
    click(svc, io, "g1", "打回", operator="ou_owner", comment="第一次")   # 预算内：两人进第 2 轮
    click(svc, io, "g1", "打回", operator="ou_owner", comment="第二次")   # 打光预算 → g1 blocked
    assert svc.status("ub-by")["g1"] == BLOCKED
    assert [p["node_id"] for p in svc.pending("ub-by")] == ["g2"]   # 法务还在等
    g2_stale = io.button_value("g2", "通过")             # 法务手里第 2 轮那张
    assert llm.counts["w"] == 2

    svc.unblock("ub-by", "g1", by="ou_owner", reason="上游重写一版再审", reopen=["draft"])

    assert llm.counts["w"] == 3                          # 上游真的重算了
    g2_cards = [c for c in io.cards.values()
                if c["buttons"][0]["action_value"]["node_id"] == "g2"]
    assert len(g2_cards) == 3, "法务被卷进新一轮却没收到新卡"
    assert svc.resume_from_event({"key": CARD_ACTION, "action_value": g2_stale,
                                  "operator_id": "ou_法务"}).get("skipped") == "stale"
    assert svc.status("ub-by")["g2"] != "done"           # 绝不能把对旧版的裁决记成放行新版


# ---------- 纯函数 ----------

def test_granted_budget_extends_the_gates_budget():
    node = {"id": "g", "deps": ["a"], "role": "gate", "reopen_budget": 1}
    dag = [{"id": "a", "deps": []}, node]
    grants = {"g": [{"grant": 2}]}
    assert effective_reopen_budget(node) == 1
    assert effective_reopen_budget(node, grants) == 3
    assert reopen_resets(dag, {"g": "failed"}, {}, {"g": 1}) == {"g": BLOCKED}
    assert reopen_resets(dag, {"g": "failed"}, {}, {"g": 1}, grants) == {"a": "pending", "g": "pending"}
    assert reopen_resets(dag, {"g": "failed"}, {}, {"g": 3}, grants) == {"g": BLOCKED}


def test_unblock_resets_cover_the_gate_and_the_chosen_ancestors_downstream():
    dag = [{"id": "a", "deps": []}, {"id": "b", "deps": ["a"]},
           {"id": "g", "deps": ["b"], "role": "gate"}, {"id": "z", "deps": ["g"]}]
    assert unblock_resets(dag, "g", None) == {"g": "pending"}
    assert unblock_resets(dag, "g", ["a"]) == {
        "a": "pending", "b": "pending", "g": "pending", "z": "pending"}
