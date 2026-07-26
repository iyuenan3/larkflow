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
- → 2026-07-24：其中「MVP 零自建前端」条款被 ADR-019 修订为妙搭真前端；本 ADR 的飞书原语复用 / hybrid 部分仍有效。

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
- → 2026-07-24：前端读 / 命令 API 需求（ADR-019）松动本条「无入站端口」；能否成立取决于妙搭云托管能否够到本机（见 DEPLOYMENT 传输可达性命门），不成立则退「命令走飞书原生轨、引擎只出站」保本条。
- → **2026-07-26 实测修正「无入站端口」的真实代价**：长连接**没有队列**。daemon 不在线时，人点卡片按钮**当场失败**（飞书弹红字「目标回调服务当前未在线」），该回调**不补投**。对比 webhook 模式：飞书会 POST 到服务器并重试。所以本条换来的不只是「不用开端口」，还附带一条硬约束：**引擎必须一直活着，否则全员的审批动作即时失效**。两个推论：① 进程守护（`Restart=always`）不是可选项；② ADR-031 的启动对账能补回投影（重发卡 / 重建待办），但**补不回丢掉的点击**，那条点击从未到达引擎。可接受的理由是它**失败得响**：用户当场看到红字会再点一次，不会出现「我明明点了，系统说没收到」的查无对证。

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
- → 2026-07-24：cards-only 被 ADR-019 修订为真前端（妙搭为主）；飞书原语复用 / hybrid 仍有效。

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
- → 2026-07-24：「auto / 会签」是 approval_policy 放行策略轴的卖点叙事（对比五维评分），非 v1.0 特性清单；会签(any/all/threshold) runtime 落 v1.3（ADR-025 / ROADMAP），严格 v1.0 门禁 = auto + single。

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
- → 2026-07-24：本条 v2 bundle 里「会签」随 ADR-025 提前到 v1.3、「子项目回填」随 ADR-024 提前到 v1.2；v2 只余 region=section 共享协同拓扑本身（见 ROADMAP / CHANGELOG）。

## ADR-019 · 2026-07-24 · 前端形态：真前端（妙搭为主 / 自建 H5 备选），修订 cards-only
- Problem: 只用卡片讲不清 larkflow 的命根子（可编辑的活图 + 打回选择性重算），需要一个能「看到整张流程、在上面点和改」的前端。
- Constraint: 单一事实源不破（checkpointer 权威）；飞书原生；尽量少自建基建。
- Decision: 做真前端。**妙搭（Miaoda）为主 + 本地开发**（飞书官方 app 平台，托管 `aiforce.cloud` + 工作台原生 + 能塞自定义 UI 承载活图画布），**开放平台自建 H5 为备选**（要完全自控 / 自托管时）。前端 = 引擎的投影 + 客户端；卡片 / 任务 / 文档仍是引擎的手（hybrid）。
- Alternatives(否决): 守 cards-only（ADR-011：卡片讲不清活图，本 ADR 修订它）；aily（AI 智能体平台，做不了 app UI）；一上来纯自建 H5（自托管 web + 域名 + 证书最重，降级为备选）。
- Tradeoff: **修订 ADR-011 + ADR-004 的「MVP 零自建前端」条款**（cards-only → 真前端；飞书原语复用 / hybrid 部分仍有效）；**松动 ADR-007**（引擎要暴露读 / 命令 API，或退飞书原生轨，见 DEPLOYMENT）；引入新平台依赖（妙搭）。前端↔引擎集成的一批开放问题（传输可达性 / 画布数据来源 / 改图命令回写 + 校验 + 鉴权 / cards vs app 双输入面）待原型验证 + 后续拍，记在 SPEC 待填 / DEPLOYMENT / ROADMAP（勿当已定）。

## ADR-020 · 2026-07-24 · 交付物 handle 权威登记家 = state.outputs[node_id]（deliverable.container 降为声明位）
- Problem: 交付物 = 带 type 的飞书 handle，模型 `(容器, region)`（ADR-016）。写第一个 produce 执行体前须敲定 handle 的权威登记表落在哪：`state.outputs[node_id]`（现标 scratch）还是节点 schema 的 `deliverable.container`（dag channel，活图声明位）。两者并存当权威会让 produce / merge / 选择性重算三处 churn。
- Constraint: 单一事实源不破（业务真相源 = checkpointer，ADR-002）；选择性重算「旁支复用旧产出」依赖 handle 跨 overwrite 稳定（ADR-014 / ADR-016，已实测）。
- Decision: **`outputs[node_id]` 是交付物 handle 的唯一权威登记表**（它在 state 里、随 checkpointer 持久，故仍 checkpointer 权威、飞书仍投影）。承接 seg-1 已验证机制：下游经 `state["outputs"].get(dep)` 读上游产出、reopen 从不清 outputs，故未重算旁支跨 overwrite 仍读旧 handle（选择性重算「旁支复用」的实证基础）。`deliverable.container` 降为**活图 dag 里的声明位 / produce create 后回填的指针**，非第二份权威；两者回填一致性由 produce 执行体末步保证。
- Alternatives(否决): `deliverable.container` 当权威（dag channel 上另建读回机制，活图改 dag 时须防覆盖已回填 handle，churn 更大）；引擎自存交付物内容本体（破单一事实源，ADR-016 已否）。
- Tradeoff: outputs 是 scratch reducer channel（node_id 键不相交、可交换合并），兼作登记表须守「每 worker 只写自己键 + reopen 不清 outputs」不变量（seg-1 已成立，v1 改动勿破）。**refine SPEC 产出协议 + ARCHITECTURE 交付物节 + 禁改项**（已同步）。

