"""装配：把引擎 + IO + LLM + 驱动缝成一个可用的 LarkFlowService。

`build_service` 默认全本地（MockLarkIO + StubLLM + FakeDeliverableStore + 内存 SQLite），
零外部依赖，供 e2e 测试与本地演示。真飞书 / 真 LLM 阶段换成 CliLarkIO + OpenAICompatLLM
+ 文件 checkpointer 即可（见 build_real_service），引擎 / 驱动 / 模板一行不改。
"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from .config import RoleResolver
from .engine import Executors, build_graph
from .engine.support import assert_v1_supported
from .io import Correlations, FakeDeliverableStore, MockLarkIO
from .io.deliverable import DeliverableIO
from .io.lark_io import LarkIO
from .llm import LLMClient, StubLLM
from .model import load_template
from .service import LarkFlowService
from .templates import (
    CONTRACT_LLM_HANDLERS,
    CONTRACT_TOOL_HANDLERS,
    DEFECT_LLM_HANDLERS,
    DEFECT_TOOL_HANDLERS,
)

HANDLERS = {
    "contract": (CONTRACT_TOOL_HANDLERS, CONTRACT_LLM_HANDLERS),
    "defect": (DEFECT_TOOL_HANDLERS, DEFECT_LLM_HANDLERS),
}


def build_service(
    template: str = "contract",
    *,
    conn: sqlite3.Connection | None = None,
    io: LarkIO | None = None,
    llm: LLMClient | None = None,
    resolver: RoleResolver | None = None,
    deliverables: DeliverableIO | None = None,
    tool_handlers: dict | None = None,
    llm_handlers: dict | None = None,
):
    """返回 (service, io)。默认本地栈；template = 模板名（templates/<name>.yaml）。"""
    conn = conn or sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()

    io = io or MockLarkIO()
    llm = llm or StubLLM()
    resolver = resolver or RoleResolver()
    deliverables = deliverables or FakeDeliverableStore()
    dag = load_template(template)
    default_tools, default_llms = HANDLERS.get(template, ({}, {}))

    executors = Executors(
        io=io,
        resolver=resolver,
        llm=llm,
        deliverables=deliverables,
        tool_handlers=default_tools if tool_handlers is None else tool_handlers,
        llm_handlers=default_llms if llm_handlers is None else llm_handlers,
    )
    assert_v1_supported(dag)           # 模板别用引擎 v1 还没实现的语义（宁可不跑，不静默降级）
    executors.validate_coverage(dag)   # 装配期自检，别跑到一半才炸

    graph = build_graph(executors, saver)
    corr = Correlations(conn)
    service = LarkFlowService(graph=graph, io=io, correlations=corr, resolver=resolver,
                              dag=dag, executors=executors)
    return service, io


def build_contract_service(**kw):
    """v1.0 第一个 win 的合同图（本地栈）。"""
    return build_service("contract", **kw)


def build_defect_service(**kw):
    """seg-1 缺陷流（已迁 v1 契约，作为回归载体保留）。"""
    return build_service("defect", **kw)
