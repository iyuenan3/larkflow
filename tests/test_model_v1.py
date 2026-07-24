"""v1 节点契约（executor × role + 配置）与模板护栏单测。

对应 SPEC「模板节点契约（ADR-015）」+ CONVENTIONS「模板生成护栏」。
零外部依赖：纯数据进、纯校验出。
"""
import copy

import pytest

from larkflow.model import load_template, validate_template
from larkflow.model.node import is_gate, is_produce, node_by_id
from larkflow.model.template import TemplateError


def base_dag() -> list[dict]:
    """最小合法 v1 模板：三型齐全 + 一道 human gate（有可回退祖先）+ 人定稿。

    留两个 human 节点，好让「把 review 换成别的 executor」的护栏用例不被护栏①抢先拦下。
    """
    return [
        {"id": "seed", "label": "收集素材", "executor": "tool", "role": "produce",
         "deps": [], "deliverable": {"region": "whole"}},
        {"id": "draft", "label": "AI 起草", "executor": "llm", "role": "produce",
         "deps": ["seed"], "prompt": "照素材写一稿", "model_role": "writer",
         "deliverable": {"region": "whole"}},
        {"id": "review", "label": "法务复核", "executor": "human", "role": "gate",
         "deps": ["draft"], "assignee_role": "法务", "signal": "card_action",
         "approval_policy": "single"},
        {"id": "finalize", "label": "负责人定稿", "executor": "human", "role": "produce",
         "deps": ["review"], "assignee_role": "负责人", "signal": "task_complete",
         "deliverable": {"region": "whole"}},
    ]


def mutate(nid: str, **fields) -> list[dict]:
    """改某节点字段后返回整张 dag（值为 None 表示删字段）。"""
    dag = base_dag()
    node = node_by_id(dag, nid)
    for k, v in fields.items():
        if v is None:
            node.pop(k, None)
        else:
            node[k] = v
    return dag


# ---------- 基础形状 ----------

def test_base_dag_is_valid():
    validate_template(base_dag())  # 不抛即通过


def test_role_helpers():
    dag = base_dag()
    assert is_produce(node_by_id(dag, "draft"))
    assert not is_gate(node_by_id(dag, "draft"))
    assert is_gate(node_by_id(dag, "review"))


def test_reject_unknown_executor():
    with pytest.raises(TemplateError, match="executor"):
        validate_template(mutate("seed", executor="robot"))


def test_reject_unknown_role():
    with pytest.raises(TemplateError, match="role"):
        validate_template(mutate("seed", role="review"))


def test_reject_seg1_legacy_fields():
    """旧契约 type / on_fail / gate 已废：留在模板里必须报错，避免静默失效。"""
    dag = base_dag()
    dag[0]["type"] = "tool"
    with pytest.raises(TemplateError, match="type"):
        validate_template(dag)

    dag = base_dag()
    node_by_id(dag, "review")["on_fail"] = "draft"
    with pytest.raises(TemplateError, match="on_fail"):
        validate_template(dag)


def test_reject_duplicate_id_and_dangling_and_cycle():
    dag = base_dag()
    dag.append(dict(dag[0]))
    with pytest.raises(TemplateError, match="重复"):
        validate_template(dag)

    with pytest.raises(TemplateError, match="依赖不存在"):
        validate_template(mutate("draft", deps=["nope"]))

    dag = base_dag()
    node_by_id(dag, "seed")["deps"] = ["review"]
    with pytest.raises(TemplateError, match="环"):
        validate_template(dag)


# ---------- 字段级护栏 ----------

def test_produce_requires_deliverable():
    with pytest.raises(TemplateError, match="deliverable"):
        validate_template(mutate("draft", deliverable=None))


def test_deliverable_region_must_be_whole_or_section():
    with pytest.raises(TemplateError, match="region"):
        validate_template(mutate("draft", deliverable={"region": "半篇"}))
    # section（共享协同，v2）在 schema 层合法：模型统一、实现分期（ADR-018）
    validate_template(mutate("draft", deliverable={"region": {"section": "第三条"}}))


def test_gate_must_not_declare_deliverable():
    with pytest.raises(TemplateError, match="deliverable"):
        validate_template(mutate("review", deliverable={"region": "whole"}))


def test_llm_requires_prompt_and_model_role():
    with pytest.raises(TemplateError, match="prompt"):
        validate_template(mutate("draft", prompt=None))
    with pytest.raises(TemplateError, match="model_role"):
        validate_template(mutate("draft", model_role=None))


def test_gate_requires_approval_policy():
    with pytest.raises(TemplateError, match="approval_policy"):
        validate_template(mutate("review", approval_policy=None))
    with pytest.raises(TemplateError, match="approval_policy"):
        validate_template(mutate("review", approval_policy="随便"))
    # 会签 / 阈值（v1.3 runtime）在 schema 层合法
    validate_template(mutate("review", approval_policy="all"))
    validate_template(mutate("review", approval_policy={"threshold": "反对 > 1/3"}))


