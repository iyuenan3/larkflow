"""装配：把引擎 + IO + LLM + 驱动缝成一个可用的 LarkFlowService。

build_defect_service 默认全本地（MockLarkIO + StubLLM + 内存 SQLite），零外部依赖，
供 e2e 测试与本地演示。真飞书阶段传入 CliLarkIO + OpenAICompatLLM + 文件 checkpointer 即可，
引擎 / 驱动 / 模板一行不改。
"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from .config import RoleResolver
from .engine import Executors, build_graph
from .io import Correlations, MockLarkIO
from .io.lark_io import LarkIO
from .llm import LLMClient, StubLLM
from .model import load_template
from .service import LarkFlowService
from .templates import DEFECT_LLM_HANDLERS, DEFECT_TOOL_HANDLERS


def build_defect_service(
    *,
    conn: sqlite3.Connection | None = None,
    io: LarkIO | None = None,
    llm: LLMClient | None = None,
    resolver: RoleResolver | None = None,
):
    """返回 (service, io)。默认本地栈。"""
    conn = conn or sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()

    io = io or MockLarkIO()
    llm = llm or StubLLM()
    resolver = resolver or RoleResolver()
    dag = load_template("defect")

    executors = Executors(
        io=io,
        resolver=resolver,
        llm=llm,
        tool_handlers=DEFECT_TOOL_HANDLERS,
        llm_handlers=DEFECT_LLM_HANDLERS,
    )
    graph = build_graph(executors, saver)
    corr = Correlations(conn)
    service = LarkFlowService(graph=graph, io=io, correlations=corr, resolver=resolver, dag=dag)
    return service, io
