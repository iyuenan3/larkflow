# ARCHITECTURE · larkflow

## 核心结构：两层（不变）
- **领域图（数据模型）**：一张节点依赖图 = 工作流「是什么」。节点 = 步骤，边 = 依赖（deps 解锁下游），gate 节点把关（放行 / 打回）。持久在 checkpointer + 投影到飞书。**图会变**：项目进行中可编辑未来节点（受控活图，见下）。
- **LangGraph 引擎（运行时）**：有环 Pregel 运行时 = 工作流「怎么推进」。一张**固定**的编排器图**解释** state 里的领域图数据，派发就绪节点、跑 tool / llm、human 节点 interrupt 持久挂起、打回环回，直到跑完。

> LangGraph 读领域图 → 推进 → 写回。图是数据，引擎是解释器。「有环」正是打回 / 选择性重算 / 重启所需。

## 受控活图（图运行中可编辑，ADR-013）
- **冻结线 = 执行前沿**：已完成 / 在跑（done/running）的节点冻结，只有未来（pending）节点可增删改。前沿是一条线不是一个点（并行时冻结线 = 所有 done/running 的节点）。
- **运行时改图几乎免费**：编排器图固定、被解释的 `dag` 在 state 里、每个 super-step 重读，故改 state 里的 dag = 改图，无需重编译（复用 seg-1 选型）。合法变更只在 pending 子图内，保证仍是 DAG、不删在跑节点。
- **只改未来、不改历史**：已完成节点的产出冻在 checkpointer 里就是权威。

## 打回 = 选择性重算（ADR-014）
- 人在 gate 节点当场手选一组节点 `S` 打回（运行时多选，非模板预声明单目标）。
- 重算集 = `S ∪ 传递下游(S)`（`stale_downstream`）；不在集内的旁支**复用旧产出**。打回目标本身也解冻。
- 引擎实现：把被打回集 + 其传递下游重置 `pending`，固定编排器环下一步自然重派（`larkflow/engine/gates.py`）。
- **权限层（谁能打回，防踢皮球，ADR-023）**：机制之上再过一层权限，三类主体 —— 项目 owner 全域（任一祖先节点）；参与人（人工节点负责人）限自己「责任段」；跨界打回走 escalation。**精确判据（责任段 = 打回后重算集不牵连别的人工节点）与 escalation 协议见 DECISIONS ADR-023。**
- 打回权威两源：**个人主体**（owner / 主负责人 / 责任段参与人）+ **集体投票**（A 类投票门阈值自动，见节点模型）。权限层 = 纯图函数 `allowed_reopen(...)`；某人可见的打回候选 = 机制合法 ∩ 权限允许（审核卡 / 画布据此过滤，字段与函数契约见 SPEC）。

## 节点模型（2 role × 3 executor，ADR-015）
- **executor**：`tool`（确定性程序）/ `llm`（AI）/ `human`（人）。
- **role**：`produce`（往交付物上写）/ `gate`（把关，放行或打回一组节点）。
- 「AI 收集」「AI 起草」「AI 整合（fan-in）」都是 `(llm, produce)` + 不同配置，引擎不为业务新增节点类型。
- **tool 的行为同样是配置**：`tool: {kind, args}` 从与模板无关的内置能力库选取（ADR-026）。这条是「节点契约恒为数据」的兑现方式：新增业务场景 = 新增一个 yaml，零 Python，否则 ADR-022 的生成主路径不成立。
- `produce` 的 `deliverable` 可省 = **纯动作节点**（发通知 / 调外部系统 / 确认线下动作）；强制每个 produce 都产一份飞书文档会把纯审批 / 纯通知 / 纯决策类流程整类挡在门外。
- gate 有放行策略配置（含 bypass 自动放行）+ 运行时多选打回；意见可落成飞书文档评论。字段枚举与产出 schema 见 SPEC。
- **assignee_role**（派给谁，业务指派角色）与 **role**(produce|gate)（干啥）正交；human 节点须 ≥1 负责人（护栏）。
- **多人节点**（投票 / 会签，ADR-025）= `human × role + vote 配置`（voters / 阈值 / 主负责人）：**A 类审批投票门**（role:gate）票到阈值 → 引擎自动 pass / reopen（集体投票）；**B 类决策表决**（role:produce）产出决策值到 outputs、不自动打回、只主负责人手动。
- **条件分支**（ADR-025）：决策节点只产决策值；下游节点带 `when: {决策节点: 值}` 守卫，未匹配者标 `skipped`（置灰）。分支从 deps + 守卫**涌现**，引擎不识「分支」概念（skip 传播 + ready 规则见 SPEC）。