def test_human_requires_signal():
    with pytest.raises(TemplateError, match="signal"):
        validate_template(mutate("review", signal=None))
    # message 变体推迟（ADR-021：定稿信号只认结构化）
    with pytest.raises(TemplateError, match="signal"):
        validate_template(mutate("review", signal="message"))


# ---------- 护栏 ①..⑤（CONVENTIONS 编号） ----------

def test_guardrail_1_all_three_executors_present():
    dag = base_dag()
    node_by_id(dag, "seed")["executor"] = "llm"
    node_by_id(dag, "seed")["prompt"] = "x"
    node_by_id(dag, "seed")["model_role"] = "writer"
    with pytest.raises(TemplateError, match="护栏①"):
        validate_template(dag)


def test_guardrail_2_gate_needs_reopenable_ancestor():
    dag = base_dag()
    node_by_id(dag, "review")["deps"] = []   # 无祖先 → 打回无处可回
    with pytest.raises(TemplateError, match="护栏②"):
        validate_template(dag)


def test_guardrail_3_auto_gate_must_be_tool_and_human_gate_not_auto():
    # llm 绝不自动放行：AI 评审须落成 (llm, produce) 出意见 + human gate 拍板
    with pytest.raises(TemplateError, match="护栏③"):
        validate_template(mutate("review", executor="llm", approval_policy="auto",
                                 prompt="审", model_role="reviewer", signal=None,
                                 assignee_role=None))
    # human + auto 自相矛盾（挂人却不问人）
    with pytest.raises(TemplateError, match="护栏③"):
        validate_template(mutate("review", approval_policy="auto"))
    # tool + auto = 确定性机检自动放行，合法
    validate_template(mutate("review", executor="tool", approval_policy="auto",
                             signal=None, assignee_role=None))
    # tool + single 无意义（无人可审）
    with pytest.raises(TemplateError, match="护栏③"):
        validate_template(mutate("review", executor="tool", signal=None, assignee_role=None))


def test_guardrail_4_human_needs_assignee_and_vote_needs_primary():
    with pytest.raises(TemplateError, match="护栏④"):
        validate_template(mutate("review", assignee_role=None))
    # 多人节点：voters 里必须有且仅有一名主负责人
    with pytest.raises(TemplateError, match="护栏④"):
        validate_template(mutate("review", vote={"voters": ["法务", "财务"], "primary": "商务"}))
    with pytest.raises(TemplateError, match="护栏④"):
        validate_template(mutate("review", vote={"voters": ["法务", "财务"]}))
    # 合法多人节点（vote 是 v1.3 runtime，schema 层 v1 就得认，不报错）
    validate_template(mutate("review", vote={"voters": ["法务", "财务"], "primary": "法务",
                                             "policy": "any"}))


def test_guardrail_5_when_guard_must_reference_ancestor_decision():
    with pytest.raises(TemplateError, match="护栏⑤"):
        validate_template(mutate("review", when={"nope": "A"}))
    dag = base_dag()
    dag.append({"id": "late", "label": "后置决策", "executor": "tool", "role": "produce",
                "deps": ["review"], "deliverable": {"region": "whole"}})
    node_by_id(dag, "review")["when"] = {"late": "A"}   # 引用了自己的下游 → 永不可求值
    with pytest.raises(TemplateError, match="护栏⑤"):
        validate_template(dag)
    # 合法守卫（when 是 v1.3 runtime，schema 层 v1 就得认）
    validate_template(mutate("review", when={"draft": "走法务"}))


# ---------- 缺陷模板（seg-1 载体，已迁 v1 契约） ----------

def test_defect_template_migrated_to_v1():
    dag = load_template("defect")
    validate_template(dag)
    assert {n["id"] for n in dag} == {"intake", "triage_ai", "triage_review", "reproduce",
                                      "assign", "fix", "qa_verify", "close"}
    assert {n["executor"] for n in dag} == {"tool", "llm", "human"}
    assert {n["id"] for n in dag if is_gate(n)} == {"triage_review", "reproduce", "qa_verify"}
    for n in dag:
        assert "type" not in n and "on_fail" not in n and "gate" not in n, n


def test_load_template_returns_independent_copy():
    """dag 会被写进 state（活图会改它）：两次加载不得共享同一份可变对象。"""
    a = load_template("defect")
    b = load_template("defect")
    node_by_id(a, "intake")["label"] = "改过了"
    assert node_by_id(b, "intake")["label"] != "改过了"
    assert copy.deepcopy(a) is not a
