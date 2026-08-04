# DECISIONS · larkflow（append-only ADR）

## ADR-001 · 2026-07-23 · 引擎选 LangGraph（有环）而非纯 DAG 编排器
- Problem: 工作流需要打回重做、iterate 循环、节点级重启、wait 挂起等**循环语义**，且要持久的人在环。
- Constraint: 飞书原生 + human 节点可挂起数天再续跑。
- Decision: 用 LangGraph（Pregel 有环运行时；interrupt + checkpointer + Send + Command）。
- Alternatives(否决): Airflow / Prefect（纯 DAG，不能环）；Temporal（重、非 agent 控制流语义）；自建引擎（成本高）。
- Tradeoff: LangGraph 执行态与业务数据须清晰分层（见 ADR-002）。

## ADR-002 · 2026-07-23 · 两层 + 单一事实源 + 飞书投影
- Problem: 领域对象（待办 / 实例 / 依赖 / 门禁）与引擎执行态如何分工，避免两套真相源打架。
- Decision: 领域 DAG = 数据模型；实例运行态由 LangGraph checkpointer 持有，是**权威写侧**；飞书任务 / 多维表格 / 文档 = **投影读侧**（从图事件同步）。采用「单一事实源 + 广播投影」思路。
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
  - **条件分支**：B 永远只产决策值；下游用法分两种：当参数读 = 参数化下游（情况 1）；带 `when: {B: 值}` 守卫 = 选分支（情况 2），未匹配节点标 `skipped`（置灰）。引擎加两零件：节点 `when` 字段 + `skipped` 终态。ready 规则见 SPEC；**分支从 deps + 守卫涌现，引擎不识「分支」概念**。
- Alternatives(否决): 按业务枚举投票 / 分支节点类型（爆炸）；单独维护「分支集」数据（deps + 守卫已能涌现，多余）。
- Tradeoff: 三条新护栏 / 不变量：决策取值域须被分支守卫全覆盖（或留默认支，否则某取值下全 skip、饿死汇合点）；打回决策 = skipped 复活（`reopen_resets` 加 skipped→pending）；**置灰 ≠ 删除**（skipped 是引擎按决策没跑、可复活；活图删除是 owner 拿掉、没了）。**extends ADR-015**（`approval_policy` 加 threshold）。投票 + 分支落 v1.3。

## ADR-026 · 2026-07-25 · tool 节点 = 数据化能力库（`tool: {kind, args}`），per-id handler 降为逃生舱
- Problem: v1.0 as-built 里 tool 节点的行为只能靠「按 node id 注册的 Python handler」提供，装配期 `validate_coverage` 缺了就整张模板拒跑。叠加当时的护栏①（三型齐全，强制每张图都有 tool 节点），推论是：**不存在任何一张只加 yaml 就能跑的业务图**。实测两条：一张纯 llm+human 的 PRD 接力图被护栏①拒；为凑三型补一个 `archive` 节点后被 `tool 节点缺 handler` 拒。
- Constraint: 禁改项「节点契约恒为数据，使生成 = 加 AI 作者 + 人审门、执行器一行不改」；ADR-022 把模板生成定为 v1.1 主路径，而 AI 只能生成 YAML、生不出 Python。
- Decision: 节点契约新增 `tool: {kind, args}`。`kind` 取自一张**与模板无关**的内置能力注册表（`larkflow/engine/tools.py`，v1 实装 `record` / `summarize_links` / `notify` / `noop` / `format_check` / `expect_fields`），业务参数下沉 `args`。`validate_coverage` 只校验 kind 可解析。按 node id 注册的 handler 保留为**逃生舱**（真正一次性的确定性代码），不再是唯一路径。随之删除 `contract_handlers.py` / `defect_handlers.py`，`templates/` 目录只剩 yaml。
- Alternatives(否决): 保持 per-id handler 只放开护栏①（新业务仍要写 Python + 改装配表，生成主路径仍不成立）；让 AI 生成 Python handler（不安全、不可审）。
- Tradeoff: 能力库的覆盖度成为新的产品边界（不够用就得加 kind，但那是**一次性跨业务投资**，不是每个模板一份）。撞名风险同时消失：注册表按 kind 索引，不再按 node id，`close` / `checks` 这类高频命名不会跨模板串业务（as-built 曾实测放行）。**refine ADR-015 的节点契约**（SPEC 已同步）。

## ADR-027 · 2026-07-25 · 护栏①「三型齐全」降级为 lint，不作运行准入
- Problem: ADR-010 原文是「三条护栏**进生成 prompt**」，as-built 却把「每张图 tool/llm/human 三型齐全」实现成 `validate_template` 的 raise，并且经模板加载 / `start(template=…)` / **每次 `edit_graph`** 三条路径生效。
- Constraint: 通用产品最常见的两类流程恰好不满足它：纯人协作（招聘接力 / 采购审批 / 报销）与纯 AI+人（视频脚本 / PRD 初稿）。ADR-010 自己的种子库分层第一层就是「纯人协作」。
- Decision: 从 `validate_template` 移除，改为 `lint_template(dag) -> list[str]` 的风格提示（供生成器与人审门用）。结构不变量（id 唯一 / deps 不悬挂 / 无环 / 护栏②③④⑤ / 字段级）全部保留为硬校验。
- Alternatives(否决): 保留硬校验但给模板加豁免开关（等于承认它不是不变量，还多一个字段）。
- Tradeoff: 生成器可能产出「没有任何把关节点」的图，故 lint 额外提示这一条；把关的必要性由人审门保证。附带修掉一个活图缺陷：运行中删掉图里最后一个 llm 节点这种日常编辑不再被拒。

## ADR-028 · 2026-07-25 · 绕开 LangGraph 的 super-step 屏障：保值写回 + 借位重排
- **Status**：实现约束（非产品决策），记下来是因为它反直觉、且踩过两次。
- Problem: LangGraph 的 super-step 是屏障：只要有人工节点挂在 `interrupt` 上，`dispatch` 就不会再执行。实测两个后果：① gate 判了打回却落不了地（上游不重算、没人被通知，直到那个不相干的人碰巧响应）；② **完全不相干的并行分支一起停**（B 跑完，C/D/E 全卡在另一条支的签字上）。多方并行接力正是本产品的定义形态。
- Constraint: 不 per-instance 编译新图；打回重置逻辑必须留在引擎里（不在驱动层重算）；`update_state` 会落新 checkpoint，而在飞的 super-step 里**已完成任务的写入尚未提交**，不保值就会被静默丢掉（实测：刚点的裁决连同意见变 `None`）。
- Decision: 驱动层 `_write_state` 做两件事：① **保值**：把当前观测到的 `status` / `outputs` / `dag` 原样带上再 `update_state`；② **借位**：`as_node=<某 worker>`，其唯一出边就是 `dispatch`，于是 dispatch 真的执行一次（打回逻辑仍在引擎里）。`_advance` 据「按 dag 该就绪却没在飞」的判据一拍一拍推，直到没活可干。
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
- Alternatives(否决): 只发私信回执（`_tell` 已有，但解决不了「旧卡看起来能点」和「聊天记录里查不到裁决」）；把「你没有权限」写到卡上（卡可能已被转发，越权的是某个看到卡的人，改卡会改掉**所有人**看到的内容，包括真正的负责人，故越权一律只走私信，不动卡）。
- Tradeoff: 更新失败**绝不能**影响已落地的裁决（卡片是投影，权威结论在 checkpointer），故失败只记进 `provision_errors`。token 有 30 分钟与 2 次的硬限制，超时的卡就永远停在旧样子，没有补救通道（要补得靠 message_id 走另一套更新 API，未做）。没有 token 的通道（飞书任务完成、进程内直调）自然跳过。

## ADR-038 · 2026-07-26 · 对账轮询在等的飞书任务：长连接会**静默死亡**，而两条入站通道的失败可见性不对称
- Problem: 真跑第一条 e2e 时，Mac 睡了一夜，`lark-cli` 的长连接静默死掉：进程全活、TCP 显示 ESTABLISHED、`lark-cli event status` 说 running、日志无任何异常，而它自己的账本写着 `RECEIVED 0`，**10 小时 48 分一条事件都没收到**。人在飞书里把「负责人定稿」点完成了，引擎永远不知道，实例就此停死。ADR-007 早写了「长连接没有队列」，但漏了更狠的一层：**断了不一定有人知道**。`EventPump` 的退避重启只在**子进程退出**时触发，子进程没退出就永远不重启。
- Constraint: 红线「完成必须来自显式信号，绝不从『文档不再变化』推断」不能破：轮询读的必须仍是**人的真实动作**（任务的 completed 状态），不是引擎自己替人下结论。
- Decision: `reconcile` 时扫在等的 `task_complete` 节点，按**派单幂等键**反查本轮那条飞书待办；已完成就补一次 resume。**只做任务、不做卡片**，理由是两条通道的失败可见性完全不对称：卡片点击在通道死时**失败得响**（用户当场看到红字「目标回调服务当前未在线」，会再点一次），而且卡片没有「状态」可查；任务完成**失败得无声无息**（用户看到任务已完成、引擎还在等，双方都觉得自己对，谁也不会去查）。捞回来这件事**必须记进 `provision_errors` 并打日志**：它是「入站通道漏过事件」的故障信号，静默自愈会让一条死掉的通道永远不被发现。
- Alternatives(否决): 靠心跳 / 「N 分钟没收到事件就重启」（安静期本来就是常态，判不准）；把卡片也做成轮询（卡片没有可查的状态，且它已经失败得响）；每次事件都全量对账（把偶发通道故障的代价摊到每一次点击上）。
- Tradeoff: **反查必须按「本轮」定位那条待办，不能按 node id 翻关联表**：一个节点被打回 N 次就有 N+1 条待办，旧的那几条永远停在「已完成」，按 node id 翻会拿第 1 轮的完成去推第 3 轮，**每对账一次白烧一轮打回预算**（我第一版就是这么写的，真栈上两次重启把 `checks` 的预算从 1 烧到 3、实例直奔 blocked）。正确索引是派单幂等键 `{实例}:{节点}:{轮次}`，它天然只指向本轮那条；该键**只在 `_dispatch_key` 一处拼**，派单与轮询共用，两处各拼一次必然漂移（第一版漏了 `:kind` 段，后果是轮询永远查不到、丢事件永远捞不回来，且没有任何症状）。仍未解决：通道死掉期间**卡片点击是真丢的**，只能靠人再点一次；引擎对「通道是否还活着」依然没有主动探测。

## ADR-039 · 2026-07-26 · 定期对账：`task_complete` 的推送在真栈上根本不到，轮询是它唯一的通道
- Problem: ADR-038 把轮询当「安全网」加进启动对账。真栈继续调试发现更硬的事实：**任务事件这条推送从头到尾就没通过一次**。隔离实验（daemon 停掉、单独跑消费者）：`pre-consume setup` 正常执行、`feishu-websocket: connected`、以 bot 身份亲手建任务并亲手完成（`ok=true`）、甚至按 lark-cli 的提示把 app 自己加成 `--follower cli_xxx`，`RECEIVED` 始终为 0。对照组：卡片事件同一条 bus 上工作正常。事件名 `update_**user_access**_v2` 暗示它按「谁对这条任务有访问关系」推送，而引擎建的任务 assignee 是人、app 只是创建者；但加 follower 也无效，**真正原因未查明**（标为未验）。
- Constraint: 红线「完成必须来自显式信号」不破：轮询读的是任务的 `completed` 状态，那仍是人的真实动作。
- Decision: `larkflow serve` 起一条**定期对账线程**（`LARKFLOW_SWEEP_SECONDS`，默认 120s，配 0 关掉），逐个未完成实例跑 `reconcile`（内含 ADR-038 的任务轮询）。于是 `task_complete` 节点的可靠性**不再依赖推送**。一轮出错不许让后续所有轮停摆（与启动对账同款纪律）；捞回来的每一笔都打日志，那是「推送漏了」的故障信号。
- Alternatives(否决): 把人工产出节点改用 `card_action`（卡片确实通，但「完成一件活」的语义就是待办，改成按钮是拿产品迁就渠道故障）；只在启动时对账（人交了卷要等到下次重启才知道，实测就是这么卡住的）；把订阅换成用户身份（user token 会过期、需要设备流重新授权，服务器上更脆）。
- Tradeoff: 延迟从「秒级推送」退化成「≤ 一个扫描周期」，默认 120s。开销 = 每周期每个在等的任务节点一次 `task tasks get`。**卡片通道仍然只靠推送**（它没有可查的状态，且通道死时用户当场看得见红字）。`task.task.update_user_access_v2` 为什么不推送**仍是未解之谜**，接下来若查清，轮询可退回安全网角色、周期调大。
  - **2026-07-27 更新：谜底基本揭开，这条前提在真部署上不成立。** 在 alicloud-sh（全新宿主、全新 bus）上，人点完任务 22:04:58 → 22:05:00 第 2 轮任务已派出，整条链 2 秒跑完，事件推送与路由都正常，**一次都没轮到轮询兜底**。也就是说「不推送」不是飞书的行为，而是本机那个跨重启被复用的陈旧 bus（见下条线索）根本没订上这个事件。据此：轮询**降级回安全网**，周期可调大；但在把 daemon 的存活信号补上之前别关掉它（今天这一跑没触发过兜底路径，那条路在真机上仍然零覆盖）。

