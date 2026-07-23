# SPEC · larkflow

⚑ 部分定型（节点契约 + 引擎契约已定并本地跑通；卡片视觉 schema 待 dev app）。

## 模板节点契约（已定，路线 1 与生成共用）
一张模板 = 节点数组，节点：
```
{
  id:      string          # 节点唯一 id
  label:   string          # 展示名
  type:    "tool"|"llm"|"human"   # 机械动作 / AI 备料 / 人担责
  role:    string          # human 节点的 assignee 角色（如 开发/评审人/QA/负责人）；tool/llm 填 "-"
  gate:    string          # 门禁：达标条件；无则 "-"
  deps:    string[]        # 前置节点 id（依赖解锁）
  on_fail: string          # 【第一段补白】带 gate 节点的回边目标节点 id；无门禁则省略
  signal:  "task_complete"|"card_action"  # 【第一段补白】human 节点完成信号来自哪个 EventKey；tool/llm 省略
}
```
- 边由 `deps` 表达；门禁不达标 → 回边（环）到 `on_fail`（环的出口 = 门禁达标）。回边落地 = 把 `on_fail` 及其**全部传递下游**重置 `pending`，固定编排器环下一步自然重派（引擎实现 `larkflow/engine/gates.py`）。
- SPEC 已写「回边到指定上游」，`on_fail` 即那个「指定上游」的字段名（写码时补白，非改架构）。
- 生成新模板走 few-shot，须过护栏（三型齐全 / 每门禁配 `on_fail` 回边 / 放行节点强制 human / human 必声明 signal），校验落地 `larkflow/model/template.py`，见 DECISIONS ADR-010。
- 首个实例化模板 = 缺陷生命周期（`larkflow/templates/defect.yaml`，第一段 8 节点，见 ADR-009）。

## 引擎运行时契约（已定，本地 e2e 跑通）
- LangGraph state（禁改项：只放执行游标 + scratch）：`{dag, status(reducer), outputs(reducer), meta}`。业务真相源 = SQLite checkpointer（thread_id = 实例 id）；飞书 = 投影。
- 固定编排器图：`START → dispatch → [Send(<type>_worker, payload)…] 或 END`，`worker → dispatch`（唯一真环边）。worker 从 Send payload 读 node_id/dag（**Send 的 payload 是 worker 完整输入 state，不并入主 channel**，对抗复核纠正点）。
- human 节点纯挂起（`interrupt()` 只传数据）；飞书任务/卡由驱动层 `LarkFlowService` 在 `__interrupt__` 后建，`idem_key` 含 `interrupt.id`（重放去重、reopen 出新单）。`durability="sync"`。

## 飞书事件订阅 EventKey（已定，研究证实为静态常量，不需 dev app 上下文）
- `card.action.trigger`（仅 bot）：卡片按钮点击。路由键塞进按钮 `behaviors[].callback.value`，原样回传为事件 `action_value`（自描述 `{thread_id, interrupt_id, node_id, verdict}`）。⚠️ dev app 须在开发者后台开「事件与回调 → 回调配置」，否则静默零事件、无预检。
- `task.task.update_user_access_v2`（user|bot）：任务事件。**无专门完成事件**，完成 = `.event.event_types[]` 含 `task_completed_update`（自行 filter）；`.event.task_guid` 经关联表回映射到 `(thread_id, interrupt_id)`。

## 待填（dev app 建好后验）
- 卡片视觉 schema 细节（派单卡 / 门禁卡通过·打回 / 确认卡的排版），及 role → open_id 的通讯录解析。