## 交付物：(容器, region) 统一飞书文档 handle（ADR-016）
- 交付物 = 带 type 的飞书 handle（doc token / 云盘 file token）；内容在飞书（投影），引擎只存指针 + 元数据。**对人**是文档链接（可看 / 协同 / 评论审），**对下游 llm** 是可 fetch 的正文。
- 模型 = `(容器 handle, region)`：独立 doc 与共享 doc 一段是同一抽象的两个粒度，选择性重算复用粒度随之从「整个 doc」推广到「doc 内其他段」。
- **版本靠飞书原生**（稳定 handle + overwrite + 飞书 history），引擎不自建版本。
- **handle 权威登记 = `state.outputs[node_id]`**（在 checkpointer 内 → 仍 checkpointer 权威、飞书仍投影）；节点 `deliverable.container` 是活图声明位 / produce create 后回填指针，非第二份权威（ADR-020）。
- 具体 region 枚举、produce/consume 命令协议见 SPEC；理由见 DECISIONS ADR-016。

## 子项目：交付物流转递归自身（ADR-024）
- 一个 produce 节点的交付物，可由「人写 / AI 写 / 一整个子项目产出」。子项目 = 独立 larkflow 项目（自己的 thread / owner / 参与人），其最终交付物 handle **回填**父节点 `outputs[node]`。
- 父节点挂起等子实例完成信号，**复用 interrupt / 挂起 + 关联表 + 幂等**（与 human 节点等人点卡同机制）；关联表扩父子映射。
- 边界隔离：父 owner 可打回父节点（= 整个子项目重开），**够不到子项目内部**；子 owner 全权管子内部，权限 / 打回规则递归。防下钻失控（深度上限）。落 v1.2。

## 运行时约束：super-step 屏障（ADR-028）
- LangGraph 的 super-step 是屏障：只要有人工节点挂在 `interrupt` 上，`dispatch` 就不再执行。后果是 ① 打回判了却落不了地 ② 不相干的并行分支一起停。**多方并行接力是本产品的定义形态**，故驱动层必须主动推进：保值写回（带上在飞的 status/outputs，否则新 checkpoint 会丢掉刚点的裁决）+ 借位重排（`as_node=<worker>` 让引擎自己的 dispatch 真跑一次），一拍一拍推到没活可干。
- 打回不是无限的：每道门有打回预算，超了标 `blocked` 终态并通知发起人（ADR-029）。

## 组件
- **引擎服务**：Python + LangGraph + SQLite checkpointer（宿主 alicloud-sh，见 DEPLOYMENT / DECISIONS ADR-007）。含固定编排器图 + 模板注册表 + 节点执行器（tool/llm/human）+ 驱动层 `LarkFlowService`。
- **飞书事件入口**：引擎 spawn `lark-cli event consume <EventKey>` 子进程，读 stdout 的 NDJSON 事件（@bot / 点卡片 / 任务完成 / 定稿消息）→ 起实例 / 唤醒挂起的图。不接飞书 SDK（ADR-005）。
- **飞书动作出口 + 交付物读写**：建任务 / 发卡 / 建改文档 / 传云盘 / 读正文 / 版本，走 lark-cli（`markdown` / `docs` / `drive` / `task` / `im`）。
- **LLM**：OpenAI 兼容接口，按任务角色路由到不同模型 / 供应商（火山方舟 / 中转站 / 直连，各角色独立 key，ADR-017）。
- **意图路由层**：pre-graph 引擎外独立一层（headless 不依赖），把 @bot NL / 结构化表单收成 `start(template, inputs)`（意图分类 + 模板匹配 / 生成 + 确认；v1.1，ADR-021 / ADR-022，详见〈数据流〉触发）。
- **前端**：妙搭应用（本地开发），详见〈前端呈现〉。

