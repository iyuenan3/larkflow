# larkflow · 飞流 · AIREADME

> 飞书原生的企业协作 DAG：把多人流程拆成有依赖、有唯一责任人、可验收和可追溯的节点。
>
> 文档状态：2026-08-04 Phase 1 中央工作流基础实现，飞书 IM 窄命令、Human-Agent-Tool-Human、完成投影、Owner 查询、两类重启、运行中未来区域编辑和自动节点失败恢复均已在开发环境闭环。失败恢复真实验收覆盖两个连续重试、新 Attempt 历史保护、人工接管、飞书 Task 完成、完成文档与最终通知。跨人员分工已同时完成群聊 `role=@成员` 和单聊 Card 2.0 人员选择的开发真栈验收。可操作卡片现统一在动作耐久落库后先尝试显示无按钮的“处理中”，再收口为无按钮的成功或拒绝状态；人员选择卡的最终状态已从飞书服务端回读。Personal Agent Edge Proof v0 已完成 loopback 开发部署与 SSH 隧道跨机验收，公网设备链路受 ICP 接入备案阻断，Caddy 已停止。`Target` 表示目标产品契约，`As-built` 表示当前代码事实，两者不得混写。
>
> last-synced: dc77faad92e5d45f0271e45747bcbede3dd2ac02 · 2026-08-04

## 阅读顺序

1. [CORE.md](CORE.md)：产品身份、边界和不变量。
2. [PRODUCT_STRATEGY.md](PRODUCT_STRATEGY.md)：当前证据边界、取舍和成功标准。
3. [PRD.md](PRD.md)：简化 MVP 的功能、体验和验收。
4. [DAG_TEMPLATE_SPEC.md](DAG_TEMPLATE_SPEC.md)：DAG Contract v0.2 目标契约。
5. [ARCHITECTURE.md](ARCHITECTURE.md)：目标架构、数据权威和原型迁移边界。

既有设计的范围取舍见 [`research/design-simplification.md`](../research/design-simplification.md)。

## 状态

| 文件 | 状态 | 摘要 |
|---|:--:|---|
| CORE | ✅ | Target 身份、简化边界、Edge Proof 和不变量 |
| PRODUCT_STRATEGY | ✅ | 范围收敛取舍、窄 Edge 实验，明确未做市场验证 |
| PRD | ✅ | Target 单层 DAG MVP 与 Edge Proof 功能契约 |
| DAG_TEMPLATE_SPEC | ✅ | v0.2 模板、mention 角色绑定、草稿预览、未来区域编辑和两类重启已实现 |
| ARCHITECTURE | ✅ | Target 模块化单体、中央 Worker、飞书 IM / Card / Task / Doc 投影、失败恢复、Agent / Tool adapter、Edge Proof 和剩余差距 |
| RELATIONS | ✅ | Target 飞书、mention 与人员选择卡身份边界、中央 lark-cli、Edge HTTPS、Node Runner 与 LangGraph 边界 |
| ROADMAP | ✅ | Phase 1 已落码跨人员分工、飞书窄命令、未来区域编辑、两类重启、失败恢复与完成通知，Edge 公网设备链路仍受备案阻断 |
| SPEC | ✅ | legacy 契约、Target CLI、十个飞书窄命令、人员选择与失败恢复卡、Task 入站、未来区域编辑、两类重启、完成投影与私有 Edge v1 HTTP |
| DEPLOYMENT | ✅ | Legacy ECS 与 Target 六服务、飞书真实闭环、Caddy、PostgreSQL、备份和恢复实录 |
| CONVENTIONS | ✅ | Target 与 As-built 的命名、状态、安全和文档约定 |
| DECISIONS | ✅ | Append-only ADR 历史，新 ADR 显式 supersede 旧范围 |
| CHANGELOG | ✅ | Append-only 已实现变更，最新为可操作卡片即时视觉反馈契约 |
| MEMORY | ⚑ | Append-only 经验，仍含语义占位，已记录回调契约漂移、即时反馈竞态与历史无损去重 |

## 按任务读取

- 改产品范围：CORE + PRODUCT_STRATEGY + PRD + DECISIONS
- 核对既有设计的简化范围：`../research/design-simplification.md` + PRD + DECISIONS
- 改模板：DAG_TEMPLATE_SPEC + CONVENTIONS + PRD
- 改运行时或数据模型：ARCHITECTURE + SPEC + DECISIONS
- 改飞书集成：RELATIONS + ARCHITECTURE + DEPLOYMENT
- 判断“设计了还是做了”：先看 ARCHITECTURE 的差距表，再看 SPEC 和代码
- 未来恢复外部验证：`../research/phase-0/README.md`