## ADR-021 · 2026-07-24 · 入口与意图路由：结构化 + @bot NL 双入口，都收敛到 start(template, inputs)，带确认步
- Problem: 一条飞书消息进来，引擎怎么知道要「起新项目 / 操作已有项目 / 只是问一句」？现有引擎假设「已经知道起哪个项目」（`start(template, inputs)` 被明确调用），缺一层意图识别。
- Constraint: 单一事实源不破；ADR-003 对「AI 现场决策」持谨慎（易畸形、要兜底）。
- Decision: 两个入口**都收敛到 `start(template, inputs)`**，引擎入口无关。① **结构化**：妙搭「新建项目」表单（选模板 + 填要素），精确 / 兜底。② **@bot NL（倾向主入口）**：pre-graph **意图路由层**（intent 分类 + 模板匹配 / 生成 + 要素抽取 + 兜底降级）产出一份「项目定义提议」→ **确认步**（人看 / 改 / 起）。定稿 / 审核优先用结构化信号（完成任务 / 点卡片），自由文本消息只当**内容**不当**信号**。
- Alternatives(否决): 纯结构化入口（放弃 NL 自然体感）；纯 NL 无确认（赌 AI 一次对，破 ADR-003 谨慎）。
- Tradeoff: 意图路由层是**引擎外独立一层**（v1.1 实现），引擎 headless 不依赖它。确认步把 NLU 从「赌」变「AI 提议 + 人拍板」，同构于产品 produce/gate 哲学。

## ADR-022 · 2026-07-24 · 模板生成升为主路径（few-shot 种子 + 纯生成），受控活图 + 确认降低 ADR-003 生成风险
- Problem: 用户要「主要靠 AI 现场生成业务图」，与 ADR-003「路线 1 策展起步」、ADR-010「few-shot 生成」如何合流并降险。
- Constraint: 生成图仍须结构合法（`validate_template` 护栏）+ 不破单一事实源。
- Decision: 生成升为**主路径**：有种子借种子（few-shot 检索最近 2-3 张）、无种子也能纯生成；两者都强制过护栏 + 确认步。准确率抓手：结构化输出 schema、护栏拒畸形自动重生成、确认 + 受控活图人工补正、**飞轮**（确认过的图回收进种子库）、多候选生成选优。
- Alternatives(否决): 守 ADR-003「路线 1 优先」（覆盖窄、违用户诉求）；纯生成不设护栏 / 确认（易畸形，正是 ADR-003 原顾虑）。
- Tradeoff: **softens ADR-003「路线 1 优先」与 ADR-010 定位**：ADR-003 当年怕生成，是因无确认步 / 无受控活图；二者俱在后，生成从「一次浇死」变「AI 起草图、人定稿图、运行中可改」。生成质量是独立开放轴，**v1 的 win 不押它**（win 用策展合同图，见 ROADMAP）。

## ADR-023 · 2026-07-24 · 打回权限模型：机制 × 权限两层 + 防踢皮球精确判据 + 节点负责人 / 主负责人 + escalation
- Problem: 谁能打回哪些节点？要防参与人把上游 / 同级他人的活踢回去返工（踢皮球）。
- Constraint: 机制层已有（选择性重算 reopen ⊆ 传递祖先，ADR-014 / ADR-020）。
- Decision: 打回 = **机制层 ∩ 权限层**。权限层：① **项目 owner** 可打回本项目任一祖先节点。② **参与人（人工节点 H 的负责人）** 可打回 N 当且仅当 N ∈ 传递祖先(H) 且**重算集(N ∪ 传递下游)里除 H 自己与 H 的下游人工节点外，不牵连任何别的人工节点**（串行时退化为「最多到上一个人工节点」）。③ 跨界打回走 **escalation**：通知 {项目 owner + 目标节点负责人（多人节点取主负责人）}，任一方同意即执行（轻量版）。**节点负责人**：每个人工节点 ≥1 负责人；多人节点设 1 名**主负责人**为手动打回权主体。打回权威两源：个人主体（owner / 主负责人 / 责任段参与人）+ 集体投票（A 类阈值自动，ADR-025）。
- Alternatives(否决): 只按「上一个人工节点」路径判（DAG 并行分支下漏踢皮球：打回共同上游会连累旁支他人的人工节点）；人人可全域打回（破责任边界）。
- Tradeoff: 权限层 = 纯图函数 `allowed_reopen(dag, actor, owner, assignees, from_node)`，可穷举测；候选集 = 机制合法 ∩ 权限允许，审核卡 / 画布据此过滤。escalation v1 只做「申请 + 通知 + 一键同意」。
- **→ 2026-07-25 实现状态（as-built，v0.5.0）**：权限层已落码 `larkflow/engine/permissions.py`（纯图函数 `allowed_reopen` / `reopen_verdict` / `collateral_humans` / `primary_owner` / `approvers_for`），接进 `service.resume`：actor 取自事件**顶层** `operator_id`（卡片封套里塞的身份字段一律无效，红线⑤），越权返回 `unauthorized_reopen`，跨界则落一笔申请到新的追加型 state channel `escalations` + 通知审批人且**不执行**打回（全或无：一组目标里只要有一个跨界，整笔都不执行）。身份的货币单位 = **令牌集合**（角色名 ∪ open_id），`open_id → 角色集合` 的反解在驱动层 `RoleResolver.roles_of` 做一次（一对多），纯函数层因此不必认识飞书、可穷举测。判据 ② 落成 `collateral_humans` 的四类豁免（target 自己 / H 自己 / H 的传递下游 / actor 自己担的人工节点）；**「豁免 target 自己」是从 ADR 括号里那句「串行退化为最多回到上一个人工节点」反推的，不是字面**（不豁免则 QA 连打回给开发都不行），若本意另有所指，整套判据的松紧要重定。**仍未做**：③ 的「一键同意」（没有 approve / reject 通道，记录 `status` 永远 pending，等真 dev app 的卡片回调）；`unblock(reopen=[…])` 没接这层（ADR-030 的已知绕行路）；A 类集体投票的打回权威（ADR-025，v1.3）。放行侧的身份判定不在本 ADR 范围，另见 ADR-032。

