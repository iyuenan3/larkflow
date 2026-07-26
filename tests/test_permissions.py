"""打回权限层（ADR-023）：机制层 ∩ 权限层。

机制层（gates.py）管「回得回去吗」；权限层管「你有资格让**谁**返工吗」。少了权限层，
「防踢皮球」就是一句空话：任何人都能把任意合法祖先踢回去让别人重做。

三条规则（ADR-023）：
  ① 项目 owner（meta.reporter）可打回本项目任一祖先。
  ② 参与人（人工节点 H 的主负责人）可打回 N，当且仅当 N ∈ 传递祖先(H) 且重算集
     (N ∪ N 的传递下游) 里除 N 自己、H 自己、H 的下游人工节点、以及 actor 自己担的节点
     之外，不牵连任何别的人工节点。
  ③ 跨界打回走 escalation：通知 {项目 owner + 目标节点主负责人}，任一方同意即执行。
     v1 只做「申请 + 通知 + 落权威 state 可查」，一键同意等接真 dev app。
"""
import pytest

from larkflow.app import build_service
from larkflow.config import RoleResolver
from larkflow.engine.permissions import allowed_reopen, reopen_verdict
from larkflow.io import FakeDeliverableStore
from larkflow.io.events import CARD_ACTION, TASK_UPDATE
from support import CountingLLM, card_target

OWNER = {"ou_owner"}


# ---------- 拓扑（纯函数用，不必过模板校验） ----------

def _llm(nid, deps):
    return {"id": nid, "label": nid, "executor": "llm", "role": "produce", "deps": deps}


def _human(nid, deps, who, role="produce"):
    return {"id": nid, "label": nid, "executor": "human", "role": role,
            "deps": deps, "assignee_role": who}


def _tool(nid, deps):
    return {"id": nid, "label": nid, "executor": "tool", "role": "produce", "deps": deps}


# a(AI) → h1(甲) → m(AI) → g(乙 门)
SERIAL = [_llm("a", []), _human("h1", ["a"], "甲"), _llm("m", ["h1"]),
          _human("g", ["m"], "乙", role="gate")]

# d(AI) →(gA 甲 门, gB 乙 门)→ tail：共享上游，打回 d 会连累旁支的人
PARALLEL = [_llm("d", []), _human("gA", ["d"], "甲", role="gate"),
            _human("gB", ["d"], "乙", role="gate"), _tool("tail", ["gA", "gB"])]

# 全 human：h1(甲) → h2(乙) → g(丙 门)
ALL_HUMAN = [_human("h1", [], "甲"), _human("h2", ["h1"], "乙"),
             _human("g", ["h2"], "丙", role="gate")]

# 单人项目：同一个人担所有角色
SOLO = [_human("h1", [], "我"), _human("h2", ["h1"], "我"),
        _human("g", ["h2"], "我", role="gate")]

# 纯 AI 上游：打回谁都不牵连人
AI_ONLY = [_llm("a", []), _llm("b", ["a"]), _human("g", ["b"], "甲", role="gate")]


# ---------- ① owner 全域 ----------

def test_the_project_owner_can_reopen_any_ancestor_of_the_gate():
    """owner 不受防踢皮球判据约束：整张图的祖先都归他调度。"""
    assert allowed_reopen(PARALLEL, actor_roles=OWNER, owner_roles=OWNER,
                          from_node="gA") == ["d"]
    assert allowed_reopen(ALL_HUMAN, actor_roles=OWNER, owner_roles=OWNER,
                          from_node="g") == ["h1", "h2"]


def test_being_owner_and_participant_at_once_gives_the_union_not_the_intersection():
    """owner 权限更大：他同时是参与人时不该被参与人那条更严的判据削掉。"""
    actor = {"ou_owner", "甲"}          # 既是发起人又是 gA 的负责人
    assert allowed_reopen(PARALLEL, actor_roles=actor, owner_roles=OWNER,
                          from_node="gA") == ["d"]
    # 同一个人若只有参与人身份，这一步就得走 escalation（见下）
    assert allowed_reopen(PARALLEL, actor_roles={"甲"}, owner_roles=OWNER,
                          from_node="gA") == []


# ---------- ② 参与人：串行退化 ----------

def test_a_serial_graph_degenerates_to_at_most_the_previous_human_node():
    """ADR-023 括号里那句：串行图下参与人最多回到上一个人工节点。"""
    assert allowed_reopen(SERIAL, actor_roles={"乙"}, owner_roles=OWNER,
                          from_node="g") == ["h1", "m"]
    # 越过 h1 往更上游（a）会把甲拖进返工 → 不在直接可打回集里
    assert "a" not in allowed_reopen(SERIAL, actor_roles={"乙"}, owner_roles=OWNER,
                                     from_node="g")


def test_reopening_past_the_previous_human_node_needs_escalation():
    v = reopen_verdict(SERIAL, actor_roles={"乙"}, owner_roles=OWNER,
                       from_node="g", targets=["a"])
    assert v["allowed"] == [] and v["denied"] == []
    assert [e["target"] for e in v["needs_escalation"]] == ["a"]
    assert v["needs_escalation"][0]["collateral"] == ["h1"]


def test_all_human_chain_stops_exactly_at_the_previous_person():
    assert allowed_reopen(ALL_HUMAN, actor_roles={"丙"}, owner_roles=OWNER,
                          from_node="g") == ["h2"]


# ---------- ② 参与人：并行才暴露的踢皮球 ----------

