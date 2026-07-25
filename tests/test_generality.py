"""通用性硬约束：这是通用交付物流转引擎，不是合同流引擎。

每条测试都对应一个「换业务场景就得改引擎」的具体死法。它们红了就意味着产品定位塌了，
不是普通回归。
"""
import pytest

from larkflow.app import build_service
from larkflow.engine.tools import TOOL_KINDS
from larkflow.io import FakeDeliverableStore
from larkflow.io.events import CARD_ACTION, TASK_UPDATE
from larkflow.model import load_template, validate_template
from support import CountingLLM

REQ = {"岗位": "后端工程师", "团队": "基础架构", "薪酬区间": "40-60k", "招聘人数": "2"}


def build(template="hiring"):
    # 正文要能过 hiring 的 auto 机检门（required=[初筛标准] + min_chars）
    llm = CountingLLM({"writer": "初筛标准：技术栈匹配、年限达标、稳定性良好；渠道清单：内推 / 猎头 / 社区。"})
    store = FakeDeliverableStore()
    svc, io = build_service(template, llm=llm, deliverables=store)
    return svc, io, llm, store


def card(io, node, label, **ov):
    return {"key": CARD_ACTION, "action_value": dict(io.button_value(node, label), **ov),
            "operator_id": "ou_op"}


def task_done(io, summary):
    guid = next(t["guid"] for t in reversed(list(io.tasks.values())) if t["summary"] == summary)
    return {"key": TASK_UPDATE, "event": {"task_guid": guid,
                                          "event_types": ["task_completed_update"]}}


# ---------- 只加 yaml 就能跑一个新业务场景 ----------

def test_a_brand_new_vertical_runs_with_zero_python():
    """招聘接力：仓库里除了 hiring.yaml 没有一行为它写的代码。"""
    import larkflow.templates as pkg
    assert not [f for f in dir(pkg) if f.endswith("HANDLERS")], "模板包里不该再有 per-template handler"

    svc, io, llm, store = build()
    svc.start(instance_id="h-1", reporter="ou_hr", inputs=REQ)

    assert svc.status("h-1")["req"] == "done"           # tool 节点靠 tool.kind 跑起来了
    assert svc.status("h-1")["jd"] == "done"
    assert [p["node_id"] for p in svc.pending("h-1")] == ["jd_review"]
    assert "后端工程师" in llm.prompt_of("writer", 0)


def test_ai_branch_keeps_moving_while_a_human_sits_on_their_task():
    """人的分支挂着，不相干的 AI 分支必须照常推进（super-step 屏障不能变成业务阻塞）。"""
    svc, io, llm, store = build()
    svc.start(instance_id="h-2", reporter="ou_hr", inputs=REQ)
    svc.resume_from_event(card(io, "jd_review", "通过"))

    st = svc.status("h-2")
    assert [p["node_id"] for p in svc.pending("h-2")] == ["interview"]   # 只等面试官
    for nid in ("sourcing", "screen", "shortlist"):
        assert st[nid] == "done", (nid, st)          # AI 那条链一路跑到机检门


def test_pure_action_node_needs_no_deliverable():
    """发通知 / 调外部系统这类纯动作节点不产文档，强制它建一份飞书文档是荒谬的。"""
    svc, io, llm, store = build()
    svc.start(instance_id="h-3", reporter="ou_hr", inputs=REQ)
    svc.resume_from_event(card(io, "jd_review", "通过"))
    svc.resume_from_event(task_done(io, "面试记录"))
    svc.resume_from_event(card(io, "decision", "通过"))
    svc.resume_from_event(task_done(io, "发放 Offer"))

    st = svc.status("h-3")
    assert all(st[n["id"]] == "done" for n in svc.dag), st
    assert "deliverable" not in svc.outputs("h-3")["notify_candidate"]
    assert any("Offer 已发出" in n["text"] for n in io.notifications)


def test_reopen_across_a_mixed_ai_and_human_frontier():
    """录用决策打回到 AI 起草的 JD：跨越 AI / 人两类节点，人的面试记录不被牵连重做。"""
    svc, io, llm, store = build()
    svc.start(instance_id="h-4", reporter="ou_hr", inputs=REQ)
    svc.resume_from_event(card(io, "jd_review", "通过"))
    svc.resume_from_event(task_done(io, "面试记录"))
    interview_handle = svc.outputs("h-4")["interview"]["deliverable"]

    svc.resume_from_event(card(io, "decision", "打回", reopen=["sourcing"], comment="渠道太窄"))

    assert llm.counts["writer"] >= 3
    assert svc.outputs("h-4")["interview"]["deliverable"] == interview_handle  # 面试记录原样复用
    assert svc.status("h-4")["interview"] == "done"                            # 面试官没被重新叫回来


# ---------- 能力库本身 ----------

def test_tool_kinds_are_business_agnostic():
    assert {"record", "summarize_links", "notify", "noop", "format_check"} <= set(TOOL_KINDS)


def test_missing_tool_kind_fails_at_assembly_with_an_actionable_message():
    dag = [{"id": "x", "label": "X", "executor": "tool", "role": "produce", "deps": [],
            "deliverable": {"region": "whole"}, "tool": {"kind": "没这个能力"}}]
    with pytest.raises(Exception, match="tool.kind"):
        build_service(dag)


def test_per_id_handler_still_wins_as_an_escape_hatch():
    dag = [{"id": "x", "label": "X", "executor": "tool", "role": "produce", "deps": [],
            "deliverable": {"region": "whole"}, "tool": {"kind": "noop"}}]
    svc, io = build_service(dag, tool_handlers={"x": lambda n, s, e: {"ok": True, "content": "定制"}})
    svc.start(instance_id="esc-1", inputs={})
    assert svc.status("esc-1")["x"] == "done"


def test_produce_that_declares_a_deliverable_but_produces_nothing_blows_up():
    """静默空洞：下游会经透传悄悄读到祖父的正文，全程无声。宁可炸。"""
    dag = [{"id": "x", "label": "X", "executor": "tool", "role": "produce", "deps": [],
            "deliverable": {"region": "whole"}, "tool": {"kind": "noop"}}]
    svc, io = build_service(dag)
    with pytest.raises(Exception, match="没产出"):
        svc.start(instance_id="hole-1", inputs={})


# ---------- 三张模板并存 ----------

@pytest.mark.parametrize("name", ["contract", "defect", "hiring"])
def test_all_shipped_templates_are_valid_and_runnable(name):
    dag = load_template(name)
    validate_template(dag)
    svc, io = build_service(name)
    svc.start(instance_id=f"tpl-{name}", reporter="ou_r", inputs={"标题": "x"})
    assert svc.status(f"tpl-{name}")            # 起得来、有推进


# ---------- 演示入口（别让它烂掉：它是唯一能用手摸到引擎的地方） ----------

@pytest.mark.parametrize("name", ["contract", "defect", "hiring"])
def test_demo_auto_runs_every_shipped_template(name, capsys):
    from larkflow.demo import run_auto

    run_auto(name)
    out = capsys.readouterr().out
    assert "收尾" in out and "交付物" in out
    assert "❌" not in out and "⛔" not in out          # 剧本应当一路走通
