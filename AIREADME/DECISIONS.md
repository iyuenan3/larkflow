# DECISIONS · larkflow（append-only ADR）

## ADR-001 · 2026-07-23 · 引擎选 LangGraph（有环）而非纯 DAG 编排器
- Problem: 工作流需要打回重做、iterate 循环、节点级重启、wait 挂起等**循环语义**，且要持久的人在环。
- Constraint: 飞书原生 + human 节点可挂起数天再续跑。
- Decision: 用 LangGraph（Pregel 有环运行时；interrupt + checkpointer + Send + Command）。
- Alternatives(否决): Airflow / Prefect（纯 DAG，不能环）；Temporal（重、非 agent 控制流语义）；自建引擎（成本高）。
- Tradeoff: LangGraph 执行态与业务数据须清晰分层（见 ADR-002）。

## ADR-002 · 2026-07-23 · 两层 + 单一事实源 + 飞书投影
- Problem: 领域对象（待办 / 实例 / 依赖 / 门禁）与引擎执行态如何分工，避免两套真相源打架。
- Decision: 领域 DAG = 数据模型；实例运行态由 LangGraph checkpointer 持有，是**权威写侧**；飞书任务 / 多维表格 / 文档 = **投影读侧**（从图事件同步）。CQRS 味道，映射 CC 的「单一事实源 + 广播投影」。
- Alternatives(否决): 领域 DAG 单独存 DB 与 checkpointer 并列（双真相源易漂移）。
- Tradeoff: 需要一层稳定的「图事件 → 飞书投影」同步。

## ADR-003 · 2026-07-23 · 路线 1 策展模板起步，节点契约按数据设计
- Problem: 领域 DAG 由人策展写死，还是 AI 现场生成。
- Decision: MVP 走路线 1（少量手写策展模板 spec）；节点契约按数据设计，使路线 2（AI 生成图）= 加 AI 作者节点 + 人审门，**执行器一行不改**。
- Alternatives(否决): 一上来做通用 AI-DAG 解释器（慢、AI 生成图不安全 / 易畸形、抽象易猜错）。
- Tradeoff: 覆盖面受策展模板限制，需兜底降级（未认识请求 → checklist / 推最近模板 / 转人工）。

## ADR-004 · 2026-07-23 · 飞书当核心，复用飞书原语，MVP 零自建前端
- Problem: 各模块（IM / 待办 / 项目 / 看板 / 审核）是否自建。
- Decision: 飞书当身体，复用原语：IM 机器人 + 卡片 / 任务 / 多维表格 / 云文档 / 画板 / 审批。MVP 零自建前端。
- Alternatives(否决): 自建全套前端与待办系统（重、偏离飞书原生定位）。
- Tradeoff: 受飞书原语能力与 API 限制。

## ADR-005 · 2026-07-23 · lark-cli 定位 = 出口 + 工具手；入口方案待验
- Problem: 「深度依赖 lark-cli」的边界。
- Decision: lark-cli = 出站动作（写文档 / 表格 / 任务 / 卡）+ 节点内 AI 工具 + 运维。**入站常驻事件订阅**需验 lark-cli 的 event 域是否够（长连接 / 断线重连 / 并发），不够则用飞书官方 SDK 长连接。
- Alternatives(否决): 全押 lark-cli 当事件服务器（未验证其常驻订阅能力）。
- Tradeoff: 入口可能引入第二个依赖（飞书 SDK）。
- **→ 2026-07-23 已落定（验证后）**：lark-cli 有完整 event 消费系统，`event consume <EventKey>` 连 event bus daemon、按 EventKey 订阅、**NDJSON 逐行流到 stdout**，专为 AI 子进程设计（带 ready-marker / `--timeout` / `--output-dir`）。**入口 + 出口都压 lark-cli，不引入飞书 SDK**（引擎 spawn `event consume` 子进程读其 stdout）。宿主见 ADR-007。留待 dev app 建好再验：卡片 action 与任务完成的确切 EventKey（`event list` 需 app 上下文）。

## ADR-006 · 2026-07-23 · 项目命名 larkflow / 飞流
- Decision: slug `larkflow`，中文名「飞流」。
- Alternatives(否决): weaver / 织（太文艺）；flowdesk（撞加密公司 Flowdesk）；relay（撞 relay.app 同类工作流工具）；cadence / conductor（撞 Uber Cadence / Netflix Conductor 工作流引擎）；axon（撞 Axon 警用装备）。
- Tradeoff: `lark` 略蹭飞书品牌；团队内部工具可接受。