## ADR-024 · 2026-07-24 · 子项目 spawn：交付物流转递归自身 + 回填 + 边界隔离
- **Status（暂定）**：设计草案，v1.2 首个真子项目校验前，模型级取值（回填协议 / 打回粒度 / 深度上限）视为暂定、可改。
- Problem: 参与人接到一个节点（如「编写合同法律部分」），想把它拆成自己的一张流程图来完成。
- Constraint: 单一事实源不破（父子各自 checkpointer 权威）；ADR-016 / ADR-020 的交付物 handle 模型。
- Decision: **子项目 = larkflow 递归自己**。一个 produce 节点的交付物，可由「人写 / AI 写 / 一整个子项目产出」。子项目 = 独立 larkflow 项目（自己的 thread / owner / 参与人），其最终交付物 handle **回填**父节点 `outputs[node]`（ADR-020）。父节点挂起等子实例完成信号（**复用 interrupt / 挂起 + 关联表 + 幂等**，与 human 节点等人点卡同机制）。边界：父 owner 可打回父节点（= 整个子项目重开），**够不到子项目内部**；子 owner 全权管子内部，规则递归。
- Alternatives(否决): 节点只能人 / AI 直接产出（表达不了多方接力的自然下钻）。
- Tradeoff: 新增 spawn + 回填 + 父子关联（扩关联表）+ 防下钻失控（深度上限）。打回粒度 v1 简单：「打回节点 = 整个子项目重开」。**refine ADR-018：子项目从 v2 提到 v1.2**；子项目内部选择性重算留 v2。

## ADR-025 · 2026-07-24 · 多人节点：投票门(A) / 决策表决(B) + 条件分支（when 守卫 / skipped）
- **Status（暂定）**：设计草案，v1.3 首个真多人 / 分支项目校验前，模型级取值（默认支 / skipped 复活 / 投票阈值 / escalation 阈值）视为暂定、可改。
- Problem: 多人节点怎么建模；「审上游交付物」与「对未来的决策表决」是两回事；条件分支怎么表达而不新增节点类型。
- Constraint: 节点契约恒为数据、引擎不为业务新增节点类型（ADR-015 红线）。
- Decision: 多人节点 = `human × role + vote 配置`。**投票 = 会签(any/all) 推广到阈值**。
  - **A 类 审批投票门**（role:gate）：阈值 `approval_policy`（如 `reopen_if 反对 > 1/3`）；票到阈值 → 引擎**自动** pass / reopen（集体投票 = 打回权威，是「只有主负责人打回」的例外）；reopen 目标 = 把关的上游（默认，主负责人可加宽）。
  - **B 类 决策表决**（role:produce）：产出决策值到 `outputs[node]`，**不自动打回**；要打回上游只能主负责人手动。
  - **条件分支**：B 永远只产决策值；下游用法分两种 —— 当参数读 = 参数化下游（情况 1）；带 `when: {B: 值}` 守卫 = 选分支（情况 2），未匹配节点标 `skipped`（置灰）。引擎加两零件：节点 `when` 字段 + `skipped` 终态。ready 规则见 SPEC；**分支从 deps + 守卫涌现，引擎不识「分支」概念**。
- Alternatives(否决): 按业务枚举投票 / 分支节点类型（爆炸）；单独维护「分支集」数据（deps + 守卫已能涌现，多余）。
- Tradeoff: 三条新护栏 / 不变量 —— 决策取值域须被分支守卫全覆盖（或留默认支，否则某取值下全 skip、饿死汇合点）；打回决策 = skipped 复活（`reopen_resets` 加 skipped→pending）；**置灰 ≠ 删除**（skipped 是引擎按决策没跑、可复活；活图删除是 owner 拿掉、没了）。**extends ADR-015**（`approval_policy` 加 threshold）。投票 + 分支落 v1.3。

## ADR-026 · 2026-07-25 · tool 节点 = 数据化能力库（`tool: {kind, args}`），per-id handler 降为逃生舱
- Problem: v1.0 as-built 里 tool 节点的行为只能靠「按 node id 注册的 Python handler」提供，装配期 `validate_coverage` 缺了就整张模板拒跑。叠加当时的护栏①（三型齐全，强制每张图都有 tool 节点），推论是：**不存在任何一张只加 yaml 就能跑的业务图**。实测两条：一张纯 llm+human 的 PRD 接力图被护栏①拒；为凑三型补一个 `archive` 节点后被 `tool 节点缺 handler` 拒。
- Constraint: 禁改项「节点契约恒为数据，使生成 = 加 AI 作者 + 人审门、执行器一行不改」；ADR-022 把模板生成定为 v1.1 主路径，而 AI 只能生成 YAML、生不出 Python。
- Decision: 节点契约新增 `tool: {kind, args}`。`kind` 取自一张**与模板无关**的内置能力注册表（`larkflow/engine/tools.py`，v1 实装 `record` / `summarize_links` / `notify` / `noop` / `format_check` / `expect_fields`），业务参数下沉 `args`。`validate_coverage` 只校验 kind 可解析。按 node id 注册的 handler 保留为**逃生舱**（真正一次性的确定性代码），不再是唯一路径。随之删除 `contract_handlers.py` / `defect_handlers.py`，`templates/` 目录只剩 yaml。
- Alternatives(否决): 保持 per-id handler 只放开护栏①（新业务仍要写 Python + 改装配表，生成主路径仍不成立）；让 AI 生成 Python handler（不安全、不可审）。
- Tradeoff: 能力库的覆盖度成为新的产品边界（不够用就得加 kind，但那是**一次性跨业务投资**，不是每个模板一份）。撞名风险同时消失：注册表按 kind 索引，不再按 node id，`close` / `checks` 这类高频命名不会跨模板串业务（as-built 曾实测放行）。**refine ADR-015 的节点契约**（SPEC 已同步）。

