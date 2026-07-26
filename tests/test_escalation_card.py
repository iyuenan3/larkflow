"""审批卡：把 ADR-023 ③ 的「一键同意」真正做成一键。

ADR-040 只做完了引擎那一半：`approve_escalation` / `reject_escalation` 有了，但审批人在
飞书里收到的仍然是一条**纯文本**，要拍板得有人去敲 `larkflow approve`。对一个「飞书原生」
的产品来说，这等于把出口修在了大多数审批人根本走不到的地方。

封套设计（SPEC 待填的那一条）：

    {"kind": "escalation", "thread_id": …, "node_id": <门>, "seq": <第几笔>,
     "decision": "approve" | "reject"}

它**没有 interrupt_id**：拍板不是在答复某个中断，而是对一笔申请表态。所以 `_route` 要多一条
分支，不能沿用「thread_id + interrupt_id」那把钥匙。

红线照旧、且这里最容易破：**身份只取事件顶层的 `operator_id`**。封套是前端可自由构造的，
往里塞 by / actor 一律无效。一张审批卡若被转发，收到的人点了也只能以他自己的身份被判。
"""
from __future__ import annotations

from larkflow.io.events import CARD_ACTION
from test_escalation_approve import CROSS, click, escalate, redo_a, run_to_gates

APPROVE, REJECT = "同意", "驳回"


def approval_cards(io, seq=None):
    """发给审批人的那些卡（按封套自描述筛，不靠标题猜）。"""
    out = []
    for c in io.cards.values():
        av = (c["buttons"] or [{}])[0].get("action_value") or {}
        if av.get("kind") == "escalation" and (seq is None or av.get("seq") == seq):
            out.append(c)
    return out


def press(svc, card, label, *, operator, token=None, **override):
    av = next(b["action_value"] for b in card["buttons"] if b["label"] == label)
    ev = {"key": CARD_ACTION, "action_value": dict(av, **override), "operator_id": operator}
    if token:
        ev["token"] = token
    return svc.resume_from_event(ev)


# ---------- 发卡 ----------

def test_every_approver_gets_a_card_with_two_buttons_not_a_wall_of_text():
    svc, io, llm, _ = escalate("kc-1")

    cards = approval_cards(io)
    assert {c["target"] for c in cards} == {"ou_owner", "ou_甲"}, \
        "两个审批人各收一张，不是发一张群发了事"
    for c in cards:
        assert [b["label"] for b in c["buttons"]] == [APPROVE, REJECT]
        av = c["buttons"][0]["action_value"]
        assert av["kind"] == "escalation" and av["node_id"] == "g" and av["seq"] == 1
        assert "interrupt_id" not in av, "拍板不是答复中断，别把这把钥匙混进来"
        assert "乙审" in c["summary"] and "甲写材料" in c["summary"], "卡上要说清在批什么"


def test_the_requester_still_gets_plain_text_because_he_has_nothing_to_press():
    """申请人收到的是回执，不是待办：给他按钮只会让他误以为自己能拍板。"""
    svc, io, llm, _ = escalate("kc-2")
    assert any(n["target"] == "ou_乙" for n in io.notifications)
    assert not [c for c in approval_cards(io) if c["target"] == "ou_乙"]


# ---------- 点下去要真的算数 ----------

def test_pressing_approve_executes_the_reopen():
    svc, io, llm, _ = escalate("kc-3")
    card = next(c for c in approval_cards(io) if c["target"] == "ou_甲")

    out = press(svc, card, APPROVE, operator="ou_甲")

    assert out.get("approved") is True, out
    assert svc.status("kc-3")["a"] == "pending"
    assert svc.pending_escalations("kc-3", "g") == []


def test_pressing_reject_closes_the_request_and_reopens_nothing():
    svc, io, llm, _ = escalate("kc-4")
    card = next(c for c in approval_cards(io) if c["target"] == "ou_owner")

    out = press(svc, card, REJECT, operator="ou_owner")

    assert out.get("rejected_request") is True, out
    assert svc.status("kc-4")["a"] == "done"
    assert svc.escalations("kc-4", "g")[0]["effective_status"] == "rejected"


# ---------- 身份：这里最容易破 ----------

def test_the_actor_is_the_person_who_pressed_it_not_whatever_the_envelope_claims():
    """卡片可以被转发、封套可以被伪造。往里塞 by / actor 一律无效（红线⑤）。"""
    svc, io, llm, _ = escalate("kc-5")
    card = next(c for c in approval_cards(io) if c["target"] == "ou_甲")

    out = press(svc, card, APPROVE, operator="ou_路人", by="ou_甲", actor="ou_owner")

    assert out.get("rejected") == "unauthorized_approve", out
    assert svc.status("kc-5")["a"] == "done", "越权不许动权威 state"
    assert len(svc.pending_escalations("kc-5", "g")) == 1


def test_a_card_action_without_an_operator_is_refused():
    svc, io, llm, _ = escalate("kc-6")
    card = next(c for c in approval_cards(io) if c["target"] == "ou_甲")
    av = next(b["action_value"] for b in card["buttons"] if b["label"] == APPROVE)

    out = svc.resume_from_event({"key": CARD_ACTION, "action_value": av})

    assert out.get("skipped") == "unidentified_actor", out


def test_the_requester_cannot_press_his_own_approval_card():
    """自批那条闸对卡片通道同样生效（把卡转给自己也没用）。

    用甲自提自批那张图：他既写 a 又把 g 这道门，于是他本人就在 a 的审批人集合里，
    审批卡真的会发到他手上。
    """
    from test_escalation_approve import SELF

    svc, io, llm, _ = escalate("kc-7", dag=SELF, who="ou_甲", gate="g")
    card = next(c for c in approval_cards(io) if c["target"] == "ou_甲")
    assert press(svc, card, APPROVE, operator="ou_甲").get("rejected") == "self_approve"


