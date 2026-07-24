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

## ADR-012 · 2026-07-24 · 定位升格：从「跑固定流程」到「交付物在会变的图上流转」
- Problem: 两个真实场景（合同起草、PRD 细化）表明「缺陷流」只是特例，通用形态是交付物在会变的图上被多方接力生产 / 审核、反复打回。
- Decision: 定位升格为**交付物流转引擎**。核心对象 = 交付物（合同 / PRD / 文档），核心动作 = 交付物在节点间流转 + 任意打回。缺陷流降为退化特例 + seg-1 已验证载体（交付物 = 修复、图固定单链）。
- Alternatives(否决): 守立项「缺陷流为首个场景 / 跑固定流程」定位（覆盖窄、错失真需求）。
- Tradeoff: 引擎从「跑固定缺陷流」抽象成「跑任意会变的交付物流转图」，牵动 ADR-013..018。**supersede ADR-009「首个场景 = 缺陷流」**（缺陷流保留为 seg-1 载体，不再是产品首场景）。

## ADR-013 · 2026-07-24 · 受控活图（图运行中可编辑，冻结线 = 执行前沿）
- Problem: 用户要流程图在项目进行中可变（加法务节点、再拉会议再重写）。静态模板实例做不到；纯自由画布破坏一致性 / 校验。
- Constraint: 单一事实源不破（ADR-002）；已完成节点产出不可乱改。
- Decision: **受控活图** = 冻结线随执行前沿走：done/running 冻结，只有 pending 可增删改；打回把前沿往回拉、解冻那段。**只改未来、不改历史**。运行时改图几乎免费：复用固定编排器解释 state 里 `dag`（ADR-003），`dag` 每 super-step 重读，改 state 即改图、无需重编译；合法变更只在 pending 子图内（仍是 DAG、不删在跑节点）。
- Alternatives(否决): 静态模板实例（与「运行中改图」正面冲突）；纯自由画布（校验 / 一致性 / 防环最难，破单一事实源风险）。
- Tradeoff: 需定义「合法变更」边界 + pending 子图校验。ADR-003「不 per-instance 编译」的选型正好成为活图基础。

## ADR-014 · 2026-07-24 · 打回 = 选择性重算 + 运行时多选目标 + 目标解冻
- Problem: 打回后哪些节点重跑。用户要「打回某板块只重算它 + 其下游，旁支不动」，且打回目标是审核时当场选的。
- Decision: gate 节点运行时手选一组 `S` 打回；重算集 = `S ∪ 传递下游(S)`，旁支复用旧产出（依赖 handle 稳定，ADR-016）；打回目标本身解冻。引擎复用 seg-1 已有 `stale_downstream`（传递下游闭包），把重算集重置 `pending`。
- Alternatives(否决): 模板预声明单一 `on_fail`（seg-1 缺陷流那样，静态、不支持运行时多选）；打回即全流程重来（浪费旁支产出）。
- Tradeoff: gate 产出从「通过 / 不通过」升级为「通过 / 打回哪几个 + 意见」。**refine seg-1 SPEC 的静态 `on_fail`** 为运行时 `reopen` 集。

## ADR-015 · 2026-07-24 · 节点模型 2 role × 3 executor + approval_policy（修正 win 核心）
- Problem: 有哪几类节点；审核要区分单人 / 会签 / 自动通过；win 的核心门禁形态是什么。
- Decision: 节点 = **executor(tool/llm/human) × role(produce/gate) + 配置**，两正交维度自由组合，业务差异全下沉配置，引擎不为业务新增节点类型。gate 一个配置轴 `approval_policy`（`auto` bypass / `single` / `any` / `all` 会签），吃掉「exec_mode / 自动通过」。**修正**：win 核心不是五维 AI 评分，而是「可换执行体（人 / AI）+ auto/会签 + 打回流转」；五维评分降为可选增强（Later）。
- Alternatives(否决): 按业务枚举节点类型（合同 / PRD / 视频节点，爆炸、不可复用）；强制五维 AI 评分门禁（两个真实场景的门禁其实是人拍板 + 可选 AI 审，五维非刚需）。
- Tradeoff: `human-produce` 与 `human-gate` 完成语义不同（定稿信号 vs 卡片），配置分开。**修正立项「评分门禁」为 win 卖点的表述**（CORE / PRD 已同步）。