## 前端呈现（妙搭为主，ADR-019；修订 ADR-011 cards-only）
- 前端 = **妙搭应用**（Miaoda，本地开发，飞书托管 `aiforce.cloud`，挂工作台）；开放平台自建 H5 为备选。
- 定位 = 引擎的**投影 + 客户端**：读节点状态 / 交付物、发审核裁决 / 改图命令回引擎；不持有真相源（checkpointer 仍权威）。
- 核心视图 = 项目详情的**活图画布**（DAG 按状态上色、点节点、画布上加删改未来节点 = 受控活图可视化）。
- 卡片 / 任务 / 文档不废：仍是引擎的「手」（produce 写文档、派任务、发通知），前端叠在其上（hybrid）。
- **两视角两表面**：**参与人**（法务 / 财务 / 各节点负责人）chat-first，在飞书收派单卡 / 审核卡、开文档，基本不进 app；**可进 app 看只读全貌画布**（了解项目全局），但不可编辑。**发起人 / 管理者** = app 驾驶舱：编辑画布（受控活图改图）+ 全域打回。**可见 ≠ 可操作**（参与人见全貌、只能在责任段内操作，ADR-023）。
- **页面**：P1 项目列表（进度 / 卡在谁 / 交付物链接）｜P2 新建项目（选模板 + 填要素；@bot 与结构化两入口，ADR-021）｜P3 项目详情 = 活图画布（参与人只读 / owner 可编辑）｜P4 审核 · 待办聚合。
- **引擎新增读 / 命令 API**：前端要够到引擎，引擎需暴露读状态 + 收命令的接口，**松动 ADR-007「无入站端口」**（具体形态待妙搭原型后定）。
- **开放问题（待原型验证 + 后续拍，勿当已定）**：
  - 传输可达性：妙搭云托管（`aiforce.cloud`）能否够到自托管引擎（alicloud-sh 无公网入站，ADR-007）？不能则退「命令走飞书原生轨、引擎只出站消费」保 ADR-007（详见 DEPLOYMENT 命门）。**原型第一必验项。**
  - 画布数据来源：拉引擎整图读 API / 推 / 读多维表格投影（行式，可能载不动完整拓扑 + 边）；含实时刷新模型（详见 SPEC 待填）。
  - 改图命令回写：合法性校验**必须在引擎权威侧**（复用 ADR-013，不信前端）+ 乐观并发（命令带读取时 checkpoint 版本）+ 鉴权（否则破单一事实源）+ cards/app 双输入面统一幂等（详见 SPEC 待填）。

## 数据流（一次实例）
1. 触发 → 起实例：@bot NL 走**意图路由层**（分类 + 模板匹配 / 生成 + 抽要素 + **确认步**）或结构化「新建项目」表单，都产出 `start(template, inputs)` → 起 LangGraph 实例（thread_id = 实例 id，选中 / 生成的模板 = 初始图）。意图路由层在引擎外，headless 不依赖（ADR-021 / ADR-022）。
2. 派发：编排器并行 super-step，produce 节点跑（tool/llm 自动物化交付物到飞书；human 节点 interrupt → 驱动层建飞书任务 / 文档 → 挂起）。
3. 完成信号：human 节点靠人主动发的信号（定稿消息 / 完成任务 / 点卡片）→ `Command(resume=)` 唤醒该分支。
4. gate + 打回：gate 节点放行解锁下游 / 打回一组 → 选择性重算（重置 pending，旁支复用旧产出）。
5. 运行中改图：发起人在未来（pending）区增删改节点，下一 dispatch 生效。
6. 投影：图状态变 → 飞书任务 / 卡 / 多维表格看板（前端取图状态的方式，拉 API / 推 / 读投影，待定，见〈前端呈现〉开放问题）。
7. 收口：全 done → 交付物定稿 + 汇总卡 + 通知发起人。

