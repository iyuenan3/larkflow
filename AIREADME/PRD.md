# PRD · larkflow（飞流）

> 状态：Target Draft · MVP 产品契约 · 2026-07-30

## 1. Summary

larkflow 是基于飞书的企业协作 DAG 产品。发起人从企业模板启动一个目标，系统把顶层工作包分配给真实人员；责任人可以亲自完成、调用本地个人 Agent，或在授权范围内展开下一层 DAG。中央控制面保存模板、实例、权限、知识和能力治理；飞书承载 IM、待办、文档、云盘和身份。

MVP 必须支持最多 3 层 DAG。合同起草仅作为解释案例，首个试点场景应依据真实频次和协调成本选择。

## 2. Contacts

| 角色 | 负责人 |
|---|---|
| Product owner | Maxwell |
| Engineering owner | TBD |
| Design / Feishu UX | TBD |
| Pilot enterprise process owner | 每个试点指定 1 人 |
| Security / tenant admin | 每个试点指定 1 人 |

产品范围变化由 Product owner 决策；模板发布和企业数据授权必须由对应租户的流程 Owner / 管理员批准。

## 3. Background

现有协作通常散落在群聊、个人待办、审批和文档中。发起人知道顶层目标，却需要人工找到各部门负责人、反复催办和拼接结果；部门负责人又会在本部门继续拆解。设备离线、人员改派、跨部门打回和责任争议会让纯 Agent 编排失效。

《CC 730 PRD v1.2 完整合集 v3》提供了 DAG 模板、项目协同和三级下钻等启发，但其目标是构建更完整的办公平台。larkflow 不复制该边界：我们复用飞书，不要求 Project 与 DAG 1:1，也不把待办直接分配给 Agent。

当前仓库是中心化 LangGraph + SQLite 原型，已验证部分飞书投影和流程恢复机制，但尚未实现多租户模板库、三级父子实例、本地 Agent 边缘运行时和中央能力治理。详见 [ARCHITECTURE.md](ARCHITECTURE.md) 的差距表。

## 4. Objective & Key Results

**目标：** 让一个飞书企业能用经治理的模板完成真实的跨责任边界工作，并在人或设备离线、返工和下钻时保持责任、状态与审计连续。

MVP 验收 KRs：

1. 一个试点企业发布并复用至少 3 个真实模板，其中至少 1 个跨部门。
2. 跑通至少 1 个完整 L1 → L2 → L3 实例，L3 无法继续下钻。
3. 100% 人工工作包分配到唯一真实人员；个人 Agent 离线时待办和工作流仍可查询、改派和催办。
4. 100% 状态变更记录 actor、时间、来源、前后状态和模板快照版本。
5. 至少一种本地 Agent 通过 lark-cli 完成“领取上下文 → 执行 → 提交结果”，最终提交由责任人确认。
6. 相比同流程的飞书原生基线，人工催办或状态同步时间下降至少 30%。

## 5. Market Segments

### Primary

已使用飞书、拥有重复跨团队流程、并正在引入 Claude/Codex 等个人 Agent 的 50–1000 人企业。优先寻找有明确流程 Owner、每月重复 ≥10 次、当前依赖群聊催办的场景。

### Personas and JTBD

- **发起人：** 从模板发起目标，只看必要的顶层进度、阻塞和最终验收。
- **工作包责任人：** 接收属于自己的待办，自主选择执行方式并对结果负责。
- **部门主管：** 把部门工作包继续拆解，不向父层暴露无权限的内部细节。
- **流程管理员：** 把历史实践变成模板，控制发布、版本、锁和适用范围。
- **企业 AI 管理员：** 管理知识、Skill、MCP 和本地 Agent 可获得的权限。

## 6. Value Propositions & Alternatives

| 用户 | larkflow 价值 | 当前替代方案 |
|---|---|---|
| 发起人 | 跨部门承诺、阻塞和验收一张图可追踪 | 群聊 + 人工催办 + 表格 |
| 责任人 | 人保留责任与选择权，Agent 可代执行 | 自己复制上下文给 Claude/Codex |
| 部门主管 | 用子 DAG 组织内部工作，只向父层交付契约 | 临时拉群、私下拆任务 |
| 企业 | 模板、能力、权限和审计统一治理 | 飞书审批/Base 自动化 + 各自 Agent |

若一个场景只需单人 Agent 建待办，或一个固定单层审批即可解决，larkflow 不应进入；直接使用个人 Agent 或飞书原生能力成本更低。

## 7. Solution

### 7.1 Enterprise onboarding

1. 管理员创建租户并授权所需飞书范围。
2. 同步组织与人员，定义企业角色和模板治理者。
3. 在明确授权下索引知识源，登记可用 Skill、MCP 和安全策略。
4. 通过访谈、历史材料和现有流程导入生成**候选** Enterprise Process Map。
5. 流程 Owner 校准候选 DAG，发布为平台、行业、企业或部门模板。

