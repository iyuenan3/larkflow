"""受控活图的鉴权与审计（`edit_graph` 一直没有 actor 这件事）。

`unblock` 至少要求署名（`missing_audit`），`edit_graph` 连署名都不要，任何拿得到 service
的人都能改图，而且**改完什么痕迹都不留**：改过什么、谁改的、为什么改，事后完全不可考。

它比无鉴权的 `unblock` 更狠：
  · `unblock` 最多把合法祖先踢回去让人返工，代价是时间。
  · `edit_graph` 能**直接删掉一道还在等的门**，于是这道审核从此不存在，流程静默放行。
    交付物照样往下走，没有任何人收到「这道门被撤了」的信号。

在把它接到 CLI（`larkflow edit`）之前必须补上这一层：命令行是真实攻击面，而引擎侧
「一切权限在权威侧算」的红线不该在这个入口上开个天窗。

口径照 ADR-024：改 / 删一道门是 **owner 跳过审核的正路**，所以 owner-only，
不套 ADR-023 那三条（那是「让别人返工」的尺，不是「改图」的尺）。
"""
from __future__ import annotations

import pytest

from larkflow.app import build_service
from larkflow.engine.livegraph import GraphEditError

DAG = [
    {"id": "draft", "label": "AI 起草", "executor": "llm", "role": "produce", "deps": [],
     "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "gate", "label": "法务复核", "executor": "human", "role": "gate", "deps": ["draft"],
     "assignee_role": "法务", "signal": "card_action", "approval_policy": "single"},
    {"id": "close", "label": "收口", "executor": "tool", "role": "produce", "deps": ["gate"],
     "tool": {"kind": "noop"}},
]

EXTRA = {"id": "audit", "label": "补一道审计", "executor": "tool", "role": "produce",
         "deps": ["gate"], "tool": {"kind": "noop"}}


def svc_at_gate(iid):
    svc, io = build_service(DAG)
    svc.start(instance_id=iid, reporter="ou_owner", inputs={})
    assert [p["node_id"] for p in svc.pending(iid)] == ["gate"]
    return svc, io


# ---------- 鉴权 ----------

def test_the_project_owner_can_edit():
    svc, io = svc_at_gate("ed-1")
    out = svc.edit_graph("ed-1", [{"op": "add_node", "node": EXTRA}],
                         by="ou_owner", reason="合规要求补一道审计")
    assert out["edited"] == 1 and "audit" in out["nodes"]


def test_a_participant_cannot_rewrite_the_graph():
    """法务是这张图里的人，但改图不是他的权。"""
    svc, io = svc_at_gate("ed-2")
    before = [n["id"] for n in svc.dag_of("ed-2")]

    out = svc.edit_graph("ed-2", [{"op": "add_node", "node": EXTRA}],
                         by="ou_法务", reason="我想加一道")

    assert out.get("rejected") == "unauthorized_edit", out
    assert [n["id"] for n in svc.dag_of("ed-2")] == before, "被拒之后图一个字都不许变"


def test_a_stranger_cannot_delete_a_gate_that_is_waiting_on_someone():
    """这是这一层存在的理由：删掉在等的门 = 这道审核从此不存在，流程静默放行。"""
    svc, io = svc_at_gate("ed-3")
    out = svc.edit_graph("ed-3", [{"op": "remove_node", "id": "gate"}],
                         by="ou_路人", reason="碍事")
    assert out.get("rejected") == "unauthorized_edit", out
    assert [p["node_id"] for p in svc.pending("ed-3")] == ["gate"], "门还得好好在那等着"


def test_editing_without_saying_who_you_are_is_refused():
    """直调路径（运维脚本 / demo）不许因为「省事」就无名改图，与 unblock 同一条纪律。"""
    svc, io = svc_at_gate("ed-4")
    for bad in (None, "", "  "):
        out = svc.edit_graph("ed-4", [{"op": "add_node", "node": EXTRA}],
                             by=bad, reason="随便")
        assert out.get("rejected") == "missing_audit", out


def test_a_reason_is_mandatory_too():
    """「为什么改」是审计的一半：只记谁改的、不记为什么，事后照样说不清。"""
    svc, io = svc_at_gate("ed-5")
    out = svc.edit_graph("ed-5", [{"op": "add_node", "node": EXTRA}], by="ou_owner", reason="")
    assert out.get("rejected") == "missing_audit", out


# ---------- 审计 ----------

def test_every_edit_leaves_a_trail():
    svc, io = svc_at_gate("ed-6")
    svc.edit_graph("ed-6", [{"op": "add_node", "node": EXTRA}],
                   by="ou_owner", reason="合规要求补一道审计")
    svc.edit_graph("ed-6", [{"op": "remove_node", "id": "audit"}],
                   by="ou_owner", reason="搞错了，撤回")

    log = svc.edit_log("ed-6")
    assert len(log) == 2
    assert [r["by"] for r in log] == ["ou_owner", "ou_owner"]
    assert log[0]["reason"] == "合规要求补一道审计" and log[1]["reason"] == "搞错了，撤回"
    assert [op["op"] for op in log[0]["ops"]] == ["add_node"]
    assert log[0]["at"] and log[0]["nodes_after"], "改完长什么样也要留下，否则复盘要靠猜"


def test_a_refused_edit_leaves_no_trail():
    """审计记的是**发生过的事**（ADR-034）。没生效的尝试写进去就是假审计。"""
    svc, io = svc_at_gate("ed-7")
    svc.edit_graph("ed-7", [{"op": "add_node", "node": EXTRA}], by="ou_路人", reason="碍事")
    assert svc.edit_log("ed-7") == []


