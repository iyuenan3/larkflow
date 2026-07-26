"""受控活图：运行中改未来（ADR-013）。

冻结线 = 执行前沿；只改 pending 子图、不删在跑节点、改完仍是合法 DAG。
另一条 as-built 事实：改图（update_state）会让**挂起中断换 id**（实测连空更新也换），
故驱动层要按 node 重绑迁移链，否则改一次图就把在等的人手里的卡片点废。
"""
import pytest

from larkflow.app import build_defect_service
from larkflow.engine.executors import ExecutorError
from larkflow.engine.livegraph import GraphEditError, apply_ops
from larkflow.io.events import CARD_ACTION
from larkflow.model.template import TemplateError
from support import card_target

DAG = [
    {"id": "a", "label": "A", "executor": "tool", "role": "produce", "deps": [],
     "deliverable": {"region": "whole"}},
    {"id": "b", "label": "B", "executor": "llm", "role": "produce", "deps": ["a"],
     "prompt": "p", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "h", "label": "H", "executor": "human", "role": "gate", "deps": ["b"],
     "assignee_role": "QA", "signal": "card_action", "approval_policy": "single"},
]
NEW_NODE = {"id": "c", "label": "C", "executor": "llm", "role": "produce", "deps": ["b"],
            "prompt": "p2", "model_role": "w", "deliverable": {"region": "whole"}}


# ---------- 纯函数：冻结线 ----------

def test_add_update_remove_pending_nodes():
    out = apply_ops(DAG, {"a": "done"}, [
        {"op": "add_node", "node": NEW_NODE},
        {"op": "update_node", "id": "h", "set": {"deps": ["b", "c"]}},
    ])
    assert [n["id"] for n in out] == ["a", "b", "h", "c"]
    assert next(n for n in out if n["id"] == "h")["deps"] == ["b", "c"]
    assert [n["id"] for n in DAG] == ["a", "b", "h"]        # 不改入参

    assert [n["id"] for n in apply_ops(DAG, {}, [{"op": "remove_node", "id": "h"}])] == ["a", "b"]


def test_frozen_nodes_cannot_be_touched():
    for status in ({"b": "done"}, {"b": "running"}, {"b": "failed"}):
        with pytest.raises(GraphEditError, match="冻结线"):
            apply_ops(DAG, status, [{"op": "update_node", "id": "b", "set": {"prompt": "x"}}])
        with pytest.raises(GraphEditError, match="冻结线"):
            apply_ops(DAG, status, [{"op": "remove_node", "id": "b"}])


def test_malformed_ops_are_rejected():
    with pytest.raises(GraphEditError, match="ops 为空"):
        apply_ops(DAG, {}, [])
    with pytest.raises(GraphEditError, match="未知 op"):
        apply_ops(DAG, {}, [{"op": "rename", "id": "b"}])
    with pytest.raises(GraphEditError, match="已存在"):
        apply_ops(DAG, {}, [{"op": "add_node", "node": DAG[0]}])
    with pytest.raises(GraphEditError, match="不存在"):
        apply_ops(DAG, {}, [{"op": "remove_node", "id": "nope"}])
    with pytest.raises(GraphEditError, match="不得改 id"):
        apply_ops(DAG, {}, [{"op": "update_node", "id": "h", "set": {"id": "h2"}}])


# ---------- 驱动层：真实例上改图 ----------

def _pause_at_triage_review():
    svc, io = build_defect_service()
    iid = "live-1"
    svc.start(instance_id=iid, reporter="ou_r", inputs={"title": "x"})
    return svc, io, iid


AUDIT = {"id": "audit", "label": "复盘小结", "executor": "llm", "role": "produce",
         "deps": ["close"], "prompt": "写复盘", "model_role": "writer",
         "deliverable": {"region": "whole"}}


def click(svc, io, node, label="通过", *, operator=None):
    """operator 默认 = 收到这张卡的人。缺 operator 的卡片事件一律不路由（fail closed）。"""
    return svc.resume_from_event({
        "key": CARD_ACTION, "action_value": io.button_value(node, label),
        "operator_id": operator or card_target(io, node)})


