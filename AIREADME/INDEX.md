# larkflow · 飞流 · AIREADME

> 飞书原生的企业协作 DAG：把跨人、跨部门工作分解为有责任人、可下钻、可验收、可追溯的工作包。
>
> 文档状态：2026-07-30 产品重定位草案。`Target` 表示目标产品契约，`As-built` 表示当前代码事实，两者不得混写。
>
> last-synced: f4b6d59 · 2026-07-30

## 阅读顺序

1. [CORE.md](CORE.md)：产品身份、边界和不变量。
2. [PRODUCT_STRATEGY.md](PRODUCT_STRATEGY.md)：目标市场、价值、取舍和关键假设。
3. [PRD.md](PRD.md)：MVP 用户问题、范围、体验与验收。
4. [DAG_TEMPLATE_SPEC.md](DAG_TEMPLATE_SPEC.md)：DAG Template v0.1 目标契约。
5. [ARCHITECTURE.md](ARCHITECTURE.md)：目标架构、数据权威和原型迁移边界。

## 文档地图

| 文档 | 状态 | 作用 |
|---|:--:|---|
| CORE | Target | 产品身份、用户、non-goals、硬约束 |
| PRODUCT_STRATEGY | Target | 九段产品战略画布、指标、赌注与实验 |
| PRD | Target | MVP 需求、优先级、用户旅程和发布验收 |
| DAG_TEMPLATE_SPEC | Target Draft | 模板作用域、版本、Role Slot、三级子 DAG、权限与锁 |
| ARCHITECTURE | Target + Gap | 中央控制面、飞书投影、本地 Agent 边缘运行时及迁移差距 |
| RELATIONS | Target | 飞书、lark-cli、个人 Agent、LLM、MCP/Skill 的系统边界 |
| ROADMAP | Target | 从现有原型迁移到可试点 MVP 的阶段 |
| SPEC | As-built | 当前 Python 引擎、legacy YAML 与 CLI 契约 |
| DEPLOYMENT | As-built | 现有 ECS + SQLite 原型部署实录；不是目标 SaaS 架构 |
| CONVENTIONS | Target + As-built | 代码、模板、状态和文档约定 |
| DECISIONS | Append-only | ADR 历史；新 ADR 会显式 supersede 旧结论 |
| CHANGELOG | Append-only | 已实现变更和产品文档重置记录 |
| MEMORY | Append-only | 实验、审查和不可丢失的经验 |

## 按任务读取

- 改产品范围：CORE + PRODUCT_STRATEGY + PRD + DECISIONS
- 改模板：DAG_TEMPLATE_SPEC + CONVENTIONS + PRD
- 改运行时或数据模型：ARCHITECTURE + SPEC + DECISIONS
- 改飞书或本地 Agent 集成：RELATIONS + ARCHITECTURE + DEPLOYMENT
- 判断“设计了还是做了”：先看 ARCHITECTURE 的差距表，再看 SPEC 和代码