## ADR-016 · 2026-07-24 · 交付物 = (容器, region) 统一飞书文档 handle + 飞书原生版本 + produce/consume 协议
- Problem: 交付物形态（网址 / 视频 / 文档）如何统一；两种拓扑（各自产出再合并 vs 同文档协同）如何用一个模型表达；版本化怎么做。
- Constraint: 单一事实源不破（内容是投影）；lark-cli 写能力（已验，见 MEMORY 2026-07-24）。
- Decision: 交付物 = 带 type 的飞书 handle，模型 `(容器, region)`：`whole`（独立 doc / markdown / 整篇 overwrite）、`section`（共享 doc 一段 / docx block 级）。对人是文档链接，对下游 llm 消费时 fetch 正文。**版本靠飞书原生**（稳定 handle + overwrite + 飞书 history），引擎不自建版本。**统一产出协议**：produce 末步物化到飞书交回 handle。
- Alternatives(否决): 交付物存引擎内容本体（破单一事实源、丢飞书协同 / 版本）；两种拓扑做成两套模式（`whole` 是 `section` 退化，一个抽象即可）；自建版本系统（飞书原生免费）。
- Tradeoff: 下游 llm 消费要「按需 fetch 正文」；共享协同需预划 section + docx block_id 稳定性待验（v2）。视频 / 二进制只做终态交付物。

## ADR-017 · 2026-07-24 · LLM 从 newapi 改为通用多角色 OpenAI 兼容路由
- Problem: LLM 网关选型。写代码 / 生成图片等不同任务需路由到不同模型；可能用中转站。
- Decision: LLM 走 **OpenAI 兼容接口，按任务角色路由**：每角色一组 `(base_url, api_key, model)`，可分别指向火山方舟 / 中转站 / 直连供应商，各角色独立 key。llm 节点配 `model_role` 选角色。不直连厂商专有 SDK。
- Alternatives(否决): 自建 newapi 网关（立项方案，现先不用，运维负担 + 单点）；直连单一厂商 SDK（锁定、无法按任务路由）。
- Tradeoff: 多角色配置 + key 管理下沉 `.env` / keychain。**supersede 立项「LLM 只走 newapi」红线**（CORE / ARCHITECTURE / RELATIONS / CONVENTIONS + CLAUDE.md 已同步；newapi-proxy 不再是依赖）。

## ADR-018 · 2026-07-24 · 实现分期：v1 独立 doc 拓扑 / v2 共享协同
- Problem: 两种交付物拓扑复杂度不同，一期全上风险大。
- Decision: **模型统一（`(容器,region)` 盖两种）、实现分期**。v1 只做独立 doc 拓扑（`region=whole`，markdown + merge 节点，produce 闭环已实测），先落一个「各自产出再合并」真项目（合同类）。v2 做共享协同（`region=section`，docx + 预划 section + 子项目回填 + 会签 + AI-gate）。
- Alternatives(否决): 一期同上两种拓扑（并发写 section / docx block_id 稳定性 / 子项目回填三重复杂度压 v1）；只做静态独立 doc 不留 section（模型不完整，v2 要改模型）。
- Tradeoff: v1 首个真项目具体选哪个待定（合同最贴）；共享协同的 docx section 稳定性 v2 先验。

## ADR-019 · 2026-07-24 · 前端形态：真前端（妙搭为主 / 自建 H5 备选），修订 cards-only
- Problem: 只用卡片讲不清 larkflow 的命根子（可编辑的活图 + 打回选择性重算），需要一个能「看到整张流程、在上面点和改」的前端。
- Constraint: 单一事实源不破（checkpointer 权威）；飞书原生；尽量少自建基建。
- Decision: 做真前端。**妙搭（Miaoda）为主 + 本地开发**（飞书官方 app 平台，托管 `aiforce.cloud` + 工作台原生 + 能塞自定义 UI 承载活图画布），**开放平台自建 H5 为备选**（要完全自控 / 自托管时）。前端 = 引擎的投影 + 客户端；卡片 / 任务 / 文档仍是引擎的手（hybrid）。
- Alternatives(否决): 守 cards-only（ADR-011：卡片讲不清活图，本 ADR 修订它）；aily（AI 智能体平台，做不了 app UI）；一上来纯自建 H5（自托管 web + 域名 + 证书最重，降级为备选）。
- Tradeoff: **修订 ADR-011**（cards-only → 真前端）；**松动 ADR-007**（引擎要暴露读 / 命令 API 给前端，非纯无入站端口）；引入新平台依赖（妙搭）。前端具体架构（引擎 API 形态、cards vs app 边界、画布可行性）待妙搭原型验证后细化。第一步 = 妙搭 html 创意模式可交互原型验画布。