## ADR-027 · 2026-07-25 · 护栏①「三型齐全」降级为 lint，不作运行准入
- Problem: ADR-010 原文是「三条护栏**进生成 prompt**」，as-built 却把「每张图 tool/llm/human 三型齐全」实现成 `validate_template` 的 raise，并且经模板加载 / `start(template=…)` / **每次 `edit_graph`** 三条路径生效。
- Constraint: 通用产品最常见的两类流程恰好不满足它 —— 纯人协作（招聘接力 / 采购审批 / 报销）与纯 AI+人（视频脚本 / PRD 初稿）。ADR-010 自己的种子库分层第一层就是「纯人协作」。
- Decision: 从 `validate_template` 移除，改为 `lint_template(dag) -> list[str]` 的风格提示（供生成器与人审门用）。结构不变量（id 唯一 / deps 不悬挂 / 无环 / 护栏②③④⑤ / 字段级）全部保留为硬校验。
- Alternatives(否决): 保留硬校验但给模板加豁免开关（等于承认它不是不变量，还多一个字段）。
- Tradeoff: 生成器可能产出「没有任何把关节点」的图，故 lint 额外提示这一条；把关的必要性由人审门保证。附带修掉一个活图缺陷：运行中删掉图里最后一个 llm 节点这种日常编辑不再被拒。

## ADR-028 · 2026-07-25 · 绕开 LangGraph 的 super-step 屏障：保值写回 + 借位重排
- **Status**：实现约束（非产品决策），记下来是因为它反直觉、且踩过两次。
- Problem: LangGraph 的 super-step 是屏障：只要有人工节点挂在 `interrupt` 上，`dispatch` 就不会再执行。实测两个后果：① gate 判了打回却落不了地（上游不重算、没人被通知，直到那个不相干的人碰巧响应）；② **完全不相干的并行分支一起停**（B 跑完，C/D/E 全卡在另一条支的签字上）。多方并行接力正是本产品的定义形态。
- Constraint: 不 per-instance 编译新图；打回重置逻辑必须留在引擎里（不在驱动层重算）；`update_state` 会落新 checkpoint，而在飞的 super-step 里**已完成任务的写入尚未提交**，不保值就会被静默丢掉（实测：刚点的裁决连同意见变 `None`）。
- Decision: 驱动层 `_write_state` 做两件事 —— ① **保值**：把当前观测到的 `status` / `outputs` / `dag` 原样带上再 `update_state`；② **借位**：`as_node=<某 worker>`，其唯一出边就是 `dispatch`，于是 dispatch 真的执行一次（打回逻辑仍在引擎里）。`_advance` 据「按 dag 该就绪却没在飞」的判据一拍一拍推，直到没活可干。
- Alternatives(否决): 驱动层自己算重置写回（打回逻辑分裂成两份）；给 status 加代次计数器让 worker 直接写重置（改动 status 表示，波及全部读写方）；放弃并行（与产品定位冲突）。
- Tradeoff: 每一拍都会让挂起中断换 id，靠已有的 `interrupt_remap` 重绑（旧卡继续有效、不重复派单）。`reopen_counts` 用累加 reducer，故保值写回**不带**它。

## ADR-029 · 2026-07-25 · 打回预算 + `blocked` 终态
- Problem: auto 机检门 + 产不出合格内容的上游 = 无限重算，单次 invoke 里 super-step 一路涨到 `recursion_limit` 才崩，实例停在半截、投影孤悬、谁也不知道发生了什么（实测于招聘图的机检门）。这是通用产品的常态：AI 未必满足得了机检。
- Decision: 每道门记打回次数（`reopen_counts`，dispatch 单写者、累加 reducer），超预算（节点可配 `reopen_budget`，默认 3）则把该门标 `blocked` 终态而非继续重算，并通知发起人「已停下等人介入」。`blocked` 不是 `done`，下游不会解锁。
- Alternatives(否决): 只调大 recursion_limit（推迟而非解决）；无限重试（烧 LLM 额度且永不收敛）。
- Tradeoff: 兑现 MEMORY finding C 的推迟条件（「seg-2 回填自动化门禁时」）。人介入方式 v1 = 改要素 / 改图后重试 + **显式解除**（`service.unblock` / `larkflow unblock`，2026-07-25 已落码，见 ADR-030；解除后节点回 pending，受控活图这才够得着它，于是「改图后重试」这条路才真的通）。

