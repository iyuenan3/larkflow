"""并行 / 打回 / 故障隔离的回归：每条都对应一个实测复现过的缺陷。

这些缺陷有个共同点：合同图那条「顺序刚好合适」的 e2e 全都测不出来。多方并行接力是
产品的定义形态，不是边角场景。
"""
import io as _io
import json
import time

import pytest

from larkflow.app import build_contract_service, build_service
from larkflow.config import RoleError, RoleResolver
from larkflow.engine.gates import BLOCKED, reopen_resets
from larkflow.io import FakeDeliverableStore
from larkflow.io.events import EventPump
from larkflow.io.events import CARD_ACTION, TASK_UPDATE
from support import CountingLLM

INPUTS = {"甲方": "A", "乙方": "B", "价款": "30万", "期限": "12个月"}


def contract():
    llm = CountingLLM({"writer": "商务条款", "legal": "法律条款", "editor": "合并稿"})
    store = FakeDeliverableStore()
    svc, io = build_contract_service(llm=llm, deliverables=store)
    return svc, io, llm, store


def card(io, node, label, **ov):
    return {"key": CARD_ACTION, "action_value": dict(io.button_value(node, label), **ov),
            "operator_id": "ou_op"}


# ---------- 并行门：打回必须当场落地 ----------

def test_reopen_lands_immediately_even_if_a_sibling_gate_has_not_answered():
    """财务先打回、法务还没点：上游必须当场重算并通知，不能等在另一个人身上。"""
    svc, io, llm, store = contract()
    svc.start(instance_id="r-1", reporter="ou_owner", inputs=INPUTS)
    assert {p["node_id"] for p in svc.pending("r-1")} == {"finance_gate", "legal_gate"}
    cards_before = len(io.cards)

    svc.resume_from_event(card(io, "finance_gate", "打回", comment="账期不对"))

    assert llm.counts["writer"] == 2                     # 商务稿当场重算
    assert llm.counts["legal"] == 1                      # 法律支不受牵连
    assert len(io.cards) > cards_before                  # 财务收到新一轮门禁卡
    assert svc.status("r-1").get("legal_gate", "pending") != "done"   # 法务仍在等他自己点
    assert "legal_gate" in {p["node_id"] for p in svc.pending("r-1")}


def test_editing_the_graph_does_not_swallow_a_verdict_made_moments_ago():
    """改图会落新 checkpoint，在飞的写入若不保值就会被静默丢掉（人点的裁决凭空消失）。"""
    svc, io, llm, store = contract()
    svc.start(instance_id="r-2", reporter="ou_owner", inputs=INPUTS)
    svc.resume_from_event(card(io, "finance_gate", "打回", comment="账期不对"))
    verdict = svc.outputs("r-2")["finance_gate"]
    assert verdict["comment"] == "账期不对"

    svc.edit_graph("r-2", [{"op": "add_node", "node": {
        "id": "audit", "label": "复盘", "executor": "llm", "role": "produce", "deps": ["close"],
        "prompt": "p", "model_role": "editor", "deliverable": {"region": "whole"}}}])

    assert svc.outputs("r-2")["finance_gate"] == verdict   # 裁决与意见都还在
    assert llm.counts["writer"] == 2                        # 重算也没被回滚


def test_pending_does_not_report_people_who_already_answered():
    svc, io, llm, store = contract()
    svc.start(instance_id="r-3", reporter="ou_owner", inputs=INPUTS)
    svc.resume_from_event(card(io, "legal_gate", "通过"))
    assert [p["node_id"] for p in svc.pending("r-3")] == ["finance_gate"]


# ---------- 打回意见必须进重算的输入 ----------

