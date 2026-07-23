# larkflow · 飞流 · AIREADME
> 飞书原生的 AI 工作流编排引擎（LangGraph 驱动，落到人）｜ 生命周期: 立项 pre-code
> last-synced: 4146145 · 2026-07-23
<!-- 立项 baseline SHA；check.sh --drift 据此算 AIREADME 落后 HEAD 多少 commit -->

## 状态
| 文件 | 状态 | 摘要 |
|---|:--:|---|
| CORE | ✅ | 身份 / non-goals（cards-only）/ 红线 |
| RELATIONS | ✅ | 飞书开放平台 / lark-cli（入口+出口）/ newapi / LangGraph；宿主 alicloud-sh |
| ARCHITECTURE | ✅ | 两层 + 数据流 + 首模板缺陷流分两段 + 禁改 |
| PRD | ✅ | win = 证采用 + 门禁；首场景缺陷流；cards-only |
| DECISIONS | ✅ | ADR-001..011（选型 / 路线 / 宿主 / 租户 / 首模板 / 生成 / 前端 / 命名）|
| CONVENTIONS | ✅ | 节点契约 / few-shot 护栏 / 禁用模式 |
| ROADMAP | ✅ | Now 第一段 / Next 第二段 / Later |
| SPEC | ✅ | 节点契约已定；飞书事件 / 卡片 schema 待 dev app |
| CHANGELOG | ✅ | v0.0.0 立项 + 设计定稿 |
| DEPLOYMENT | ⚑ | 未部署；宿主 alicloud-sh + SQLite + lark-cli event 入口已定 |
| MEMORY | ⚑ | 占位：尚无运行事故 |

## 按任务读
- 跨项目了解 → CORE + RELATIONS
- 改架构 → ARCHITECTURE + DECISIONS
- 部署 / 运维 → DEPLOYMENT
- 加功能 → PRD + ROADMAP + CONVENTIONS
