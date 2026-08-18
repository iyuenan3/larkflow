"""交付物 (容器, region) + handle 权威登记单测（ADR-016 / ADR-020）。

红线：handle 的唯一权威登记表 = state.outputs[node_id]；跨 overwrite 稳定
（这正是选择性重算「旁支复用旧产出」的实证基础）。内容在飞书（投影），引擎只存指针。
"""
import pytest

from larkflow.engine.deliverables import materialize, prior_handle, read_upstream
from larkflow.io.deliverable import Deliverable, FakeDeliverableStore

NODE = {"id": "draft", "label": "商务条款初稿", "executor": "llm", "role": "produce",
        "deps": [], "prompt": "写", "model_role": "writer",
        "deliverable": {"region": "whole"}}


def state_with(outputs: dict | None = None) -> dict:
    return {"dag": [NODE], "outputs": outputs or {}, "meta": {"instance_id": "wf-1"}}


# ---------- Deliverable 值对象 ----------

def test_deliverable_roundtrip_dict():
    """handle 要进 state（SQLite checkpointer 序列化），故以纯 dict 存。"""
    d = Deliverable(type="docx", token="doccn123", url="https://x/doccn123", region="whole")
    assert Deliverable.from_dict(d.to_dict()) == d
    assert isinstance(d.to_dict(), dict)


# ---------- DeliverableIO / 内存实现 ----------

def test_create_then_fetch():
    io = FakeDeliverableStore()
    h = io.create(title="商务条款初稿", content="第一版", idem_key="wf-1:draft:create")
    assert h.token and h.url and h.type == "docx" and h.region == "whole"
    assert io.fetch(h) == "第一版"


def test_create_is_idempotent_on_idem_key():
    """崩溃恢复会重跑 super-step；同 idem_key 不得多出一份交付物。"""
    io = FakeDeliverableStore()
    a = io.create(title="t", content="v1", idem_key="k")
    b = io.create(title="t", content="v1", idem_key="k")
    assert a == b and len(io.docs) == 1


def test_overwrite_keeps_handle_stable_and_versions():
    """重跑走 overwrite：handle 不变（旁支复用的基础），版本由飞书原生留痕。"""
    io = FakeDeliverableStore()
    h1 = io.create(title="t", content="第一版", idem_key="k")
    h2 = io.overwrite(h1, content="第二版")
    assert h2 == h1                      # token / url 逐字不变
    assert io.fetch(h1) == "第二版"
    assert io.versions(h1) == ["第一版", "第二版"]


def test_fetch_unknown_handle_raises():
    io = FakeDeliverableStore()
    with pytest.raises(KeyError):
        io.fetch(Deliverable(type="docx", token="nope", url="", region="whole"))


def test_section_region_not_implemented_in_v1():
    """模型统一、实现分期（ADR-018）：region=section 属 v2 共享协同拓扑。"""
    io = FakeDeliverableStore()
    with pytest.raises(NotImplementedError, match="section"):
        io.create(title="t", content="x", region={"section": "第三条"}, idem_key="k")


# ---------- produce 末步：handle 写进 outputs[node_id] ----------

def test_materialize_first_run_creates_and_registers_handle():
    io = FakeDeliverableStore()
    out = materialize(io, NODE, state_with(), content="第一版")

    handle = Deliverable.from_dict(out["deliverable"])
    assert io.fetch(handle) == "第一版"
    assert prior_handle({"draft": out}, "draft") == handle   # outputs 即权威登记表


def test_materialize_rerun_overwrites_same_handle():
    """打回重算：outputs 不被清 → 第二次跑 overwrite 同一 handle，不新建文档。"""
    io = FakeDeliverableStore()
    first = materialize(io, NODE, state_with(), content="第一版")
    second = materialize(io, NODE, state_with({"draft": first}), content="第二版")

    assert second["deliverable"] == first["deliverable"]     # handle 稳定
    assert len(io.docs) == 1
    assert io.fetch(Deliverable.from_dict(second["deliverable"])) == "第二版"


def test_materialize_honors_predeclared_container():
    """deliverable.container = 活图里的声明位（写进既有容器），非第二份权威（ADR-020）。"""
    io = FakeDeliverableStore()
    existing = io.create(title="既有合同", content="占位", idem_key="pre")
    node = dict(NODE, deliverable={"region": "whole", "container": existing.to_dict()})

    out = materialize(io, node, state_with(), content="正文")

    assert out["deliverable"]["token"] == existing.token   # 不新建，写进声明的容器
    assert len(io.docs) == 1
    assert io.fetch(existing) == "正文"


def test_materialize_does_not_mutate_dag_node():
    """handle 只登记进 outputs：dag 不是 reducer channel，worker 并行写它会丢写。"""
    node = dict(NODE, deliverable={"region": "whole"})
    before = dict(node["deliverable"])
    materialize(FakeDeliverableStore(), node, state_with(), content="x")
    assert node["deliverable"] == before


def test_read_upstream_fetches_dep_contents_and_skips_unregistered():
    """下游经 outputs[dep] 取 handle 再 fetch 正文（ADR-016 consume）。"""
    io = FakeDeliverableStore()
    up_a = materialize(io, dict(NODE, id="a", label="商务稿"), state_with(), content="商务正文")
    up_b = materialize(io, dict(NODE, id="b", label="法律稿"), state_with(), content="法律正文")
    merge_node = dict(NODE, id="merge", deps=["a", "b", "note"])

    texts = read_upstream(io, {"outputs": {"a": up_a, "b": up_b, "note": {"ok": True}}}, merge_node)

    assert texts == {"a": "商务正文", "b": "法律正文"}   # note 无 handle → 跳过不报错


def test_read_upstream_sees_through_nodes_without_deliverables():
    """gate 不产交付物：不透传的话，往图里插一道复核门就切断了下游的数据流。"""
    io = FakeDeliverableStore()
    draft = materialize(io, dict(NODE, id="draft", label="初稿"), state_with(), content="初稿正文")
    dag = [
        {"id": "draft", "deps": []},
        {"id": "review", "deps": ["draft"]},      # gate：无交付物
        {"id": "publish", "deps": ["review"]},
    ]
    texts = read_upstream(io, {"dag": dag, "outputs": {"draft": draft}}, dag[2])
    assert texts == {"draft": "初稿正文"}


def test_prior_handle_none_when_unregistered():
    assert prior_handle({}, "draft") is None
    assert prior_handle({"draft": {"ok": True}}, "draft") is None