## ADR-040 · 2026-07-26 · escalation 的同意 / 拒绝通道：在只追加的账本上表达「已拍板」
- Problem: ADR-023 ③ 把 escalation 的 v1 范围钉成「申请 + 通知 + 一键同意」三件，落码只做了前两件。`_escalate` 把申请写进权威 state，而**全仓没有任何 approve / reject 通道**：`status` 硬编码 `"pending"` 一处写入，reducer `extend_lists` 又只追加不覆盖，于是一笔申请落库后**物理上不可能**再变成别的状态。更要命的是它在**默认路径**上：v0.5.0 把卡上默认打回目标改成「只剔 denied、保留要走审批的」（那是修「静默的部分打回」的直接后果），于是默认那颗「打回」按钮天然带着跨界目标，一点就落进 escalation，人收到「已提交申请、等人拍板」，而那个按钮不存在。与 ADR-029 的 `blocked` 死局同类：机制把人送进一个状态，却没给出口。
- Constraint: 追加型 channel 没有 UPDATE（红线：只改未来不改历史），所以「同意」不能原地改 `status`。判定顺序照 `resume` 的既有纪律（陈旧必须排在权限之前）。审计写在事情发生**之后**（ADR-034）。配额与对外读接口必须用同一把尺，两套口径正是「审批通道被永久锁死」那条已修缺陷的根因。
- Decision: 同意 / 拒绝各**追加一条裁决记录** `{kind: "verdict", ref: <申请 seq>, verdict, by, at}`，申请的状态改为**派生**（`effective_status` ∈ pending / approved / rejected / expired）。旧记录没有 `kind`，缺省即 `request`，向后兼容靠缺省值而不是改历史。这顺带修掉「`escalations()` 旧记录 status 恒为 pending」那条挂了很久的 finding：它不是漏写，是存储模型决定的，只能改读法。五道闸按序：审计（`by` 空即拒）→ 已裁决（幂等）→ 陈旧 → 禁自批 → 权限。**先执行、后记账**：中间崩掉的话打回已落地、门进新一轮、那笔申请按轮次自然作废，不会被二次同意；反过来（先记后执行）崩掉就是「显示已批准、其实什么都没发生」，没有任何机制能发现。
- **修订 ADR-023 ③ 两处**：① **禁自批**。`approvers_for` = owner 令牌 ∪ 目标节点主负责人，而申请人完全可能正好是后者（他打回自己的活，但重算集牵连了第三个人），不禁的话他自己提、自己批，那三条规则被整个绕开。owner 恒在审批人集合里且 owner 走不到 escalation 这条路（他有全域权、直接执行），所以禁自批不会造成一笔申请无人可批。② **审批人身份两把尺**：令牌求交之外，再认「当初真通知到的那些 open_id」。只认令牌的话，`roles_of` 反解一旦静默失效（自定义 resolver 没有该方法、角色映射后来改了、assignee 配成飞书群），这笔申请就**没人同意得了**，死局原样复发；我们当初亲口告诉了他「该你拍板」，那次通知本身已经在权威 state 里、不可伪造，拿它当授权凭据是收敛的。
- **新增一条作废判据**：门**已被答复**的申请当场作废。申请不是裁决，`_ack_escalation` 明说「你手里这张卡仍然有效」，所以提申请的人完全可能没等批下来就自己点了通过，这是常态不是刁钻构造。轮次那把尺在这里不管用（点通过不会让 `attempts` 变），必须另看门的状态，否则驾驶舱一直显示「等人拍板」而门早就过去了，真有人去点同意还会试着掀开一道已经放行的门。
- Alternatives(否决): 原地改 `status`（破追加型不变量，且 reducer 根本不支持）；另开一个 channel 存裁决（读者要跨两个 channel 归并，ADR-034 正因为「读者一多必然有人漏做」否决过同类方案）；把 OR 语义改成「每个目标的负责人各同意一次」（ADR-023 原文写的就是「任一方同意即执行」并自称轻量版，改它属于改契约，留给 Maxwell 拍）。
- Tradeoff: **OR 语义的代价照收**：一笔申请含多个目标时，A 目标的负责人一个人就能同意掉牵连 B 的整笔（`_escalate` 把 `approvers` 合并成了一个集合）。记录里已经存了 `escalated` 逐项来源，将来要收紧成逐目标收齐是纯增量，不用改历史。批准替代的是**权限层不是机制层**：执行前仍按当前活图重跑 `illegal_reopen`（不过实测这条在今天不可达，门在 running、祖先全 done，冻结线让图改不动那条祖先链，故属防御性）。`seq` 改按申请数计（`len(_requests(log))+1`），因为 log 里现在混着裁决记录，用 `len(log)` 会跳号。
- **落地当天的对抗 review 又抓出三条，都已修并钉住**（教训是同一个：**「活性」这件事只许有一把尺**，自己再写一遍判据必错）：
  - **驳回之后申请人永远提不了同一笔，而且零反馈**。`_escalate` 的去重自己写了一遍判据、拿记录里的 `status == "pending"` 当活性，而那正是本 ADR 亲口说过「冻在落库那一刻、永远 pending」的字面量。于是驳回后再点同一个打回：命中 duplicate 分支 → 卡不变 → `_ack_escalation` 的幂等键与上次逐字相同又被 `_once` 吞掉 → 他一个字都收不到，审批人那边也查无此事。改成在 `_live_escalations` 的结果里找。
  - **门 `blocked` 之后，同轮没拍板的申请永远显示待批、批不动也退不掉**。`blocked` 时 `attempt_increments` 为空、`attempts` 一动不动（「轮次已过」不触发），而 `_answered` 当初照 `_unanswered` 只认 done / failed（「门已答复」也不触发），三条出局判据一条都不命中；另一头 `_execute_approved_reopen` 按「还有没有挂起中断」判，blocked 之后没有中断，于是同意永远回 `stale`，只有 reject 出得去。`_answered` 补进 `BLOCKED`。
  - **打回预算耗尽时批准：什么都没退回，却宣告「已退回重做」**。`reopen_resets` 把门标 `blocked` 并**一个节点都不重置**，于是 `reopened` 是空的，而返回照样 `approved: True`，`_announce_verdict` 又读的是 `record["targets"]`（形参 `reopened` 收了却一次没用），于是申请人被告知「X 已退回重做」、旁支负责人被告知「你被卷进返工」，两句都是假的。**批准是真的、落地不是**，两件事分开报：返回加 `landed: False`，通知改说实话，没人被卷进来就不去惊动旁支负责人。

## ADR-041 · 2026-07-26 · 打回那一刻就关掉旧轮次的飞书待办
- Problem: `_provision` 每一轮建一条新待办，而在此之前**没有任何代码去关旧的**（`complete_task` 三处定义、零调用点，代码注释里早写着「重复的待办永远躺在人的待办列表里（实测）」）。真栈第一条 e2e 之后清场，`finalize` 被机检打回 3 次留下 4 条「负责人定稿」，其中 2 条从头到尾没人点过、也永远不会有人点。最难受的不是「旧轮次里已点完的那些」，是**被卷进新一轮、但新一轮还没轮到派单**的旁支节点：它得等上游返工完成，这中间人手里那条旧单一直开着，长得和能干的活一模一样，点下去只有静默 no-op。任务通道没有卡片那套「陈旧当场作废」的对称物（ADR-037 靠回调 token，任务事件没有 token）。
- Constraint: **绝不能关本轮那条**。ADR-038 的轮询会把 `completed == True` 当成人的真实完成信号并 resume，关错一条就是引擎替人交了卷，破「完成必须来自显式信号」这条红线。关单是投影侧动作：失败只记 `provision_errors`、不抛、不影响 checkpointer 里的裁决。幂等走本地 `_once`（飞书那 1 小时窗口靠不住，而且 `CliLarkIO.complete_task` 压根没传 `--idempotency-key`）。
- Decision: 挂在 `_handle`（每次推进都跑），对每个 `signal == task_complete` 的节点按 `range(当前轮次)` 把 0..N-1 轮的待办逐个关掉，`range` 天然排除本轮。**按当前轮次现算，不做前后 diff**：轮次是在调用方的 `graph.invoke` 里就 +1 的，`_advance` 拿不到那一刻的前值；现算是幂等的，重启 / 对账重跑都对，而且天然把「上次关单失败的」补上。幂等键 `{实例}:{节点}:{旧轮次}:task-closed` **必须带旧轮次号**，不带的话同一节点第二次被打回会被幂等表整个吞掉、第二条僵尸永远关不掉。挂 `_handle` 而不是挂 `_provision`：后者要等到给他派新单的时候，旁支节点可能要等一整轮上游返工。
- Alternatives(否决): 挂 `_provision`（旁支节点的孤儿单会在人手里多活一整轮，而那正是最难受的那一类）；给待办改标题标「已作废」（lark-cli 只有 `+complete`，没有取消 / 改标题）；靠人自己忽略（僵尸单与真单在待办列表里长得一模一样，这正是这个项目一路在打的那类静默）。
- **落地当天对抗 review 的一处收紧**：扫描从 `_handle`（泵循环里**每拍**都调）移到 `_advance`（**一次推进一次**，放在 `finally` 里，推进本身炸了旧单照样该收拾）。成功时两者没差别（`_once` 之后全是本地幂等表查询），但**失败时不记键**，挂在 `_handle` 上会每一拍都真去 spawn 一次 lark-cli，实测一次打回能放大到 81 次调用。同一轮 review 报的「关单确定性失败→无界重试」被证伪了：`lark-cli task +complete` 自己是「读当前状态 + 条件写」两步（`--dry-run` 可见 `complete task if not completed`），关一条已完成的单不会报错，所以那条载重前提不成立；但放大这件事属实，故照收。
- Tradeoff: `task +complete` 把一条作废的单标成「已完成」，人可能读成「我交过卷了」，但飞书没有第二种关法。**卡片通道仍不对称**：human produce 若配 `card_action`，它的旧卡只有在人点了之后才会被 ADR-037 标失效，打回那一刻无法主动作废（`update_card` 只吃回调 token，没有按 message_id 改卡的能力）。真栈行为只验过一半：我手工用 bot 身份关掉两条 assign 给用户的待办成功（返回带 `already_completed`，自带幂等语义），但**引擎自动关单这条路径在真栈从没跑过**，尤其是「关掉之后飞书会不会回吐 task_completed_update 事件」未知（推演是会被 `resume` 的陈旧分支挡住）。

