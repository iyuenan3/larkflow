# CHANGELOG · larkflow

## v0.4.0 · 2026-07-25 · 从「合同流引擎」改回通用引擎（对抗 review 收口）
- Reviewed: 6 维度 62 agent 对抗 review（通用性 / 引擎不变式 / 真实栈 / 文档符合度 / 测试有效性 / 产品泛化），逐条自行复现。**根因不是代码质量，是上一轮围绕合同图做 TDD，把这一个用户故事的假设焊进了准入层与绑定层。**
- Added: **tool 数据化能力库**（ADR-026，`tool: {kind, args}` + 与模板无关的全局注册表）；`lint_template`（ADR-027）；打回意见回流（进 llm prompt + 回喂上一稿 + 进人工卡片）；打回预算与 `blocked` 终态（ADR-029）；`reconcile()`；`blocked()`；`templates/hiring.yaml`（招聘接力，**零 Python** 的第二个业务场景）。
- Changed: 护栏①「三型齐全」降级为 lint（ADR-027）；`produce` 的 `deliverable` 改为可省（纯动作节点）；human gate 禁用 `task_complete`（否则审批门是橡皮图章）；驱动层绕开 super-step 屏障（ADR-028，保值写回 + 借位重排）；`pending()` 过滤已答复者；`recursion_limit` 按运行时 dag 现算；派单 per-interrupt 隔离；EventPump 异常隔离 / stderr drain / 退避重启；真栈角色严格解析 + `LARKFLOW_ROLES` JSON。
- Removed: `templates/contract_handlers.py`、`templates/defect_handlers.py`（模板目录只剩 yaml）；`app.HANDLERS` 按模板名的注册表。
- Fixed（都实测复现过）：并行门先打回时打回不落地、改图吞掉刚做出的裁决、不相干并行分支被人工节点卡死、打回后 AI 用逐字节相同的 prompt 重跑（等于空转）、跨模板 node id 撞名静默跑错业务、auto 门无限重算撞 recursion limit。
- 127 tests pass（新增 25，其中 `tests/test_generality.py` 是「通用产品」的可执行断言）。

## v0.3.0 · 2026-07-24 · 引擎 v1.0 核心 headless 跑通（代码追上第二 / 三轮设计）
- Added: v1 节点契约落码（`executor × role + 配置`，护栏①..⑤ + 字段级）；交付物层（`Deliverable{type,token,url,region}` + `DeliverableIO` create/overwrite/fetch + handle 权威登记 `outputs[node_id]`）；通用 produce/gate 执行体（per-role 取代 per-node-id）；选择性重算 v1（运行时手选 reopen 组 + 合法域校验 + 结构性终止）；auto 门短路；merge 扇入（引擎零改）；受控活图 `edit_graph`；首张策展合同图 `templates/contract.yaml` + 机检 / 收口 handler；驱动泛化 `start(template, inputs)` + `build_service`；真实栈（`CliDeliverableIO` 走 lark-cli markdown、多角色 LLM env 装配、`build_real_service`）。
- Changed: `type→executor` / 旧 `role→assignee_role` / 去 `on_fail`；`defect.yaml` 迁 v1 作回归载体；`LLMClient` 主接口改 `complete(prompt, model_role)`；`service` 删掉最后一处模板硬编码（动态指派留 v1.1）；`read_upstream` 透过不产交付物的节点看上游。
- Verified: v1.0 win 的 headless 判定版一次跑通（交付物真流转 + 打回**可感知省算**：旁支 AI 长文不重跑、旧 handle 复用、全程不新建文档 + auto 门自动放行 / 打回 + 运行中改图）。102 测绿，全程 Mock/Stub/`:memory:`。
- Learned: 挂起时 `update_state` **必让中断换 id**（实测四种情形一律换）→ 加 `interrupt_remap` 重绑，否则改一次图就废掉在等的人手里的卡（见 MEMORY）。
- 未做（明确留下）：真飞书 / 真 LLM e2e（需 dev app + 事件回调）；ADR-023 权限层 `allowed_reopen`（v1.0 只做机制层）；崩溃对账 `reconcile`（MEMORY finding D）；reopen 预算（finding C）。