## ADR-030 · 2026-07-25 · `blocked` 的解除通道：人显式介入 + 追加预算（不重置计数）
- Problem: ADR-029 把反复打回仍不通过的门停成 `blocked` 终态，却没留出口。那道门自己过不了冻结线（受控活图只动 pending 节点）、它的上游是 done 也动不了、每次 `reopen_resets` 又把它重新算成 blocked。发起人收到「可改要素 / 改图后重试」的通知，实际无路可走：`blocked` 是死局。
- Constraint: 只改未来不改历史（历史 `attempts` / `outputs` / `reopen_counts` 一条都不能改）；完成靠显式信号（引擎绝不自动解除，自动解除 = 把 ADR-029 消灭的无限重算原样放回来）；冻结线不许为解除而放宽（破 ADR-013）。
- Decision: 加一条**人显式触发**的通道 `unblock(instance, node, by, reason, grant=1, reopen=None)`。语义 = 把这道门放回执行前沿（status 回 pending）并**追加**一份打回预算（`真实预算 = 节点配置 + Σgrant`），可选连带解冻一组祖先（过引擎侧 `illegal_reopen` 合法域校验，不信调用方给的目标）。四条落地口径：① 额度**两层有界**（单次 grant 收进 [1,3]，同一节点累计解除不超过 3 次，耗尽即拒并通知发起人）；② 必审计（谁 / 何时 / 为什么 / 追加多少）落新的**追加型** state channel `unblocks`（reducer 只追加不覆盖），`unblock_log()` 读；③ 解除只让门回 pending，**绝不顺带放行**（放行仍须来自门自己的执行体或人的裁决）；④ 冻结线一寸不放宽，「改图后重试」靠「解除后节点回 pending、受控活图这才够得着它」自然成立。被解除重置的节点 `attempts` +1（轮次是派单幂等键的一部分，不 +1 的话人手里还是上一轮那张卡，新一轮无人被叫）。
- Alternatives(否决): 重置 `reopen_counts`（洗白审计，「这道门一共真打回过几次」从此不可考）；只封解除次数、不封单次 grant（一次 `grant=10**9` 就把预算机制原地废掉，变异测试实测退化成无限重算）；解除时顺带把门标 done（人替机检签字，破「完成靠显式信号」红线）；放宽冻结线让人直接编辑 blocked 节点（破 ADR-013）。
- Tradeoff: **没有权限层**：`by` 只进审计不做鉴权，于是 `unblock(reopen=[…])` 是一条绕过 ADR-023 的路（谁调得到 service 谁就能借解除把任意合法祖先踢回去返工）。今天调用方只有运维 / demo，可接受；**接前端或卡片按钮之前必须先补**（做法：拿 `by` 当 actor 过一遍 `reopen_verdict`，与 `resume` 用同一把尺）。没有请求级幂等：auto 门解除后常常当场再 blocked，双击 / 重放会花掉两份额度（有界且每笔可审，不是幂等）。额度耗尽后没有第二条出路，产品答案是「改图 / 换要素重开一个实例」，引擎侧不提供「换实例接力」原语（那是 ADR-021 入口层的事）。`blocked` 也不是真终态：另一道门打回共同祖先时，`reopen_resets` 会把 blocked 节点当普通下游重置回 pending，不经审计、不花额度（信息没丢，报的是同一个仍然成立的条件，故未改）。

## ADR-031 · 2026-07-25 · 常驻服务形态：一个 daemon + 一次性 CLI，多进程共用一个 SQLite
- Problem: 引擎测全绿但**跑不起来**：`EventPump` 写好了却没有任何一行生产代码把它接到 `resume_from_event`；`reconcile()` 实现了却从不在启动时跑（崩溃自愈只做了一半）；`build_real_service()` 造出来的对象没人 start、没人 serve、没有信号处理。且 ADR-030 的救场动作（解除 blocked）必须能在 daemon 跑着的时候执行，而它写的是同一个 SQLite 文件。
- Constraint: ADR-007「无入站端口」（事件靠出站长连接）；单一事实源 = checkpointer，绝不新建实例表；红线「测试全程 Mock / Stub / `:memory:`，绝不构造 `build_real_service`」（它会真发消息、真建文档）。
- Decision: 进程拓扑 = **一个常驻 daemon（`larkflow serve`）+ 若干一次性 CLI 命令**（`start / status / pending / unblock / reconcile`）。
  - daemon 的一生：装信号 → **启动全实例对账** → 起泵（每 EventKey 一条 `lark-cli event consume` 子进程 + 一条泵线程）→ block 到 SIGINT / SIGTERM → 停订阅 → 等在飞的那条事件跑完 → 关 DB。顺序是硬的：对账排在起泵之前（事件在半对账状态下进来会与推进拍打架），装信号排在最前（慢对账期间也停得下来）。
  - 实例枚举的真相源就是 checkpointer（按 thread_id 去重），**不建实例表**。对账**逐实例容错**（一个坏实例不许让整个服务起不来）、跳过已跑完的实例（没有投影要重建、重推只会重发通知）。`larkflow reconcile`（不带实例）与 daemon 启动走**同一条代码路径**，容错 / 跳过 / 报告只有一份。
  - 多进程写同一个 SQLite 选**真解决**而非「检测到 daemon 就拒绝」，三件事缺一不可：① `open_db` 开 WAL + busy_timeout（SQLite 层：读不被写堵、写与写排队而不是当场 `database is locked`）；② 跨进程 flock 按 instance_id 一把，作为 `lock_factory` 注进 `LarkFlowService`，把「同实例状态变更串行」这条既有不变量从进程内原样扩到跨进程（应用层的读改写丢更新才是真危险，丢的是人刚点下的裁决、事后无迹可查）；③ `<DB>.serve.lock` 单例锁，同一个 DB 只许一个 daemon。开不了 WAL（多半是 DB 放在网络盘上，而那里 flock 同样不可靠）**直接拒绝启动**，不降级。
  - CLI 的一切外部依赖可注入（默认 factory 才是 `build_real_service`，且函数内延迟 import），于是每条子命令都能测穿而绝不构造真栈。退出码：0 成功 / 1 运行期失败或被拒 / 2 用法错。
