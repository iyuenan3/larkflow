# larkflow · 飞流 · AIREADME
> 飞书原生的交付物流转工作流引擎（LangGraph 驱动，落到人）｜ 生命周期: 引擎 v1.0 核心 headless 跑通 · 待接真栈
> last-synced: 42adf46 · 2026-07-24
<!-- 产品最终形态定稿后的同步锚点；check.sh --drift 据此算 AIREADME 落后 HEAD 多少 commit -->

## 状态
| 文件 | 状态 | 摘要 |
|---|:--:|---|
| CORE | ✅ | 身份（交付物流转引擎）/ non-goals / 红线（活图·历史·信号·多角色 LLM·前端投影）|
| RELATIONS | ✅ | 飞书开放平台 / lark-cli / 妙搭前端 / LLM 多角色 / LangGraph；宿主 alicloud-sh |
| ARCHITECTURE | ✅ | 两层 + 受控活图 + 选择性重算 + 打回权限(防踢皮球) + 节点 2×3 + 投票/分支 + 子项目 + 交付物 (容器,region) + 前端两视角 + 禁改 |
| PRD | ✅ | win = 证交付物流转 + 打回省算；独立 doc 拓扑；入口 @bot + 生成；前端两视角 |
| DECISIONS | ✅ | ADR-001..025（012-018 第二轮；019 前端；020 handle 权威；021-025 入口/生成/打回权限/子项目/投票分支）|
| CONVENTIONS | ✅ | 节点契约 executor×role / 完成信号 / few-shot 护栏 / 禁用模式 |
| ROADMAP | ✅ | Now 实现分层 v1.0(win)→v1.1 生成→v1.2 子项目→v1.3 投票分支 / Next v2 共享协同 + 前端可编辑 |
| SPEC | ⚑ | 节点契约(含投票/分支/打回权限) + 引擎契约 + 产出协议已定；卡片视觉 + 引擎前端 API + 生成契约待定 |
| CHANGELOG | ✅ | v0.0.0 立项 / v0.1.x 引擎 + 第二轮 + 前端 + handle 权威 / v0.2.0 产品最终形态 / v0.3.0 引擎 v1.0 headless |
| DEPLOYMENT | ⚑ | 未部署；宿主 alicloud-sh + SQLite + lark-cli event 入口已定 |
| MEMORY | ✅ | seg-1 对抗性审查 9 项（去重 6 根因）+ 2026-07-24 产出协议实测 |

## 按任务读
- 跨项目了解 → CORE + RELATIONS
- 改架构 → ARCHITECTURE + DECISIONS
- 部署 / 运维 → DEPLOYMENT
- 加功能 → PRD + ROADMAP + CONVENTIONS
