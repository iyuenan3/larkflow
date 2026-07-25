# SPEC · larkflow

⚑ 部分定型（节点契约含投票 / 分支 / 打回权限 + 引擎契约 + 产出协议已定，seg-1 本地跑通 + 产出闭环实测；卡片视觉 schema / 引擎前端 API / 生成契约待 dev app + 原型）。

## 模板节点契约（ADR-015：executor × role + 配置）
一张模板 = 节点数组，节点：
```
{
  id:       string
  label:    string
  executor: "tool" | "llm" | "human"      # 谁执行：确定性程序 / AI / 人
  role:     "produce" | "gate"            # 干啥：产出交付物 / 把关放行或打回
  deps:     string[]                      # 前置节点 id（依赖解锁）
  # produce 专属（**可省**：发通知 / 调外部系统 / 确认线下动作这类纯动作节点不产文档）
  deliverable: { container?: handle, region: "whole" | {section: selector} }  # 交付物落点（ADR-016）
  # tool 专属（ADR-026）：确定性动作由**配置**选取，不写 Python
  tool:     { kind: string, args?: object }   # kind ∈ 内置能力库（与模板无关的全局注册表）
  # llm 专属（ADR-017；v1 护栏③只允许 llm 当 produce，见下）
  prompt:   string                        # 生成 / 评分指令
  model_role: string                      # 路由到哪个 LLM 角色
  # gate 专属
  approval_policy: "auto" | "single" | "any" | "all" | {threshold: expr}  # 放行策略（auto=bypass；threshold=投票阈值，ADR-025）
  reopen_budget:   int                    # 这道门最多打回几次，超了标 blocked 终态（默认 3，ADR-029）
  # human 专属
  assignee_role: string                   # 派给谁（开发 / QA / 负责人…）
  signal:   "task_complete" | "card_action" | "message"  # 完成信号来源
  # 多人节点（投票 / 会签，ADR-025）
  vote:    { voters: assignee_role[], primary: assignee_role, policy: expr }  # voters / 主负责人(primary) / 聚合阈值（用 assignee_role 而非 role=produce|gate）
  # 条件分支（ADR-025）
  when:    { <decision_node_id>: value }   # 守卫：上游决策值匹配才 eligible，否则 skipped
}
```
- 节点状态：`pending | done | failed | blocked | skipped`。`blocked` = 该门反复打回仍不通过、已超打回预算、停下等人介入（ADR-029），它不是 `done`，下游不解锁。
- 边由 `deps` 表达。**打回不在模板里预声明单目标**：gate 节点运行时产出 `{passed, reopen: [节点 id…], comment}`，`reopen` 是当场手选的一组；引擎把该组 + 其传递下游重置 `pending`（选择性重算，`larkflow/engine/gates.py` 的 `stale_downstream`）。`reopen` 合法域（每个目标须 ⊆ gate 的 deps 传递祖先）在**运行时**校验、非法则拒（seg-1 的模板期护栏②b 随 v1 搬到运行时）。
- **投票门 / 决策表决产出（ADR-025）**：A 类审批投票门（role:gate + 阈值 approval_policy）票到阈值 → 引擎自动判 `{passed, reopen}`（reopen 默认 = 把关的上游，主负责人可加宽）；B 类决策表决（role:produce）产出决策值到 `outputs[node]`，不自动打回。
- **打回权限契约（ADR-023）｜as-built：v1.0 只实现机制层**，权限层 `allowed_reopen` 尚未落码（v1.0 单 owner 场景够用；进多参与人前必须补，否则「防踢皮球」是空的）。引擎已把机制合法域 `reopen_candidates` 交给卡片 / 前端，权限过滤是叠在其上的下一层。某人可见的打回候选 = 机制合法（⊆ 传递祖先）∩ 权限允许。权限 = 纯函数 `allowed_reopen(dag, actor_openid, project_owner, node_assignees, from_node) -> set[node_id]`：owner 全域；参与人限「重算集不牵连别的人工节点」的责任段；集体投票（A 类）另算。审核卡 / 画布据此过滤候选。
- **条件分支 skip / ready（ADR-025）**：节点 `skipped` ⟺ `when` 守卫失配 或 所有 deps 都 skipped；节点 ready ⟺ pending 且 deps 全 done/skipped 且 ≥1 dep done 且守卫通过。分支从 deps + 守卫涌现。打回决策节点 → 其 skipped 下游复活为 pending。
- 生成新模板走 few-shot 护栏（三型齐全 / 每 gate 有可回退祖先 / 放行节点强制 human / human 声明 signal / human 节点 ≥1 负责人 / 多人节点须 1 主负责人 / 条件分支决策取值域被分支守卫全覆盖或留默认支），校验落 `larkflow/model/template.py`（ADR-010 / ADR-023 / ADR-025）。
- **护栏① as-built：不是硬校验**（ADR-027）。「三型齐全」回到 ADR-010 原意（进生成 prompt），落 `lint_template()` 当风格提示。纯人协作流（招聘接力 / 采购审批）与纯 AI+人流（视频脚本）都是合法流程，硬校验会把它们整类挡在门外，还会让「运行中删掉最后一个 llm 节点」被拒。
- **tool 能力库（ADR-026）**：`tool.kind` 从与模板无关的全局注册表解析（v1 实装 `record` / `summarize_links` / `notify` / `noop` / `format_check` / `expect_fields`），装配期 `validate_coverage` 只校验 kind 可解析。按 node id 注册的 handler 仅作逃生舱。**新增一个业务场景 = 新增一个 yaml**，`larkflow/templates/` 目录内没有 Python。
- **human gate 的 signal 只能是 `card_action`**：任务只有「完成」没有「打回」，把「完成任务」当审批裁决 = 审批门静默变橡皮图章，且会在 outputs 留下本人从未做出的「同意」。
- **护栏③ as-built 判据（v1 已实现）**：`approval_policy=="auto"` 的 gate 只能是 `tool`（确定性机检 bypass）；其余 policy 的 gate 只能是 `human`（人拍板）。推论：**`llm` 在 v1 校验下不能当 gate**（红线「绝不让 LLM 自动放行」，CONVENTIONS 护栏③）；AI 评审须落成 `(llm, produce)` 出意见 + `human` gate 拍板，同构 ADR-021/022「AI 提议 + 人拍板」。v2 若要真 AI-gate 需改本护栏并记 ADR。
- 护栏⑤ as-built 只校验「守卫可求值」（`when` 的决策节点须是本节点传递祖先）；「决策取值域被守卫全覆盖或留默认支」需决策节点声明取值域，字段待 v1.3 定（见〈待填〉）。
- seg-1 首个实例化模板 = 缺陷生命周期（`larkflow/templates/defect.yaml`，8 节点 = ADR-009 完整 11 节点计划的 as-built 退化子集，seg-2 回填 ci_test/code_review/release_note；ADR-009 / ADR-012）。
- **as-built vs v1 字段名（step 1 已迁完）**：`node.py` / `template.py` / `defect.yaml` / `gates.py` 已在本节 v1 schema 上：`type` → `executor`；seg-1 的 `role`（业务指派串，如 负责人/QA/开发）→ `assignee_role`，`role` ∈ {produce, gate} 是正交维（消解撞名）；门禁判据从 `gate` 字符串 + 静态 `on_fail` 单目标 → `role=="gate"` + `approval_policy` + 运行时 `reopen`。旧字段（`type`/`on_fail`/`gate`）留在模板里会被 `validate_template` 显式拒绝（防静默失效）。缺陷流三个人工确认节点随之归为 `gate`（`single`），tool/llm/human-produce 节点各声明 `deliverable`。

