"""引擎纯逻辑单测：就绪判定 / 门禁 / 选择性重算（打回）/ 传递下游。

模板护栏与 v1 节点契约的单测在 tests/test_model_v1.py。
"""
from larkflow.engine.gates import (
    BLOCKED,
    all_done,
    finish,
    illegal_reopen,
    ready_nodes,
    reopen_candidates,
    reopen_resets,
    reopen_targets,
    stale_downstream,
)
from larkflow.model import load_template, validate_template
from larkflow.model.node import node_by_id

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
    assert stale_downstream(DAG, "triage_review") == {
        "reproduce", "assign", "fix", "qa_verify", "close"}


def test_finish_gate_fail_marks_failed_only():
    # 修 A：worker 只标自己 failed（写不相交键，无并行竞争）；打回由 dispatch 做
    delta = finish(DAG, "qa_verify", {"passed": False})
    assert delta["status"] == {"qa_verify": "failed"}


def test_finish_gate_pass_marks_done():
    assert finish(DAG, "qa_verify", {"passed": True})["status"] == {"qa_verify": "done"}


def test_finish_produce_marks_done_regardless():
    # produce 节点不看 passed（它不把关）
    assert finish(DAG, "fix", {"anything": 1})["status"] == {"fix": "done"}


def test_reopen_targets_defaults_to_gated_upstream():
    qa = node_by_id(DAG, "qa_verify")
    assert reopen_targets(qa, {"passed": False}) == ["fix"]          # 缺省 = 把关的直接上游
    assert reopen_targets(qa, {"passed": False, "reopen": ["assign"]}) == ["assign"]  # 运行时手选


def test_reopen_resets_default_targets_gated_upstream():
    # dispatch 单点打回：qa_verify failed 且未手选 → fix + qa_verify + close 回 pending
    resets = reopen_resets(DAG, {"qa_verify": "failed"})
    assert resets == {"fix": "pending", "qa_verify": "pending", "close": "pending"}
    # 无 failed → 空
    assert reopen_resets(DAG, {"fix": "done"}) == {}


def test_reopen_resets_uses_runtime_picked_set():
    """打回目标是审核当场手选的一组（ADR-014），不在模板里预声明。"""
    resets = reopen_resets(
        DAG,
        {"qa_verify": "failed"},
        {"qa_verify": {"passed": False, "reopen": ["triage_ai"]}},
    )
    # triage_ai + 其全部传递下游 + gate 自身
    assert resets == {k: "pending" for k in
                      ["triage_ai", "triage_review", "reproduce", "assign", "fix",
                       "qa_verify", "close"]}


def test_reopen_candidates_are_transitive_ancestors():
    assert reopen_candidates(DAG, "qa_verify") == sorted(
        ["intake", "triage_ai", "triage_review", "reproduce", "assign", "fix"])
    assert reopen_candidates(DAG, "intake") == []


def test_illegal_reopen_detects_non_ancestors():
    assert illegal_reopen(DAG, "qa_verify", ["fix", "assign"]) == []
    assert illegal_reopen(DAG, "qa_verify", ["close"]) == ["close"]      # 自己的下游
    assert illegal_reopen(DAG, "qa_verify", ["qa_verify"]) == ["qa_verify"]  # 自己
    assert illegal_reopen(DAG, "qa_verify", ["nope"]) == ["nope"]


def test_illegal_targets_in_state_are_dropped_not_raised():
    """入口用引擎侧身份挡一道；state 里仍出现非法值时**绝不能抛**。

    抛出去会让此后每一次推进都在同一处炸：实例永久砖化，pending() 还谎报「无人等待」。
    降级为剔除非法目标；全非法就把这道门标 blocked 叫人。
    """
    # 一半合法一半非法：只按合法的那部分打回
    mixed = reopen_resets(DAG, {"qa_verify": "failed"},
                          {"qa_verify": {"passed": False, "reopen": ["fix", "close"]}})
    assert mixed == {"fix": "pending", "qa_verify": "pending", "close": "pending"}

    # 全非法：不抛、不乱重置，标 blocked
    only_bad = reopen_resets(DAG, {"qa_verify": "failed"},
                             {"qa_verify": {"passed": False, "reopen": ["close"]}})
    assert only_bad == {"qa_verify": BLOCKED}


def test_reopen_always_resets_the_gate_itself_so_the_loop_terminates():
    """目标是祖先 ⇒ gate ∈ 目标的传递下游 ⇒ gate 必被重置 ⇒ 打回环结构性终止。"""
    for target in reopen_candidates(DAG, "qa_verify"):
        resets = reopen_resets(DAG, {"qa_verify": "failed"},
                               {"qa_verify": {"passed": False, "reopen": [target]}})
        assert resets.get("qa_verify") == "pending", target
        assert resets.get(target) == "pending", target


def test_all_done():
    status = {n["id"]: "done" for n in DAG}
    assert all_done(DAG, status)
    status["close"] = "pending"
    assert not all_done(DAG, status)