## v0.2.0 · 2026-07-24 · 产品最终形态定型（入口 / 生成 / 打回权限 / 投票分支 / 子项目 / 实现分层）
- Added: ADR-021 入口与意图路由（结构化 + @bot NL 双入口，确认步）；ADR-022 模板生成升为主路径（受控活图 + 确认降低 ADR-003 生成风险）；ADR-023 打回权限模型（机制 × 权限两层，防踢皮球精确判据，节点负责人 / 主负责人，escalation）；ADR-024 子项目 spawn（交付物流转递归 + 回填 + 边界隔离）；ADR-025 多人节点投票门(A) / 决策表决(B) + 条件分支（when 守卫 / skipped）。
- Changed: 前端呈现 → 两视角两表面（参与人 chat-first 可只读看全貌 / 发起人 app 驾驶舱可编辑，可见 ≠ 可操作）+ 页面 P1-P4；节点契约补 assignee_role / vote / when，状态机加 skipped；生成从 ADR-003 路线 1 优先升为主路径。理由见 DECISIONS ADR-021..025。
- Changed: ROADMAP 从「Now v1」细化为**实现分层** v1.0(第一个 win) → v1.1 生成 → v1.2 子项目 → v1.3 投票分支，v2 共享协同 + 前端可编辑；子项目 / 会签从 v2 提前到 v1.2 / v1.3。
- Reviewed: 两轮对抗性 workflow review —— 内部一致性（修 12 项）+ PM 产品视角（6 把 pm-skill 尺子）。据 PM review 补：v1.0/v1.1 间加**采用 gate**、win 判据改「可感知省算」、修 win↔画布（v1.0 改图走命令 / 卡片）矛盾、PRD 补频次假设 + vs 飞书原生一节、ADR-024/025 加暂定头。
- 注：本版为纯设计 / 文档定稿（未动代码），代码仍 seg-1 契约、待 v1.0 step 1 迁移。

## v0.1.2 · 2026-07-24 · 开写就绪度复盘 + 交付物 handle 权威定家
- Decided: 交付物 handle 权威登记表 = `state.outputs[node_id]`（`deliverable.container` 降为活图声明位 / 回填指针）；固化 `on_fail`（静态单目标）→ `reopen`（运行时多选 + 运行时祖先校验）代码契约；澄清 v1 `role`（produce|gate）与 as-built `role`（业务指派串 = `assignee_role`）撞名。理由见 DECISIONS ADR-020。
- Reviewed: v1 开写就绪度审查（6 维找缺口 + 对抗验证 + 合成）无硬阻塞：seg-1 引擎原语全复用，剩下是照 SPEC 落码；net-new 集中在交付物 IO 层 + 执行体泛化 + merge + 活图 edit_graph；前端 spike 不在关键路上。

## v0.1.1 · 2026-07-24 · 前端形态定：真前端（妙搭为主）
- Changed: 修订 cards-only（ADR-011）为真前端；妙搭（Miaoda，本地开发）为主、开放平台自建 H5 备选；前端 = 引擎投影 + 客户端；松动 ADR-007（引擎将暴露读 / 命令 API）；README / About 已写。理由见 DECISIONS ADR-019。

## v0.1.0 · 2026-07-24 · 第一段引擎跑通 + 第二轮设计（交付物流转）
- Added: seg-1 本地引擎跑通（8 节点缺陷流，固定编排器解释数据 DAG + SQLite checkpointer + 驱动层 LarkFlowService，15 测试绿，`larkflow/`）；对抗性审查 9 项（见 MEMORY）；交付物产出协议在测试组织实测（markdown create/fetch/overwrite/版本）；公开仓库 github.com/iyuenan3/larkflow。
- Changed: 定位升格为交付物流转引擎（缺陷流降退化特例）；节点模型 → executor×role + approval_policy；门禁 win 核心从五维评分修正为「可换执行体 + auto/会签 + 打回流转」；引入受控活图 + 选择性重算打回；交付物 → (容器,region) 统一飞书文档 + 飞书原生版本；LLM 从 newapi 改为通用多角色 OpenAI 兼容路由。理由见 DECISIONS ADR-012..018。
- Removed: newapi-proxy 依赖（LLM 改多角色路由）。

## v0.0.0 · 2026-07-23 · 立项
- Added: 项目立项（larkflow / 飞流）；git init（main）；AIREADME 骨架（INDEX / CORE / RELATIONS / ARCHITECTURE / PRD / DECISIONS / CONVENTIONS / ROADMAP 实填，SPEC / DEPLOYMENT / MEMORY 语义占位）；CLAUDE.md router。
- 定架构：两层（领域 DAG 数据 + LangGraph 有环引擎）+ 单一事实源（checkpointer）+ 飞书投影；路线 1 策展模板起步；飞书原语复用、MVP 零自建前端。理由见 DECISIONS ADR-001..006。
- 设计定稿（同日）：入口 lark-cli event consume（NDJSON，不接 SDK）/ 宿主 alicloud-sh + SQLite / dev 独立飞书租户 / 第一张模板 = 缺陷生命周期（分两段建）/ 模板生成走 few-shot（种子库 + 3 护栏）/ 工作台 cards-only；win = 证采用 + 门禁。理由见 DECISIONS ADR-005（结论）+ ADR-007..011。
