# CLAUDE.md · larkflow（飞流）· router

> 飞书原生的 AI 工作流编排引擎（LangGraph 驱动）。本文件是 router；详细真相源在 `AIREADME/`（先读 `AIREADME/INDEX.md`）。
> 当前：立项 · 第一段引擎已跑通（2026-07-24）；第二轮设计定（交付物流转，ADR-012..018）。

## 状态
第一段本地引擎已跑通（8 节点缺陷流）。定位升格为交付物流转引擎（受控活图 + 选择性重算 + 交付物 (容器,region)，见 `AIREADME/ARCHITECTURE`）。下一步：建 dev 飞书 app + v1 独立 doc 拓扑真项目跑通。

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
- 只改未来、不改历史：活图只改 pending 节点，打回解冻重跑 append 新版，不原地改历史产出。
- key / 凭证不入库；LLM 走 OpenAI 兼容多角色路由（火山方舟 / 中转站 / 直连），不直连厂商 SDK；clean-room 不搬雇主代码 / 业务 / 命名。

## 维护责任（什么变 → 更新哪个 AIREADME）
- 架构 / 选型变 → `ARCHITECTURE`（+ `DECISIONS` 记理由）
- 产品方向变 → `PRD` + `ROADMAP`
- 对外契约变 → `SPEC`
- 部署变 → `DEPLOYMENT`
- 里程碑 / release → `CHANGELOG`
- 踩坑 / 事故 → `MEMORY`

## 元信息
- git: main。slug `larkflow` / 中文名「飞流」。
- 依赖：飞书开放平台 + lark-cli + LLM 多角色路由 + LangGraph（见 `AIREADME/RELATIONS`）。
- AIREADME 体系：`/aireadme` 触发维护；标准在 `~/.claude/skills/aireadme/`。