## ADR-042 · 2026-07-26 · 受控活图的鉴权与审计：owner-only + `edits` 追加型 channel
- Problem: `edit_graph` 此前连 actor 都不收，任何拿得到 service 的人都能改图，而且改完**什么痕迹都不留**：改过什么、谁改的、为什么改，事后完全不可考。它比无鉴权的 `unblock`（ADR-030 已记的留白）更狠：`unblock` 最多把合法祖先踢回去让人返工，代价是时间；`edit_graph` 能**直接删掉一道还在等的门**，那道审核从此不存在、流程静默放行、没有任何人收到信号。而 `larkflow edit` 子命令正要把这个入口开到命令行上。
- Constraint: 一切权限在引擎权威侧算（红线④）。审计只记**真发生过的**改动（ADR-034），被拒的、被引擎校验拦下的都不留痕，否则就是假审计。追加型 channel 不进保值集。冻结线不因为「你是 owner」而放宽（ADR-013 是引擎不变量，不是权限问题）。
- Decision: **owner-only + 必署名**：`by` 必须是 `meta.reporter`，`by` / `reason` 任一为空即 `missing_audit`（照 ADR-030 的先例），非 owner 返回 `unauthorized_edit`。口径照 ADR-024：改 / 删一道门是 **owner 跳过审核的正路**，所以不套 ADR-023 那三条（那是「让别人返工」的尺，不是「改图」的尺）。审计落新的追加型 channel `edits`（键固定 `"log"`），每条记 `{by, at, reason, ops, nodes_after}`，`edit_log()` 读。记 `nodes_after` 是因为只记 ops 的话，一张跑到一半的图为什么长成现在这样，事后要靠把所有 ops 重放一遍才知道。
- Alternatives(否决): 照 `unblock` 只署名不鉴权（那正是 ADR-030 自己写下「接前端或卡片按钮之前必须先补」的留白，现在正是那个时刻）；套 ADR-023 三条规则（改图不是打回，参与人对「图长什么样」本来就没有话语权，套上去既过严又语义错位）；零参数上线（等于把天窗开到 CLI 上）。
- Tradeoff: owner 判据用的是 `by == meta.reporter` 字面相等，与打回权限层的 `_actor_roles` 令牌求交**口径不同**（那边 owner 可以靠角色令牌命中）。今天 `_owner_roles` 就是 `{reporter}`，两者等价；将来 owner 若扩成一组角色，这两处要一起改。审计把 `ops` 原样 deepcopy 进权威 state，ops 里可能有很长的 prompt，checkpointer 会随改图次数线性变大（改图次数天然稀少，暂不设上限）。存量实例的 state 里没有 `edits` 键，读取一律走 `or {}` 兜底。
- **对抗 review 抓出的一条，已修**：**抛异常 ≠ 图没变**。`_write_state` 是先 `update_state` 落 checkpoint、再 `invoke` 跑一拍，而新加的节点**就在这一拍上执行**，执行体那条路上没有任何 try/except。真栈里这条一点都不刁钻：加一个知会某角色的 `notify` 节点，四道前置校验全过（`RoleResolver.validate_coverage` 只扫 `assignee_role` 与 `vote.voters`，根本不看 `tool.args`），到运行时 `resolver.resolve` 才抛 `RoleError`；`LLMUnavailable` 同理。裸抛出去的后果是调用方与 CLI 报「改图被拒」并退 1，而人照提示重试就撞「id 已存在」，他既不知道图已经改了，也不知道该去修什么。改法：捕获后**先核对 dag 到底落库没有**（落了才算数），落了就如实返回 `advance_error` + 「不要重试」，并把失败记进 `provision_errors`；没落库照抛。
- Tradeoff（新）：这条只解决了**报告**的诚实性，没解决原子性。「dag 已改 + 新节点跑不起来」这个中间态是真实存在的，出路是修好根因后 `larkflow reconcile` 继续，或者再发一次 `edit` 把那个节点删掉。要真正原子得让 `_write_state` 支持「先跑再提交」，那是引擎层的改动，没做。

## ADR-043 · 2026-07-26 · 审批卡：把「一键同意」真的做成一键
- Problem: ADR-040 只做完了引擎那一半。审批人在飞书里收到的仍是一条**纯文本**，要拍板得有人去敲 `larkflow approve`。对一个「飞书原生」的产品来说，这等于把出口修在大多数审批人根本走不到的地方，ADR-023 ③ 写的「一键同意」名不副实。SPEC 把「审批卡的封套」列为待填，就是卡在这里。
- Constraint: 身份**只**取事件顶层 `operator_id`，封套是前端可自由构造的攻击面（红线④ / ADR-032）。投影失败不影响已落地的裁决。越权不改卡（ADR-037：卡可能已被转发，越权的是看到卡的某个人，不是这张卡本身，把「你没有权限」写上去会改掉所有人看到的内容）。
- Decision: 封套 `{"kind": "escalation", "thread_id", "node_id": <门>, "seq": <第几笔>, "decision": "approve"|"reject"}`，两颗按钮「同意 / 驳回」。**刻意不带 `interrupt_id`**：拍板不是在答复某个中断，而是对一笔申请表态，所以 `_route` 加第三条分支，按 `kind` 分流，不能沿用「thread_id + interrupt_id」那把钥匙。按钮文案与门禁卡的「通过 / 打回」**用不同的字**：两者挂在同一个 `node_id` 上并存，同字会让人以为自己在批那道门。`decision` 只认这两个值，认不出来一律当没发生：猜错的方向是「把一个说不清的点击当成同意」，那会真的让别人返工。拍完把卡改成「已处理」（ADR-037 同款），另一位审批人后来点他那张时得到 `already_settled` 并当场标「已由 X 处理过」。
- Alternatives(否决): 复用门禁卡的 `verdict` 字段（语义撞车：`pass` 在门上是放行、在申请上是同意，同一个词两种后果，出错方向还是「误放行」）；只给 owner 发卡（ADR-023 的审批人集合本来就含目标节点主负责人，砍掉等于改权限模型）；走飞书原生审批流（多一套外部状态机，与「checkpointer 是单一事实源」直接冲突）。
- Tradeoff: 审批人现在会收到**卡**而不是消息，`notified` 记的相应变成「卡发出去了」。同一笔申请给同一个人一辈子只发一张（幂等键沿用 `{实例}:{门}:escalation:{seq}:{人}`），所以卡若被误删就只能走 CLI 兜底。**卡片通道仍然只靠推送**（ADR-038：长连接死掉时点击当场失败且不补投），审批这条同样没有轮询兜底，因为它和门禁卡一样「失败得响」。申请人拿到的仍是纯文本回执，不给他按钮是刻意的：给了只会让他误以为自己能拍板。

## ADR-044 · 2026-07-26 · 停订阅要带走整棵进程树（否则每次停机都报一次假故障）
- Problem: 真机上重启 daemon，**每一次**都打这两行：`故障 drain: TimeoutError: 10.0s 内没排空在飞的事件，连接不关` + `已停止（未排空）。事件 0 条（推进 0 / 跳过 0 / 故障 1）`。一条事件都没有，却判「没排空」。根因：`lark-cli event consume` 是**两级进程**（`node …/bin/lark-cli` 再派生真正的 CLI），`proc.terminate()` 只杀得到第一级，孙进程继续握着 stdout / stderr，管道**永远不 EOF**，`_pump` 与 `_drain_stderr` 一直阻塞在 `for line in proc.stdout` 上，`join(timeout)` 必然超时。
- Constraint: 不动正在处理的那条事件（它可能正握着实例锁写 checkpointer），所以停机仍是「先停订阅、再等在飞的跑完、最后才关连接」三步；`stop()` 只负责让子进程走人。
- Decision: 子进程用 `start_new_session=True` 起在**自己的进程组**里，`stop()` 按组发 SIGTERM、赖着不走再按组 SIGKILL。拿不到 pid / 不是真进程（测试替身）时退回逐个 `terminate()`。`_spawn` 拆出 `_argv`，让测试只替换「跑哪条命令」而 `Popen` 的参数走真实现：连 `Popen` 一起替掉就等于把要验的修复烤进替身，真实现漏了也测不出来。
- Alternatives(否决): 把 drain 超时调长（治不了，管道永不 EOF，多久都超时）；`join` 时不等 `_drain_stderr`（那条线程存在的理由是防止 stderr 写满 64KB 管道把子进程卡死，不等于可以不管它是否退出）；忽略这条故障（见 Tradeoff）。
- Tradeoff: 这条的代价不是「多打一行日志」。**退出码恒定非 0**，于是「这次停机到底干不干净」这个信号被恒噪声淹没，真出现半截写的那一次也没人看得出来。而这套判据正是 v0.5.1 专门加来解决这个问题的，等于被自己废掉了。修完真机复验：`已停止（干净）。事件 0 条（推进 0 / 跳过 0 / 故障 0）`。
- 顺带记录（**未解**）：`lark-cli event _bus` 是它自己的**共享单例**，不随我们的 consume 子进程一起走，干净停机后仍然存活并被下一个 daemon 复用。实测见过一个 **3 小时 12 分**的旧 bus 被新起的 consumer 挂上去。ADR-039 那个「`task.task.update_user_access_v2` 根本不推送」的谜，以及 ADR-038 那次「10 小时 48 分零事件」，都可能与这个跨重启复用的旧 bus 有关。~~**这是线索不是结论**，没有验证过。~~ → **2026-07-27 坐实了一半**：换到一台全新宿主（bus 也是全新起的）之后，ADR-039 那个「任务事件根本不推送」当场消失，2 秒内推送并正确路由。所以那条线索对 ADR-039 成立。ADR-038 那次「10 小时 48 分零事件」是否同因**仍未验**（那需要真的跑够十小时）。**运维含义**：换代码 / 换配置之后，只重启 `larkflow serve` 是不够的，旧 bus 会活下来被复用；排查「收不到事件」时第一件事是确认 bus 是不是这一轮新起的（`lark-cli event status` 的 `pid` 与 `uptime_sec`）。

## ADR-045 · 2026-07-30 · 产品重定位为飞书原生的企业协作 DAG

- **Status：Accepted · Target。**
- Problem：旧定位把“多人/AI 接力产出合同类交付物”当产品中心，容易退化成 Agent 建待办或单场景流程引擎；参考方案的完整办公平台边界又与本项目实际优势冲突。
- Decision：larkflow 复用飞书 IM、Task、Docs、Drive 和 Directory，负责企业模板、跨人/部门 DAG、责任边界、父子工作契约、验收和审计。合同只作示例，首个 beachhead 由真实频次与协调成本验证。
- Supersedes：ADR-012 的“交付物流转”产品身份；ADR-018 的合同型首发假设；ADR-019 绑定妙搭为主前端的产品结论。飞书原生方向保留，具体 UI 在原型后决定。
- Trade-off：产品范围从一个可快速演示的交付物流转器扩大为企业协作系统，必须用试点和分阶段领域模型控制实现风险。

## ADR-046 · 2026-07-30 · 待办只分配给人，个人 Agent 是边缘执行方式

- **Status：Accepted · Target。**
- Problem：员工电脑可能关机或离线，把企业待办直接分给 Agent 会让责任、提醒、改派和审计依赖设备在线。
- Decision：每个人工工作包必须解析到唯一真实人员。责任人自行选择 manual、personal_agent 或被允许的 child_dag。员工电脑运行 lark-cli + Claude/Codex，通过中央安装注册、任务领取和结果回传协议工作；离线不改变待办归属或中央状态。人类 Gate 只能由本人确认。
- Refines：ADR-005。lark-cli 不再只是中央出口/工具手，同时是个人 Agent Edge；中央 adapter 与个人 edge 使用不同身份和权限。
- Trade-off：需要设备注册、离在线、撤销、升级和人工接管能力，但获得稳定责任边界和多 Agent 可替换性。

## ADR-047 · 2026-07-30 · 业务 DAG 独立于 LangGraph，中央数据库持有业务真相

- **Status：Accepted · Target。**
- Problem：LangGraph checkpointer 适合恢复 Agent 运行，却不适合作为多租户模板治理、跨人查询、三级权限和长期审计的产品数据库。
- Decision：目标中央控制面用 PostgreSQL 保存 Tenant、Template/Version、Instance、Node、Attempt、Assignment、Parent/Child 和 Audit。业务调度器解释无环产品 DAG。LangGraph 仅可用于单个复杂 AI 节点内部，其 checkpoint 不是跨人业务真相。
- Supersedes：ADR-001 的“LangGraph 是整个业务运行时”、ADR-002 的“checkpointer 是单一事实源”、ADR-007/ADR-031 的 SQLite 单机部署作为目标形态。旧实现保留为 as-built 原型和迁移证据。
- Trade-off：必须拆分现有 service state 与产品领域模型，但避免框架锁定，并获得可查询、可授权、可迁移的企业数据边界。

## ADR-048 · 2026-07-30 · MVP 固定三级 DAG 与父子 Work Contract

- **Status：Accepted · Target。**
- Problem：跨部门目标会由主管继续拆解，但无限递归会让权限、可见性和进度聚合失控；父层直接操纵子层又会破坏部门责任。
- Decision：实例最多 L1 企业/跨部门、L2 部门、L3 团队/个人。L3 禁止下钻。父节点责任人不因创建子 DAG 而转移；父子只交换 Work Contract、聚合状态、阻塞、交付物和验收记录。父层拒绝创建父工作包的新 Attempt，子 Owner 决定内部重开范围。
- Supersedes：ADR-024 的暂定无限递归心智和“子项目”命名；保留独立实例、边界隔离和交付回填思想。
- Trade-off：少数复杂企业流程需压平或拆成多个顶层实例，换取 MVP 可理解、可授权和可运营。

