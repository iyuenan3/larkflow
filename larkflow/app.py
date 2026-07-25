"""装配：把引擎 + IO + LLM + 驱动缝成一个可用的 LarkFlowService。

`build_service` 默认全本地（MockLarkIO + StubLLM + FakeDeliverableStore + 内存 SQLite），
零外部依赖，供 e2e 测试与本地演示。真飞书 / 真 LLM 阶段换成 CliLarkIO + OpenAICompatLLM
+ 文件 checkpointer 即可（见 build_real_service），引擎 / 驱动 / 模板一行不改。

**新增一个业务场景 = 新增一个 templates/<name>.yaml**：这里没有按模板名注册的东西，
tool 节点的行为由 `tool.kind` 从内置能力库选取（ADR-026）。
"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from .config import RoleResolver, env, load_llm_roles
from .engine import Executors, build_graph
from .engine.support import assert_v1_supported
from .io import Correlations, FakeDeliverableStore, MockLarkIO
from .io.deliverable import CliDeliverableIO, DeliverableIO
from .io.lark_io import CliLarkIO, LarkIO
from .llm import LLMClient, OpenAICompatLLM, StubLLM
from .model import load_template
from .model.template import validate_template
from .service import LarkFlowService
from .store import DEFAULT_LOCK_TIMEOUT, InstanceLocks, open_db


def build_service(
    template: str | list[dict] = "contract",
    *,
    conn: sqlite3.Connection | None = None,
    io: LarkIO | None = None,
    llm: LLMClient | None = None,
    resolver: RoleResolver | None = None,
    deliverables: DeliverableIO | None = None,
    tool_handlers: dict | None = None,
    llm_handlers: dict | None = None,
    strict_roles: bool = False,
    lock_factory=None,
):
    """返回 (service, io)。默认本地栈；template = 模板名或 dag。

    `lock_factory` 省略 = 进程内锁（测试 / demo 只有一个进程）。真栈注跨进程锁，
    见 `build_real_service`。
    """
    conn = conn or sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()

    io = io or MockLarkIO()
    llm = llm or StubLLM()
    resolver = resolver or RoleResolver()
    deliverables = deliverables or FakeDeliverableStore()
    if isinstance(template, str):
        dag = load_template(template)          # load_template 内部已校验
    else:
        dag = template
        validate_template(dag)                 # 直接传 dag 也必须过同一把尺，别开后门

    executors = Executors(
        io=io, resolver=resolver, llm=llm, deliverables=deliverables,
        tool_handlers=tool_handlers, llm_handlers=llm_handlers,
    )
    assert_v1_supported(dag)           # 模板别用引擎 v1 还没实现的语义（宁可不跑，不静默降级）
    executors.validate_coverage(dag)   # 每个 tool 节点都要有可执行体（kind 或逃生舱）
    if strict_roles:
        resolver.validate_coverage(dag)  # 真栈：派单对象必须真配过，别把假 open_id 发到飞书

    graph = build_graph(executors, saver)
    corr = Correlations(conn)
    service = LarkFlowService(graph=graph, io=io, correlations=corr, resolver=resolver,
                              dag=dag, executors=executors, lock_factory=lock_factory)
    return service, io


def build_contract_service(**kw):
    """v1.0 第一个 win 的合同图（本地栈）。"""
    return build_service("contract", **kw)


def build_defect_service(**kw):
    """seg-1 缺陷流（已迁 v1 契约，作为回归载体保留）。"""
    return build_service("defect", **kw)


def build_real_service(template: str = "contract", *, db_path: str | None = None,
                       identity: str = "bot", profile: str | None = None,
                       folder_token: str | None = None,
                       lock_timeout: float | None = None):
    """真实栈：真飞书（lark-cli）+ 真 LLM（多角色路由）+ 文件 SQLite checkpointer。

    `larkflow serve` / CLI 用；**跑之前需要 dev 飞书自建应用 + 事件回调配置 + LLM 角色
    env**（见 DEPLOYMENT / .env.example）。本地测试绝不构造它（会真发消息、真建文档）。

    交付物 IO 与 checkpointer 共用同一个 SQLite 连接，好让 markdown +create 的本地幂等表
    与实例运行态一起持久（该命令没有 --idempotency-key）。

    这里比 `build_service` 多两件**只在多进程下才需要**的事（见 `store.py`）：
      · `open_db` 开 WAL + busy_timeout（daemon 与一次性命令同时开着这个文件）。
      · `InstanceLocks` 当 lock_factory，把「同一实例串行」从进程内扩到跨进程。
    """
    path = db_path or env("LARKFLOW_DB", "larkflow.sqlite")
    conn = open_db(path)
    locks = InstanceLocks.for_db(
        path, timeout=DEFAULT_LOCK_TIMEOUT if lock_timeout is None else lock_timeout)
    corr = Correlations(conn)
    io = CliLarkIO(identity=identity, profile=profile)
    deliverables = CliDeliverableIO(
        identity=identity, profile=profile,
        folder_token=folder_token or env("LARKFLOW_DRIVE_FOLDER"),
        idem_store=corr.idem_store(),
    )
    llm = OpenAICompatLLM(load_llm_roles())
    return build_service(
        template, conn=conn, io=io, llm=llm,
        resolver=RoleResolver.from_env(strict=True), deliverables=deliverables,
        strict_roles=True, lock_factory=locks,
    )