## 交付物产出 / 消费协议（ADR-016，产出闭环已实测）
- 交付物 = 飞书 handle（doc token / 云盘 file token），模型 `(容器, region)`。
- **handle 权威登记（ADR-020）**：produce 末步把物化得到的 handle（+ region + type）写进 `state.outputs[node_id]`，它是交付物 handle 的**唯一权威登记表**；节点 schema 的 `deliverable.container` 是活图 dag 里的声明位 / create 后回填指针，非第二份权威。下游经 `outputs[dep]` 取 handle 再 fetch 正文；reopen 不清 outputs，故未重算旁支跨 overwrite 复用旧 handle。
- **produce**：`markdown +create`（首跑）/ `+overwrite`（重跑，handle 不变、飞书自动留版本）；docx 用 `docs +create/+update`；二进制走 `drive +upload`。
- **consume**（下游 llm 读上游正文）：`markdown +fetch` / `docs +fetch`。
- **审计 / 版本**：`markdown +diff`、`drive +version-history`、`docs +history-*`（引擎不自建版本）。
- 闭环已在测试组织实测通过（handle 跨 overwrite 稳定 = 选择性重算「旁支复用」的实证基础，详见 MEMORY 2026-07-24）。

## 引擎运行时契约（seg-1 本地 e2e 跑通）
- LangGraph state（禁改项：只放执行游标 + scratch）：`{dag, status(reducer), outputs(reducer), meta}`（`status` ∈ pending / running / done / failed / skipped，skipped = 条件分支未选支，ADR-025）。**`dag` 是可写 channel**，改它 = 运行时改图（受控活图，ADR-013）。业务真相源 = SQLite checkpointer（thread_id = 实例 id）；飞书 = 投影。
- 固定编排器图：`START → dispatch → [Send(<executor>_worker, payload)…] 或 END`，`worker → dispatch`（唯一真环边）。worker 从 Send payload 读 node_id/dag（**Send 的 payload 是 worker 完整输入 state，不并入主 channel**）。
- human 节点纯挂起（`interrupt()` 只传数据）；飞书任务 / 卡由驱动层 `LarkFlowService` 在 `__interrupt__` 后建，`idem_key` 含 `interrupt.id`（重放去重、reopen 出新单）。`durability="sync"`。

