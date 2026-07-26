"""点完卡片之后，卡片本身要**变样**（真跑第一条 e2e 时用户当场提出来的）。

现场原话：「点了【通过】或者【打回】，卡片没有任何变化，会让用户不知道点过了没、点了什么」。

这不是体验瑕疵，是这个项目一路在打的那类**静默**：
  · 点完没反馈 → 人以为没点上 → 再点一次（重复点击正是审批配额被烧光的燃料，见 ADR-023）
  · 打回之后旧卡失效，但它**长得和能点的卡一模一样**，下一轮再翻上去点，只会静默 no-op
  · 隔一天回看聊天记录，无法回答「这道门当时到底谁放的行」

飞书给了通道：事件里带 `token`（30 分钟内有效、最多用 2 次），
`POST /open-apis/interactive/v1/card/update` 换**整张**卡（不支持局部更新）。
卡是我们自己生成的，结构已知，不必去解析 `card_content` 的 userDSL。

**红线自检**：卡片是投影，往它写是对的方向；权威结论仍只在 checkpointer 里，
更新失败绝不能影响已经落地的裁决。
"""
from __future__ import annotations

from larkflow.app import build_service
from larkflow.config import RoleResolver
from larkflow.io.events import CARD_ACTION

ROLES = RoleResolver({"法务": "ou_falv", "甲": "ou_jia", "乙": "ou_yi"})


def graph() -> list[dict]:
    return [
        {"id": "a", "label": "AI 起草", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "g", "label": "法务复核", "executor": "human", "role": "gate", "deps": ["a"],
         "assignee_role": "法务", "signal": "card_action", "approval_policy": "single"},
        {"id": "end", "label": "收口", "executor": "tool", "role": "produce", "deps": ["g"],
         "tool": {"kind": "noop"}},
    ]


def click(svc, io, label, who="ou_falv", token="tk-1", **extra):
    av = dict(io.button_value("g", label))
    av.update(extra)
    return svc.resume_from_event({"key": CARD_ACTION, "action_value": av,
                                  "operator_id": who, "token": token,
                                  "message_id": "om_probe"})


def start():
    svc, io = build_service(graph(), resolver=ROLES)
    svc.start(instance_id="i1", reporter="ou_owner", inputs={})
    return svc, io


def settled(io):
    return io.card_updates[-1] if io.card_updates else None


# ---------- 通过 / 打回：卡片要说出发生了什么 ----------

def test_a_pass_settles_the_card_with_who_and_what():
    svc, io = start()
    assert click(svc, io, "通过").get("resumed")
    up = settled(io)
    assert up and up["token"] == "tk-1"
    text = str(up["card"])
    assert "已通过" in text and "ou_falv" in text, "谁点的要写在卡上，隔天回看要答得出"
    assert "button" not in text, "按钮必须撤掉：留着就还能点，而点了只会静默 no-op"


def test_a_reopen_says_where_it_went_back_to():
    svc, io = start()
    assert click(svc, io, "打回", reopen=["a"], comment="价款不对").get("resumed")
    text = str(settled(io)["card"])
    assert "已打回" in text
    assert "AI 起草" in text, "退回到哪一环要用**标签**说人话，不是节点 id"
    assert "价款不对" in text, "意见要留在卡上，那是这次打回的理由"
    assert "button" not in text


# ---------- 失效的旧卡：这条最值钱 ----------

def test_a_stale_card_is_marked_so_nobody_clicks_it_again():
    """打回之后这道门进入新一轮、发了新卡，**旧卡必须当场作废**。

    不标的话它和新卡长得一模一样，人翻聊天记录往上点，得到的是静默 no-op。
    """
    svc, io = start()
    old = dict(io.button_value("g", "通过"))          # 先把**旧卡**的封套存下来
    click(svc, io, "打回", reopen=["a"], comment="改", token="tk-1")
    io.card_updates.clear()
    out = svc.resume_from_event({"key": CARD_ACTION, "action_value": old,
                                 "operator_id": "ou_falv", "token": "tk-old"})
    assert out.get("skipped") == "stale", out
    text = str(settled(io)["card"])
    assert "已失效" in text and "button" not in text


# ---------- 不该动卡的那些情形 ----------

def test_an_unauthorized_click_does_not_rewrite_the_card_for_everyone():
    """卡可能已被转发，越权的是**看到卡的某个人**，不是这张卡本身。

    把「你没有权限」写上去会改掉所有人看到的内容，包括真正的负责人。
    他该收到的是一条私信（已有 `_tell`），不是把公共投影改了。
    """
    svc, io = start()
    out = click(svc, io, "通过", who="ou_路人")
    assert out.get("rejected") == "unauthorized_pass"
    assert not io.card_updates, "越权不许改卡"


def test_no_token_means_no_update_attempt():
    svc, io = start()
    av = dict(io.button_value("g", "通过"))
    svc.resume_from_event({"key": CARD_ACTION, "action_value": av, "operator_id": "ou_falv"})
    assert not io.card_updates


def test_a_failed_card_update_never_undoes_the_decision():
    """卡片是投影，权威结论在 checkpointer。飞书那一下失败了，裁决照样算数。"""
    svc, io = start()

    def boom(**kw):
        raise RuntimeError("飞书 500")

    io.update_card = boom
    assert click(svc, io, "通过").get("resumed"), "投影失败不许把已落地的裁决带走"
    assert svc.status("i1")["g"] == "done"