def test_edit_adds_future_node_and_it_runs_at_the_end():
    svc, io, iid = _pause_at_triage_review()

    res = svc.edit_graph(iid, [{"op": "add_node", "node": AUDIT}], by="ou_r", reason="测试改图")
    assert "audit" in res["nodes"]

    for node in ("triage_review", "reproduce"):
        click(svc, io, node)
    guid = list(io.tasks.values())[-1]["guid"]
    svc.resume_from_event({"key": "task.task.update_user_access_v2",
                           "event": {"task_guid": guid, "event_types": ["task_completed_update"]}})
    click(svc, io, "qa_verify")

    assert svc.status(iid)["audit"] == "done"          # 运行中加的节点真跑了
    assert svc.outputs(iid)["audit"]["deliverable"]["token"]


def test_edit_keeps_the_card_already_in_someones_hands_working():
    """改图让中断换 id：旧卡必须还能点（否则改一次图就废掉所有在等的人）。"""
    svc, io, iid = _pause_at_triage_review()
    old_card = io.button_value("triage_review", "通过")
    who = card_target(io, "triage_review")
    cards_before = len(io.cards)

    res = svc.edit_graph(iid, [{"op": "add_node", "node": AUDIT}], by="ou_r", reason="测试改图")

    assert res["remapped"] == 1
    assert len(io.cards) == cards_before               # 没重复派卡
    assert "resumed" in svc.resume_from_event(
        {"key": CARD_ACTION, "action_value": old_card, "operator_id": who})
    assert svc.status(iid)["triage_review"] == "done"


def test_edit_cannot_touch_history_or_running_frontier():
    svc, io, iid = _pause_at_triage_review()
    with pytest.raises(GraphEditError, match="冻结线"):
        svc.edit_graph(iid, [{"op": "remove_node", "id": "intake"}], by="ou_r", reason="测试改图")      # 已 done
    with pytest.raises(GraphEditError, match="冻结线"):
        svc.edit_graph(iid, [{"op": "remove_node", "id": "triage_review"}], by="ou_r", reason="测试改图")  # 正挂着人


def test_edit_must_still_pass_template_guardrails():
    svc, io, iid = _pause_at_triage_review()
    with pytest.raises(TemplateError, match="环"):
        svc.edit_graph(iid, [{"op": "update_node", "id": "close", "set": {"deps": ["close"]}}], by="ou_r", reason="测试改图")
    with pytest.raises(TemplateError, match="依赖不存在"):
        svc.edit_graph(iid, [{"op": "add_node", "node": {**AUDIT, "deps": ["nope"]}}], by="ou_r", reason="测试改图")
    with pytest.raises(TemplateError, match="护栏②"):
        # 新门禁没有可回退祖先
        svc.edit_graph(iid, [{"op": "add_node", "node": {
            "id": "g2", "label": "野门", "executor": "human", "role": "gate", "deps": [],
            "assignee_role": "QA", "signal": "card_action", "approval_policy": "single"}}], by="ou_r", reason="测试改图")


def test_edit_rejects_tool_node_without_handler():
    svc, io, iid = _pause_at_triage_review()
    with pytest.raises(ExecutorError, match="ship"):
        svc.edit_graph(iid, [{"op": "add_node", "node": {
            "id": "ship", "label": "发布", "executor": "tool", "role": "produce",
            "deps": ["close"], "deliverable": {"region": "whole"}}}], by="ou_r", reason="测试改图")


def test_edit_after_the_instance_finished_still_runs_the_new_node():
    """项目跑完了再补一个节点（复盘小结）：改图后要当场推一步，别静静躺着。"""
    svc, io, iid = _pause_at_triage_review()
    for node in ("triage_review", "reproduce"):
        click(svc, io, node)
    guid = list(io.tasks.values())[-1]["guid"]
    svc.resume_from_event({"key": "task.task.update_user_access_v2",
                           "event": {"task_guid": guid, "event_types": ["task_completed_update"]}})
    click(svc, io, "qa_verify")
    assert svc.status(iid)["close"] == "done"          # 已收口

    svc.edit_graph(iid, [{"op": "add_node", "node": AUDIT}], by="ou_r", reason="测试改图")

    assert svc.status(iid)["audit"] == "done"


def test_edit_state_is_unchanged_when_rejected():
    svc, io, iid = _pause_at_triage_review()
    before = [n["id"] for n in svc.dag]
    with pytest.raises(GraphEditError):
        svc.edit_graph(iid, [{"op": "remove_node", "id": "intake"}], by="ou_r", reason="测试改图")
    assert [n["id"] for n in svc._values(iid)["dag"]] == before
