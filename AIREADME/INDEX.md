# larkflow · 飞流 · AIREADME
> 飞书原生的交付物流转工作流引擎（LangGraph 驱动，落到人）｜ 生命周期: 立项 · 第一段引擎已跑通
> last-synced: 2139e2e · 2026-07-24
<!-- 第二轮设计蒸馏后的同步锚点；check.sh --drift 据此算 AIREADME 落后 HEAD 多少 commit -->

## 状态
| 文件 | 状态 | 摘要 |
|---|:--:|---|
| CORE | ✅ | 身份（交付物流转引擎）/ non-goals / 红线（活图·历史·信号·多角色 LLM）|
| RELATIONS | ✅ | 飞书开放平台 / lark-cli（入口+出口，含交付物读写）/ LLM 多角色 / LangGraph；宿主 alicloud-sh |
| ARCHITECTURE | ✅ | 两层 + 受控活图 + 选择性重算 + 节点 2×3 + 交付物 (容器,region) + 禁改 |
| PRD | ✅ | win = 证交付物流转 + 打回省算；首场景独立 doc 拓扑（合同类）|
| DECISIONS | ✅ | ADR-001..018（012..018：定位升格 / 活图 / 选择性重算 / 节点模型 / 交付物 / LLM / 分期）|
| CONVENTIONS | ✅ | 节点契约 executor×role / 完成信号 / few-shot 护栏 / 禁用模式 |
| ROADMAP | ✅ | Now v1 独立 doc / Next v2 共享协同 / Later |
| SPEC | ⚑ | 节点契约 + 引擎契约 + 产出协议已定；卡片视觉 schema 待 dev app |
| CHANGELOG | ✅ | v0.0.0 立项 / v0.1.0 引擎跑通 + 第二轮设计 |
| DEPLOYMENT | ⚑ | 未部署；宿主 alicloud-sh + SQLite + lark-cli event 入口已定 |
| MEMORY | ✅ | seg-1 对抗性审查 9 项（去重 6 根因）+ 2026-07-24 产出协议实测 |

## 按任务读
- 跨项目了解 → CORE + RELATIONS
- 改架构 → ARCHITECTURE + DECISIONS
- 部署 / 运维 → DEPLOYMENT
- 加功能 → PRD + ROADMAP + CONVENTIONS