def test_reopen_feedback_reaches_both_the_ai_and_the_person():
    """否则重算是空转：同一份 prompt 重跑，真 LLM（temperature=0）会一字不差再生成一遍。"""
    svc, io, llm, store = contract()
    svc.start(instance_id="r-4", reporter="ou_owner", inputs=INPUTS)
    svc.resume_from_event(card(io, "legal_gate", "通过"))
    svc.resume_from_event(card(io, "finance_gate", "打回", comment="账期与价款不符"))

    second = llm.prompt_of("writer", 1)
    assert llm.prompt_of("writer", 0) != second
    assert "账期与价款不符" in second            # 意见进了重算的 prompt
    assert "商务条款 v1" in second               # 上一稿也回喂了，让它改而不是重写

    # 人侧同理：新一轮门禁卡带着上一轮意见
    svc.resume_from_event(card(io, "finance_gate", "通过"))
    waiting = {p["node_id"]: p for p in svc.pending("r-4")}
    assert waiting["finalize"]["feedback"] == []          # 定稿这一轮没有被打回过


def test_feedback_shows_up_for_a_reopened_human_node():
    svc, io, llm, store = contract()
    svc.start(instance_id="r-5", reporter="ou_owner", inputs=INPUTS)
    svc.resume_from_event(card(io, "legal_gate", "通过"))
    svc.resume_from_event(card(io, "finance_gate", "通过"))
    fin = next(p for p in svc.pending("r-5") if p["node_id"] == "finalize")
    store.overwrite(__import__("larkflow.io.deliverable", fromlist=["Deliverable"])
                    .Deliverable.from_dict(fin["deliverable"]), content="缺东西")
    guid = next(t["guid"] for t in io.tasks.values() if t["summary"] == "负责人定稿")
    svc.resume_from_event({"key": TASK_UPDATE, "event": {
        "task_guid": guid, "event_types": ["task_completed_update"]}})

    # 机检不过 → 打回定稿；这一轮的任务描述必须写清为什么被打回
    again = next(p for p in svc.pending("r-5") if p["node_id"] == "finalize")
    assert again["feedback"] and "缺要素" in again["feedback"][0]["comment"]
    desc = [t["description"] for t in io.tasks.values() if t["summary"] == "负责人定稿"][-1]
    assert "缺要素" in desc


# ---------- 打回预算：auto 门不能无限重算 ----------

def test_auto_gate_that_never_passes_gets_blocked_instead_of_spinning():
    """AI 未必满足得了机检。没有预算就会一路重算到 recursion limit，实例停在半截没人知道。"""
    dag = [
        {"id": "draft", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "p", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "check", "label": "机检", "executor": "tool", "role": "gate", "deps": ["draft"],
         "approval_policy": "auto", "reopen_budget": 2,
         "tool": {"kind": "format_check", "args": {"required": ["永远不会出现的字样"]}}},
    ]
    llm = CountingLLM({"w": "怎么写都过不了"})
    svc, io = build_service(dag, llm=llm)
    svc.start(instance_id="blk-1", reporter="ou_owner", inputs={})

    assert svc.status("blk-1")["check"] == BLOCKED
    assert svc.blocked("blk-1") == ["check"]
    assert llm.counts["w"] == 3                       # 首跑 + 2 次预算内重算，然后停
    assert any("等人介入" in n["text"] for n in io.notifications)   # 卡死了有人被通知


def test_reopen_budget_is_per_gate_and_configurable():
    node = {"id": "g", "deps": ["a"], "role": "gate", "reopen_budget": 1}
    dag = [{"id": "a", "deps": []}, node]
    assert reopen_resets(dag, {"g": "failed"}, {}, {"g": 0}) == {"a": "pending", "g": "pending"}
    assert reopen_resets(dag, {"g": "failed"}, {}, {"g": 1}) == {"g": BLOCKED}


# ---------- 派单故障隔离 ----------

class FlakyIO:
    """给某个角色派单必失败，其余正常。"""

    def __init__(self, inner, bad_role: str):
        self.inner, self.bad_role = inner, bad_role
        self.cards, self.tasks, self.notifications = inner.cards, inner.tasks, inner.notifications

    def _guard(self, target):
        if self.bad_role in str(target):
            raise RuntimeError("invalid open_id")

    def create_task(self, *, assignee, **kw):
        self._guard(assignee)
        return self.inner.create_task(assignee=assignee, **kw)

    def send_card(self, *, target, **kw):
        self._guard(target)
        return self.inner.send_card(target=target, **kw)

    def notify(self, *, target, **kw):
        return self.inner.notify(target=target, **kw)

    def button_value(self, *a, **kw):
        return self.inner.button_value(*a, **kw)