## ADR-049 · 2026-07-30 · 企业入驻渐进发现，模板发布必须治理

- **Status：Accepted · Target。**
- Problem：“先学习企业所有知识再画企业 DAG”既不可验证，也带来授权、隐私、准确性和冷启动风险；每次自然语言生成直接运行又会把组织流程交给概率输出。
- Decision：入驻按授权同步组织、登记 Knowledge/Skill/MCP、访谈流程 Owner、导入历史材料，生成候选 Process Map 和候选模板。候选必须人工校准并走 draft → in_review → published。模板分 platform、industry、enterprise、department；发布版本不可变，实例保存快照，Fork 显式记录来源。
- Supersedes：ADR-022 的“AI 现场生成流程为主路径”。自然语言和 AI 生成保留为候选创建工具，不直接发布或启动生产流程。
- Trade-off：早期 onboarding 更依赖服务和流程 Owner，但能积累可信模板与运行数据，形成真正的企业资产。

## ADR-050 · 2026-07-30 · 中央能力治理通过短时 Capability Lease 下发

- **Status：Accepted · Target。**
- Problem：个人 Agent 需要企业知识、Skill 和 MCP 才能完成工作，但把全局凭证或长期权限下发到员工电脑会扩大泄露与越权面。
- Decision：中央 Capability Registry 保存逻辑资源、版本、租户范围、策略和 Secret 引用。模板声明需求；执行时按 tenant、person、node、attempt、purpose 签发短时、可撤销、最小权限 Lease。边缘设备不获得企业应用全局凭证。
- Trade-off：增加策略评估、token/lease 服务和审计成本，换取本地 Agent 可控接入和供应商可替换性。

## ADR-051 · 2026-08-01 · Phase 0 改为既有设计简化与一致性核验

- **Status：Accepted · Target。**
- Problem：原 Phase 0 需要企业访谈、历史实例和飞书原生对照，但当前没有执行条件。把空白证据写成验证通过不诚实，把无法执行的市场门长期设为唯一工程入口也无法推进。
- Decision：当前 Phase 0 改为核对既有设计与最小闭环的一致性，并形成理由明确、可判定验收的简化范围。访谈与对照协议转为 Deferred，完整保留，未来具备条件时恢复。
- Evidence boundary：设计一致性只允许项目继续做产品与工程设计，不证明场景频率、协调收益、模板维护意愿、市场规模或付费意愿。
- Supersedes：ADR-045 中“首个 beachhead 必须先由真实频次与协调成本验证才可继续设计”的时序要求。ADR-045 的飞书原生产品身份继续有效。
- Trade-off：可以在证据受限时继续收敛实现，但商业风险保持未知，后续仍需真实使用验证。

## ADR-052 · 2026-08-01 · 既有产品设计收敛到最小闭环

- **Status：Accepted · Target。**
- Problem：2026-07-30 的目标设计吸收了三级子 DAG、个人 Agent Edge、能力注册表和复杂模板治理，导致近期范围过大。
- Decision：MVP 只保留单个顶层 DAG；模板可选；实例先创建草稿并由人确认；模板采用 `draft / enabled / disabled / deleted`、不可变版本和布尔锁；每个节点有唯一人类 Owner，执行器为 Human、Agent 或 Tool；运行中只编辑未来区域并二次确认；节点或完整重启创建新 Attempt 并重置可达下游；质量简化为 `pass/fail + evidence + suggestion`；飞书是投影，PostgreSQL 是业务真相。
- Architecture：目标先采用模块化单体、独立 Scheduler、中央 Node Runner 和数据库 outbox。LangGraph 只可用于单个复杂 Agent NodeRun。
- Deferred：模板子 DAG、临时子 DAG、三级父子契约、个人 Agent Edge、Capability Lease、Knowledge/Skill/MCP 注册表、RAG、字段级锁、五维评分、Kafka、微服务和模板市场。
- Supersedes：ADR-046 的近期个人 Agent Edge 范围、ADR-048 的 MVP 三级 DAG、ADR-049 的近期四级模板与能力入驻、ADR-050 的近期 Capability Lease。ADR-046 的人类责任原则、ADR-047 的数据库权威和 LangGraph 边界继续有效。
- Trade-off：首版不再证明多层协作或个人 Agent 差异化，换取一个可以形成闭环、可实现和可核验的范围。

## ADR-053 · 2026-08-01 · 中央节点拆为工作流聚合、Scheduler 与 Node Runner

- **Status：Accepted · As-built foundation。**
- Problem：legacy `service.py` 与全局 LangGraph state 同时承担业务真相、调度、执行和飞书投影。若新架构只增加一个巨型 CentralNode 类，旧耦合会换名保留，外部调用期间也难以建立清晰事务边界。
- Constraint：Instance Snapshot 必须独立于模板来源；每个节点只有一个人类 Owner；Human、Agent 和 Tool 只是执行器；迟到结果不能覆盖当前 Attempt；数据库事务不能跨越 LLM、Tool 或飞书调用。
- Decision：以 `WorkflowInstance` 聚合保存 Snapshot、NodeInstance 与 NodeAttempt；Scheduler 只负责确认后的初始就绪和依赖解锁；Node Runner 只负责激活节点、签发中央 worker claim、验证 Human Owner 和接受当前 Attempt 结果；应用服务在仓储乐观并发边界内协调三者。第一批使用 copy-on-read 内存仓储验证领域规则，明确不把它当目标持久化。
- Alternatives(否决)：继续扩展全局 LangGraph state；用飞书 Task 状态作为业务真相；让一个中央类同时持有数据库事务并执行外部调用。
- Tradeoff：迁移期同时存在 Target 与 legacy 两套模型，代码量暂时增加；换取 PostgreSQL adapter、outbox、飞书投影和 executor adapter 可以沿明确 Port 分批接入，且 529 项离线测试能够独立证明领域规则与 legacy 回归。

## ADR-054 · 2026-08-01 · PostgreSQL 聚合事务与 outbox 划定外部副作用边界

- **Status：Accepted · As-built foundation。**
- Problem：领域内核如果先写状态再直接调用飞书，外部调用失败会留下半完成状态；如果把数据库事务跨过飞书、LLM 或 Tool 调用，长事务、锁等待和不可控重试会破坏中央调度。审计若独立写入，也可能出现状态已变但审计缺失。
- Constraint：tenant 必须进入所有业务主键和仓储查询；Instance、Node、Attempt、Audit 与 Outbox 的一次命令结果必须原子提交；migration 必须随 wheel 分发并支持重入；审计不可更新或删除；outbox worker 崩溃后必须能回收过期 claim；数据库事务不得包住任何外部 I/O。
- Decision：PostgreSQL 保存完整 Instance Snapshot JSONB，同时用规范化 NodeInstance、Dependency 与 Attempt 表支持运行态约束；仓储按 Instance version 做 compare-and-swap，并在同一显式事务内写聚合、AuditEvent 与 OutboxEvent。migration runner 使用事务级 advisory lock。Outbox 以稳定聚合版本去重，通过 `FOR UPDATE SKIP LOCKED` 批量认领，并用 claim token、租期、失败时间和重试时间控制发布。
- Boundary：Human 激活与所有节点终态写 outbox 请求投影同步。Agent 和 Tool 激活不进入 outbox，而由应用层拿到已提交的 NodeActivation 后立即调用 executor；否则排队延迟会消耗 NodeAttempt 自己的短时 claim，任务出队时可能已经过期。执行结果仍必须带当前 Attempt、节点版本和 claim token 回到服务端。
- Alternatives(否决)：在数据库事务中直接调用飞书、LLM 或 Tool；用飞书对象状态作为提交日志；当前规模提前引入 Kafka 或 CDC；把 Agent 与 Tool 执行激活也放入通用投影 outbox。
- Tradeoff：Snapshot 与规范化运行态存在受控重复，换取实例历史自包含和高频状态约束。当前只有 Instance 聚合仓储与 outbox 存储，还没有 Template Service、Projection worker、executor worker、生产备份或 schema 升级 runbook；真实 PostgreSQL 14 验证证明 adapter 路径可执行，不等于生产装配完成。

## ADR-055 · 2026-08-01 · 自动执行使用可恢复租约与稳定 Attempt 幂等键

- **Status：Accepted · As-built foundation。**
- Problem：Agent 或 Tool 节点的 claim 已提交后，Worker 可能在调用 executor 前后进程退出。没有恢复扫描时节点会永久停在 running；恢复时直接创建新 Attempt 又会把同一次逻辑执行拆成两段历史，并让外部幂等失去稳定身份。
- Constraint：数据库事务不能跨越外部 I/O；迟到结果不得覆盖新认领；Human 节点不占自动执行容量；同步 Worker 不能一次签发多个租约再串行消耗；executor 必须面对 at-least-once 调用。
- Decision：仓储从持久化状态扫描 ready 节点和已到期自动认领。单步 Runtime Worker 每次最多认领一个自动节点，先提交 `claimed_by + claim_token + claim_expires_at`，再调用 `AutomatedExecutor`。到期恢复保留同一 Attempt，轮换 token、Worker 和节点版本；旧 Worker 的版本、token 或身份任一不匹配即拒绝。外部幂等键固定为 `tenant_id:attempt_id`，恢复前后不变。
- Alternatives(否决)：把 executor 调用放进数据库事务；把自动执行激活放入通用 projection outbox；恢复时创建新 Attempt；只依赖进程内队列或 Worker 心跳判断失联。
- Tradeoff：自动执行明确是 at-least-once，executor adapter 必须实现幂等。当前同步 Worker 只提供一个 tick，没有常驻循环、退避或业务重试预算；普通 executor 异常会把实例标记 failed，有限重试留给 Phase 2。

## ADR-056 · 2026-08-01 · Target 开发数据库自建在现有 ECS，并限制为本机 peer authentication

- **Status：Accepted · Development deployment。**
- Problem：当前预算不支持托管 PostgreSQL，但 Target Runtime 需要长期真实数据库继续开发；只保留一次性测试库无法接入后续常驻 Worker。直接复用超级用户、开放公网 5432 或把长期密码写入 env 都会扩大攻击面。
- Constraint：现有宿主只有 1.6 GB 内存且同时运行 legacy 服务；legacy SQLite 不能停机或混接；凭证不得进入仓库；开发环境必须有可恢复备份，但不能冒充生产高可用。
- Decision：在 `alicloud-sh` 的 PostgreSQL 14 上建立 `larkflow_target_dev`，由无密码角色与同名 Unix 服务用户通过本机 Unix socket 的 peer authentication 访问。5432 只监听 localhost，撤销 `PUBLIC` 的数据库连接权，并配置 statement、lock 和 idle transaction 超时。每天生成 custom-format 本地备份、保留约 7 天，并用一次性新库完成真实恢复演练。
- Alternatives(否决)：现在购买 RDS；继续只用临时数据库；让 Target 复用 legacy SQLite；应用使用 postgres 超级用户；保存长期 TCP 密码；把 PostgreSQL 暴露到公网。
- Tradeoff：省去托管数据库成本并获得可持续开发环境，但数据库、备份和宿主处于同一故障域，没有异机副本、PITR、自动故障转移或托管升级。该方案只适用于当前开发阶段，生产前必须补异机备份、恢复周期、容量告警与升级流程。

## ADR-057 · 2026-08-01 · Target 运行时使用独立 CLI、能力过滤与 systemd 常驻进程

- **Status：Accepted · Development deployment。**
- Problem：单步 Worker 已能证明 claim 规则，但没有可持续运行、可停机和可被 systemd 拉起的应用入口。若只按 `executor=tool` 选择 adapter，开发 adapter 会先认领自己不支持的业务 Tool kind，再把节点错误标成失败。
- Constraint：Target 与 legacy CLI、服务和持久化必须隔离；启动时先验证 migration 与数据库权限；空闲轮询不能忙等；SIGTERM 必须唤醒等待并留下干净停止证据；瞬时数据库错误不得杀死 daemon；只有 adapter 明确接受的具体节点才能在 claim 前进入候选集。
- Decision：新增独立 `larkflow-target` CLI 与常驻 Worker loop，使用最小和最大间隔的可中断指数退避、结构化日志、`hostname:pid` Worker identity 和 systemd `Restart=on-failure`。Worker 在读取不可变 Snapshot 后调用 adapter 的能力判定，把允许的 node key 交给事务服务认领。开发环境只注册 `development.echo`，用于普通执行与崩溃恢复演练。
- Alternatives(否决)：复用 legacy `larkflow serve`；用 cron 反复执行 run-once；只按 Human / Agent / Tool 三类粗粒度认领；未配置 adapter 时先 claim 再失败；在 systemd unit 中保存数据库密码。
- Tradeoff：每个候选实例在 claim 前多一次聚合读取，换取不会误认领未知能力。当前服务能证明持久化运行和恢复，不提供真实 Agent、业务 Tool 或飞书投影；Projection worker 接入前 outbox 会持续积压。