## 首个工作流与 seg-1
- **seg-1 已建**：8 节点缺陷流本地引擎（`larkflow/templates/defect.yaml`，LangGraph + SQLite，15 测试绿）= 交付物流转的**退化特例**（交付物 = 修复，图固定单链），验证了 interrupt/resume + 打回 + checkpointer。（8 节点 = as-built，含 reproduce 门禁；ADR-009 原规划完整缺陷流 11 节点、seg-2 回填 ci_test/code_review/release_note，as-built 未落这三节点。）
- **v1.0 目标（第一个 win）**：一个「各自产出再合并」形态的真实交付物流转项目（独立 doc 拓扑，合同类：商务 + 法律双起草 → 财务 / 法务分头 gate（single 复核）→ merge → 定稿 → auto 格式检查；ADR-018，实现分层见 ROADMAP）。
- **v1.0 引擎 as-built（2026-07-24，headless 已跑通）**：`templates/contract.yaml` 落地上述拓扑（分头挂门 = 省算的结构前提），引擎侧 v1 契约 / 交付物层 / 通用执行体 / 选择性重算 / auto 门 / merge 扇入 / 受控活图 `edit_graph` 全部落码，102 测绿（Mock/Stub/`:memory:`）。**剩下的是真栈**：dev 飞书应用 + 事件回调 + 真 LLM 角色 env（代码已写，见 `build_real_service`）。

## 关键选型（理由见 DECISIONS）
- 引擎 = LangGraph 有环（ADR-001）；两层 + 单一事实源（ADR-002）；固定编排器解释数据图（ADR-003，正好 enables 活图）。
- 定位 = 交付物流转引擎（ADR-012）；受控活图（ADR-013）；选择性重算（ADR-014）；节点 2×3 + approval_policy（ADR-015）；交付物 (容器,region)（ADR-016）；LLM 多角色路由（ADR-017）；实现分期（ADR-018）；前端妙搭（ADR-019）；交付物 handle 权威登记（ADR-020）；入口意图路由 + 确认（ADR-021）；生成升主（ADR-022）；打回权限模型（ADR-023）；子项目 spawn（ADR-024）；多人投票 + 条件分支（ADR-025）。
- 宿主 alicloud-sh + SQLite（ADR-007）；入口 lark-cli event consume（ADR-005）。

## 禁改项
- LangGraph state 只放执行游标 + in-flight scratch（`outputs[node_id]` 兼作交付物 handle 权威登记表，仍在 checkpointer 内、不破本条，ADR-020）；业务真相源 = checkpointer，飞书是投影，不反向写真相。
- 一张**固定**编排器图解释变化的 `dag` 数据；**不 per-instance 现编译新 LangGraph 图**（这正是运行时改图的基础）。
- 受控活图：只改 pending 子图、不删在跑节点、不成环；**只改未来不改历史**。
- 完成靠显式信号，引擎不猜定稿。
- 前端（妙搭 / 备选 H5）只读投影 + 发命令，不持有真相源（ADR-019）。
- 飞书事件入口只用 lark-cli event consume（NDJSON 子进程），不引入飞书 SDK；前端的读 / 命令入站是引擎自有 API、另论（ADR-019，见〈前端呈现〉）。
- 节点契约恒为数据（executor × role + 配置，见 SPEC），使生成（ADR-010 / ADR-022 升主）= 加 AI 作者 + 人审门、执行器不改。
- 条件分支从 deps + `when` 守卫涌现，引擎不维护「分支」实体；`skipped` 是引擎按决策未跑（可复活），区别于活图删除（ADR-025）。
- 子项目边界：父只能操作「子项目所在的那个节点」、够不到子内部；打回权限层在引擎权威侧算、不信前端（ADR-023 / ADR-024）。