- Alternatives(否决): 一次性命令检测到 daemon 在跑就报错（daemon 一起来运维就再也 unblock 不了、起不了新项目，而 blocked 的出口恰恰是运维命令，产品当场做死）；新建一张实例表当枚举源（第二个真相源，一旦与 checkpointer 漂移，对账反而变成损坏源）；只在 CLI 外面套锁（daemon 侧不参与 = 等于没锁，根因位置在 service 的临界区）；起 HTTP 服务收事件（破 ADR-007）。
- Tradeoff: flock 是**建议锁**（裸 sqlite3 进来写照样能覆盖），且只在本地盘可靠。service 的锁**不可重入**：今天那 5 处临界区确认无嵌套，但将来谁在临界区里调另一个带锁的公开方法，就从「进程内瞬时自锁」变成「跨进程真死锁到超时」，这条不变量只靠约定维持。单进程模型：每 EventKey 一条泵线程串行处理，没有 worker 池，一个慢 LLM 节点会挡住同一条通道上的后续事件（单租户团队 MVP 够用）。启动对账会对每个未完成实例真跑一次推进拍，实例多了启动会变慢且这段时间入站通道还没起（无并发 / 无分批）。健康检查与指标只在内存与 stderr 日志里（ADR-007 无入站端口下有意为之）。Windows 跑不了（flock 依赖 fcntl，构造时直接抛而不静默降级成「没有锁」）。`build_real_service` 这条路**仍然零测试覆盖**（红线：测试绝不构造真栈），只把它的调用方测穿了。

## ADR-032 · 2026-07-25 · 身份判定覆盖同一张卡的两个按钮：新增应答权 `can_answer`
- Problem: ADR-023 只说了打回。落码后暴露一个更重的洞：同一张审核卡上「通过」那颗按钮**零校验**，任何拿得到 `interrupt_id` 的人（卡被转发、`assignee_role` 解析成群、封套被伪造）都能替把关人放行，还在 `outputs` 里留下他本人从未做出的「同意」。实测复现：身份判定原按 `passed` 分两支，而非 gate 节点的 fail 落在两支之外（`gates.finish` 对非 gate 根本不看 `passed`、照样标 done），陌生人把封套里的 `verdict` 改一个字就替别人把定稿签了。
- Constraint: 红线「一切权限 / 合法性在引擎权威侧算，绝不信前端回传」（卡片 `action_value` 是前端可自由构造的攻击面）。
- Decision: 加第二把尺 `permissions.can_answer(dag, actor_roles, node_id)`：**应答人集合** = `assignee_role` ∪ `vote.voters` ∪ `vote.primary`，**不含项目 owner**（打回是调度，owner 全域；放行是代签，谁的活谁签。owner 要跳过一道门有留痕的正路：受控活图改 / 删该节点，ADR-013）。分支判据从「`passed` 是不是真」换成「**这一下是不是一次打回**」（gate + 判不通过 + 目标组非空），于是「不是打回的每一下都是应答」，一律过 `can_answer`。卡片通道缺 `operator_id` 一律 **fail closed**（独立 skip 值 `unidentified_actor`，好让 daemon 日志分得清「与我无关」和「入站通道认不出人」）；任务通道保留**结构性豁免**（飞书任务事件不带完成人，身份靠「这条 task_guid 是引擎发给谁的」+ 关联表，且 `_route` 已禁止 gate 走这条路、并核对关联行 kind 必须是 task）。
- Alternatives(否决): 只判打回那半边（= 让人返工要过三条规则、让交付物生效零校验）；拿「这张卡发给了谁」当判据（卡可被转发）；把 owner 并进应答人（owner 就能伪造一条「他同意了」的审计记录，比打回越权更重）。
- Tradeoff: 「owner 不能代签」是本 ADR 拍的产品口径，ADR-023 没写；要改只需把 owner 令牌并进 `can_answer`。`assignee_role` 映射成飞书群 `oc_` 时该节点**无人可应答**（反解不出角色 → 放行与打回都被拒），真栈若配了群会把那道门卡死；解法二选一：装配期拒绝解析成群的 assignee，或引入「群成员皆可应答」语义 + 群成员查询。非 gate 节点的 fail 现在被判成一次**应答**（应答人自己传 fail 仍会把节点标 done，`gates.finish` 的语义没动）；要让 produce 节点表达「我没做完」需要新语义。任务通道只收窄到 kind=task，没有真判人。