## ADR-058 · 2026-08-01 · Feishu Task Projection 使用独立 Outbox Worker 与受限数据库身份

- **Status：Accepted · Development deployment。**
- Problem：Human 节点已经能进入 `waiting_human`，但 outbox 没有消费者，责任入口无法到达飞书。若 Projection 与 Runtime 共用进程，飞书故障会影响领域调度；若复制 Linux lark-cli 的加密配置与 master key，又会扩大凭据访问边界。
- Constraint：数据库事务不得跨越飞书调用；外部创建是 at-least-once；不同 outbox 消费者不能互相认领事件；Task GUID、幂等键和同步版本必须持久化；Projection 身份不得更新 Instance、Node 或 Attempt；测试不得访问真实飞书。
- Decision：新增独立 Projection Worker，只认领 `node.projection_create_requested` 与 `node.projection_sync_requested`。Worker 先提交 outbox claim，再以 `tenant + node_instance + attempt + kind` 派生稳定幂等键调用 lark-cli，随后 UPSERT Projection 并发布 outbox；失败按有界指数时间重试。Human 进入 `waiting_human` 后创建任务，节点进入 done、failed 或 canceled 后按 Task GUID 完成任务。非 Human 投影事件发布为 noop，其他事件留给其所属消费者。
- Deployment：开发机不复制飞书凭据。Projection systemd 服务以现有 `lf-dev` OS 身份运行，复用该身份下的测试 profile；同名 PostgreSQL peer 角色只有所需表的 SELECT、Outbox UPDATE 与 Projection INSERT / UPDATE。它通过单用户 ACL 穿越 Target venv，不能读取 Runtime env，也不能写领域状态。Projection 启动只读验证 migration，不获得 schema DDL 权限。
- Alternatives(否决)：在领域事务中直接调用飞书；把 Projection 合进 Runtime loop；复制 lark-cli 的 config、密文和 master key；让 Projection 使用 `lf_target_dev` 的完整数据库权限；不按事件类型过滤共享 outbox。
- Tradeoff：当前开发部署复用了 legacy OS 身份来持有测试飞书凭据，仍不是生产身份拓扑。Task 创建 / 完成已真栈验证，但入站事件、IM / Doc 投影、启动全量对账、缺失对象重建和最小飞书 scope 回归仍未完成。

## ADR-059 · 2026-08-01 · Target Task 入站使用单消费者、耐久 Inbox 与凭据隔离双阶段

- **Status：Accepted · Development deployment。**
- Problem：Target Human 节点已能创建飞书 Task，但人在飞书完成任务后仍不能推进 Target 状态。同一 EventKey 只允许一个消费者，而且 V2 事件信封只给 event ID、Task GUID 与事件类型，不能证明是谁完成。直接让 Target 领域服务读取 legacy 的飞书 profile 会扩大凭据边界。
- Constraint：legacy 长连接必须保持唯一消费者；事件必须以 event ID 去重；不信任客户端 actor；飞书读取不得跨越领域数据库事务；legacy 不得修改 Target 领域状态；Target 领域身份不得因入站而获得飞书凭据。
- Decision：legacy 事件观察桥接只把 Task 完成信号持久化到 PostgreSQL Inbox。持有测试 profile 的 `lf-dev` 校验 Worker 在事务外读取 Task 详情，把可验证字段写回 Inbox。不持有飞书凭据的 `lf_target_dev` 领域 Worker 再校验 Projection 当前轮次、Task 绑定、企业应用来源、`mode=1`、唯一 Owner assignee、Task 已完成与完成人严格等于 Owner，最后以 Owner actor 调用 Human 提交命令。Task Projection 因此固定使用原生 API、`mode=1`、唯一 assignee、稳定 client token 与绑定字段。
- Alternatives(否决)：再启一个相同 EventKey 消费者；从事件信封推断 actor；给 `lf_target_dev` 授予 legacy 凭据目录读权；让 legacy 直接提交 Human 节点；在持久化事件前同步读取飞书；继续创建不能稳定得到完成人的 `mode=2` 任务。
- Tradeoff：一个简单完成事件需要 Inbox 与两个独立服务，运维拓扑更多；换取事件耐久性、崩溃恢复、凭据隔离和服务端可验证授权。当前只表达「Owner 已在飞书确认完成」，不支持从 Task 携带任意结果内容。

## ADR-060 · 2026-08-01 · 首个 Target Agent 采用窄 LLM 契约并让租期覆盖完整路由预算

- **Status：Accepted · As-built foundation。**
- Problem：Target 已能可靠认领 Agent 节点，但缺少真实 executor。直接复用 legacy prompt 字段会把旧模板语义带进新内核；只设置单线路 timeout 又无法约束主备故障切换的最坏总时长，正常慢调用可能在结果提交前失去 claim。Agent 结果若只留在 PostgreSQL，最终人工 Owner 也无法在飞书责任入口中复核。
- Constraint：数据库事务不得跨越 LLM 调用；Agent 只能处理当前节点与已提交输入；模型供应商和凭证不能进入 Instance Snapshot；Runtime 只能认领 adapter 明确接受的 kind；迟到结果不得覆盖新 claim；测试不访问网络或真实凭证。
- Decision：首个 adapter 只接受 `work.agent={kind: llm.generate, model_role, instructions}`。它把节点目标、输出、验收、冻结的 Instance 输入和直接依赖结果组成 prompt，返回正文、逻辑角色与 tenant-scoped Attempt 请求标识。下游 Human Task 优先展示直接依赖的正文，并只在投影描述中截断。Target 装配复用 OpenAI 兼容逻辑角色，但启动时计算每个角色主线路与全部备用线路的 timeout 总和，要求最长总和加安全余量严格小于 Node claim TTL；SDK 内层重试继续为零。
- Security：Target Runtime 使用独立 OS 身份。复制其他服务已有的 `LLM_API_KEY` 会扩大凭证可读边界，机器管理授权不自动等于该复制授权；开发部署应使用专属 key，或在说明风险后取得明确复用授权。
- Alternatives(否决)：继续让 Agent 节点保持 ready 但无 executor；复用 legacy `prompt / model_role` 顶层形状；在数据库事务内调用模型；静默截断持久化结果；只比较一个 timeout 而忽略备用链；默认把 legacy key 复制给 Target。
- Tradeoff：模型调用仍是 at-least-once，Worker 崩溃后可能重复生成和计费，稳定请求标识只提供审计与可选 adapter 幂等，不代表供应商已保证幂等。当前只有单次生成，没有有限业务重试、质量闭环或复杂 LangGraph NodeRun；云端真链路在独立凭证装配前保持未验证。

## ADR-061 · 2026-08-01 · 模板启用使用最新不可变版本，变更采用停用后追加

- **Status：Accepted · As-built foundation。**
- Problem：数据库已有 Template 与 TemplateVersion 表，但没有领域服务、模板 aggregate 并发版本、生命周期审计或正式实例化入口。若另加 active version pointer，它可能与模板状态和最新版本漂移；若允许 enabled 模板直接追加版本，两个并发管理员可能在不知情时改变后续实例来源。
- Constraint：模板版本不可更新或删除；实例必须保存完整图、参数、Owner 绑定、模板版本和锁状态；模板不得保存真实人员 ID、供应商端点或长期凭证；所有数据库事务都不能跨越飞书、LLM 或 Tool 调用。
- Decision：Template aggregate 使用独立 version 做 compare-and-swap，并把生命周期变化和版本追加写入 `workflow_template_events`。`draft` 与 `disabled` 可追加严格连续的新版本，`enabled` 只允许实例化最新版本，不允许追加；修改路径固定为 `disable -> append version -> enable`，因此不设置 active pointer。实例化要求参数和逻辑 Owner 角色精确绑定，并生成含 `template_version_id` 与 `locked` 的冻结 Snapshot。`preview` 只读校验 draft，`confirm` 保持独立命令。
- Alternatives(否决)：允许原地更新版本；在 enabled 状态静默追加；另存 active pointer；把人员 ID 或模型配置写进模板；创建草稿后自动确认启动。
- Evidence：离线测试覆盖生命周期、输入类型、角色绑定、非法引用、供应商配置、Owner 授权和只读预览。一次性 PostgreSQL 14 数据库中的两路并发启用恰好一条成功、一条命中 optimistic concurrency，版本触发器拒绝更新，实例外键与冻结快照均回读一致。
- Tradeoff：当前每次启用都选择最新版本，简单且确定，但暂不支持同时灰度多个版本。角色绑定仍由调用方提供，企业目录有效性校验和模板管理界面尚未实现；`locked` 已进入快照，真正的运行中编辑命令仍在 Phase 2。

## ADR-062 · 2026-08-01 · Task 凭据验证采用有限预算与显式耗尽终态

- **Status：Accepted · As-built foundation。**
- Problem：指数退避只限制调用频率，不限制总次数。一条服务端详情长期不可验证的完成事件已经失败 24 次并继续每五分钟读取，形成永久外部调用和日志噪声。
- Constraint：飞书事件只作唤醒信号，不能绕过 Task 详情读回；短暂最终一致性和瞬时网络故障仍需自动恢复；终止不能让领域 Worker 获得未验证 payload；运维必须能从数据库和结构化日志识别耗尽事件。
- Decision：凭据侧默认最多验证 24 次，保留 5 秒起步、5 分钟封顶的指数退避，总等待窗口约 90 分钟。达到预算仍失败时，使用当前 claim token 原子写入 `exhausted`、终止时间、`outcome=exhausted:verification_attempts`、失败阶段和最后错误，随后清除 claim。`exhausted` 不进入任何 claim 条件，日志单独累计耗尽数。
- Alternatives(否决)：永久重试；第一次读不到立即丢弃；只限制退避上限而不设总预算；把未验证事件交给领域 Worker 决定；静默标记终止但不保留原因和告警信号。
- Tradeoff：超过约 90 分钟的真实飞书或凭据故障可能让有效事件进入终态。当前没有自动 redrive 命令，因此非零耗尽必须人工调查；投影全量对账与缺失对象恢复完成后，应由权威状态恢复仍在等待的 Human 节点，而不是重新开启无限事件重试。

## ADR-063 · 2026-08-01 · Task 对账以 PostgreSQL 为权威且只在明确删除时换绑

- **Status：Accepted · Development deployment。**
- Problem：Outbox 可以恢复未发布事件，但无法修复“事件已发布而 Projection 记录丢失”或“Projection 仍在而飞书 Task 已删除”。若启动时只看进程和 Outbox 状态，当前 Human 责任入口可以永久缺失。
- Constraint：PostgreSQL 继续是业务权威；飞书读写不进入数据库事务；创建响应丢失时重试必须幂等；权限或瞬时故障不得导致新 Task；两个对账者不得相互覆盖新绑定；已结束流程不补发历史责任入口。
- Decision：Projection 常驻进程在 Outbox 循环前，按 Instance ID 分页扫描当前 `waiting_human` 节点和尚未收口的已有终态 Projection。没有 Projection 时复用 Attempt 级稳定键创建；已有 Projection 先查 Task，只有 Task v2 返回 `1470404` 才使用 `repair_generation + 1` 派生新稳定键重建。替换 Projection 必须同时匹配旧 GUID、旧幂等键、记录身份和同步版本；响应丢失时下次仍使用同一 repair generation。单个实例失败记入结构化报告后继续其他实例。
- Deployment：开发服务器已运行该版本；启动和显式对账均只读确认两条现有 Task 绑定不变且无失败。真实删除后的外部重建仍待单独验收。
- Alternatives(否决)：只重放已发布 Outbox；每次启动无条件重建所有 Task；将任意读失败视为已删除；原地复用已确认删除对象的 client token；无并发条件覆盖 Projection；为已终止且没有 Projection 的历史节点补发任务。
- Tradeoff：启动时每个活跃 Human Task 多一次只读 API，大租户的启动时间与 API 配额会随当前等待节点数增长。当前只在启动和显式命令中对账，没有周期调度；真实飞书 Task 删除后重建与最小 scope 仍需开发环境验收。
- Evidence（2026-08-02）：专用单 Human 实例的旧 Task 被确认删除并读回 `1470404` 后，对账只重建 1 条、将 Projection 原子换绑到不同 GUID、写入 `repair_generation=1`；第二次对账不再重建。人工完成新 Task 后，凭据侧验证和领域侧提交各成功处理 1 条，修复后的 Projection 保持绑定并进入完成态。该证据完成真实删除重建及后续入站验收，最小 scope 回归仍未完成。