## ADR-007 · 2026-07-23 · 引擎宿主 = alicloud-sh + SQLite checkpointer
- Problem: 引擎跑哪、用什么持久化实例运行态。
- Constraint: 手上闲置机 alicloud-sh = Ubuntu 22.04 / 2 核 / 1.6G 内存 / 40G 盘（用 11%）、开机 61 天负载近 0、只开 22 端口；内存偏小。
- Decision: 引擎宿主用 alicloud-sh；checkpointer 用 **SQLite**（langgraph-checkpoint-sqlite），单租户团队 MVP 足够，省下 Postgres 内存。事件入口是出站长连接（见 ADR-005），**该机无需开任何入站端口 / 域名 / 证书**，当前只开 22 的锁死状态正合适。
- Alternatives(否决): Postgres checkpointer（1.6G 内存吃紧）；新购云主机（现有闲置够用）。
- Tradeoff: 内存吃紧后再升配或迁 Postgres；SQLite 并发写有限，单租户可接受。

## ADR-008 · 2026-07-23 · 开发用独立飞书租户（clean-room 测试组织）
- Problem: 在哪个飞书租户建、用谁的凭证。
- Decision: 用一个独立飞书组织（个人 / 企业试用版）建应用做开发测试，凭证独立。
- Alternatives(否决): 直接在团队 / 雇主租户开发（凭证 / 数据 / 边界缠绕、风险大）。
- Tradeoff: 「要不要落到团队租户用」当后续部署决定。

## ADR-009 · 2026-07-23 · 第一张模板 = 缺陷生命周期，分两段建
- Problem: 第一张策展模板选哪个流程（唯一硬阻塞）。
- Constraint: 第一个 win 重心 = 采用 + 门禁，先证这两个。
- Decision: 缺陷生命周期（11 节点，5 门禁 5 环 + 1 旁路，tool/llm/human 三型齐全）。**分两段建**：第一段先搭「人主干 + G5 验证门禁」（`intake → triage_ai → triage_review → assign(派飞书卡) → fix → qa_verify(可 reopen 打回) → close`，`ci_test`/`code_review` 先用人工确认桩）证「采用 + 门禁」；第二段回填 `ci_test` 真 CI / `code_review` / `release_note`，补全 11 节点。
- Alternatives(否决): 特性生命周期（18 节点、工具链太重，当第二张压轴）；PR 评审（单角色、CI 门禁已被 GitHub 覆盖）；Sprint 仪式（cadence 非离散实例、建模最重）。
- Tradeoff: 缺陷流只覆盖研发缺陷场景，其余靠后续模板 + 生成补。

## ADR-010 · 2026-07-23 · 模板生成走 few-shot（种子库 + 召回范例），软化 ADR-003
- Problem: 用户要「模板按工作内容生成」，与 ADR-003「路线 1 策展起步」如何合流。
- Decision: 把策展模板当**种子库 + AI 生成的少样本范例**。用户说「给 X 类工作生成流程」时，召回最相近的 2-3 张种子当 few-shot 喂 LLM、照同一节点 schema 产新图（不从零发明）。三条护栏进生成 prompt：① 每张含 tool/llm/human 三型且各有落点；② 每道门禁配一条显式回边（杜绝只有前向边的假流程）；③ 责任 / 放行 / 风险裁决节点强制 human，绝不让 LLM 自动放行。种子库按集成重量分层（纯人协作 < 单点 CI < 全链 deploy），生成优先复用轻集成骨架。
- Alternatives(否决): 纯路线 2 从零生成图（不安全 / 易畸形 / 抽象猜错）；纯路线 1 永不生成（覆盖受限、违背用户诉求）。
- Tradeoff: 生成质量受种子库覆盖度限制；生成图仍应过人审门。**这是路线 1（策展种子）与路线 2（生成）的合流。**

## ADR-011 · 2026-07-23 · 工作台 MVP = cards-only，小程序面板延后
- Problem: 「企业应用呈现在工作台」是否要自建小程序 / H5 面板。
- Constraint: 第一个 win 先证采用，而采用靠团队在任务卡上真处理、不靠 dashboard。
- Decision: MVP = app 注册到工作台 + bot + 交互卡片 + 飞书任务，近乎零前端。小程序 / H5 全局面板延后（采用证明后、需全局视图时再加）。
- Alternatives(否决): MVP 即建小程序 dashboard 首页（不服务「先证采用 + 门禁」、增前端成本）。
- Tradeoff: leader 暂无自建全局看板，靠飞书多维表格投影凑合。