## ADR-033 · 2026-07-25 · 外部写动作的幂等性收回本地，不外包给飞书的 1 小时窗口
- Problem: 派单（建任务 / 发卡）与通知的幂等原本押在 lark-cli 的 `--idempotency-key` 上，而飞书那个去重窗口**只有 1 小时**；人工节点等的是人，超过 1 小时是常态。实测后果：每次 `serve` 重启 / 每次 `larkflow reconcile` 都真的给所有还在等的人再发一遍卡、再建一条待办（重复的待办没有任何代码去关掉它，永远躺在人的待办列表里），「实例卡住了」这类通知也会隔夜重播。
- Constraint: ADR-031 把对账变成**每次启动都跑**的常规动作，这就把该缺陷从「偶发」变成「每次重启必现」。
- Decision: 幂等键收回本地：`_once(key, make)` 走 `correlations.idem_store()`（与交付物 `markdown +create` 复用同一张表、同一个 SQLite）。全仓统一成一条规则：**idem_key 标识「一件事」，一辈子只做一次；要让同类事情再发一次，就把区分它的东西放进 key**（`:{attempt}` / `:{seq}` / `:{已解除次数}` / `:{总轮次}` 全是这么来的）。**先调外部、成功了才记键**：失败就当没做过，下次对账自然重试。派单拿不到外部对象 id 时抛错、**不写幂等表**（记成「已派」会永久挡住重试，那个人从此没人叫，而实例看上去一切正常）。本地命中时仍补写一次关联表：「崩在建任务与写关联表之间」那种投影缺失，正是靠这一笔补回来的。
- Alternatives(否决): write-ahead（先落意图、再调 API、再落 external_id）：拿不回外部对象 id，重启后照样得再调一次，闭合不了那个崩溃窗口，只多出一个无法解释的中间态；给 `reconcile` 加「只补投影不重派」的模式（本地幂等表落地后 reconcile 天然如此，多一个模式就多一处口径）。
- Tradeoff: 残留崩溃窗口：外部调用成功、进程死在写幂等表之前，重启后仍会重调一次（超过飞书那 1 小时窗口就是真重复 + 一条永远关不掉的孤儿待办）；闭合它需要外部系统支持「按幂等键查回对象」，lark-cli 没有这个能力。本地幂等表与飞书之间**没有对账**：有人在飞书侧手删了待办 / 撤回了卡，引擎认为「已派」就再也不会补发（改之前靠 1 小时窗口过期能意外补上），强制重发只能手工清那一行、没有命令。受控活图把 pending 节点的 `assignee_role` 换人时**不会重新派单**（幂等键只含 `{实例}:{节点}:{轮次}`），新负责人收不到卡：改之前是「超 1 小时靠窗口过期意外补发」，现在是确定性地不发；修法是改图时给受影响节点 `attempts` +1，或把派单对象放进幂等键。
- 后续修正（2026-07-26，v0.5.1）：本地幂等表是**永久**的，于是「键选窄了」的后果从「隔小时补发一次」升级为**彻底静默**。已知被这条放大并修掉的一处：`blocked` 通知的键只含「已解除次数」，而 `blocked` 并不是真终态（别的门打回共同祖先就能把它重置回前沿再跑一次，那条路一次解除都没花），重新卡死时键没变、发起人再也收不到。键补上轮次。**新增外部写动作时，先问一句「同一个键会不会覆盖两件本该分开告诉人的事」。**

## ADR-034 · 2026-07-26 · 审计记录写在事情**发生之后**：投影侧事实与权威意图分离
- Problem: 跨界打回的申请记录里有个 `notified`（当时通知了谁）。原实现在**发通知之前**就把它写进权威 state，飞书那一下失败时留下一条「已通知」的假记录。审批人隔天来查「谁该拍板」，系统说通知过了、人却从没收到，于是没人再去追。同类形状还有：把「打算做的事」当成「做过的事」记进 checkpointer。
- Constraint: `escalations` / `unblocks` 都是**追加型** channel（红线：只改未来不改历史），写下去就没有 UPDATE 可以修正；所以顺序本身就是正确性的一部分，不能靠事后补写兜。
- Decision: 凡是记录「投影侧发生了什么」的字段，一律**先做、后记**，记的是结果不是意图：`_tell` 返回是否真送达，`notified` 只放送达的人，没送到的进 `notify_failed`（让对账 / 运维看得见「有申请但没人知道」）。与之相对，记录「引擎侧的意图」的字段（如 `approvers` 存角色令牌）照旧先算先写，它本来就与送达无关。两类字段在同一条记录里并存，语义分开。
- Alternatives(否决): 先写记录再发、失败后追加一条更正（追加型 channel 上要求所有读者自己做「后写覆盖先写」的归并，读者一多必然有人漏做）；把 `notified` 从权威 state 挪进日志（那样审计就不可查，等于放弃 ADR-023 ③ 要的「系统里查得到这笔申请」）。
- Tradeoff: 反向窗口变实：通知发出去了、写 state 之前进程死掉 = 人收到了、系统里查无此事。取这一侧是因为它**可恢复**（收到的人一问就发现，申请人重点一次即可），而假审计不可恢复且会主动误导人停止追查。extends ADR-023 ③。

## ADR-035 · 2026-07-26 · 推进的收敛判据要看累加通道，不能只看 `status`
- Problem: `_advance` 判断「这一拍到底动了没有」只比 `status` 快照。一道门**重试再次失败**时，状态从 `failed` 出发，经 pending、重跑，又回到 `failed`：前后快照逐字相同，于是被当成「推不动了」提前返回，实例停在 `failed` 而不是 `blocked`。后果是双重的，且两条都指向 ADR-029 / ADR-030：`blocked` 通知不发（没人知道它又死了），`unblock` 还会以 `not_blocked` 拒绝它，承诺的出口当场失效。
- Constraint: 那个提前返回不能删（ADR-028 的推进循环靠它防空转）；判据必须是**单调**的，否则一个来回就抵消。
- Decision: 收敛判据 = `(status, reopen_counts, attempts)` 三元组。后两个是累加型 reducer 通道、只增不减，正好补上 status 看不见的那一拍。
- Alternatives(否决): 用推进拍数当唯一上界（回到 ADR-029 要消灭的「撞上限才发现」）；比 checkpoint id（每次 `update_state` 都换，等于永不收敛、必然空转到上界）。
- Tradeoff: 判据变宽 = 极端情况下多推一两拍才停，代价是常数级；相比之下「实例停在没有出口的中间态」是死局。**这条是自查时撞出来的，四个视角的对抗 review 一条都没报**：它需要「门重试再次失败」这个具体时序才现形，静态读代码看不出来。extends ADR-028。