def test_one_bad_assignee_does_not_block_everyone_elses_card():
    from larkflow.io import MockLarkIO

    llm = CountingLLM({"writer": "商务条款", "legal": "法律条款", "editor": "合并稿"})
    flaky = FlakyIO(MockLarkIO(), bad_role="财务")
    svc, io = build_contract_service(llm=llm, io=flaky, deliverables=FakeDeliverableStore())
    svc.start(instance_id="f-1", reporter="ou_owner", inputs=INPUTS)

    sent = {c["target"] for c in flaky.cards.values()}
    assert "ou_法务" in sent                       # 法务照常收到
    errs = svc.provision_errors["f-1"]
    assert [e["node_id"] for e in errs] == ["finance_gate"]   # 失败被记下来，不是被吞掉

    # 修好之后 reconcile 能把漏掉的补上（幂等，不会给法务重发）
    flaky.bad_role = "没人"
    svc.reconcile("f-1")
    assert {c["target"] for c in flaky.cards.values()} == {"ou_法务", "ou_财务"}
    assert len([c for c in flaky.cards.values() if c["target"] == "ou_法务"]) == 1


# ---------- 角色解析：真栈不许伪造 open_id ----------

def test_strict_resolver_refuses_to_invent_open_ids():
    r = RoleResolver({"财务": "ou_fin"}, strict=True)
    assert r.resolve("财务") == "ou_fin"
    with pytest.raises(RoleError, match="法务"):
        r.resolve("法务")
    with pytest.raises(RoleError, match="法务"):
        r.validate_coverage([{"id": "a", "assignee_role": "法务"}])


def test_roles_can_be_configured_with_a_json_env_since_chinese_names_are_not_valid_env_keys():
    r = RoleResolver.from_env({"LARKFLOW_ROLES": json.dumps({"财务": "ou_fin", "法务": "ou_leg"})})
    assert r.resolve("财务") == "ou_fin" and r.resolve("法务") == "ou_leg"


# ---------- 事件泵：不能因为一条事件就永久变聋 ----------

class FakeProc:
    def __init__(self, lines, stderr_lines=("[event] ready event_key=k\n",)):
        self.stdout = _io.StringIO("".join(lines))
        self.stderr = _io.StringIO("".join(stderr_lines))
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_a_failing_handler_does_not_kill_the_only_inbound_channel():
    seen, errors = [], []

    def on_event(key, obj):
        seen.append(obj["seq"])
        if obj["seq"] == 1:
            raise RuntimeError("resume 里炸了")

    lines = [json.dumps({"seq": i}) + "\n" for i in range(5)]
    pump = EventPump(on_event, on_error=lambda where, exc: errors.append(where), max_restarts=0)
    pump._spawn = lambda key: FakeProc(lines)
    pump.start(["k"])
    for _ in range(50):
        if len(seen) == 5:
            break
        time.sleep(0.01)
    pump.stop()

    assert seen == [0, 1, 2, 3, 4], seen        # 出错那条之后的事件照常处理
    assert any("on_event" in e for e in errors)  # 但故障被喊出来了


# ---------- 第二轮对抗验证挖出的回归（都实测复现过） ----------

SHARED = [
    {"id": "draft", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
     "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "g1", "label": "财务审", "executor": "human", "role": "gate", "deps": ["draft"],
     "assignee_role": "财务", "signal": "card_action", "approval_policy": "single"},
    {"id": "g2", "label": "法务审", "executor": "human", "role": "gate", "deps": ["draft"],
     "assignee_role": "法务", "signal": "card_action", "approval_policy": "single"},
    {"id": "tail", "label": "收口", "executor": "tool", "role": "produce", "deps": ["g1", "g2"],
     "deliverable": {"region": "whole"}, "tool": {"kind": "noop"}},
]


