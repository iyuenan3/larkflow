# CLAUDE.md · larkflow（飞流）· router

> 飞书原生的**通用**交付物流转工作流引擎（LangGraph 驱动）。本文件是 router；详细真相源在 `AIREADME/`（先读 `AIREADME/INDEX.md`）。
> 当前：引擎 v1.0 核心 headless 跑通 + 通用性收口（2026-07-25）；设计全定（ADR-012..029）。

## 状态
引擎 v1.0 核心已落码跑通并做完通用性收口（v1 节点契约 + tool 数据化能力库 + 交付物层 + 选择性重算 + 打回意见回流 + 打回预算 + 受控活图 + 三张模板 + 真实栈代码，140 测绿，全程 Mock/Stub/`:memory:`）。**新增业务场景 = 只加一个 yaml，零 Python**（见 `templates/hiring.yaml`）。**下一步 = 接真栈**：建 dev 飞书 app + 开事件回调 + 配 LLM 角色 env → 跑真 e2e（`build_real_service`）；并行轨是妙搭前端原型验三命门。已知留白见 CHANGELOG v0.3.0 / v0.4.0：ADR-023 权限层 `allowed_reopen` 未落码、`blocked` 门无解除通道、`reconcile()` 未在启动时自动跑。

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

## 常用命令（全程不联网）
```bash
pytest -q                              # 140 passed
python -m larkflow.demo --auto         # 自动跑一遍，打印「打回省算」的证据
python -m larkflow.demo                # 交互式：你扮演所有的人（h 看命令）
python -m larkflow.demo --template hiring   # 换业务图（contract / defect / hiring）
```
新增业务场景 = 新增一个 `larkflow/templates/<名字>.yaml`，**零 Python**（tool 行为由 `tool: {kind, args}`
从 `engine/tools.py` 的能力库选取）。`tests/test_generality.py` 把这条钉成硬约束。

## 元信息
- git: main（已推 github.com/iyuenan3/larkflow）。slug `larkflow` / 中文名「飞流」。
- push 走 Clash：`git -c http.proxy=http://127.0.0.1:7897 push`（github.com 直连被墙、harness 代理也不通）。
- 依赖：飞书开放平台 + lark-cli + 妙搭前端 + LLM 多角色路由 + LangGraph（见 `AIREADME/RELATIONS`）。
- AIREADME 体系：`/aireadme` 触发维护；标准在 `~/.claude/skills/aireadme/`。
