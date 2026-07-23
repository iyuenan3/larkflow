# CLAUDE.md · larkflow（飞流）· router

> 飞书原生的 AI 工作流编排引擎（LangGraph 驱动）。本文件是 router；详细真相源在 `AIREADME/`（先读 `AIREADME/INDEX.md`）。
> 当前：立项 pre-code（2026-07-23）。

## 状态
立项，无代码。架构已定（两层 + 单一事实源 + 飞书投影，见 `AIREADME/ARCHITECTURE`）。下一步：选第一张策展模板 + 验飞书事件入口方案。

## 加载路由（任务 → 读）
| 任务 | 读 |
|---|---|
| 了解定位 / 红线 | `AIREADME/CORE` |
| 改架构 / 选型 | `AIREADME/ARCHITECTURE` + `DECISIONS` |
| 加功能 / 产品 | `AIREADME/PRD` + `ROADMAP` + `CONVENTIONS` |
| 部署 / 运维 | `AIREADME/DEPLOYMENT`（未部署）|
| 依赖关系 | `AIREADME/RELATIONS` |

## 红线（详见 `AIREADME/CORE`「绝不」）
- 单一事实源不破：checkpointer 权威，飞书是投影，不反向写真相。
- key / 凭证不入库；LLM 只走 newapi；clean-room 不搬雇主代码 / 业务 / 命名。

## 维护责任（什么变 → 更新哪个 AIREADME）
- 架构 / 选型变 → `ARCHITECTURE`（+ `DECISIONS` 记理由）
- 产品方向变 → `PRD` + `ROADMAP`
- 对外契约变 → `SPEC`
- 部署变 → `DEPLOYMENT`
- 里程碑 / release → `CHANGELOG`
- 踩坑 / 事故 → `MEMORY`

## 元信息
- git: main。slug `larkflow` / 中文名「飞流」。
- 依赖：飞书开放平台 + lark-cli + newapi-proxy + LangGraph（见 `AIREADME/RELATIONS`）。
- AIREADME 体系：`/aireadme` 触发维护；标准在 `~/.claude/skills/aireadme/`。