def test_a_shared_upstream_cannot_be_reopened_over_a_bystanders_head():
    """并行分支下的关键判据：打回共同上游会连累旁支他人，按「上一个人工节点」判是漏的。"""
    assert allowed_reopen(PARALLEL, actor_roles={"甲"}, owner_roles=OWNER,
                          from_node="gA") == []
    v = reopen_verdict(PARALLEL, actor_roles={"甲"}, owner_roles=OWNER,
                       from_node="gA", targets=["d"])
    assert [e["target"] for e in v["needs_escalation"]] == ["d"]
    assert v["needs_escalation"][0]["collateral"] == ["gB"]      # 被连累的是乙那道门


def test_the_gates_own_downstream_humans_are_not_collateral():
    """H 只要打回任何东西，自己必被重置、下游必然跟着重来，这不算额外连累。"""
    dag = [_llm("d", []), _human("g", ["d"], "甲", role="gate"),
           _human("after", ["g"], "乙"), _tool("tail", ["after"])]
    assert allowed_reopen(dag, actor_roles={"甲"}, owner_roles=OWNER, from_node="g") == ["d"]


def test_a_purely_ai_upstream_is_free_to_reopen():
    assert allowed_reopen(AI_ONLY, actor_roles={"甲"}, owner_roles=OWNER,
                          from_node="g") == ["a", "b"]


def test_a_one_person_project_is_never_hit_by_the_anti_pingpong_rule():
    """所有人工节点都是他自己：他连累的只有他自己，打回自己上游必须畅通。"""
    assert allowed_reopen(SOLO, actor_roles={"我"}, owner_roles=OWNER,
                          from_node="g") == ["h1", "h2"]


def test_one_person_wearing_several_hats_is_not_collateral_to_himself():
    dag = [_human("h1", [], "甲"), _llm("m", ["h1"]), _human("g", ["m"], "甲", role="gate"),
           _human("side", ["m"], "甲", role="gate"), _tool("tail", ["g", "side"])]
    assert allowed_reopen(dag, actor_roles={"甲"}, owner_roles=OWNER, from_node="g") == ["h1", "m"]


# ---------- 没有资格的人 ----------

def test_a_stranger_gets_denied_not_escalated():
    """既不是 owner 也不是这道门的主负责人：他没有申请权，不是「跨界」而是「没资格」。"""
    assert allowed_reopen(SERIAL, actor_roles={"ou_路人"}, owner_roles=OWNER,
                          from_node="g") == []
    v = reopen_verdict(SERIAL, actor_roles={"ou_路人"}, owner_roles=OWNER,
                       from_node="g", targets=["m"])
    assert v["denied"] == ["m"] and v["needs_escalation"] == []


def test_an_empty_actor_has_no_rights_at_all():
    """身份识别不出来就一律拒（fail closed）：绝不因为「不知道你是谁」而放行。"""
    assert allowed_reopen(SERIAL, actor_roles=set(), owner_roles=OWNER, from_node="g") == []
    assert reopen_verdict(SERIAL, actor_roles=None, owner_roles=OWNER,
                          from_node="g", targets=["m"])["denied"] == ["m"]


def test_mechanism_illegal_targets_are_denied_not_escalated():
    """越出传递祖先的目标是机制层的事，权限层不给它开 escalation 这条路。"""
    v = reopen_verdict(SERIAL, actor_roles=OWNER, owner_roles=OWNER,
                       from_node="g", targets=["h1", "没这个节点"])
    assert v["allowed"] == ["h1"] and v["denied"] == ["没这个节点"]


def test_an_auto_gate_has_no_human_owner_so_only_the_project_owner_stands_there():
    dag = [_llm("a", []), {"id": "chk", "label": "机检", "executor": "tool", "role": "gate",
                           "deps": ["a"], "approval_policy": "auto"}]
    assert allowed_reopen(dag, actor_roles={"甲"}, owner_roles=OWNER, from_node="chk") == []
    assert allowed_reopen(dag, actor_roles=OWNER, owner_roles=OWNER, from_node="chk") == ["a"]


# ---------- ③ escalation 的审批人 ----------

def test_escalation_names_the_owner_and_the_targets_primary_owner():
    v = reopen_verdict(ALL_HUMAN, actor_roles={"丙"}, owner_roles=OWNER,
                       from_node="g", targets=["h1"])
    entry = v["needs_escalation"][0]
    assert entry["target"] == "h1"
    assert entry["approvers"] == ["ou_owner", "甲"]      # 项目 owner + 目标节点负责人
    assert entry["collateral"] == ["h2"]


def test_a_multi_person_node_is_represented_by_its_primary_owner():
    """多人节点取主负责人（ADR-023 ④）：手动打回权的主体只有一个。"""
    dag = [
        {"id": "panel", "label": "评审组", "executor": "human", "role": "produce", "deps": [],
         "vote": {"voters": ["主", "副"], "primary": "主"}},
        _llm("m", ["panel"]),
        {"id": "g", "label": "门", "executor": "human", "role": "gate", "deps": ["m"],
         "vote": {"voters": ["审1", "审2"], "primary": "审1"}},
    ]
    assert allowed_reopen(dag, actor_roles={"审1"}, owner_roles=OWNER, from_node="g") == ["m", "panel"]
    assert allowed_reopen(dag, actor_roles={"审2"}, owner_roles=OWNER, from_node="g") == []
    v = reopen_verdict(dag, actor_roles={"审1"}, owner_roles=OWNER, from_node="g", targets=["panel"])
    assert v["allowed"] == ["panel"]      # panel 就是上一个人工节点，仍在畅通范围内