## 飞书事件订阅 EventKey（研究证实为静态常量，不需 dev app 上下文）
- `card.action.trigger`（仅 bot）：卡片按钮点击。路由键塞按钮 `behaviors[].callback.value`，原样回传为 `action_value`（自描述 `{thread_id, interrupt_id, node_id, passed, reopen}`，与 gate 产出的 `passed`/`reopen` 逐字对齐）。⚠️ dev app 须在开发者后台开「事件与回调 → 回调配置」，否则静默零事件。
- `task.task.update_user_access_v2`（user|bot）：任务事件。完成 = `.event.event_types[]` 含 `task_completed_update`（自行 filter）；`.event.task_guid` 经关联表回映射到 `(thread_id, interrupt_id)`。
- human-produce 定稿信号：优先用 `task_complete` / `card_action` 结构化信号（ADR-021，无歧义）；发消息（message）变体走 IM 消息事件、推迟（待 dev app 定确切 EventKey，且消息无自描述封套、到中断的关联需另设计）。

## 待填（dev app 建好后验）
- 条件分支决策节点的**取值域声明字段**（护栏⑤全覆盖判据的前提，v1.3 定；v1 只校验守卫引用祖先）。
- 卡片视觉 schema（派单卡 / 门禁卡通过·打回·多选 reopen / 定稿确认卡的排版），assignee_role → open_id 通讯录解析。
- 共享协同拓扑的 docx block_id 跨 update 稳定性（v2）。
- 引擎读 / 命令 API（供前端，ADR-019；形态待原型后定）：
  - **读 as-built（驱动层已有，尚未暴露成网络接口）**：`status(instance_id)` 状态表 / `outputs(instance_id)` 产出 + 交付物 handle 登记表 / `pending(instance_id)` 卡在谁手上（节点 / 负责人 / 交付物链接 / 待审上游链接 / 打回候选）。
  - **读**：画布要整张 `dag`（节点 + 边 + pending 子图 + 状态），多维表格行式投影可能不够；定「整图读接口 + 返回字段 + 刷新 / 实时模型（轮询 / 推送）」。
  - **改图命令 as-built（引擎侧已实现，前端形态仍待定）**：`LarkFlowService.edit_graph(instance_id, ops)`，ops = `[{op: add_node, node:{…}} | {op: remove_node, id} | {op: update_node, id, set:{…}}]`；引擎权威侧串校验「只触 pending 子图（挂起 human 节点并入冻结线当 running）→ 仍过 validate_template → 不用 v1 未实现语义 → 新增 tool 节点有 handler」→ `update_state` 写 dag channel → 立刻 `invoke(None)` 推一步。**副作用（实测，见 MEMORY 2026-07-24）**：update_state 必让挂起中断换 id，故驱动层按 node 记 `interrupt_remap` 迁移链、旧卡继续有效且不重复派单。尚缺：乐观并发（读取时 checkpoint 版本）+ 鉴权。
  - **改图命令（前端侧待定）**：报文 schema（op + 目标节点 + deps）；**校验在引擎权威侧**（复用 ADR-013：只改 pending / 仍是 DAG / 不删在跑节点）；乐观并发（命令带读取时 checkpoint 版本，冻结线已推进则拒、令前端重取）；命令经 checkpointer `update_state` 改 dag channel 并触发下一 dispatch。
  - **鉴权**：调用方认证（服务间 token / mTLS / 飞书身份透传择一）；命令带已验证操作人 open_id，供 gate `approval_policy=any/all` 按人归因去重；最小权限（前端只能对 pending 子图与本人有权的 gate 发命令）。
  - **cards 与 app 双输入面**：同一 interrupt 决策用统一幂等键（含 interrupt_id）跨两面去重；app 命令复用卡片自描述封套，引擎单处理器消费。