def test_a_bystander_gate_reopened_by_someone_else_gets_a_fresh_card():
    """两道门共享上游：财务打回会把还在等的法务一起卷进新一轮。

    法务必须收到**新卡**，且他手里上一轮那张卡必须失效。否则会出现最坏的情况：
    法务从未见过 v2，引擎却在权威 state 里记下「法务放行 v2」（审计记录被伪造，实测复现过）。
    """
    llm = CountingLLM({"w": "正文"})
    svc, io = build_service(SHARED, llm=llm, deliverables=FakeDeliverableStore())
    svc.start(instance_id="sh-1", reporter="ou_o", inputs={})
    g2_old = io.button_value("g2", "通过")          # 法务手里第 1 轮那张

    svc.resume_from_event({"key": CARD_ACTION, "operator_id": "ou_财务",
                           "action_value": dict(io.button_value("g1", "打回"), comment="不行")})

    assert llm.counts["w"] == 2                     # 上游已重算成 v2
    g2_cards = [c for c in io.cards.values() if c["buttons"][0]["action_value"]["node_id"] == "g2"]
    assert len(g2_cards) == 2, "法务没收到第 2 轮的新卡"

    stale = svc.resume_from_event({"key": CARD_ACTION, "action_value": g2_old,
                                   "operator_id": "ou_法务"})
    assert stale.get("skipped") == "stale"
    assert svc.status("sh-1").get("g2") != "done"   # 绝不能把对 v1 的裁决记成放行 v2


def test_illegal_reopen_without_node_id_cannot_brick_the_instance():
    """卡片封套是前端可自由构造的：少一个 node_id 不能绕开合法域校验。

    绕开的后果是非法值落进权威 state，此后每一次推进都在同一处抛，实例永久砖化，
    而 pending() 还谎报「无人等待」（实测复现过）。
    """
    svc, io, llm, store = contract()
    svc.start(instance_id="brick-1", reporter="ou_owner", inputs=INPUTS)
    av = dict(io.button_value("finance_gate", "打回"), reopen=["legal_draft"])
    av.pop("node_id")

    res = svc.resume_from_event({"key": CARD_ACTION, "action_value": av, "operator_id": "ou_财务"})

    assert res["rejected"] == "illegal_reopen" and res["illegal"] == ["legal_draft"]
    svc.reconcile("brick-1")                                   # 不抛
    assert {p["node_id"] for p in svc.pending("brick-1")} == {"finance_gate", "legal_gate"}
    assert "resumed" in svc.resume_from_event(card(io, "legal_gate", "通过"))   # 实例还活着


def test_illegal_targets_that_somehow_reached_state_block_instead_of_bricking():
    from larkflow.engine.gates import reopen_resets as rr
    dag = [{"id": "a", "deps": []}, {"id": "g", "deps": ["a"], "role": "gate"},
           {"id": "z", "deps": ["g"]}]
    assert rr(dag, {"g": "failed"}, {"g": {"passed": False, "reopen": ["z"]}}) == {"g": BLOCKED}


def test_no_duplicate_dispatch_after_the_engine_pumps_or_the_graph_is_edited():
    """中断 id 每推进一拍就换。拿它当幂等键会让同一个人反复收到新卡且无上限（实测）。"""
    svc, io, llm, store = contract()
    svc.start(instance_id="dup-1", reporter="ou_owner", inputs=INPUTS)
    svc.edit_graph("dup-1", [{"op": "add_node", "node": {
        "id": "audit", "label": "复盘", "executor": "llm", "role": "produce", "deps": ["close"],
        "prompt": "p", "model_role": "editor", "deliverable": {"region": "whole"}}}])
    svc.resume_from_event(card(io, "finance_gate", "通过"))
    svc.reconcile("dup-1")
    svc.reconcile("dup-1")

    legal = [c for c in io.cards.values() if c["buttons"][0]["action_value"]["node_id"] == "legal_gate"]
    assert len(legal) == 1, "法务从头到尾没被叫过第二次，却拿到了多张卡"