def test_permissions_are_computed_on_whatever_graph_you_hand_in():
    """权限必须按**运行时** dag 算：活图改过之后，同一个人的权限就该跟着变。"""
    before = [_llm("d", []), _human("g", ["d"], "甲", role="gate")]
    assert allowed_reopen(before, actor_roles={"甲"}, owner_roles=OWNER, from_node="g") == ["d"]
    after = before + [_human("side", ["d"], "乙", role="gate")]
    assert allowed_reopen(after, actor_roles={"甲"}, owner_roles=OWNER, from_node="g") == []


def test_verdict_keeps_the_order_it_was_given_and_drops_duplicates():
    v = reopen_verdict(SERIAL, actor_roles=OWNER, owner_roles=OWNER,
                       from_node="g", targets=["m", "h1", "m"])
    assert v["allowed"] == ["m", "h1"]


# ---------- 接线：service.resume ----------

PERM = [
    {"id": "a", "label": "甲写材料", "executor": "human", "role": "produce", "deps": [],
     "assignee_role": "甲", "signal": "task_complete", "deliverable": {"region": "whole"}},
    {"id": "b", "label": "AI 整合", "executor": "llm", "role": "produce", "deps": ["a"],
     "prompt": "整合", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "c", "label": "AI 补充", "executor": "llm", "role": "produce", "deps": ["b"],
     "prompt": "补充", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "g", "label": "乙审", "executor": "human", "role": "gate", "deps": ["c", "b"],
     "assignee_role": "乙", "signal": "card_action", "approval_policy": "single"},
    {"id": "side", "label": "丙审", "executor": "human", "role": "gate", "deps": ["b"],
     "assignee_role": "丙", "signal": "card_action", "approval_policy": "single"},
    {"id": "tail", "label": "收口", "executor": "tool", "role": "produce", "deps": ["g", "side"],
     "deliverable": {"region": "whole"}, "tool": {"kind": "summarize_links", "args": {}}},
]


def perm_service(iid):
    """跑到「乙 与 丙 两道门同时挂着」的现场。"""
    from larkflow.io.deliverable import Deliverable

    llm = CountingLLM({"w": "正文"})
    store = FakeDeliverableStore()
    svc, io = build_service(PERM, llm=llm, deliverables=store)
    svc.start(instance_id=iid, reporter="ou_owner", inputs={})
    p = next(x for x in svc.pending(iid) if x["node_id"] == "a")
    store.overwrite(Deliverable.from_dict(p["deliverable"]), content="材料")
    guid = next(t["guid"] for t in io.tasks.values() if t["summary"] == "甲写材料")
    svc.resume_from_event({"key": TASK_UPDATE, "event": {
        "task_guid": guid, "event_types": ["task_completed_update"]}})
    assert {x["node_id"] for x in svc.pending(iid)} == {"g", "side"}
    return svc, io, llm


def click(svc, io, node_id, label, *, operator, **ov):
    return svc.resume_from_event({"key": CARD_ACTION, "operator_id": operator,
                                  "action_value": dict(io.button_value(node_id, label), **ov)})


def test_an_outsider_cannot_reopen_through_a_forwarded_card():
    """卡片可以被转发 / 伪造：身份取自事件的 operator_id，且在引擎权威侧判。"""
    svc, io, llm = perm_service("pm-1")
    before_status, before_out = dict(svc.status("pm-1")), dict(svc.outputs("pm-1"))

    res = click(svc, io, "g", "打回", operator="ou_路人", reopen=["b"])

    assert res["rejected"] == "unauthorized_reopen" and res["denied"] == ["b"]
    assert svc.status("pm-1") == before_status          # state 一点没被弄脏
    assert svc.outputs("pm-1") == before_out
    assert svc.escalations("pm-1") == {}                # 没资格的人也不给他留申请
    assert llm.counts["w"] == 2                         # 上游没有被重算


def test_a_cross_boundary_reopen_is_escalated_and_not_executed():
    """乙 打回甲写的材料会把还在等的丙一起卷进返工 → 落申请 + 通知，但绝不当场执行。"""
    svc, io, llm = perm_service("pm-2")
    before_status = dict(svc.status("pm-2"))

    res = click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="材料不全")

    assert res["escalated"] == ["a"]
    assert svc.status("pm-2") == before_status          # 打回没有落地
    assert llm.counts["w"] == 2                         # 上游没重算
    assert "g" not in svc.outputs("pm-2")               # 也没记下「乙 判了打回」

    log = svc.escalations("pm-2", "g")
    assert len(log) == 1 and log[0]["by"] == "ou_乙"
    assert log[0]["targets"] == ["a"] and log[0]["comment"] == "材料不全"
    assert log[0]["collateral"] == ["side"] and log[0]["status"] == "pending"
    assert log[0]["at"]

    # 审批人 = 项目 owner + 目标节点主负责人（角色须过 resolver，别把中文名当 open_id 发出去）。
    # 他们收到的是**可点的审批卡**（ADR-043），不是纯文本；申请人那条回执才是 notify。
    told = {c["target"] for c in io.cards.values()
            if (c["buttons"][0]["action_value"] or {}).get("kind") == "escalation"}
    assert {"ou_owner", "ou_甲"} == told
    assert any(n["target"] == "ou_乙" for n in io.notifications), "申请人拿回执"
    assert res["approvers"] == ["ou_owner", "ou_甲"]
    # 记录里存的是**令牌**（角色名不会随映射变动而失真）+ 当时真发给了谁，两者都要
    assert log[0]["approvers"] == ["ou_owner", "甲"]
    assert log[0]["notified"] == ["ou_owner", "ou_甲"]

    # 申请那一拍写了权威 state（会换中断 id），但不许因此重复派单。
    # 只数**派单卡**：审批卡挂在同一个 node_id 上（ADR-043），不分开数会把它们算进来。
    assert len([c for c in io.cards.values()
                if c["buttons"][0]["action_value"]["node_id"] == "g"
                and c["buttons"][0]["action_value"].get("kind") != "escalation"]) == 1

    # 乙 手里那张卡还有效：申请不是裁决，他仍然可以放行
    assert "resumed" in click(svc, io, "g", "通过", operator="ou_乙")
    assert svc.status("pm-2")["g"] == "done"
    # 后续推进拍不许把追加型 channel 再累加一遍（保值写回踩过这个坑）
    assert len(svc.escalations("pm-2", "g")) == 1


