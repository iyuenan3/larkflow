# ROADMAP · larkflow

> 状态：Target Delivery Plan · 2026-07-30
>
> 原 v1.0–v1.3“合同交付流 + LangGraph 全局运行时”路线已停止扩张。现有代码作为机制原型保留，后续里程碑以 [PRD.md](PRD.md) 的 KRs 为准。

## Phase 0 · Product evidence and architecture reset

目标：在继续扩代码前验证 beachhead，并切断目标设计与 legacy 原型的混写。

- 完成本轮 AIREADME、ADR 和 DAG Template v0.1 对齐。
- 访谈 5 家目标企业，回溯最近 30 天跨责任边界流程。
- 用飞书 Task + 审批 + 文档复刻候选流程，测出 larkflow 的增量价值。
- 选择一个每月 ≥10 次、有明确 Owner、至少两个责任人的试点流程。
- 对现有代码做迁移清单：保留 Feishu adapters、幂等、对账、权限纯函数经验；隔离全局 LangGraph/SQLite 假设。

**Exit gate：** 至少 3 家企业确认同类问题，且一个试点 Owner 愿意维护并二次使用模板。

## Phase 1 · Central workflow foundation

目标：一个企业能发布模板，并把单层工作包可靠派给人。

- PostgreSQL 领域模型：Tenant、Person、Role Binding、Template/Version、Instance、Node、Attempt、Assignment、Audit。
- 飞书组织同步和必需 Role Slot 解析；歧义时禁止启动。
- 模板 lifecycle、不可变发布版本、实例快照和基础权限。
- 业务 DAG scheduler、状态转换、验收/拒绝和新 Attempt。
- 飞书 Task/IM/Doc 投影、幂等、outbox/inbox 与对账。
- 从 legacy 原型迁移可复用 adapter；测试继续使用 mock Lark I/O。

**Demo：** 从已发布模板启动实例，两个真实责任人在飞书完成派单、提交、打回和再次验收；服务重启后状态一致。

## Phase 2 · MVP three-level collaboration

目标：验证产品的核心护城河，而不只是一层任务编排。

- L1/L2/L3 `parent_instance_id`、`level`、Work Contract 和 Contract Summary。
- 责任人创建子 DAG；L3 强制禁止下钻。
- 父子权限隔离、交付回填、聚合进度、阻塞和升级。
- 父层拒绝创建新 Attempt；子 Owner 决定内部重开范围。
- 企业/部门模板库、发布审核、合规锁和基础运营视图。
- 跑通至少一个跨部门真实试点。

**Demo：** 发起人只看 L1；部门主管分别展开 L2/L3；内部细节受权限保护；最终交付、打回和审计闭环。

## Phase 3 · Personal Agent edge pilot

目标：证明“待办属于人，Agent 可代执行”在离线和撤权场景下成立。

- ECS 下发安装/注册引导，设备身份、版本和撤销。
- 标准 Work Package / Result Envelope。
- Capability Registry 与单节点、单 Attempt、短时 Lease。
- 支持一条 Claude 或 Codex + lark-cli 执行路径。
- 离线等待、人工接管、改派、Lease 过期和设备丢失演练。
- 人类 Gate 仅接受本人确认。

**Demo：** 一项工作先因设备离线保持为人的待办，随后由个人 Agent 执行、责任人确认并被上层验收；撤销设备后无法继续访问。

## Phase 4 · Enterprise onboarding and template flywheel

目标：把一次试点变成可扩展的企业模板库。

- 授权知识源索引、Skill/MCP 注册和策略管理。
- 访谈/历史材料导入形成候选 Process Map。
- 候选流程的人审校准、模板推荐和运行数据反馈。
- Platform / Industry 模板 Fork 到 Enterprise / Department。
- 显式 diff/merge 升级和脱敏行业沉淀。

自动发现始终生成候选，不自动发布生产模板。

## MVP release gate

MVP = Phase 1–3 全部完成，并满足 PRD 的 6 项 KRs。任何只跑通单层合同流、只展示 DAG 画布或只让 Agent 建待办的版本，都不算 MVP。

## Later

- 高级图形化模板编辑、版本可视化 diff/merge。
- 模板效果分析、SLA 预测和阻塞风险预警。
- 更多本地 Agent 和受支持执行器。
- 跨企业协作、跨办公套件或模板市场。
- 在证据充分后扩展层级或流程表达能力。

## Explicitly parked

- 无限递归 DAG、业务回边和 Agent 作为企业责任人。
- 一次性“学习企业所有知识”。
- 自研 IM、云盘、云文档和通用任务系统。
- 继续为 legacy LangGraph 全局状态增加新的产品领域语义。
