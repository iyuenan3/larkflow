# SPEC · larkflow

⚑ 部分定型（节点契约 + 引擎契约 + 产出协议已定，seg-1 本地跑通 + 产出闭环实测；卡片视觉 schema 待 dev app）。

## 模板节点契约（ADR-015：executor × role + 配置）
一张模板 = 节点数组，节点：
```
{
  id:       string
  label:    string
  executor: "tool" | "llm" | "human"      # 谁执行：确定性程序 / AI / 人
  role:     "produce" | "gate"            # 干啥：产出交付物 / 把关放行或打回
  deps:     string[]                      # 前置节点 id（依赖解锁）
  # produce 专属
  deliverable: { container?: handle, region: "whole" | {section: selector} }  # 交付物落点（ADR-016）
  prompt:   string                        # llm-produce 的生成指令
  model_role: string                      # llm-produce 路由到哪个 LLM 角色（ADR-017）
  # gate 专属
  approval_policy: "auto" | "single" | "any" | "all"   # 放行策略（auto = bypass）
  # human 专属
  assignee_role: string                   # 派给谁（开发 / QA / 负责人…）
  signal:   "task_complete" | "card_action" | "message"  # 完成信号来源
}
```
- 边由 `deps` 表达。**打回不在模板里预声明单目标**：gate 节点运行时产出 `{passed, reopen: [节点 id…], comment}`，`reopen` 是当场手选的一组；引擎把该组 + 其传递下游重置 `pending`（选择性重算，`larkflow/engine/gates.py` 的 `stale_downstream`）。
- 生成新模板走 few-shot 护栏（三型齐全 / 每 gate 配回边 / 放行节点强制 human / human 声明 signal），校验落 `larkflow/model/template.py`（ADR-010）。
- seg-1 首个实例化模板 = 缺陷生命周期（`larkflow/templates/defect.yaml`，8 节点，退化特例，ADR-009 / ADR-012）。

## 交付物产出 / 消费协议（ADR-016，产出闭环已实测）
- 交付物 = 飞书 handle（doc token / 云盘 file token），模型 `(容器, region)`。
- **produce**：`markdown +create`（首跑）/ `+overwrite`（重跑，handle 不变、飞书自动留版本）；docx 用 `docs +create/+update`；二进制走 `drive +upload`。
- **consume**（下游 llm 读上游正文）：`markdown +fetch` / `docs +fetch`。
- **审计 / 版本**：`markdown +diff`、`drive +version-history`、`docs +history-*`（引擎不自建版本）。
- 闭环已在测试组织实测通过（handle 跨 overwrite 稳定 = 选择性重算「旁支复用」的实证基础，详见 MEMORY 2026-07-24）。

## 引擎运行时契约（seg-1 本地 e2e 跑通）
- LangGraph state（禁改项：只放执行游标 + scratch）：`{dag, status(reducer), outputs(reducer), meta}`。**`dag` 是可写 channel**，改它 = 运行时改图（受控活图，ADR-013）。业务真相源 = SQLite checkpointer（thread_id = 实例 id）；飞书 = 投影。
- 固定编排器图：`START → dispatch → [Send(<executor>_worker, payload)…] 或 END`，`worker → dispatch`（唯一真环边）。worker 从 Send payload 读 node_id/dag（**Send 的 payload 是 worker 完整输入 state，不并入主 channel**）。
- human 节点纯挂起（`interrupt()` 只传数据）；飞书任务 / 卡由驱动层 `LarkFlowService` 在 `__interrupt__` 后建，`idem_key` 含 `interrupt.id`（重放去重、reopen 出新单）。`durability="sync"`。

## 飞书事件订阅 EventKey（研究证实为静态常量，不需 dev app 上下文）
- `card.action.trigger`（仅 bot）：卡片按钮点击。路由键塞按钮 `behaviors[].callback.value`，原样回传为 `action_value`（自描述 `{thread_id, interrupt_id, node_id, verdict/reopen}`）。⚠️ dev app 须在开发者后台开「事件与回调 → 回调配置」，否则静默零事件。
- `task.task.update_user_access_v2`（user|bot）：任务事件。完成 = `.event.event_types[]` 含 `task_completed_update`（自行 filter）；`.event.task_guid` 经关联表回映射到 `(thread_id, interrupt_id)`。
- human-produce 定稿信号（发消息）：走 IM 消息事件（待 dev app 定确切 EventKey）。

## 待填（dev app 建好后验）
- 卡片视觉 schema（派单卡 / 门禁卡通过·打回·多选 reopen / 定稿确认卡的排版），role → open_id 通讯录解析。
- 共享协同拓扑的 docx block_id 跨 update 稳定性（v2）。