def test_a_replayed_reopen_on_an_already_answered_gate_creates_no_request():
    """飞书事件 at-least-once：一张早就答完的旧卡被重放，绝不能凭空生出一笔审批申请。"""
    svc, io, llm = perm_service("pm-13")
    stale = dict(io.button_value("g", "打回"), reopen=["a"])
    assert "resumed" in click(svc, io, "g", "通过", operator="ou_乙")

    res = svc.resume_from_event({"key": CARD_ACTION, "operator_id": "ou_乙",
                                 "action_value": stale})

    assert res.get("skipped") == "stale"
    assert svc.escalations("pm-13") == {}


def test_the_owner_can_execute_the_same_reopen_the_participant_had_to_ask_for():
    svc, io, llm = perm_service("pm-3")

    res = click(svc, io, "g", "打回", operator="ou_owner", reopen=["a"], comment="材料不全")

    assert "resumed" in res
    assert svc.status("pm-3")["a"] == "pending"          # 甲被真的叫回来重写
    assert svc.escalations("pm-3") == {}                  # 走不到 escalation


def test_a_reopen_inside_the_participants_own_lane_just_works():
    """乙 打回只在他这一支的 AI 节点：不牵连任何人，不需要任何审批。"""
    svc, io, llm = perm_service("pm-4")

    res = click(svc, io, "g", "打回", operator="ou_乙", reopen=["c"], comment="补充不够")

    assert "resumed" in res
    assert llm.counts["w"] == 3                          # c 重算了
    assert svc.status("pm-4").get("side") != "done"      # 丙 完全没被牵动
    assert svc.escalations("pm-4") == {}


def test_the_default_reopen_target_on_the_card_is_already_permission_filtered():
    """卡上的默认目标只剔「点了必被拒」的（denied），**保留**要走审批的那些。

    g 的 deps = [c, b]：c 在乙自己这一支，b 会连累丙。两个都得留在卡上：把 b 删掉的话，
    乙 点一下就只把 c 退回去重做，b 与那笔本该产生的审批申请一起无声消失，
    `_check_reopen` 的「全或无」被发卡那一刻的过滤架空
    （见 test_the_card_button_never_silently_reopens_only_half_of_the_default）。
    """
    svc, io, llm = perm_service("pm-5")
    assert io.button_value("g", "打回")["reopen"] == ["c", "b"]
    # 丙 那道门只有一个上游，且它必然连累乙：申请路径同样保留，而不是把 reopen 整个拿掉
    # （拿掉会退回引擎默认目标，反而绕过这层过滤）
    assert io.button_value("side", "打回")["reopen"] == ["b"]


# a(AI) → g(乙 门)：默认目标全在乙自己这一支，打回不牵连任何人
LANE = [
    {"id": "a", "label": "AI 初稿", "executor": "llm", "role": "produce", "deps": [],
     "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "g", "label": "乙审", "executor": "human", "role": "gate", "deps": ["a"],
     "assignee_role": "乙", "signal": "card_action", "approval_policy": "single"},
]


def test_clicking_the_default_reopen_button_executes_without_asking_anyone():
    """默认目标全都在他自己这一支时，点一下就当场执行，不惊动任何人。

    这条原来跑在 g 上（deps = [c, b]，b 会连累丙）。它当时之所以「不用问人」，是因为
    发卡时把 b 悄悄删掉了，测到的其实是那次静默的部分打回，不是这条性质。
    """
    llm = CountingLLM({"w": "正文"})
    svc, io = build_service(LANE, llm=llm, deliverables=FakeDeliverableStore())
    svc.start(instance_id="pm-6", reporter="ou_owner", inputs={})
    assert io.button_value("g", "打回")["reopen"] == ["a"]

    assert "resumed" in click(svc, io, "g", "打回", operator="ou_乙", comment="再补一版")

    assert llm.counts["w"] == 2                  # 上游真的重算了
    assert svc.escalations("pm-6") == {}


def test_an_implicit_default_reopen_is_checked_too():
    """不带 reopen 的「打回」用的是引擎默认目标组，它同样必须过权限层，否则是条绕行路。"""
    svc, io, llm = perm_service("pm-7")
    av = dict(io.button_value("g", "打回"))
    av.pop("reopen", None)                               # 前端可以什么都不带
    res = svc.resume_from_event({"key": CARD_ACTION, "operator_id": "ou_乙",
                                 "action_value": av})
    assert res["escalated"] == ["b"]                     # 默认目标含 b（会连累丙）→ 申请
    assert svc.status("pm-7")["b"] == "done"             # 没有执行