def test_an_edit_that_the_engine_throws_out_leaves_no_trail():
    """引擎侧校验（冻结线 / 成环 / 悬挂）拦下的同样没发生过。"""
    svc, io = svc_at_gate("ed-8")
    with pytest.raises(GraphEditError):
        svc.edit_graph("ed-8", [{"op": "remove_node", "id": "draft"}],   # 已 done，冻结线以外
                       by="ou_owner", reason="不要这一步了")
    assert svc.edit_log("ed-8") == []


def test_the_audit_channel_is_never_replayed_by_a_preserving_write():
    """与 unblocks / escalations 同类：只追加，且**绝不能进 `_write_state` 的保值集**。

    进了会怎样：`edits` 的 reducer 是 `extend_lists`（只追加不覆盖），保值写回等于把整条
    既有 log 再追加一遍，于是每推进一拍审计就翻一倍，一条 reason 被复述 N 次。

    这条测试的上一版是**空跑**：它拿 `reconcile` 当「后续推进」，而在这个现场
    `reconcile` 一次 `_write_state` 都不调（实测），于是它声称保护的不变量从未被执行到，
    把 `edits` 加进保值集的变异体照样全绿。这里直接打在不变量所在的那一层，并**断言它
    真的被调到了**，免得哪天又悄悄退化成空跑。
    """
    svc, io = svc_at_gate("ed-9")
    svc.edit_graph("ed-9", [{"op": "add_node", "node": EXTRA}], by="ou_owner", reason="补一道")
    once = len(svc.edit_log("ed-9"))
    assert once == 1

    seen, real = [], svc._write_state
    svc._write_state = lambda iid, updates, **kw: (seen.append(sorted(updates)),
                                                   real(iid, updates, **kw))[1]
    for _ in range(3):
        svc._write_state("ed-9", {})      # 泵推进拍：updates 里没有 edits
    assert seen and all("edits" not in s for s in seen), "先确认这几拍确实不带 edits"

    assert len(svc.edit_log("ed-9")) == once, "保值写回不许把审计复述一遍"


# ---------- 与冻结线的关系不许被这一层动摇 ----------

def test_being_the_owner_does_not_unfreeze_history():
    """owner 有改图权，但「只改未来、不改历史」是引擎不变量，不是权限问题（ADR-013）。"""
    svc, io = svc_at_gate("ed-10")
    with pytest.raises(GraphEditError):
        svc.edit_graph("ed-10", [{"op": "remove_node", "id": "draft"}],
                       by="ou_owner", reason="我是 owner 我说了算")


# ---------- 抛异常 ≠ 图没变 ----------

class BoomLLM:
    """只有**新加的那个节点**一执行就炸（真栈最常见：角色解析不出 / LLM 掉线）。

    原有节点照常跑完，否则连 `start` 都起不来，测不到「改图之后那一拍」这个时机。
    """

    def complete(self, *, prompt, model_role):
        if model_role == "boom":
            raise RuntimeError("LLM 掉线了")
        return f"{model_role} 正文"


def test_an_edit_that_lands_but_then_fails_to_advance_says_so_instead_of_looking_rejected():
    """`_write_state` 是**先落 checkpoint、再跑一拍**，而新节点就在这一拍上执行。

    执行体那条路上没有任何 try/except，所以新节点抛的异常会直接穿过 `edit_graph` 出去，
    而图**早就落库了**。裸抛的后果：调用方与 CLI 报「改图被拒」并退 1，人照提示重试就撞
    「id 已存在」，于是他既不知道图已经改了，也不知道该去修什么。

    真栈里这条一点都不刁钻：加一个知会某角色的 notify 节点，四道前置校验全过
    （`validate_coverage` 只扫 assignee_role 与 voters，根本不看 `tool.args`），
    到运行时 `resolver.resolve` 才抛 RoleError。
    """
    svc, io = build_service(DAG, llm=BoomLLM())
    svc.start(instance_id="ed-11", reporter="ou_owner", inputs={})
    boom_node = {"id": "extra", "label": "补一段", "executor": "llm", "role": "produce",
                 "deps": [], "prompt": "p", "model_role": "boom",
                 "deliverable": {"region": "whole"}}

    out = svc.edit_graph("ed-11", [{"op": "add_node", "node": boom_node}],
                         by="ou_owner", reason="补一段说明")

    assert "rejected" not in out, f"图已经改了，不能报成被拒：{out}"
    assert out["edited"] == 1 and "extra" in out["nodes"]
    assert "LLM 掉线了" in out.get("advance_error", ""), out
    assert "不要重试" in out.get("detail", ""), "得明确告诉人别重试"
    assert "extra" in {n["id"] for n in svc.dag_of("ed-11")}, "图确实落库了"
    assert len(svc.edit_log("ed-11")) == 1, "已生效的改图要留审计"
    errs = " ".join(e.get("error", "") for e in svc.provision_errors.get("ed-11") or [])
    assert "推进失败" in errs, "运维要能看见这条"


def test_an_edit_that_never_landed_still_raises():
    """反过来那一半不许被和稀泥：图没落库就是真的被拒，照抛。"""
    svc, io = svc_at_gate("ed-12")
    with pytest.raises(GraphEditError):
        svc.edit_graph("ed-12", [{"op": "remove_node", "id": "draft"}],
                       by="ou_owner", reason="删掉已 done 的")
    assert svc.edit_log("ed-12") == []