## ADR-064 · 2026-08-02 · Human Task 完成以周期读回为可靠入口

- **Status：Accepted · As-built foundation。**
- Problem：开发应用在线版本中的 Task 变化事件使用用户身份，而服务器只装配 bot profile。即使临时放宽应用 scope，bot EventKey 消费者仍收不到测试组织中人工完成 Task 的事件，外部 Task 已完成但 Target Human 节点永久停在 `waiting_human`。把事件总线进程存活或应用版本发布成功视为链路健康，无法证明完成信号可达。
- Constraint：不能信任事件或轮询结果中的 actor；任何飞书读取都不得进入领域数据库事务；可靠路径必须耐久、幂等、可恢复；单个 Task 读取失败不能阻塞其他实例；不能为此新增持有更宽权限的领域服务。
- Decision：现有 Projection 服务周期扫描当前 `waiting_human` 节点的 Task Projection，默认每 30 秒按 Instance ID 分页读取 Task。只有详情明确为 `done` 且存在完成时间时，才以 tenant、Projection、Task GUID 和完成时间派生稳定信号 ID，写入 PostgreSQL Inbox。已有凭据验证 Worker 仍在事务外重新读取 Task，领域 Worker 仍校验当前 Attempt、Projection 绑定、应用来源、唯一 Owner 与完成人。飞书事件保留为可选低延迟信号，但不再承担可靠性。
- Operations：新增 `reconcile-completions` 一次性命令，以及 `LARKFLOW_TARGET_COMPLETION_POLL_SECONDS` 和 `LARKFLOW_TARGET_COMPLETION_POLL_BATCH_SIZE`。常驻 Projection 每次扫描输出结构化计数，启动后立即执行一次，后续按单调时钟调度。轮询与事件重复到达由 Inbox 幂等和领域状态校验共同吸收。
- Alternatives(否决)：要求服务器登录用户 profile；继续扩大应用 scope 等待 bot 事件；让 legacy 定时扫描 Target 状态；轮询发现完成后直接提交 Human 节点；新增第五个 Target daemon 和另一套凭据。
- Tradeoff：每个等待中的 Human Task 会产生周期只读 API 调用，调用量随活跃责任入口线性增长。当前单企业开发阶段接受 30 秒延迟与轮询成本；进入更大规模前应增加分页游标、速率预算、抖动和失败告警，而不是重新把事件当成唯一可靠通道。
- Evidence：完整离线套件 `622 passed, 7 skipped`。开发服务器首次扫描读取 3 个当前 Human Task，观察到 2 个完成、1 个待办，新增 2 条 Inbox 信号；凭据侧验证 2 条，领域侧提交 2 条，两个滞留实例、Node 与 Projection 随后全部完成。显式重跑只读取剩余待办 Task，新增信号为 0。五个服务均为 active、`NRestarts=0`。该验证使用已回归的最小业务 scope；临时应用版本管理 scope 尚未移除。
- Evidence（权限收口，2026-08-02）：关闭 `application:application:patch` 与 `application:application:self_manage` 后，权限页只剩 `task:task:read` 和 `task:task:writeonly`，并显示当前修改均已发布；在线版本 `1.0.7` 保持已发布。收口后机器人成功创建并完成临时 Task，显式完成轮询读取 1 条当前 Human Task、结果为 1 条待办且失败为 0；五个服务保持 active、`NRestarts=0`。

## ADR-065 · 2026-08-02 · Personal Agent Edge 作为可撤销的本人只读执行器直连中央节点

- **Status：Accepted · Experimental Proof，未部署。**
- Problem：真实产品设想不是让中央服务器代替每名员工运行同一种模型，而是员工在本人电脑上选择已有的 Codex、Claude 或同类工具完成本人负责的 Agent 节点。若把本机 Edge 建在飞书 `lark-cli` 上，设备身份、飞书用户登录、企业应用凭据与流程执行身份会混在一起，还会错误地让飞书连接承担任务传输和租约协议。
- Constraint：中央 PostgreSQL 继续是唯一业务真相；每个节点仍有唯一人类 Owner；设备不能代答 Human gate；配对、领取、续租和结果必须由服务端重新授权；设备失窃后可撤销；迟到结果不得覆盖当前 Attempt；Proof 不能引入任意 shell、写文件、后台常驻、通用能力安装或中央飞书凭据下发。
- Decision：Personal Agent Edge 是可选的 User-owned Agent Runtime，通过中央私有 HTTPS API 直连，不使用 `lark-cli`。管理员为指定 tenant 和 person 签发最多 1 小时、一次性配对码，设备换取可撤销凭据，中央只保存 purpose-separated hash。Proof 唯一 capability 固定为 `personal.readonly`；设备只可领取 Owner 等于该 person、executor 为 Agent 且 kind 精确匹配的节点。执行继续复用现有 NodeAttempt 的 Worker、token、版本和租期，长任务在原 claim 上续租，撤销或过期后续租与回传均拒绝。
- Local boundary：用户每次手工执行 `run-once` 并选择一个明确工作区。Codex 以 `read-only + ephemeral + ignore-user-config` 启动，子进程不继承 Edge credential、Target DSN 或飞书应用凭据，超时终止整个进程组。本机执行器基础设施异常不调用领域失败命令，交由租约到期后恢复，避免一台电脑的安装问题直接把业务流程判失败。
- Network boundary：Gateway 强制监听 loopback，仓库不直接开放公网端口；远程设备必须经独立 HTTPS 反向代理。客户端拒绝非 loopback 明文 HTTP、重定向、URL 内凭据和带路径地址，并默认不继承系统代理。`/edge/v1` 只提供 pair、claim、renew、complete 和 fail，不提供图编辑、Human 提交或飞书能力。
- Alternatives(否决)：让每台电脑直接访问 PostgreSQL；复用员工或服务器的 `lark-cli` 作为传输层；给设备下发中央飞书应用 secret；把电脑本身当作组织责任人；按 `executor=agent` 粗粒度领取所有 Agent 节点；第一版直接运行任意命令或常驻监听。
- Supersedes / refines：取代 ADR-046 中“通过本机 lark-cli 接入 Codex / Claude”的传输细节，保留其“待办只分配给人，个人 Agent 只是边缘执行方式”的责任原则。它把 ADR-052 的个人 Edge 后置结论收窄为“产品化仍后置，但允许窄 Proof”，不恢复 ADR-050 的通用 Capability Lease。
- Tradeoff：普通 `0600` 文件只能满足 Proof，不能替代 Keychain、硬件密钥或设备证明；只读沙箱只证明写入受限，目录级读取隔离尚未验证，恶意中央输入仍可能诱导 Agent 读取所选工作区之外的可读文件，工作内容也可能发送给模型供应商；心跳会增加写负载；手工 `run-once` 无法证明持续采用。离线、真实 PostgreSQL 与合成数据本机链路只证明当前协议可执行，不证明真实 HTTPS、企业政策或市场价值。
- Update（2026-08-02）：不改原决策的 HTTPS 与产品化边界。长期开发库已应用 Edge migration，Gateway 已作为仅监听 loopback 的 systemd 服务部署；本机通过临时 SSH 隧道完成跨机 Codex 领取、续租、回传和撤销验收。公网 HTTPS 仍未部署。
- Update（2026-08-02）：专用 DNS-only 子域名、Caddy 和受信任源站证书已经完成源站验证，Gateway 仍保持 loopback。中国内地 ECS 的公网入口必须先满足 ICP 接入备案，不接受“证书已签发等于公网 Edge 已可用”，也不通过 Cloudflare Tunnel 或非标准端口绕过备案。当前 Caddy 已停止并禁用开机启动，配置与证书保留；完成接入备案或迁移到合规的非中国内地环境后，必须重新执行配对、领取、续租、回传和撤销验收。现场证据见 MEMORY。

## ADR-066 · 2026-08-02 · Tool executor 按 kind 路由并以确定性内容检查作为首个业务能力

- **Status：Accepted。**
- Problem：`executor=tool` 只说明执行方式，不能作为业务能力标识。单一开发 echo 无法证明真实 Human、Agent 与 Tool 的依赖结果能在同一中央 DAG 中闭环，也不能安全扩展到多个 Tool。
- Decision：增加 `ToolExecutorRouter`，按 `work.tool.kind` 精确选择内部 adapter，未知 kind 在 claim 前拒绝。首个 `content.check` 只读取直接依赖正文，执行长度与必需词检查，返回稳定 `pass / fail + evidence + suggestion` 和请求标识，不调用模型、不写飞书。质量 verdict 同时进入 Attempt 的 `quality_result`，完整结果可被下游 Human Task 展示。
- Alternatives(否决)：按 node id 注册 handler；让 Runtime 先 claim 再发现不支持；把检查 prompt 交给 LLM；为每个模板新增 Python executor 类型。
- Tradeoff：当前检查只覆盖确定性文本契约，不证明事实正确、语义质量或业务合规；更多 Tool 必须逐个定义输入来源、副作用、幂等键和失败语义。开发真栈已闭环，不代表生产装配完成。

## ADR-067 · 2026-08-02 · Owner 目录校验在草稿写入前 fail closed 且默认关闭

- **Status：Accepted，live validation blocked by scope。**
- Problem：模板角色绑定和无模板 Snapshot 都接受外部 open_id。只做非空校验会把离职、冻结、跨租户或拼错的人固化为唯一 Owner，随后 Task 投影和授权链路无法可靠恢复。
- Decision：Workflow Service 接受可选 `PersonDirectory` Port，在草稿写入前去重读取 Instance Owner 与所有节点 Owner。返回 ID 必须精确匹配，且目录明确证明已激活并非冻结、离职、退出或未入职；缺字段和读取失败都拒绝创建。功能由 `LARKFLOW_TARGET_VALIDATE_DIRECTORY` 显式启用，默认关闭。
- Tradeoff：启用会让创建草稿依赖飞书目录可用性，并需要新增通讯录只读 scope。当前代码与 670 项离线测试已通过并部署，但应用仍保持原最小 Task scope，真栈验证待权限确认。

## ADR-068 · 2026-08-03 · 飞书状态查询仅向 Instance Owner 暴露有界摘要

- **Status：Accepted · Development deployment。**
- Problem：Owner 在飞书中启动流程后缺少只读查询入口，只能依赖主动通知或外部保存 Instance ID；若直接暴露完整聚合，非 Owner 可枚举实例，节点结果正文和人员标识也可能泄露。
- Constraint：发送者必须先通过当前企业活跃成员校验；中央 PostgreSQL 是状态权威；读取不能修改领域状态、审计或 aggregate version；错误响应不能暴露实例是否存在；飞书文本回复必须有界。
- Decision：新增 `/larkflow status <instance_id>`，领域层通过 `get_for_owner` 原子读取并校验 Instance Owner。实例不存在与非 Owner 返回相同提示。回复只包含实例状态、进度、节点名称与 key、executor、节点状态和相对责任人，不包含结果正文或人员 ID；最多列出 20 个节点，每个可变字段最多 120 个字符，截断时明确提示省略数量。
- Alternatives(否决)：允许任意节点 Owner 查看完整实例；只在 IM handler 内比较客户端身份；为不存在与无权限返回不同错误；直接输出完整结果或 open_id；复用 legacy SQLite 状态；无上限拼接所有节点。
- Tradeoff：非 Instance Owner 即使负责某个节点也不能查看流程摘要；长流程只返回前 20 个节点，当前没有分页或文档链接。后续若开放协作者可见性，必须建立显式读取策略，不能放宽当前 Owner 检查。
- Evidence：完整离线套件 `694 passed, 8 skipped`。开发服务器 wheel SHA-256 为 `b81103d0edd7a38922b3a0298c27f97b54dd9a11ae229b6da9676cbb068c6c2c`，六个 Python 服务均为 active、`NRestarts=0`。测试组织中的 Owner 查询已完成实例后，耐久命令记录、回复投影和飞书服务端消息回读一致，回复包含完成状态、`4/4` 与相对责任人，且不包含 open_id。

## ADR-069 · 2026-08-03 · Owner 实例历史使用有界摘要读模型