def test_the_actor_is_taken_from_the_event_not_from_the_card_payload():
    """卡片 action_value 是攻击面：往里塞身份字段不得改变判定。"""
    svc, io, llm = perm_service("pm-8")
    res = click(svc, io, "g", "打回", operator="ou_路人",
                reopen=["a"], by="ou_owner", actor="ou_owner", operator_id="ou_owner")
    assert res["rejected"] == "unauthorized_reopen"


def test_a_repeated_identical_escalation_does_not_pile_up_requests():
    """双击 / 事件重放不该让申请堆成一摞（申请是有界的，审计仍然记得住每一笔）。"""
    svc, io, llm = perm_service("pm-9")
    first = click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="材料不全")
    again = click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="材料不全")
    assert first["escalated"] == again["escalated"]
    assert len(svc.escalations("pm-9", "g")) == 1


def test_pending_escalation_requests_are_bounded():
    """换一组目标就能再发一笔申请（目标组有 2^|祖先| 种）：不设上界就是一条刷屏通道。"""
    from larkflow.engine.permissions import MAX_PENDING_ESCALATIONS

    svc, io, llm = perm_service("pm-14")
    subsets = [["a"], ["b"], ["a", "b"], ["b", "a"], ["a", "c"], ["b", "c"], ["a", "b", "c"]]
    results = [click(svc, io, "g", "打回", operator="ou_乙", reopen=s) for s in subsets]

    assert len(svc.escalations("pm-14", "g")) == MAX_PENDING_ESCALATIONS
    assert results[-1]["rejected"] == "too_many_escalations"
    assert svc.status("pm-14")["b"] == "done"          # 一路下来一次打回都没落地


def test_semantically_identical_reopen_clicks_share_one_escalation_slot():
    """去重键必须按**目标集合**算，不能拿前端给的原始列表逐字比。

    `reopen_verdict` 内部早就把 targets 去过重了（同一份数据两套口径 = 内部不一致）。
    照字面比的话，`["a"]` / `["a","a"]` / 多选框顺序不同的 `["b","a"]` 各占一格配额，
    5 格一满这道门的审批通道就被自己人点死了。
    """
    svc, io, llm = perm_service("pm-15")

    for reopen in (["a"], ["a", "a"], ["a", "a", "a"]):
        assert click(svc, io, "g", "打回", operator="ou_乙", reopen=reopen)["escalated"] == ["a"]
    assert len(svc.escalations("pm-15", "g")) == 1

    click(svc, io, "g", "打回", operator="ou_乙", reopen=["a", "b"])
    click(svc, io, "g", "打回", operator="ou_乙", reopen=["b", "a"])
    assert len(svc.escalations("pm-15", "g")) == 2      # 顺序不同 = 同一笔申请


def test_a_new_round_gives_the_gate_its_escalation_channel_back():
    """配额是「同时待批」的上限，不是「这道门一辈子」的上限。

    v1 没有 approve / reject 通道，status 永远停在 pending，所以按整条历史算 = 第 6 次起
    **永久**拒（实测过：owner 正常打回进了新一轮，这道门仍然一笔都提不了）。申请是对
    「这道门这一轮」提的，门进新一轮后旧申请连同它要打回的那一版一起作废。
    """
    from larkflow.engine.permissions import MAX_PENDING_ESCALATIONS

    svc, io, llm = perm_service("pm-20")
    for reopen in (["a"], ["b"], ["a", "b"], ["a", "c"], ["b", "c"]):
        click(svc, io, "g", "打回", operator="ou_乙", reopen=reopen)
    assert len(svc.escalations("pm-20", "g")) == MAX_PENDING_ESCALATIONS
    assert click(svc, io, "g", "打回", operator="ou_乙",
                 reopen=["a", "b", "c"])["rejected"] == "too_many_escalations"

    # owner 正常开一枪打回，g 被重置进新一轮
    assert "resumed" in click(svc, io, "g", "打回", operator="ou_owner", reopen=["c"])

    res = click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"])
    assert res["escalated"] == ["a"], "新一轮里连一笔申请都提不了 = 通道被永久锁死"
    assert len(svc.escalations("pm-20", "g")) == MAX_PENDING_ESCALATIONS + 1   # 历史一笔不丢
    # 对外读接口与配额同一把尺：上一轮那 5 笔已经随轮次作废，不该再挂在审批人的待办上
    assert [r["seq"] for r in svc.pending_escalations("pm-20", "g")] == [6]
    assert set(svc.pending_escalations("pm-20")) == {"g"}


def test_a_click_that_changed_nothing_tells_the_person_who_clicked():
    """越权 / 待审批都是「点了没反应」：不给点的人任何回执，他只会一直点。

    而每一次重复点击都在烧 escalation 配额、刷屏审批人。静默失败是那条 bug 的燃料。
    """
    svc, io, llm = perm_service("pm-16")

    click(svc, io, "g", "打回", operator="ou_路人", reopen=["b"])
    assert [n for n in io.notifications if n["target"] == "ou_路人"]

    click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"])
    told = [n for n in io.notifications if n["target"] == "ou_乙"]
    assert told and "申请" in told[0]["text"]


# ---------- 放行同样要过引擎权威侧的身份判定 ----------