def test_reopen_budget_counts_exactly_once_per_reopen():
    """保值写回若把累加型的 reopen_counts 一起带上，预算 3 会变成 1。"""
    dag = [
        {"id": "draft", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "p", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "chk", "label": "机检", "executor": "tool", "role": "gate", "deps": ["draft"],
         "approval_policy": "auto", "reopen_budget": 3,
         "tool": {"kind": "format_check", "args": {"required": ["永不出现"]}}},
    ]
    llm = CountingLLM({"w": "过不了"})
    svc, io = build_service(dag, llm=llm)
    svc.start(instance_id="cnt-1", reporter="ou_o", inputs={})
    assert svc._values("cnt-1")["reopen_counts"] == {"chk": 3}
    assert llm.counts["w"] == 4                     # 首跑 + 预算内 3 次重算


def test_a_generous_reopen_budget_does_not_fall_back_to_recursion_limit():
    """预算调大就撞 GraphRecursionError 的话，ADR-029 的预算机制原地失效。"""
    dag = [
        {"id": "draft", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "p", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "chk", "label": "机检", "executor": "tool", "role": "gate", "deps": ["draft"],
         "approval_policy": "auto", "reopen_budget": 12,
         "tool": {"kind": "format_check", "args": {"required": ["永不出现"]}}},
    ]
    svc, io = build_service(dag, llm=CountingLLM({"w": "过不了"}))
    svc.start(instance_id="big-1", reporter="ou_o", inputs={})
    assert svc.blocked("big-1") == ["chk"]


def test_machine_check_recognises_the_placeholder_the_shipped_prompts_actually_produce():
    """出厂 prompt 教 AI 写「【待确认：X】」，机检常量却是「【待填写】」→ 空壳稿通过最后一道门。"""
    from larkflow.engine.tools import format_check

    class Ex:
        deliverables = object()

    node = {"id": "chk", "deps": ["d"], "tool": {}}
    state = {"dag": [{"id": "d", "deps": []}], "outputs": {}}
    import larkflow.engine.tools as T
    orig = T.read_upstream
    T.read_upstream = lambda io, st, n: {"d": "一、【待确认：价款】\n二、【待确认：期限】\n三、其余条款从略。"}
    try:
        out = format_check(node, state, Ex(), {"required": ["价款", "期限"], "min_chars": 5})
    finally:
        T.read_upstream = orig
    assert out["passed"] is False
    assert out["placeholders"] and out["missing"] == ["价款", "期限"]   # 占位段不算数


def test_notify_targets_go_through_the_role_resolver():
    from larkflow.engine.tools import _target

    class Ex:
        resolver = RoleResolver({"法务": "ou_leg"})

    st = {"meta": {"reporter": "ou_rep"}}
    assert _target(st, "reporter", Ex()) == "ou_rep"
    assert _target(st, "法务", Ex()) == "ou_leg"       # 不解析的话真栈会把中文名当 open_id 发出去
    assert _target(st, "ou_direct", Ex()) == "ou_direct"


def test_passing_a_dag_straight_to_build_service_still_goes_through_the_guardrails():
    dag = [
        {"id": "d", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "p", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "g", "label": "审批", "executor": "human", "role": "gate", "deps": ["d"],
         "assignee_role": "老板", "signal": "task_complete", "approval_policy": "single"},
    ]
    with pytest.raises(Exception, match="card_action"):
        build_service(dag)


def test_passthrough_only_sees_through_nodes_that_produce_nothing():
    """透传是为 gate 设计的；若连「声明了落点却没产出」的节点也透传，下游会静默读到祖父的正文。"""
    from larkflow.engine.deliverables import upstream_handles

    dag = [{"id": "gp", "deps": []},
           {"id": "mid", "deps": ["gp"], "deliverable": {"region": "whole"}},
           {"id": "me", "deps": ["mid"]}]
    outputs = {"gp": {"deliverable": {"type": "markdown", "token": "t1", "url": "u", "region": "whole"}}}
    assert upstream_handles({"dag": dag, "outputs": outputs}, dag[2]) == {}

    dag[1].pop("deliverable")          # 改成纯动作节点 → 才透传
    assert list(upstream_handles({"dag": dag, "outputs": outputs}, dag[2])) == ["gp"]