- **Status：Accepted · Development deployment。**
- Problem：单实例状态查询要求用户先保存 Instance ID，无法回答“我最近有哪些流程”。若列表逐条加载完整聚合，会把节点、结果与人员标识带入不必要的读取面，也会随实例数量放大数据库开销。
- Constraint：发送者必须先通过当前企业活跃成员校验；tenant 与 Instance Owner 都必须在仓储查询中强制限定；PostgreSQL 是状态权威；列表只读且有界；排序必须稳定；不能依赖客户端过滤；不能修改领域状态、审计或 aggregate version。
- Decision：新增 `/larkflow list` 与 `InstanceSummary` 读模型。仓储在 SQL 中同时限定 tenant 和 `owner_person_id`，按 `created_at DESC, id DESC` 排序，只投影 Instance ID、目标、状态和节点完成计数。命令展示十条并查询第十一条判断是否提示更多；目标文本最多 120 个字符，不返回节点结果或任何人员 ID。
- Alternatives(否决)：扫描后在 IM handler 过滤 Owner；逐条 `get` 完整聚合；允许节点 Owner 看到实例历史；无上限返回所有实例；仅按时间排序而没有稳定并列键；复用 legacy SQLite 搜索。
- Tradeoff：当前没有分页、时间筛选或协作者视图；同一 Owner 超过十条时只能看到最近一页。后续若增加分页，游标必须同时包含创建时间和 Instance ID，并继续保持 tenant 与 Owner 的服务端过滤。
- Evidence：完整离线套件 `698 passed, 9 skipped`，删除 Owner 或 tenant 过滤均被定向变异测试捕获。一次性 PostgreSQL 14 验证返回十条、Owner 隔离、稳定排序、草稿进度和索引存在性。开发服务器应用九份 migration 后，测试组织命令记录为 `processed / instances_listed`、回复为 `sent`；飞书服务端回读到十条本人实例，包含完成与进行中进度及详情提示，不包含人员 ID。六个 Python 服务均为 active、`NRestarts=0`。

## ADR-070 · 2026-08-03 · 节点重启采用耐久预览与原子确认

- **Status：Accepted · Development deployment。**
- Problem：运行中、失败或已完成节点需要安全重做，但直接执行重启会隐藏影响范围，并可能覆盖历史结果、接受旧 Worker 的迟到回写，或在两次确认竞争时生成多轮 Attempt。Human 节点还存在外部 Task，若只改数据库状态，旧 Task 会继续误导 Owner。
- Constraint：只有 Instance Owner 可以发起和确认；影响集合必须由服务端计算为目标节点及全部可达下游；预览期间 aggregate 或图发生任何变化都必须失效；历史 Attempt、结果和质量记录不可覆盖；aggregate、审计、outbox 与预览消费必须同事务；重复确认与命令重试不能重复创建 Attempt 或 Task；飞书副作用不能进入数据库事务。
- Decision：`/larkflow restart` 创建默认 15 分钟有效的耐久 RestartPreview，绑定 tenant、Instance、创建 actor、目标节点、拓扑排序后的影响集合、aggregate version 与 `graph_revision`，不修改 aggregate 或审计。`/larkflow restart-confirm` 重新校验创建 actor 仍是当前 Instance Owner，锁定预览并重算影响；确认事务取消活动旧 Attempt、清除 claim、为影响节点创建新 Attempt、将目标置为 ready、下游置为 pending、消费预览并追加一条审计与 Human Task 收口 outbox。已消费预览返回当前状态，不再写入。节点重启不改变 `graph_revision`，因为图结构未变化。
- Safety：当前只允许 `running / done / failed` Instance 和 `running / waiting_human / done / failed` 目标节点，且目标直接依赖必须完成。失败 Instance 若仍有影响集合之外的失败节点会拒绝重启。旧 Human Attempt 的投影按历史 Attempt 状态关闭，新的稳定 Attempt 键创建不同 Task；旧 Task 或旧 Worker 的迟到结果不能推进当前 Attempt。
- Alternatives(否决)：收到命令立即重启；只在内存保存预览；相信客户端提交影响集合；覆盖原 Attempt；用 `graph_revision` 表示纯运行态重做；允许新 Owner 消费旧 Owner 的预览；数据库提交后才标记预览已消费；让重复确认再次重启；在领域事务内同步关闭或创建飞书 Task。
- Tradeoff：每次预览都会留下耐久行，当前没有后台清理过期预览；首版只提供节点重启，不提供完整实例重启、批量选择或 UI 按钮。失败节点覆盖规则偏保守，可能要求 Owner 选择更上游的共同祖先。飞书 Task 收口仍依赖 outbox 最终一致性，短暂延迟期间旧 Task 可能仍可见，但不能通过领域授权推进当前 Attempt。
- Evidence：完整离线套件 `709 passed, 10 skipped`。五类定向变异分别证明 Owner、版本、可达下游、外部失败节点和历史 Task 状态断言能够捕获缺陷。一次性 PostgreSQL 14 中两个真实连接确认同一预览，恰好一路执行、一路幂等回放，aggregate version 只增加 1、审计只有 1 条。测试组织实例在最终 Human 节点等待时完成预览、确认、旧 Task 关闭、新 Task 创建、重复确认 no-op 和新 Attempt 完成；Instance 最终为 done，两个 review Attempt 分别为 canceled 与 done，重启审计仍为 1 条。

## ADR-071 · 2026-08-03 · 完整实例重启使用显式 scope 并分代完成投影

- **Status：Accepted · Development deployment。**
- Problem：节点重启已经具备安全预览与原子确认，但完整实例重做不能可靠表达为“选择某个特殊节点”。特殊值会污染节点标识、产生含糊授权，并且无法正确处理多根图。实例再次完成时若继续复用原完成 Projection 键，Owner 也看不到新一轮文档和最终通知。
- Constraint：中央冻结图仍是唯一影响计算来源；只有 Instance Owner 可以预览和确认；节点与实例 scope 必须可判别且由数据库约束；全图重启必须支持多个根节点；旧 Attempt、结果、Task、文档、通知和审计不得覆盖；重复确认与并发确认必须幂等；首次完成的既有幂等键不能因升级改变。
- Decision：RestartPreview 增加显式 `node / instance` scope。node scope 必须有节点键，instance scope 的节点键必须为空；后者的影响集合是拓扑排序后的全部节点。共享确认事务按 scope 重算影响，为每个节点创建新 Attempt，并把所有根节点置为 ready、其他节点置为 pending。完整重启不改变 `graph_revision`。完成文档与最终通知在 Attempt 1 继续使用历史键，从 Attempt 2 开始把当前规范终端节点 Attempt 编号纳入幂等键和 Projection 唯一性。
- Alternatives(否决)：用 `*`、空字符串或虚构 root 作为节点键；逐个调用节点重启；只重启单个根节点；覆盖旧 Attempt 或旧完成 Projection；每次对账都创建新完成资源；改变首次完成的历史幂等键。
- Tradeoff：完成轮次暂以排序后的规范终端节点 Attempt 编号标识，能够覆盖当前单层 DAG 和多根场景，但产品界面还没有跨轮次浏览与显式 completion generation。过期预览仍没有后台清理；外部投影继续最终一致，短时可能先完成领域状态再出现新文档。
- Supersedes / refines：扩展 ADR-070 的首版范围限制，保留其 Owner 重授权、耐久预览、版本校验、历史不可变、同事务写入和重复确认 no-op 原则。
- Evidence：完整离线套件 `715 passed, 11 skipped`。一次性 PostgreSQL 14 中，instance scope 的两个真实连接确认同一预览，恰好一路执行、一路幂等回放，aggregate version 只增加 1，全部受影响节点进入下一 Attempt，旧结果保留且实例重启审计只有 1 条。测试组织三节点实例从完成态执行全图重启后，当前 Attempt 为 2、2、3，根节点重新调度并再次完成；重复确认不新增版本、Attempt、Task 或审计。两轮完成文档和最终通知使用不同外部 ID，新文档已从飞书服务端回读三节点结果。内容提交 `e66f6ab` 已部署到开发服务器，六个 Python 服务均为 active、`NRestarts=0`，验收窗口无错误级日志。

## ADR-072 · 2026-08-03 · 运行中图编辑采用未来区域预览与原子确认

- **Status：Accepted · Development deployment。**
- Problem：Instance 启动后仍可能需要补充、调整或删除尚未开始的工作，但直接修改冻结图会绕过 Owner 授权和 DAG 校验，也可能覆盖已执行历史、接受陈旧确认，或让飞书投影继续处理已删除节点。
- Constraint：只有当前 Instance Owner 可以发起和确认；首版只允许 `running` 且未锁定实例；已开始、已产生 claim、结果、质量、时间戳、提交者或错误的节点都不能跨越冻结线；Template 与已执行 Attempt 必须保持不变；预览期间 aggregate、revision 或编辑语义发生变化必须失效；聚合、预览消费、审计与 outbox 必须同事务；并发和重复确认不能重复应用。
- Decision：`/larkflow edit` 只接受有界的 `add_node / update_node / remove_node` JSON 操作，并只允许修改没有执行痕迹的 `pending / ready` 当前 Attempt。服务端重新验证完整 DAG、Owner 与工作定义，生成默认 15 分钟有效的耐久 GraphEditPreview，绑定 tenant、Instance、创建 actor、规范化操作、增删改集合、aggregate version、当前与目标 `graph_revision` 及候选 Snapshot SHA-256。`/larkflow edit-confirm` 重新授权创建预览的当前 Owner，重新执行操作并比较全部语义摘要；确认事务保存 aggregate、消费预览、把 `graph_revision` 增加 1、追加一条审计及必要 outbox。客户端提供的身份、revision、影响集合和候选图都不是授权事实。
- Safety：单次最多 50 个操作，确认后的图最多 100 个节点，同一节点每次只能触碰一次。新增节点创建 Attempt 1；更新节点保留未开始 Attempt 并刷新输入快照；删除节点只移除未开始 Node 与 Attempt。已删除节点的陈旧投影创建事件按 no-op 收口。若删除最后一批未完成未来节点后剩余节点均已完成，Instance 可以进入 `done`。重复确认只回读首次应用结果。
- Alternatives(否决)：直接执行消息中的图变更；把预览只存在进程内；允许编辑 `running / waiting_human / done / failed` 节点；覆盖已有 Attempt；反写 Template；信任客户端的 Owner、revision 或影响集合；只比较 aggregate version 而不比较候选图语义；用节点重启表达结构变化；在数据库事务内同步调用飞书。
- Tradeoff：操作语法刻意较窄，首版没有图形化 diff、批量多次改写同一节点、字段级 ACL 或预览后台清理。更新未开始节点会保留当前 Attempt 编号，这能避免制造虚假历史，但跨轮次产品界面仍需明确区分结构 revision 与 Attempt generation。外部投影继续最终一致，陈旧事件必须由 Worker 的 no-op 语义收口。
- Supersedes / refines：实现并细化 ADR-013 与 ADR-050 中的未来区域编辑目标，保留冻结线、模板不可变、Owner 授权、预览确认和 revision 乐观并发原则。
- Evidence：完整离线套件 `726 passed, 12 skipped`。一次性 PostgreSQL 14 应用十二份 migration，两个真实连接并发确认同一编辑预览，恰好一路执行、一路幂等回放，aggregate version 和 `graph_revision` 都只增加 1，候选节点与依赖正确，审计只有一条；测试库与临时文件随后删除。内容提交 `6645d9d` 构建的 wheel SHA-256 为 `7ef30780e53df895a4c93d3c4eeb1783007cf2ed5f5c26015120f722423169d1`，已安装到开发服务器 Target 与 legacy 环境；长期库应用 `0012_graph_edit_previews` 后，六个 Python 服务均为 active、`NRestarts=0`，部署窗口无 warning 级日志。真实飞书 edit 命令验收将在本次发布流程的文档提交后执行。

## ADR-073 · 2026-08-03 · 飞书角色绑定只引用认证 mention 元数据