def test_a_stranger_cannot_sign_off_a_gate_he_was_never_asked_to_review():
    """卡片会被转发、assignee 解析成群时群里人人点得到：放行必须与打回同一把尺。

    只判打回不判放行，等于「让人返工要过三条规则，让交付物生效零校验」，松紧正好反了。
    """
    svc, io, llm = perm_service("pm-17")
    before_status, before_out = dict(svc.status("pm-17")), dict(svc.outputs("pm-17"))

    res = click(svc, io, "g", "通过", operator="ou_路人")

    assert res["rejected"] == "unauthorized_pass" and res["actor"] == "ou_路人"
    assert svc.status("pm-17") == before_status
    assert svc.outputs("pm-17") == before_out          # 绝不留下「路人放行了」的假审计
    assert "resumed" in click(svc, io, "g", "通过", operator="ou_乙")   # 负责人照样点得动


def test_the_owner_may_reopen_anything_but_may_not_sign_in_someone_elses_name():
    """打回 = 调度（owner 全域，ADR-023 ①）；放行 = 代签（谁的活谁签）。

    owner 想跳过一道门有留痕的正路：受控活图改 / 删这个节点（ADR-013），不是替人签字。
    """
    svc, io, llm = perm_service("pm-18")
    assert click(svc, io, "g", "通过", operator="ou_owner")["rejected"] == "unauthorized_pass"
    assert "resumed" in click(svc, io, "g", "打回", operator="ou_owner", reopen=["a"])


def test_a_card_event_without_an_operator_is_not_routed_at_all():
    """身份缺失一律 fail closed：缺 operator 的卡片事件是畸形封套，不是「匿名放行」。"""
    svc, io, llm = perm_service("pm-19")

    res = svc.resume_from_event({"key": CARD_ACTION,
                                 "action_value": io.button_value("g", "通过")})

    assert res == {"skipped": "unidentified_actor"}
    assert svc.status("pm-19").get("g") != "done"


def test_every_voter_can_answer_the_node_but_only_the_primary_can_reopen():
    """多人节点：应答权归全体 voters，手动打回权只归主负责人（ADR-023 ④）。"""
    from larkflow.engine.permissions import can_answer

    dag = [_llm("m", []),
           {"id": "g", "label": "评审组", "executor": "human", "role": "gate", "deps": ["m"],
            "vote": {"voters": ["审1", "审2"], "primary": "审1"}}]

    assert can_answer(dag, actor_roles={"审2"}, node_id="g")
    assert can_answer(dag, actor_roles={"审1"}, node_id="g")
    assert not can_answer(dag, actor_roles={"ou_路人"}, node_id="g")
    assert not can_answer(dag, actor_roles=set(), node_id="g")
    assert not can_answer(dag, actor_roles={"审1"}, node_id="m")     # 非人工节点没有应答人
    assert not can_answer(dag, actor_roles={"审1"}, node_id="没这个节点")


def test_pending_filters_the_candidates_down_to_what_this_person_can_actually_click():
    svc, io, llm = perm_service("pm-10")

    mine = {p["node_id"]: p for p in svc.pending("pm-10", actor="ou_乙")}["g"]
    assert mine["reopen_candidates"] == ["c"]        # 他能当场点的
    assert mine["reopen_escalation"] == ["a", "b"]   # 他点得动、但要先请人同意的
    # 默认目标只剔 denied（这里一个都没有），要走审批的 b 必须留着：只留 allowed 的话，
    # 前端照着预勾选再回传就是一次静默的部分打回（全或无被架空）
    assert mine["reopen_default"] == ["c", "b"]
    assert mine["reopen_default"] == io.button_value("g", "打回")["reopen"]

    boss = {p["node_id"]: p for p in svc.pending("pm-10", actor="ou_owner")}["g"]
    assert boss["reopen_candidates"] == ["a", "b", "c"]
    assert boss["reopen_escalation"] == []


def test_pending_without_an_actor_still_reports_the_full_mechanism_set():
    """不传 actor = 运维 / 驾驶舱视角（全集），向后兼容，不是「谁都能点」。"""
    svc, io, llm = perm_service("pm-11")
    everyone = {p["node_id"]: p for p in svc.pending("pm-11")}["g"]
    assert everyone["reopen_candidates"] == ["a", "b", "c"]
    assert "reopen_escalation" not in everyone


def test_permission_follows_the_live_graph_not_the_template():
    """受控活图会改图；权限必须按运行时 dag 现算，不能用装配期那张。"""
    dag = [
        {"id": "d", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "g", "label": "乙审", "executor": "human", "role": "gate", "deps": ["d"],
         "assignee_role": "乙", "signal": "card_action", "approval_policy": "single"},
    ]
    svc, io = build_service(dag, llm=CountingLLM({"w": "正文"}),
                            deliverables=FakeDeliverableStore())
    svc.start(instance_id="lg-1", reporter="ou_owner", inputs={})
    assert svc.pending("lg-1", actor="ou_乙")[0]["reopen_candidates"] == ["d"]

    svc.edit_graph("lg-1", [{"op": "add_node", "node": {
        "id": "side", "label": "丙审", "executor": "human", "role": "gate", "deps": ["d"],
        "assignee_role": "丙", "signal": "card_action", "approval_policy": "single"}}], by="ou_owner", reason="测试改图")

    mine = {p["node_id"]: p for p in svc.pending("lg-1", actor="ou_乙")}["g"]
    assert mine["reopen_candidates"] == []               # 新来的丙成了会被连累的旁观者
    assert mine["reopen_escalation"] == ["d"]
    assert click(svc, io, "g", "打回", operator="ou_乙")["escalated"] == ["d"]


