# CLAUDE.md · larkflow（飞流）· router

> 飞书原生的**通用**交付物流转工作流引擎（LangGraph 驱动）。本文件是 router；详细真相源在 `AIREADME/`（先读 `AIREADME/INDEX.md`）。
> 当前：引擎 v1.0 核心 + 服务层已落码（2026-07-25）；设计全定（ADR-012..033）。

## 状态
引擎核心 + 通用性收口 + **服务层**都已落码（v1 节点契约 + tool 数据化能力库 + 交付物层 + 选择性重算 + 打回预算与 `blocked` 出口 + 打回权限层 / 应答权 + 受控活图 + `larkflow serve` 常驻与 CLI + 三张模板 + 真实栈代码，**285 测绿**）。**新增业务场景 = 只加一个 yaml，零 Python**（见 `templates/hiring.yaml`）。
**但测绿全程 Mock/Stub/`:memory:`，真栈路径一次没跑过**（`build_real_service` 零覆盖）。**下一步 = 接真栈**：建 dev 飞书 app + 开事件回调 + 配 LLM 角色 env → 起 `larkflow serve` 跑真 e2e；并行轨是妙搭前端原型验三命门（0/3）。
已知留白（详见 CHANGELOG v0.5.0 / ROADMAP v1.0）：escalation 的一键同意未做（申请落了 state，`status` 永远 pending）；`unblock` 无权限层（`by` 只进审计，`unblock(reopen=…)` 是绕过 ADR-023 的路）；改图换负责人不重新派单；`assignee_role` 配成飞书群时该节点无人可应答。

## 加载路由（任务 → 读）
| 任务 | 读 |
|---|---|
| 了解定位 / 红线 | `AIREADME/CORE` |
| 改架构 / 选型 | `AIREADME/ARCHITECTURE` + `DECISIONS` |
| 加功能 / 产品 | `AIREADME/PRD` + `ROADMAP` + `CONVENTIONS` |
| 部署 / 运维 | `AIREADME/DEPLOYMENT`（形态已落码，仍未真部署）|
| 依赖关系 | `AIREADME/RELATIONS` |

## 红线（前三条详见 `AIREADME/CORE`「绝不」；后两条是落码后追加的工程红线）
- 单一事实源不破：checkpointer 权威，飞书是投影，不反向写真相。
- 只改未来、不改历史：活图只改 pending 节点，打回解冻重跑 append 新版，不原地改历史产出。
- key / 凭证不入库；LLM 走 OpenAI 兼容多角色路由（火山方舟 / 中转站 / 直连），不直连厂商 SDK；clean-room 不搬雇主代码 / 业务 / 命名。
- 一切权限 / 合法性在**引擎权威侧**算，绝不信前端回传（卡片 `action_value` 是攻击面；身份只取事件顶层 `operator_id`）。见 ADR-023 / ADR-032。
- 测试全程 Mock / Stub / `:memory:`，**绝不**构造 `build_real_service`（会真发飞书消息、真建文档）。

## 维护责任（什么变 → 更新哪个 AIREADME）
- 架构 / 选型变 → `ARCHITECTURE`（+ `DECISIONS` 记理由）
- 产品方向变 → `PRD` + `ROADMAP`
- 对外契约变 → `SPEC`
- 部署变 → `DEPLOYMENT`
- 里程碑 / release → `CHANGELOG`
- 踩坑 / 事故 → `MEMORY`

## 常用命令
```bash
# 本地（全程不联网，Mock 飞书 + Stub LLM）
pytest -q                              # 285 passed
python -m larkflow.demo --auto         # 自动跑一遍，打印「打回省算」的证据
python -m larkflow.demo                # 交互式：你扮演所有的人（h 看命令；un 解除 ⛔ / esc 看审批申请）
python -m larkflow.demo --template hiring   # 换业务图（contract / defect / hiring）

# 真栈（**会真发飞书消息 / 真建文档**，需 dev app + env，见 AIREADME/DEPLOYMENT）
larkflow serve                         # 唯一的守护进程：启动对账 + 事件泵 + 优雅退出
larkflow start --template contract --reporter ou_xxx --input 甲方=某某
larkflow status <实例> / pending <实例> [--actor ou_xxx] / reconcile [实例]
larkflow unblock <实例> <节点> --by ou_xxx --reason "改了要素"   # 解除 ⛔
```
daemon 与一次性命令写同一个 SQLite：DB 必须放本地盘（WAL + 跨进程 flock，见 ADR-031）。
新增业务场景 = 新增一个 `larkflow/templates/<名字>.yaml`，**零 Python**（tool 行为由 `tool: {kind, args}`
从 `engine/tools.py` 的能力库选取）。`tests/test_generality.py` 把这条钉成硬约束。

## 元信息
- git: main（已推 github.com/iyuenan3/larkflow）。slug `larkflow` / 中文名「飞流」。
- push 走 Clash：`git -c http.proxy=http://127.0.0.1:7897 push`（github.com 直连被墙、harness 代理也不通）。
- 依赖：飞书开放平台 + lark-cli + 妙搭前端 + LLM 多角色路由 + LangGraph（见 `AIREADME/RELATIONS`）。
- AIREADME 体系：`/aireadme` 触发维护；标准在 `~/.claude/skills/aireadme/`。