# ---------- 点完之后卡片要变样（ADR-037 的纪律） ----------

def test_the_card_you_pressed_says_what_you_decided_and_loses_its_buttons():
    svc, io, llm, _ = escalate("kc-8")
    card = next(c for c in approval_cards(io) if c["target"] == "ou_甲")

    press(svc, card, APPROVE, operator="ou_甲", token="tk-1")

    up = io.card_updates[-1]
    assert up["token"] == "tk-1"
    text = str(up["card"])
    assert "已同意" in text and "ou_甲" in text
    assert "button" not in text, "留着还能点，而点了只会得到 already_settled"


def test_the_other_approvers_card_is_marked_settled_when_he_finally_presses_it():
    """两个审批人各有一张卡，任一方拍板即生效（ADR-023 的 OR 语义）。

    另一张卡此后就失效了，但它**长得和能点的一模一样**。人点下去必须当场看到「已经有人
    处理过了」，而不是静默 no-op，更不能因此再退回一轮。
    """
    svc, io, llm, _ = escalate("kc-9")
    mine = next(c for c in approval_cards(io) if c["target"] == "ou_甲")
    other = next(c for c in approval_cards(io) if c["target"] == "ou_owner")
    press(svc, mine, APPROVE, operator="ou_甲", token="tk-a")
    attempts_after = dict(svc._values("kc-9").get("attempts") or {})
    io.card_updates.clear()

    out = press(svc, other, REJECT, operator="ou_owner", token="tk-b")

    assert out.get("rejected") == "already_settled", out
    assert dict(svc._values("kc-9").get("attempts") or {}) == attempts_after, "不许再动一下"
    text = str(io.card_updates[-1]["card"])
    assert "已由" in text and "button" not in text


def test_an_unauthorized_press_does_not_rewrite_the_card_for_everyone():
    """卡可能已被转发：越权的是**看到卡的某个人**，不是这张卡本身。

    把「你没有权限」写上去会改掉所有人看到的内容，包括真正的审批人（ADR-037 同款判据）。
    """
    svc, io, llm, _ = escalate("kc-10")
    card = next(c for c in approval_cards(io) if c["target"] == "ou_甲")
    io.card_updates.clear()

    press(svc, card, APPROVE, operator="ou_路人", token="tk-x")

    assert not io.card_updates


def test_a_stale_card_from_an_older_round_cannot_reopen_anything():
    """门进了新一轮，上一轮那张审批卡还躺在聊天记录里。"""
    svc, io, llm, store = run_to_gates(CROSS, "kc-11")
    click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="第一轮")
    old = next(c for c in approval_cards(io, seq=1) if c["target"] == "ou_甲")
    svc.approve_escalation("kc-11", "g", by="ou_甲")
    redo_a(svc, io, store, "kc-11")
    before = dict(svc.status("kc-11"))

    out = press(svc, old, APPROVE, operator="ou_甲", token="tk-old")

    assert out.get("rejected") == "already_settled" or out.get("skipped") == "stale", out
    assert svc.status("kc-11") == before


# ---------- 坏封套 / 投影失败 ----------

def test_a_bogus_decision_is_not_treated_as_approval():
    """封套可伪造：`decision` 只认 approve / reject，别的一律当没发生。"""
    svc, io, llm, _ = escalate("kc-12")
    card = next(c for c in approval_cards(io) if c["target"] == "ou_甲")

    out = press(svc, card, APPROVE, operator="ou_甲", decision="奇怪的东西")

    assert out.get("skipped"), out
    assert svc.status("kc-12")["a"] == "done"
    assert len(svc.pending_escalations("kc-12", "g")) == 1


def test_a_failed_card_send_is_recorded_as_not_notified():
    """发卡失败的人不许进 `notified`（那是假审计，ADR-034）。

    而令牌那把尺照样让他拍得动：他确实是审批人，只是我们没通知到他。
    """
    svc, io, llm, store = run_to_gates(CROSS, "kc-13")
    real = io.send_card

    def flaky(*, target, summary, buttons, idem_key):
        if target == "ou_owner":
            raise RuntimeError("飞书 500")
        return real(target=target, summary=summary, buttons=buttons, idem_key=idem_key)

    io.send_card = flaky
    click(svc, io, "g", "打回", operator="ou_乙", reopen=["a"], comment="材料不全")

    rec = svc.escalations("kc-13", "g")[0]
    assert rec["notified"] == ["ou_甲"] and rec["notify_failed"] == ["ou_owner"]
    assert svc.approve_escalation("kc-13", "g", by="ou_owner").get("approved") is True


def test_a_failed_card_update_never_undoes_the_verdict():
    svc, io, llm, _ = escalate("kc-14")
    card = next(c for c in approval_cards(io) if c["target"] == "ou_甲")

    def boom(**kw):
        raise RuntimeError("飞书 500")

    io.update_card = boom
    assert press(svc, card, APPROVE, operator="ou_甲", token="tk-1").get("approved") is True
    assert svc.status("kc-14")["a"] == "pending"


def test_the_gates_own_card_still_works_after_all_this():
    """审批卡与门自己那张卡挂在同一个 node_id 上，路由不许把两者搞混。"""
    svc, io, llm, _ = escalate("kc-15")
    card = next(c for c in approval_cards(io) if c["target"] == "ou_甲")
    press(svc, card, REJECT, operator="ou_甲")          # 申请被驳回，门还在等乙
    assert svc.status("kc-15").get("g", "pending") == "pending"
    assert click(svc, io, "g", "通过", operator="ou_乙").get("resumed"), "乙 手里那张卡照样能点"
    assert svc.status("kc-15")["g"] == "done"
