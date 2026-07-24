"""扇入（merge）与「打回省算」：v1.0 win 的引擎侧判定。

拓扑 = 商务 / 法律双起草并行 → merge 整合 → 人复核门。
两件事必须成立：
  ① merge 只是一个多 deps 的 (llm, produce) 节点，引擎扇入零改（deps 全 done 才 ready）。
  ② 复核时只打回商务一支：法律支不重算、旧 handle 直接复用，merge 拿新商务 + 旧法律重整合。
"""
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from larkflow.config import RoleResolver
from larkflow.engine import Executors, build_graph
from larkflow.engine.deliverables import prior_handle
from larkflow.engine.support import assert_v1_supported
from larkflow.io import FakeDeliverableStore, MockLarkIO
from larkflow.llm import LLMClient
from larkflow.model.template import validate_template

DAG = [
    {"id": "inputs", "label": "项目要素", "executor": "tool", "role": "produce", "deps": [],
     "deliverable": {"region": "whole"}},
    {"id": "biz_draft", "label": "商务条款起草", "executor": "llm", "role": "produce",
     "deps": ["inputs"], "prompt": "写商务条款", "model_role": "biz",
     "deliverable": {"region": "whole"}},
    {"id": "legal_draft", "label": "法律条款起草", "executor": "llm", "role": "produce",
     "deps": ["inputs"], "prompt": "写法律条款", "model_role": "legal",
     "deliverable": {"region": "whole"}},
    {"id": "merge", "label": "合并成稿", "executor": "llm", "role": "produce",
     "deps": ["biz_draft", "legal_draft"], "prompt": "把上游两稿整合成一份合同",
     "model_role": "editor", "deliverable": {"region": "whole"}},
    {"id": "review", "label": "负责人复核", "executor": "human", "role": "gate",
     "deps": ["merge"], "assignee_role": "负责人", "signal": "card_action",
     "approval_policy": "single"},
]


class CountingLLM(LLMClient):
    """每个 model_role 独立计数，产出自带版本号，便于断言「谁重算了」。"""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.calls: list[dict] = []

    def complete(self, *, prompt: str, model_role: str) -> str:
        self.counts[model_role] = self.counts.get(model_role, 0) + 1
        self.calls.append({"prompt": prompt, "model_role": model_role})
        return f"{model_role} 正文 v{self.counts[model_role]}"

    def prompt_of(self, model_role: str, nth: int) -> str:
        return [c["prompt"] for c in self.calls if c["model_role"] == model_role][nth]


def build():
    llm = CountingLLM()
    store = FakeDeliverableStore()
    ex = Executors(io=MockLarkIO(), resolver=RoleResolver(), llm=llm, deliverables=store,
                   tool_handlers={"inputs": lambda n, s, e: {"ok": True, "content": "要素正文"}})
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    graph = build_graph(ex, saver)
    cfg = {"configurable": {"thread_id": "merge-1"}, "recursion_limit": 60}
    return graph, cfg, llm, store


def test_template_valid():
    validate_template(DAG)
    assert_v1_supported(DAG)


def test_merge_fans_in_both_branches_with_zero_engine_changes():
    graph, cfg, llm, store = build()
    graph.invoke({"dag": DAG, "status": {}, "outputs": {}, "meta": {"instance_id": "merge-1"}},
                 cfg, durability="sync")

    st = graph.get_state(cfg)
    assert st.values["status"]["merge"] == "done"
    assert [i.value["node_id"] for i in st.interrupts] == ["review"]  # 挂在复核门

    merge_prompt = llm.prompt_of("editor", 0)
    assert "biz 正文 v1" in merge_prompt and "legal 正文 v1" in merge_prompt
    assert "商务条款起草" in merge_prompt and "法律条款起草" in merge_prompt  # 用 label 标注来源

    # 4 个 produce 节点 = 4 份交付物，merge 是新建的一份（不写回上游任何一份）
    assert len(store.docs) == 4
    outs = st.values["outputs"]
    tokens = {n: outs[n]["deliverable"]["token"] for n in ("inputs", "biz_draft", "legal_draft", "merge")}
    assert len(set(tokens.values())) == 4


def test_reopen_one_branch_reuses_the_other_branchs_deliverable():
    """打回省算：只重算被打回支 + 其下游；法律支的 AI 长文不重跑、handle 原样复用。"""
    graph, cfg, llm, store = build()
    graph.invoke({"dag": DAG, "status": {}, "outputs": {}, "meta": {"instance_id": "merge-1"}},
                 cfg, durability="sync")
    before = graph.get_state(cfg).values["outputs"]
    legal_handle = before["legal_draft"]["deliverable"]
    merge_handle = before["merge"]["deliverable"]

    iid = graph.get_state(cfg).interrupts[0].id
    graph.invoke(Command(resume={iid: {"passed": False, "reopen": ["biz_draft"],
                                       "comment": "商务条款账期不对"}}),
                 cfg, durability="sync")

    after = graph.get_state(cfg).values["outputs"]
    assert llm.counts["biz"] == 2       # 被打回支重算
    assert llm.counts["legal"] == 1     # 旁支没动（省下一次 AI 长文起草）
    assert llm.counts["editor"] == 2    # merge 在被打回支下游，必须重整合
    assert after["legal_draft"]["deliverable"] == legal_handle   # 旧 handle 原样复用
    assert after["merge"]["deliverable"] == merge_handle         # merge 同 handle overwrite
    assert len(store.docs) == 4                                  # 打回没新建任何文档

    # 重整合读到的是「新商务 + 旧法律」
    second = llm.prompt_of("editor", 1)
    assert "biz 正文 v2" in second and "legal 正文 v1" in second


def test_reopened_gate_reruns_and_can_pass():
    graph, cfg, llm, store = build()
    graph.invoke({"dag": DAG, "status": {}, "outputs": {}, "meta": {"instance_id": "merge-1"}},
                 cfg, durability="sync")
    iid = graph.get_state(cfg).interrupts[0].id
    graph.invoke(Command(resume={iid: {"passed": False, "reopen": ["biz_draft"]}}),
                 cfg, durability="sync")

    st = graph.get_state(cfg)
    assert [i.value["node_id"] for i in st.interrupts] == ["review"]   # 门自己也解冻重问
    graph.invoke(Command(resume={st.interrupts[0].id: {"passed": True}}), cfg, durability="sync")

    final = graph.get_state(cfg)
    assert all(final.values["status"][n["id"]] == "done" for n in DAG)
    assert prior_handle(final.values["outputs"], "merge") is not None