入驻是渐进过程。未授权内容不采集；未确认候选不进入生产模板库。

### 7.2 Template library

模板遵循 [DAG_TEMPLATE_SPEC.md](DAG_TEMPLATE_SPEC.md)：四级 scope，`draft → in_review → published → deprecated → archived`，发布版本不可变，实例保存完整快照。权限细分为查看、实例化、绑定、结构编辑、审核、发布、废弃和锁管理。

MVP 提供列表、详情、版本、发布审核和基于 YAML/表单的受控编辑；高级可视化编辑与自动 diff/merge 可后置。

### 7.3 Workflow instance and human accountability

- 实例化时 Role Slot 必须解析为真实 `open_id`，解析失败则阻止启动。
- 中央控制面创建工作包，并向责任人投影飞书待办、消息和材料链接。
- 责任人选择 `manual`、`personal_agent` 或允许时的 `child_dag`。
- Agent 只能代表责任人提交草稿或执行结果，不能成为责任人或人类 Gate 的审批 actor。
- 返工创建新 Attempt；DAG 不产生回边。改派、升级和越权拒绝均进入审计。

### 7.4 Three-level DAG

- L1：企业或跨部门目标，通常分配给部门主管。
- L2：部门工作包，可继续分配到团队或个人。
- L3：最终执行层，不得创建子 DAG。

父节点与子 DAG 通过 Work Contract 连接。父层默认只获得聚合状态、阻塞原因、交付物、提交与验收记录。父层打回的是整个子工作包的新 Attempt；子 DAG Owner 决定内部哪些节点重开。

### 7.5 Local Agent edge

中央 ECS 提供受控安装与注册引导。员工电脑上的 lark-cli 连接其本地 Claude/Codex，声明支持的执行器和在线状态。责任人选择 Agent 执行后，边缘运行时用短时 Capability Lease 获取最小化上下文、知识、Skill 和 MCP 权限，并将结果及来源证据回传。

离线时任务保持在人名下并停留为可见状态；系统可提醒、等待、改派或切回人工，不把边缘设备当业务真相源。

### 7.6 Control plane and runtime boundary

中央产品数据库保存租户、模板、实例、节点、Attempt、责任人、权限和审计。飞书是交互与内容系统；投影必须可对账。业务 DAG 由产品调度器解释。LangGraph 仅可用于某个复杂 AI 节点内部，不持有跨人流程的唯一业务状态。

### 7.7 UX principles

- 参与人默认 chat/task-first：从飞书待办进入上下文、执行方式和提交入口。
- 发起人和主管看到符合权限的 DAG、状态、阻塞和验收，不看到 LangGraph 或 Agent 内部轨迹。
- 每个界面明确显示“谁负责、等什么、何时到期、如何验收、能否下钻”。
- 妙搭、自建 H5 或飞书卡片是实现选择，不是产品模型；在原型验证后再锁定。

### 7.8 Assumptions and risks

- **需求风险：** 多层流程可能低频。先用历史实例计数，不用合同案例代替验证。
- **维护风险：** 模板治理太重。先由流程 Owner 共创 3 个模板，观察二次复用。
- **集成风险：** 飞书 Task/事件能力可能不足。保留中央工作包真相和幂等对账。
- **安全风险：** 本地 Agent 扩大数据面。使用短时 Lease、最小权限、可撤销和全链路审计。
- **体验风险：** DAG 心智过重。参与人只看工作包，完整图只服务发起人和主管。

## 8. Release

### R0 · Product reset and contracts

- 本 PRD、产品战略、目标架构和 DAG Template Spec 达成一致。
- 冻结 legacy 原型的产品扩张，只保留验证和迁移价值。
- 用 5 家目标企业访谈和飞书原生对照实验验证首个 beachhead。

### R1 · Single-tenant foundation

- 租户、组织、人员、Role Slot、模板版本和中央业务数据库。
- 一个发布模板可启动，人工工作包可靠投影到飞书并可验收/返工。
- 迁移现有 lark-cli 飞书适配器、幂等和对账经验。

### R2 · MVP three-level collaboration

- L1/L2/L3 父子实例、Work Contract、权限隔离、聚合状态和新 Attempt。
- 模板库治理、审计、阻塞升级和基础运营视图。
- 跑通跨部门试点并满足前四项 KRs。

### R3 · Personal Agent pilot

- 中央安装/注册引导、一个受支持本地 Agent 路径、Capability Lease 和结果回传。
- 完成离线、撤权、改派和人工接管演练，满足剩余 KRs。

MVP 明确不包含：无限层级、完全自动企业知识学习、任意自然语言直接发布流程、跨办公平台、多 Agent 自治分工和 LangGraph 业务建模界面。