# ---------- 反向角色解析 ----------

def test_one_open_id_can_wear_several_role_hats():
    """一人多角色是常态（财务同时兼法务），所以反解必须返回集合，不能假设一对一。"""
    r = RoleResolver({"财务": "ou_x", "法务": "ou_x", "HR": "ou_y"})
    assert r.roles_of("ou_x") == {"财务", "法务"}
    assert r.roles_of("ou_y") == {"HR"}
    assert r.roles_of("") == set() and r.roles_of(None) == set()


def test_reverse_resolution_mirrors_the_local_fallback_exactly():
    """本地非 strict 下 resolve(role) 回退成 ou_<role>；反解必须与之对称，否则本地跑不通。"""
    r = RoleResolver()
    assert r.roles_of(r.resolve("财务")) == {"财务"}


def test_reverse_resolution_never_invents_a_role_that_is_actually_mapped_elsewhere():
    """「财务」已配成 ou_fin 时，ou_财务 这个 id 绝不能反解出「财务」（否则可冒名顶替）。"""
    r = RoleResolver({"财务": "ou_fin"})
    assert r.roles_of("ou_财务") == set()
    assert r.roles_of("ou_fin") == {"财务"}


def test_a_strict_resolver_refuses_to_guess_who_someone_is():
    r = RoleResolver({"财务": "ou_fin"}, strict=True)
    assert r.roles_of("ou_fin") == {"财务"}
    assert r.roles_of("ou_法务") == set()


# ---------- 与既有 e2e 的交叉断言 ----------

def test_the_shipped_templates_let_their_reviewers_do_their_job():
    """三张出厂模板里，审核人各自的常规打回必须**不需要**任何审批。

    这条是产品级判据：权限层调紧到「谁都得申请」等于把产品做死。
    """
    from larkflow.model import load_template

    contract = load_template("contract")
    assert "biz_draft" in allowed_reopen(contract, actor_roles={"财务"},
                                         owner_roles=OWNER, from_node="finance_gate")
    defect = load_template("defect")
    assert "fix" in allowed_reopen(defect, actor_roles={"QA"},
                                   owner_roles=OWNER, from_node="qa_verify")
    hiring = load_template("hiring")
    assert "sourcing" in allowed_reopen(hiring, actor_roles={"用人经理"},
                                        owner_roles=OWNER, from_node="decision")


def test_a_reviewer_cannot_drag_an_unrelated_colleague_back_into_the_loop():
    """招聘图：用人经理打回 JD 会让面试官重面，这不是他一个人能拍的。"""
    from larkflow.model import load_template

    hiring = load_template("hiring")
    v = reopen_verdict(hiring, actor_roles={"用人经理"}, owner_roles=OWNER,
                       from_node="decision", targets=["jd_review"])
    assert [e["target"] for e in v["needs_escalation"]] == ["jd_review"]
    assert v["needs_escalation"][0]["collateral"] == ["interview"]


def test_card_target_helper_points_at_the_person_who_was_asked():
    """测试替身自检：卡发给谁，谁才是该点它的人（e2e 用它当 operator_id）。"""
    svc, io, llm = perm_service("pm-12")
    assert card_target(io, "g") == "ou_乙"
    assert card_target(io, "side") == "ou_丙"


@pytest.mark.parametrize("bad", [None, "没这个节点"])
def test_unknown_gates_yield_no_rights_instead_of_raising(bad):
    assert allowed_reopen(SERIAL, actor_roles={"乙"}, owner_roles=OWNER, from_node=bad) == []


# ---------- 第三轮对抗（专攻权限层落地那一版）挖出的洞 ----------

# human **produce** 走卡片：模板层完全合法（只有 human *gate* 被限定成 card_action），
# 而 `_buttons` 给它的是单按钮 verdict=pass。攻击面就在「不是 pass 的那一半」。
CARD_PRODUCE = [
    {"id": "seed", "label": "登记", "executor": "tool", "role": "produce", "deps": [],
     "tool": {"kind": "record", "args": {}}, "deliverable": {"region": "whole"}},
    {"id": "write", "label": "作者定稿", "executor": "human", "role": "produce",
     "deps": ["seed"], "assignee_role": "作者", "signal": "card_action",
     "deliverable": {"region": "whole"}},
    {"id": "gate", "label": "法务审", "executor": "human", "role": "gate", "deps": ["write"],
     "assignee_role": "法务", "signal": "card_action", "approval_policy": "single"},
]


def test_flipping_one_field_in_the_envelope_does_not_bypass_the_answer_check():
    """身份判定按 `passed` 分了两支：pass 走 `can_answer`，fail 走打回三条规则。

    可**非 gate 的 fail** 落在两支之外：`_check_reopen` 见它不是 gate 就返回 None，
    于是一道校验都没过就 resume 了，而 `gates.finish` 对非 gate 节点根本不看 passed，
    照样把它标成 done。陌生人把封套里的 verdict 改一个字，就替作者把定稿签了。
    """
    svc, io = build_service(CARD_PRODUCE, deliverables=FakeDeliverableStore())
    svc.start(instance_id="cp-1", reporter="ou_owner", inputs={})
    av = io.button_value("write", "完成")

    # 「完成」那一下（verdict=pass）本来就拦得住陌生人
    assert svc.resume_from_event({"key": CARD_ACTION, "operator_id": "ou_路人",
                                  "action_value": av})["rejected"] == "unauthorized_pass"

    res = svc.resume_from_event({"key": CARD_ACTION, "operator_id": "ou_路人",
                                 "action_value": dict(av, verdict="fail")})

    assert res.get("rejected") == "unauthorized_pass", res
    assert svc.status("cp-1").get("write") != "done", "陌生人替作者把定稿签了"
    assert "write" not in svc.outputs("cp-1")