## ADR-036 · 2026-07-26 · LLM 备用线路：每角色一条有序链，缺项继承主配置
- Problem: 一个 `llm produce` 节点跑挂 = 那一支的产出没了，而这条图的上游可能已经花掉真人的时间。LLM 供应商掉线 / 限流 / key 过期是**常态不是异常**，不该让它把整个项目停在半截。原实现每角色只有一组 `(base_url, api_key, model)`，挂了就是挂了。
- Constraint: 不引入厂商专有 SDK（ADR-017），凭证只从 env 读、绝不入库；不能为此在模板里加字段（模板是业务的，供应商是运维的，混在一起等于让写业务流程的人操心限流）。
- Decision: 每个角色是一条**有序链**：`LLM_<ROLE>_BACKUP_*`、`BACKUP2_*`… 按序号排队，主线路打不通就顺着往下试。**缺项继承主配置**，于是只写 `BACKUP_API_KEY` = 同供应商同模型换一把 key（被限流时最常见），三项都填 = 换一家。可切的错误：连不上 / 超时 / 429 / 5xx / 401 / 404；**400 与 422 不切**（是我们自己的请求有问题，换线路只会原样再错一次还多烧一次钱）。切换**必须留痕**（`failovers` + `on_failover` → daemon 日志）。未命中的角色回退 default 时**连同 default 的备用链一起**。
- Alternatives(否决): 在模板节点里配备用（把运维关切焊进业务图，ADR-026 刚把这类东西赶出去）；靠 openai SDK 自带 retry（只在同一个端点上重试，供应商整体掉线时无用）；无脑重试所有错误（400 类请求错误会被重试两遍，账单翻倍且永远不会成功）。
- 实测（2026-07-26，火山方舟 `/api/coding/v3` + doubao-seed-2.1-turbo，测试组织）：主 / 备两把 key 均连通；把主线路换成假 key，真实的方舟 401 被正确判成可切换并自动落到备用（3.7s），切换原因留痕。单测用的是伪造异常，这一轮是真 401，判据对得上。
- **超时按角色可配**（同日实测逼出来的）：一次真实起草 **109.7s / 2570 字**，而当时默认 60s，`biz_draft` 必被掐断，且那一刻飞书文档已建、任务已派，人看到的只是「AI 那步失败了」。默认提到 300s，并加 `LLM_TIMEOUT` / `LLM_<ROLE>_TIMEOUT`（备用继承主配置）。一个数字盖不住所有角色：起草要几分钟，机检 / 分诊几秒就够。顺带一个成本信号：让它只回「ok」两个字也花了 241 token，说明这个模型产生大量推理 token，真实账单会显著高于可见输出。
- Tradeoff: **主线路可以静默死很久**，只要备用一直顶着，所以留痕是这条决策里不可省的一半，不是附赠品。客户端缓存键必须含 api_key 的哈希：只按 `(base_url, model)` 做键的话，「同端点换把 key」会命中主线路那个客户端对象，等于拿同一把挂掉的 key 再试一次，备用形同虚设（这条有专门的测试钉住）。角色正则必须排除 `BACKUP` 段，否则 `LLM_WRITER_BACKUP_BASE_URL` 会被读成「角色 writer_backup 的主配置」，凭空多出一个没人用的角色而备用静默失效。失败的那一次调用可能已经产生费用，切换后是重新生成、不是续写。

## ADR-037 · 2026-07-26 · 点完卡片要把卡片改成「已处理」（投影回写，非只出不进）
- Problem: 真跑第一条 e2e 时用户当场提出：「点了【通过】或者【打回】，卡片没有任何变化，会让用户不知道点过了没、点了什么」。这不是体验瑕疵，是这个项目一路在打的那类**静默**的又一处：① 点完没反馈 → 人以为没点上 → 再点一次，而重复点击正是 ADR-023 里审批配额被烧光的燃料；② 打回后旧卡失效，但它**和能点的新卡长得一模一样**，人翻聊天记录往上点只会得到静默 no-op；③ 隔一天回看，答不出「这道门当时谁放的行」。
- Constraint: 红线「飞书是投影，不反向写真相」管的是**读**的方向（真相只从 checkpointer 取）；往投影写正是它该有的方向。飞书的延迟更新：事件里带 `token`，30 分钟内有效、最多 2 次，且**只支持整张替换**。
- Decision: 裁决落地后把那张卡换成「已处理」版：正文照旧 + 一行结论（通过 / 打回到哪一环 + 谁 + 时间 + 意见），**按钮全部撤掉**。撤按钮不是为了好看：留着就还能点，而点了只会静默 no-op。**陈旧卡片也当场作废**（标「已失效」并撤按钮），这一条价值最高，它把「旧卡看起来还能用」这个陷阱直接消掉。卡是我们自己生成的，结构已知，**不必解析事件里的 `card_content`（userDSL）**。
- Alternatives(否决): 只发私信回执（`_tell` 已有，但解决不了「旧卡看起来能点」和「聊天记录里查不到裁决」）；把「你没有权限」写到卡上（卡可能已被转发，越权的是某个看到卡的人，改卡会改掉**所有人**看到的内容，包括真正的负责人 —— 故越权一律只走私信，不动卡）。
- Tradeoff: 更新失败**绝不能**影响已落地的裁决（卡片是投影，权威结论在 checkpointer），故失败只记进 `provision_errors`。token 有 30 分钟与 2 次的硬限制，超时的卡就永远停在旧样子，没有补救通道（要补得靠 message_id 走另一套更新 API，未做）。没有 token 的通道（飞书任务完成、进程内直调）自然跳过。
