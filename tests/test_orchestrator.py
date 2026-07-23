"""引擎纯逻辑单测：就绪判定 / 回边 / 传递下游 / 模板护栏。"""
import pytest

from larkflow.engine.gates import all_done, finish, ready_nodes, reopen_resets, stale_downstream
from larkflow.model import load_template, validate_template
from larkflow.model.template import TemplateError

DAG = load_template("defect")


def test_defect_template_valid():
    validate_template(DAG)  # 不抛即通过
    ids = {n["id"] for n in DAG}
    assert ids == {"intake", "triage_ai", "triage_review", "reproduce",
                   "assign", "fix", "qa_verify", "close"}


def test_ready_nodes_start_and_progress():
    status = {}
    ready = [n["id"] for n in ready_nodes(DAG, status)]
    assert ready == ["intake"]  # 只有无依赖的 intake

    status = {"intake": "done"}
    assert [n["id"] for n in ready_nodes(DAG, status)] == ["triage_ai"]


def test_stale_downstream_of_fix():
    # fix 的传递下游 = qa_verify + close
    assert stale_downstream(DAG, "fix") == {"qa_verify", "close"}
    # reproduce 回边到 triage_review：下游是它之后的一整串
    assert stale_downstream(DAG, "triage_review") == {
        "reproduce", "assign", "fix", "qa_verify", "close"}


def test_finish_gate_fail_marks_failed_only():
    # 修 A：worker 只标自己 failed（写不相交键，无并行竞争）；回边由 dispatch 做
    delta = finish(DAG, "qa_verify", {"passed": False})
    assert delta["status"] == {"qa_verify": "failed"}


def test_reopen_resets_reopens_upstream_chain():
    # dispatch 单点回边：qa_verify failed → fix + qa_verify + close 回 pending
    resets = reopen_resets(DAG, {"qa_verify": "failed"})
    assert resets == {"fix": "pending", "qa_verify": "pending", "close": "pending"}
    # 无 failed → 空
    assert reopen_resets(DAG, {"fix": "done"}) == {}


def test_finish_gate_pass_marks_done():
    delta = finish(DAG, "qa_verify", {"passed": True})
    assert delta["status"] == {"qa_verify": "done"}


def test_finish_no_gate_marks_done():
    delta = finish(DAG, "fix", {"passed": True, "anything": 1})
    assert delta["status"] == {"fix": "done"}


def test_all_done():
    status = {n["id"]: "done" for n in DAG}
    assert all_done(DAG, status)
    status["close"] = "pending"
    assert not all_done(DAG, status)


def test_guardrails_reject_gate_without_on_fail():
    bad = [
        {"id": "a", "label": "A", "type": "tool", "role": "-", "gate": "-", "deps": []},
        {"id": "b", "label": "B", "type": "llm", "role": "-", "gate": "-", "deps": ["a"]},
        {"id": "c", "label": "C", "type": "human", "role": "QA", "gate": "过", "deps": ["b"], "signal": "card_action"},
    ]  # c 有门禁但缺 on_fail
    with pytest.raises(TemplateError, match="护栏②"):
        validate_template(bad)


def test_guardrails_reject_missing_node_type():
    bad = [
        {"id": "a", "label": "A", "type": "tool", "role": "-", "gate": "-", "deps": []},
        {"id": "b", "label": "B", "type": "human", "role": "QA", "gate": "-", "deps": ["a"], "signal": "task_complete"},
    ]  # 缺 llm
    with pytest.raises(TemplateError, match="护栏①"):
        validate_template(bad)


def test_guardrails_reject_non_ancestor_on_fail():
    bad = [
        {"id": "a", "label": "A", "type": "tool", "role": "-", "gate": "-", "deps": []},
        {"id": "b", "label": "B", "type": "llm", "role": "-", "gate": "-", "deps": ["a"]},
        {"id": "g", "label": "G", "type": "human", "role": "QA", "gate": "过", "deps": ["b"],
         "signal": "card_action", "on_fail": "x"},  # x 是 g 的下游，非祖先
        {"id": "x", "label": "X", "type": "tool", "role": "-", "gate": "-", "deps": ["g"]},
    ]
    with pytest.raises(TemplateError, match="护栏②b"):
        validate_template(bad)


def test_guardrails_reject_human_without_signal():
    bad = [
        {"id": "a", "label": "A", "type": "tool", "role": "-", "gate": "-", "deps": []},
        {"id": "b", "label": "B", "type": "llm", "role": "-", "gate": "-", "deps": ["a"]},
        {"id": "c", "label": "C", "type": "human", "role": "QA", "gate": "-", "deps": ["b"]},
    ]  # human 缺 signal
    with pytest.raises(TemplateError, match="护栏④"):
        validate_template(bad)