def test_a_task_event_cannot_ride_a_card_correlation_into_the_engine():
    """任务通道的身份豁免只对**任务**成立：那条 task_guid 是引擎发给指定人的待办。

    关联表按 external_id 索引、不分 kind，于是拿一张卡的 message_id 冒充 task_guid
    递进来，就绕开了整条卡片通道的身份判定（actor 变 None，`can_answer` 根本不跑）。
    """
    svc, io = build_service(CARD_PRODUCE, deliverables=FakeDeliverableStore())
    svc.start(instance_id="cp-2", reporter="ou_owner", inputs={})
    msg_id = next(iter(io.cards))
    assert svc.corr.get(msg_id).kind == "card"

    res = svc.resume_from_event({"key": TASK_UPDATE, "event": {
        "task_guid": msg_id, "event_types": ["task_completed_update"]}})

    assert res == {"skipped": "unrouted"}, res
    assert svc.status("cp-2").get("write") != "done"


def test_the_card_button_never_silently_reopens_only_half_of_the_default():
    """`_check_reopen` 的「全或无」不能被发卡那一刻的过滤架空。

    g 的默认目标 = deps = [c, b]，其中 b 会连累丙。`_permitted_default` 把 b 删掉之后，
    乙 点「打回」拿到的是一次**静默的部分打回**：c 重算了、b 原样不动、申请没落、
    谁都没被告知，正是 `_escalate` 那段注释说的「比什么都不做更难排查」。
    """
    svc, io, llm = perm_service("pm-20")
    before = llm.counts["w"]

    res = click(svc, io, "g", "打回", operator="ou_乙", comment="都不行")

    assert "resumed" not in res, res
    assert llm.counts["w"] == before, "默认目标里有跨界的那一个，却把另一半当场执行了"
    assert svc.escalations("pm-20", "g"), "既没执行也没落申请，这一下彻底石沉大海"


def test_the_card_button_and_an_empty_payload_reach_the_same_verdict():
    """同一个人、同一道门、同一次「打回」，不该因为前端回没回 reopen 而有两种结局。"""
    a, io_a, llm_a = perm_service("pm-21")
    from_card = click(a, io_a, "g", "打回", operator="ou_乙", comment="不行")

    b, io_b, llm_b = perm_service("pm-22")
    av = dict(io_b.button_value("g", "打回"), comment="不行")
    av.pop("reopen", None)                       # 前端什么都不带 → 引擎默认目标组
    empty = b.resume_from_event({"key": CARD_ACTION, "operator_id": "ou_乙",
                                 "action_value": av})

    assert ("resumed" in from_card) == ("resumed" in empty), (from_card, empty)
    assert a.status("pm-21") == b.status("pm-22")
    assert llm_a.counts == llm_b.counts


def test_the_read_api_and_the_card_agree_on_the_default_reopen_target():
    """驾驶舱 / 前端读到的默认目标，必须与卡上那颗按钮里的**逐字一致**。

    两处各自过一遍权限层、口径却不同（一处只留 allowed，一处留 allowed ∪ 待审批），
    就是同一个「打回」有两种语义：照读接口预勾选再回传，等于绕出一次静默的部分打回。
    """
    svc, io, llm = perm_service("pm-23")
    for nid in ("g", "side"):
        seen = next(p for p in svc.pending("pm-23", actor=card_target(io, nid))
                    if p["node_id"] == nid)
        assert seen["reopen_default"] == io.button_value(nid, "打回")["reopen"], nid


def test_repeatedly_clicking_the_default_reopen_button_files_exactly_one_request():
    """按钮回传的目标组是引擎自己给的，双击 / 事件重放不该把申请堆成一摞。"""
    svc, io, llm = perm_service("pm-24")
    for _ in range(4):
        res = click(svc, io, "g", "打回", operator="ou_乙", comment="都不行")
    assert res.get("duplicate") is True, res
    assert len(svc.escalations("pm-24", "g")) == 1
    assert len(svc.pending_escalations("pm-24", "g")) == 1


def test_a_card_carries_no_default_target_when_its_holder_has_no_standing():
    """反解不出角色时（自定义 resolver / assignee 落在群上），卡上不留任何默认目标。

    这是 `_permitted_default` 唯一真正剔东西的那条路：它只剔 denied。剔不动的话，卡上就
    印着一个点了必被拒的目标，人点一次被拒一次，还看不出是「角色配置反解不出他」。
    """
    class OneWay:                      # 只会正向解析，反解不出来（不是所有 resolver 都有 roles_of）
        def resolve(self, role, state=None):
            return f"ou_{role}"

    llm = CountingLLM({"w": "正文"})
    svc, io = build_service(LANE, llm=llm, deliverables=FakeDeliverableStore(),
                            resolver=OneWay())
    svc.start(instance_id="pm-25", reporter="ou_owner", inputs={})

    assert "reopen" not in io.button_value("g", "打回")
    # 引擎侧照样再判一次：退回默认目标组也不放行
    assert click(svc, io, "g", "打回", operator="ou_乙")["rejected"] == "unauthorized_reopen"