- **Status：Accepted · Offline implementation。**
- Problem：模板已经使用逻辑 `owner_role`，但飞书 `/larkflow start` 过去把全部角色固定给发送者，无法从真实会话建立跨人员责任。允许用户直接填写 open_id 或显示名称会把身份解析和授权交给不可信文本，并且无法证明该人员来自本条消息或当前企业。
- Constraint：Instance Owner 仍是发送者；每个节点仍只有一个人类 Owner；模板不能保存真实人员 ID；草稿确认门不变；凭据侧可以读取企业目录，领域侧不能读取 lark-cli profile；命令必须耐久、可重放、可审计且不依赖显示名称；旧命令保持兼容。
- Decision：`start` 增加可选 `role=@成员` 绑定。`@成员` 在飞书文本中表现为 mention key，桥接层从同一条认证事件提取 key 与 open_id 并随命令持久化。凭据侧验证发送者和所有被引用人员属于当前 tenant 且状态活跃；领域侧只用已保存的 key 映射人员并冻结 Snapshot。未显式绑定的角色归发送者，发送者继续作为 Instance Owner。群聊中允许一个或多个认证 mention token 位于命令前，用于兼容 @机器人唤起。
- Safety：角色名限定为 lower snake case；重复角色、未知模板角色、非法 mention key、缺失或歧义 mention、非 open_id 标识和非活跃成员全部 fail closed。显示名称不持久化，也不参与授权。文本里直接出现的 open_id、名称或伪造 `@_user_N` 不能建立绑定。
- Persistence：migration `0013_im_command_mentions` 为 `workflow_im_commands` 增加非空 JSONB 数组。验证 Worker 和领域 Worker 从同一记录读取 mention，避免进程间重新解析显示文本或依赖短期内存。
- Alternatives(否决)：把 open_id 写进命令；按显示名称搜索通讯录；把角色绑定放进普通 JSON 输入；只在桥接进程内保存 mention；让领域 Worker直接读取飞书；为跨人员创建另开一套管理员 CLI。
- Tradeoff：首版语法适合显式模板角色，不提供自然语言识别、角色选择 UI、部门或群组 Owner，也不允许一个角色绑定多人。真实正向验证需要一个包含机器人和测试成员的群聊，因为单聊未必能 @到第三人。目录调用数随被引用的不同人员线性增加，当前上限由模板和最多 100 个绑定共同约束。
- Evidence：聚焦离线套件 `73 passed`，完整离线套件 `740 passed, 13 skipped`。覆盖原始 V2 与拍平 mention 形状、群聊前缀、恶意前缀、缺失元数据、重复与未知角色、非活跃成员、旧命令兼容、跨人员冻结 Snapshot、模板双角色和 PostgreSQL 可选往返测试。长期开发库、真实群聊、模板发布和跨人员 Task 投影尚未验收。

## ADR-074 · 2026-08-04 · 跨人员分工不依赖群聊，单聊使用耐久人员选择卡

- **Status：Accepted · Development validation。**
- Problem：`role=@成员` 能安全复用本条消息的认证 mention，但用户为了给其他员工派单而临时拉群并不是流程成立的必要条件。单聊无法稳定 mention 第三人，继续要求群聊会把聊天拓扑错误地变成工作流依赖。
- Constraint：Instance Owner 仍是发起人；每个角色仍冻结到一个活跃人员；卡片 payload、显示名称和客户端身份都不是授权事实；领域侧不能读取 lark-cli profile；回调、草稿创建、卡片回写和文本回复必须耐久、幂等并可恢复。
- Decision：当 `start` 的启用模板含发送者之外的未绑定角色时，凭据侧先读取有界活跃成员候选快照，再发送 Card 2.0 `select_person` 表单。回调只接受原命令发送者，凭据侧重新读取目录并验证每个选择仍属于候选快照且状态活跃；领域侧随后按 tenant、message 和角色绑定确定性生成一个 Instance ID，并冻结一个草稿。群聊 mention 入口继续保留，两条入口收敛到同一 Template Service 与 Instance Snapshot。
- Safety：migration `0014_role_binding_cards` 分离卡片发送、回调验证、领域处理和回复投影四类耐久状态。成功后原卡片更新为绿色已确认状态，选择器和按钮禁用，重复回调只回读首次结果。飞书回调时间接受秒、毫秒和微秒精度；已禁用选择器不再声明 `required=true`；卡片更新失败单独计数并记录，不回滚已提交草稿。
- Alternatives(否决)：强制用户拉群；按显示名称搜索；把完整通讯录或 open_id 放进不可信文本；让卡片直接创建实例；把候选列表只保存在进程内；卡片更新失败时回滚领域事务；重复点击创建多个草稿。
- Tradeoff：候选列表需要凭据侧目录权限且当前采用有界快照，大组织仍需搜索或分页体验。Runtime 与 Projection 的开发空闲退避上限已从 5 秒收紧到 1 秒，把真实回调服务端总耗时从 8.881 秒降到 3.272 秒，但增加空闲数据库轮询频率；长期应评估 PostgreSQL `LISTEN/NOTIFY` 或等价唤醒机制。
- Evidence：完整离线套件 `758 passed, 13 skipped`。开发服务器已应用十四份 migration，并安装内容提交 `19ea7be` 的 wheel；群聊 mention 与单聊 Card 2.0 均创建跨人员冻结草稿。单聊实例 `im_7575ba0f48ef145a782a20c3` 只创建一次，回复为 sent，原卡片成功冻结；Runtime、Projection 和其余四个 Python 服务均为 active、`NRestarts=0`。性能配置内容提交为 `409167d`。

## ADR-075 · 2026-08-04 · 自动节点失败通过 Owner 恢复卡进入重试或人工接管

- **Status：Accepted · Offline implementation。**
- Problem：Agent 或 Tool 执行失败后，领域内核会保留失败 Attempt，但责任人只能依赖通用重启命令和文本消息处理。这既不是就地的责任入口，也没有将“再跑一次”与“由人完成本节点”分成两条可审计路径。
- Constraint：原失败 Attempt、结果和错误不得覆盖；只有失败节点的当前唯一 Owner 可以恢复；卡片 payload 和客户端身份字段不是授权事实；回调必须耐久、可重放和幂等；PostgreSQL 聚合仍是唯一业务真相，卡片不得直接修改状态。
- Decision：Projection 在自动节点失败后向节点 Owner 发送 Card 2.0，显示稳定 `error_code` 并提供 `retry / human_takeover` 两个显式操作。`RecoveryActionInboxBridge` 把回调转为耐久 IM 命令，操作人只从飞书顶层认证字段取值。凭据侧重新校验企业成员，领域侧重新校验 Owner、Instance version、Node version 和 Attempt 编号。重试按受控节点重启语义创建新自动 Attempt；人工接管创建新 `waiting_human` Attempt，并复用现有 Task 投影和完成入站链路。
- Safety：非 Owner、非当前 Attempt、版本漂移、Human 节点和已由人提交的 Attempt 全部 fail closed。卡片不展示原始 `error_message`，避免把上游响应或凭据泄露给飞书。操作成功后使用卡片更新 token 收口原卡片，重复回调只回读首次结果。节点重启会关闭旧的人工接管 Task，避免陈旧待办继续推进领域状态。
- Alternatives(否决)：覆盖失败 Attempt；信任卡片携带的操作人；在回调进程中直接修改聚合；只提供 Instance Owner 的通用重启；人工接管时改写原 Agent / Tool executor 定义；把原始异常文本完整投影到飞书。
- Tradeoff：首版是显式人工选择，没有按错误类型的自动重试预算、暂停队列或运营告警视图。飞书长连接离线时点击不会自动进入中央 Inbox，因此回调服务的在线性仍需运维保证。
- Evidence：内容提交 `fc48b4f8a295c19ba02f08e5b87e006988eccf44`；完整离线套件 `769 passed, 13 skipped`；删除 Owner 校验的定向变异被回归测试捕获；wheel 回读包含 `recovery.py` 和 migration `0015_recovery_cards`。本 ADR 提交时长期开发库仍为十四份 migration，真实飞书恢复卡验收尚未开始。

## ADR-076 · 2026-08-04 · 可操作卡片先耐久接收再即时显示处理中

- **Status：Accepted · Development validation。**
- Problem：卡片动作虽然能沿耐久 Worker 链路正确完成，但点击后原卡片在数秒内保持不变，用户无法判断是否点中，容易重复点击。若后台最终状态先写入，而较慢的“处理中”随后回写，又会把成功或拒绝状态倒退成处理中。
- Constraint：视觉反馈不能先于耐久落库；处理中不是授权结论；飞书回调更新 token 的使用次数有限；卡片更新失败不能丢失动作；桥接进程崩溃后动作仍需恢复；历史重复回调不能为建立新唯一索引而删除。
- Decision：legacy 卡片保留既有同步接收与最终收口。Target 人员选择卡和失败恢复卡在动作严格解析并耐久插入后，先将可认领时间延后 10 秒，再用最长 3 秒的直接 lark-cli 调用把原卡片替换为蓝色无按钮“处理中”。调用结束后立即释放动作，凭据、领域和回复 Worker继续处理，最终把同一卡片替换为无按钮的成功或拒绝。10 秒延后只作为桥接进程在释放前崩溃的兜底，不是正常等待时间。
- Idempotency：人员选择卡按 tenant 与 message 只允许一个 canonical 动作。migration `0016_role_card_single_action` 先用接收时间和事件 ID 选定最早 canonical 行，把同卡其余历史行标为非 canonical，再建立部分唯一索引。非 canonical 行保留供审计，所有 claim 查询只认 canonical 动作。恢复卡继续复用 IM 命令既有 message 唯一约束。
- Alternatives(否决)：先更新卡片再落库；等待完整领域处理后才更新；在后台最终 Worker 之后补写处理中；直接删除历史重复行；把处理中视为授权成功；无限缩短所有 Worker 轮询间隔。
- Tradeoff：直接更新仍受网络和 lark-cli 子进程延迟影响，最长会占用事件线程 3 秒；超时可能发生“飞书已更新但本地未收到成功响应”，此时动作仍会由 10 秒兜底继续处理。瞬态蓝色状态可能很短，最终状态的服务端读回不能证明用户实际看到了多久，视觉性能仍需带计时的真实客户端验收。
- Evidence：实现提交 `7aa0dfa5ea413ee506f416ee7d499008db1edc93`，无损迁移修正提交 `dc77faad92e5d45f0271e45747bcbede3dd2ac02`；完整离线套件 `779 passed, 14 skipped`。长期库迁移前有一组五条历史同卡动作，迁移后 1 条 canonical、4 条非 canonical、canonical 重复组为 0。真实点击只接受一个动作并创建一个草稿，领域处理耗时 3.393 秒，最终回复耗时 5.793 秒；飞书服务端读回原卡片为绿色已确认且没有提交按钮。用户未记录瞬态视觉耗时，因此不声称已测得即时延迟。

## ADR-077 · 2026-08-04 · 卡片首个服务端反馈延迟进入耐久观测

- **Status：Accepted · Development validation。**
- Problem：人工秒表容易遗漏，最终卡片读回只能证明终态收口，既不能隔离直接“处理中”更新的耗时，也不能证明客户端何时完成渲染。没有独立指标时，用户感知、领域处理和最终回复会被混成一个总耗时。
- Constraint：服务端只能可靠测量有效回调被接受到飞书更新调用返回的区间，不能伪装成用户物理点击到客户端渲染的端到端时间；成功与失败都要可观测；指标与日志不得暴露人员、消息、卡片、Task 或凭据；观测写入与日志报告失败不能破坏已耐久接收的动作。
- Decision：migration `0017_card_feedback_metrics` 在 `workflow_im_commands` 与 `workflow_role_binding_actions` 增加 `feedback_status`、`feedback_elapsed_ms` 和 `feedback_completed_at`，以约束保证三者同时为空或同时完整。两类回调桥接器在接受有效回调时启动单调时钟，覆盖动作插入和直接卡片更新，在释放动作时原子保存 `updated / failed`、非负毫秒数与完成时间。结构化日志只记录动作类型、结果和耗时，日志报告异常被隔离。
- Alternatives(否决)：继续依赖人工计时；只记录最终领域与回复时间戳；只写日志不落库；把最终卡片读回当成延迟证据；信任客户端 payload 自报时间；为观测失败回滚动作。
- Tradeoff：两张耐久表各增加三个字段与完整性约束，桥接器多一次原子更新。该指标能稳定比较服务端反馈，却不能证明用户屏幕何时变化；客户端感知仍需单独的受控端到端测量。
- Evidence：内容提交 `c1d8fe510805cbe209a6275c4e4b3d8311b6692c`；完整离线套件 `780 passed, 14 skipped`；wheel SHA-256 `779990ca33771e0eb2ece2fa30bc8c1d4d2062625e4ded0f08e90d951d403204`。长期开发库已应用十七份 migration，六服务为 `active / running / NRestarts=0`。真实人员选择卡首个服务端反馈为 1.264 秒，真实失败恢复卡为 0.990 秒；两张卡片均从飞书服务端读回终态且不含操作控件。
