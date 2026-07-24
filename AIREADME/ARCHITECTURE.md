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

## 节点模型（2 role × 3 executor，ADR-015）
- **executor**：`tool`（确定性程序）/ `llm`（AI）/ `human`（人）。
- **role**：`produce`（往交付物上写）/ `gate`（把关，放行或打回一组节点）。
- 「AI 收集」「AI 起草」「AI 整合（fan-in）」都是 `(llm, produce)` + 不同配置，引擎不为业务新增节点类型。
- gate 有放行策略配置（含 bypass 自动放行）+ 运行时多选打回；意见可落成飞书文档评论。字段枚举与产出 schema 见 SPEC。

## 交付物：(容器, region) 统一飞书文档 handle（ADR-016）
- 交付物 = 带 type 的飞书 handle（doc token / 云盘 file token）；内容在飞书（投影），引擎只存指针 + 元数据。**对人**是文档链接（可看 / 协同 / 评论审），**对下游 llm** 是可 fetch 的正文。
- 模型 = `(容器 handle, region)`：独立 doc 与共享 doc 一段是同一抽象的两个粒度，选择性重算复用粒度随之从「整个 doc」推广到「doc 内其他段」。
- **版本靠飞书原生**（稳定 handle + overwrite + 飞书 history），引擎不自建版本。
- 具体 region 枚举、produce/consume 命令协议见 SPEC；理由见 DECISIONS ADR-016。

## 组件
- **引擎服务**：Python + LangGraph + SQLite checkpointer（宿主 alicloud-sh，见 DEPLOYMENT / DECISIONS ADR-007）。含固定编排器图 + 模板注册表 + 节点执行器（tool/llm/human）+ 驱动层 `LarkFlowService`。
- **飞书事件入口**：引擎 spawn `lark-cli event consume <EventKey>` 子进程，读 stdout 的 NDJSON 事件（@bot / 点卡片 / 任务完成 / 定稿消息）→ 起实例 / 唤醒挂起的图。不接飞书 SDK（ADR-005）。
- **飞书动作出口 + 交付物读写**：建任务 / 发卡 / 建改文档 / 传云盘 / 读正文 / 版本，走 lark-cli（`markdown` / `docs` / `drive` / `task` / `im`）。
- **LLM**：OpenAI 兼容接口，按任务角色路由到不同模型 / 供应商（火山方舟 / 中转站 / 直连，各角色独立 key，ADR-017）。
- **前端**：妙搭应用（本地开发），详见〈前端呈现〉。

## 前端呈现（妙搭为主，ADR-019；修订 ADR-011 cards-only）
- 前端 = **妙搭应用**（Miaoda，本地开发，飞书托管 `aiforce.cloud`，挂工作台）；开放平台自建 H5 为备选。
- 定位 = 引擎的**投影 + 客户端**：读节点状态 / 交付物、发审核裁决 / 改图命令回引擎；不持有真相源（checkpointer 仍权威）。
- 核心视图 = 项目详情的**活图画布**（DAG 按状态上色、点节点、画布上加删改未来节点 = 受控活图可视化），加项目列表 / 审核 / 待办。
- 卡片 / 任务 / 文档不废：仍是引擎的「手」（produce 写文档、派任务、发通知），前端叠在其上（hybrid）。
- **引擎新增读 / 命令 API**：前端要够到引擎，引擎需暴露读状态 + 收命令的接口，**松动 ADR-007「无入站端口」**（具体形态待妙搭原型后定）。
- **开放问题（待原型验证 + 后续拍，勿当已定）**：
  - 传输可达性：妙搭云托管（`aiforce.cloud`）能否够到自托管引擎（alicloud-sh 无公网入站，ADR-007）？不能则退「命令走飞书原生轨、引擎只出站消费」保 ADR-007（详见 DEPLOYMENT 命门）。**原型第一必验项。**
  - 画布数据来源：拉引擎整图读 API / 推 / 读多维表格投影（行式，可能载不动完整拓扑 + 边）；含实时刷新模型（详见 SPEC 待填）。
  - 改图命令回写：合法性校验**必须在引擎权威侧**（复用 ADR-013，不信前端）+ 乐观并发（命令带读取时 checkpoint 版本）+ 鉴权（否则破单一事实源）+ cards/app 双输入面统一幂等（详见 SPEC 待填）。

## 数据流（一次实例）
1. 触发：飞书事件 → 起 LangGraph 实例（thread_id = 实例 id，选中模板 = 初始图）。
2. 派发：编排器并行 super-step，produce 节点跑（tool/llm 自动物化交付物到飞书；human 节点 interrupt → 驱动层建飞书任务 / 文档 → 挂起）。
3. 完成信号：human 节点靠人主动发的信号（定稿消息 / 完成任务 / 点卡片）→ `Command(resume=)` 唤醒该分支。
4. gate + 打回：gate 节点放行解锁下游 / 打回一组 → 选择性重算（重置 pending，旁支复用旧产出）。
5. 运行中改图：发起人在未来（pending）区增删改节点，下一 dispatch 生效。
6. 投影：图状态变 → 飞书任务 / 卡 / 多维表格看板（前端取图状态的方式，拉 API / 推 / 读投影，待定，见〈前端呈现〉开放问题）。
7. 收口：全 done → 交付物定稿 + 汇总卡 + 通知发起人。

## 首个工作流与 seg-1
- **seg-1 已建**：8 节点缺陷流本地引擎（`larkflow/templates/defect.yaml`，LangGraph + SQLite，15 测试绿）= 交付物流转的**退化特例**（交付物 = 修复，图固定单链），验证了 interrupt/resume + 打回 + checkpointer。（8 节点 = as-built，含 reproduce 门禁；ADR-009 原规划完整缺陷流 11 节点、seg-2 回填 ci_test/code_review/release_note，as-built 未落这三节点。）
- **v1 目标**：一个「各自产出再合并」形态的真实交付物流转项目（独立 doc 拓扑，合同类，ADR-018）。

## 关键选型（理由见 DECISIONS）
- 引擎 = LangGraph 有环（ADR-001）；两层 + 单一事实源（ADR-002）；固定编排器解释数据图（ADR-003，正好 enables 活图）。
- 定位 = 交付物流转引擎（ADR-012）；受控活图（ADR-013）；选择性重算（ADR-014）；节点 2×3 + approval_policy（ADR-015）；交付物 (容器,region)（ADR-016）；LLM 多角色路由（ADR-017）；实现分期（ADR-018）；前端妙搭（ADR-019）。
- 宿主 alicloud-sh + SQLite（ADR-007）；入口 lark-cli event consume（ADR-005）。

## 禁改项
- LangGraph state 只放执行游标 + in-flight scratch；业务真相源 = checkpointer，飞书是投影，不反向写真相。
- 一张**固定**编排器图解释变化的 `dag` 数据；**不 per-instance 现编译新 LangGraph 图**（这正是运行时改图的基础）。
- 受控活图：只改 pending 子图、不删在跑节点、不成环；**只改未来不改历史**。
- 完成靠显式信号，引擎不猜定稿。
- 前端（妙搭 / 备选 H5）只读投影 + 发命令，不持有真相源（ADR-019）。
- 飞书事件入口只用 lark-cli event consume（NDJSON 子进程），不引入飞书 SDK；前端的读 / 命令入站是引擎自有 API、另论（ADR-019，见〈前端呈现〉）。
- 节点契约恒为数据（executor × role + 配置，见 SPEC），使生成（ADR-010）= 加 AI 作者 + 人审门、执行器不改。
