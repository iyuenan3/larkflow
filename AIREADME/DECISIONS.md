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
