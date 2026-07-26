# SPEC · larkflow

⚑ 部分定型（节点契约含投票 / 分支 / 打回权限 + 引擎契约 + 产出协议已定，seg-1 本地跑通 + 产出闭环实测；卡片视觉 schema / 引擎**网络** API / 生成契约待 dev app + 原型）。
> 2026-07-25：**对外契约的 as-built 面从「驱动层 Python 方法」扩到「CLI 子命令」**（ADR-031），见〈引擎对外接口 as-built〉。仍**没有任何网络接口**。

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
- 节点状态：`pending | done | failed | blocked | skipped`。`blocked` = 该门反复打回仍不通过、已超打回预算、停下等人介入（ADR-029），它不是 `done`，下游不解锁。**`blocked` 的出口 = 人显式解除**（ADR-030）：`unblock` 把它放回 pending 并**追加**一份预算（`真实预算 = 节点 reopen_budget + Σgrant`），单次 grant 收进 [1,3]、同一节点累计解除 ≤3 次；解除只回 pending，**不放行**。注意 `blocked` 不是真终态：另一道门打回共同祖先时它会被当普通下游重置回 pending（不经审计、不花额度）。
- 边由 `deps` 表达。**打回不在模板里预声明单目标**：gate 节点运行时产出 `{passed, reopen: [节点 id…], comment}`，`reopen` 是当场手选的一组；引擎把该组 + 其传递下游重置 `pending`（选择性重算，`larkflow/engine/gates.py` 的 `stale_downstream`）。`reopen` 合法域（每个目标须 ⊆ gate 的 deps 传递祖先）在**运行时**校验、非法则拒（seg-1 的模板期护栏②b 随 v1 搬到运行时）。
- **投票门 / 决策表决产出（ADR-025）**：A 类审批投票门（role:gate + 阈值 approval_policy）票到阈值 → 引擎自动判 `{passed, reopen}`（reopen 默认 = 把关的上游，主负责人可加宽）；B 类决策表决（role:produce）产出决策值到 `outputs[node]`，不自动打回。
- **打回权限契约（ADR-023）｜as-built：机制层 + 权限层都已落码**（2026-07-25，`larkflow/engine/permissions.py`）。某人可见 / 可点的打回候选 = 机制合法（⊆ 传递祖先）∩ 权限允许。
  - 纯函数签名（身份的货币单位 = **令牌集合**，角色名 ∪ open_id，见 ADR-023 实现状态）：`allowed_reopen(dag, *, actor_roles, owner_roles, from_node) -> [node_id]`（无需审批就点得动的）、`reopen_verdict(dag, *, actor_roles, owner_roles, from_node, targets) -> {allowed, needs_escalation:[{target, approvers, collateral}], denied}`、`can_answer(dag, *, actor_roles, node_id) -> bool`（应答权，ADR-032）、`collateral_humans` / `primary_owner` / `approvers_for`。`open_id → 角色集合` 的反解在驱动层 `RoleResolver.roles_of`（一对多）。
  - **actor 只取事件顶层 `operator_id`**，绝不从 `action_value` / `value` 里取（封套是前端可自由构造的攻击面；红线：权限判定只在引擎权威侧算）。卡片事件缺 `operator_id` → `{"skipped": "unidentified_actor"}`（fail closed）。
  - **不带 `reopen` 的「打回」也过权限层**（用引擎默认目标组 = gate 的直接上游），否则前端什么都不带就能绕过。
  - **全或无**：一组目标里只要有一个跨界，整笔都不执行，落一笔 escalation 申请。卡片上的默认目标只剔 `denied`、**保留** `needs_escalation`（剔掉就成了一次静默的部分打回）；`pending(actor=)` 的 `reopen_default` 与卡上那颗按钮同一把尺。
  - escalation 记录（state channel `escalations[gate_id]`，追加型）里混两类，靠 `kind` 分（缺省 = `request`，向后兼容早于 ADR-040 的记录）：
    - `request`：`{kind, by, at, from_node, targets, escalated, approvers（令牌）, notified（当时真发给了谁）, collateral, comment, attempt, seq, status}`。同一轮 + 同一人 + 同一组目标（按**集合**比）去重；每道门**每一轮**待批 ≤5 笔。`seq` = 这道门第几笔**申请**（`len(_requests(log))+1`；log 里混着裁决记录，用 `len(log)` 会跳号）。
    - `verdict`（ADR-040）：`{kind, ref（指向申请的 seq）, node_id, verdict: approved|rejected, by, at, attempt, comment, reopened?}`。
  - **同意 / 拒绝契约 as-built（ADR-040）**：`approve_escalation(instance_id, gate_id, *, by, seq=None, comment=None)` / `reject_escalation(…)` 同签名。`seq` 省略 = 本轮唯一那笔；多笔待批而不给 seq → `ambiguous_escalation` 并列候选。
    - 五道闸按序：`missing_audit`（`by` 空）→ `already_settled`（幂等）→ `stale`（轮次已过**或门已被答复**）→ `self_approve` → `unauthorized_approve`；另有 `no_such_escalation` / `illegal_reopen`。
    - **状态是派生的**：`effective_status` ∈ `pending|approved|rejected|expired`，由「有没有配对的 verdict」+「轮次是否仍是当前轮」+「门是否已被答复」算出。记录里那个 `status` 字面量冻的是落库那一刻、永远 `pending`。配额 / `pending_escalations` / `escalations` 的标注三处共用同一把尺。
    - 审批人身份**两把尺**：`_actor_roles(by) ∩ record.approvers`（令牌），或 `by ∈ record.notified`（当初真通知到的 open_id，防 `roles_of` 反解静默失效导致这笔申请无人可批）。**禁自批**（`by == record.by` 即拒）。
    - 同意 = **执行整组 `targets`**（全或无），执行前按当前活图重跑 `illegal_reopen`；**先执行、后记裁决**（崩在中间可自愈，见 ADR-040）。
  - 仍未做：`unblock(reopen=…)` 没接这层（绕行路，ADR-030）；A 类集体投票的打回权威（ADR-025，v1.3）；（审批卡已于 ADR-043 补齐，封套见下）。
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
- LangGraph state（禁改项：只放执行游标 + scratch）as-built：`{dag, status(merge), outputs(merge), reopen_counts(累加), attempts(累加), unblocks(追加), escalations(追加), meta}`（`status` ∈ pending / done / failed / blocked / skipped）。**`dag` 是可写 channel**，改它 = 运行时改图（受控活图，ADR-013）。业务真相源 = SQLite checkpointer（thread_id = 实例 id）；飞书 = 投影。
- **channel 三类，写法不同**（ADR-028 / ADR-030 踩过）：`dag / status / outputs` 走保值写回（驱动层每次 `update_state` 都要原样带上，否则在飞的 super-step 里已完成的写入会被静默丢掉）；`reopen_counts / attempts` 是累加型、`unblocks / escalations` 是追加型，**一律不进保值集**（带上就每推进一拍重复累加一次：预算 3 秒变 1、审计凭空多出假记录），只在触发那一拍原样透传一次。`reopen_counts` 只由 dispatch 写。
- 固定编排器图：`START → dispatch → [Send(<executor>_worker, payload)…] 或 END`，`worker → dispatch`（唯一真环边）。worker 从 Send payload 读 node_id/dag（**Send 的 payload 是 worker 完整输入 state，不并入主 channel**）。
- human 节点纯挂起（`interrupt()` 只传数据）；飞书任务 / 卡由驱动层 `LarkFlowService` 在 `__interrupt__` 后建。`durability="sync"`。
- **派单幂等键 = `{实例}:{节点}:{轮次}`**（`attempts`），**不用 interrupt id**：中断 id 每推进一拍就换，拿它当键会让同一个人、同一件事无上限地收到新卡 / 新待办（实测）。轮次只在真被打回 / 被解除重置时 +1。键记在**本地**幂等表（ADR-033），不押飞书那 1 小时窗口。

## 引擎对外接口 as-built（驱动层方法 + CLI；**没有网络接口**）
两个面消费同一套驱动层方法：进程内直调（demo / 脚本 / 将来的意图路由层）与 `larkflow` CLI（ADR-031）。前端要的网络 API 仍待定（见〈待填〉）。

**读**：`status` 状态表 / `outputs` 产出与交付物 handle 权威登记表 / `dag_of` **这个实例自己的活图**（受控活图会让它与装配期模板不同，绝不拿 `self.dag` 当所有实例的图）/ `finished` 是否全跑完 / `blocked` 哪些门停下了 / `pending(instance, actor=None)` 卡在谁手上 / `unblock_log` 解除审计 / `escalations`（全量）与 `pending_escalations`（现在还等谁拍板）。
- `pending()` 每项 = `interrupt_id` + 该挂起中断的 payload（`node_id / label / role / assignee_role / approval_policy / signal / deliverable(_url) / upstream[] / feedback[] / reopen_candidates / reopen_default`；`reopen_default` = gate 的直接上游）。**传 `actor` 才按人过滤**（`reopen_candidates` 只留他直接点得动的，多给一个 `reopen_escalation`）；不传 = 机制层全集（运维 / 驾驶舱口径，**不表示「谁都能点」**，真正的判定在 `resume` 侧再做一次）。

**命令**：`start(instance_id, reporter, inputs, template)` / `resume_from_event(event)` / `resume(...)` / `edit_graph(instance, ops)` / `unblock(instance, node, by, reason, grant=1, reopen=None)` / `reconcile(instance)`。
- 拒绝一律返回**结构化 dict**（不抛裸异常）。`resume` 侧：`unrouted` / `unidentified_actor` / `stale`（skip 类）；`unidentified_gate` / `illegal_reopen`（机制层）/ `unauthorized_reopen` / `unauthorized_pass` / `too_many_escalations`（权限层）。`unblock` 侧：`no_such_instance` / `no_such_node` / `not_blocked` / `missing_audit`（by / reason 空即拒，审计是不变量）/ `unblock_exhausted` / `illegal_reopen`。
- 判定顺序有硬要求：**陈旧判定排在权限层之前**（escalation 会写权威 state，一条重放的旧「打回」不该在早就答完的门上凭空生出申请）。
- 越权 / 申请已提交 / 本轮申请已满，都会给**点卡的人**回一条私信（静默失败是重复点击与配额被烧光的燃料）。

**CLI**（`larkflow <子命令>`，全局 `--db / --template / --profile / --identity / --lock-timeout / --json`）：
| 子命令 | 干什么 |
|---|---|
| `serve` | 常驻：启动全实例对账 + 起事件泵 + block 到信号（`--event-key` 可重复） |
| `start --reporter <open_id> [--input k=v]… [--id]` | 起一个实例（省略 id 则生成 `lf-<UTC+8 时间戳>-<随机>`） |
| `status <实例>` | 整张图 + 谁在等 + 有没有 ⛔ |
| `pending <实例> [--actor <open_id>]` | 卡在谁手上；带 actor = 以他的视角看点得动什么 |
| `unblock <实例> <节点> --by --reason [--grant N] [--reopen 节点]…` | 解除 ⛔（ADR-030） |
| `reconcile [实例]` | 手动对账，省略实例 = 全部（与 daemon 启动同一条代码路径） |

退出码：`0` 成功 / `1` 运行期失败或被拒（含 `not_blocked`、实例不存在、对账仍有派单失败、拿不到实例锁）/ `2` 用法错。`--json` 输出机器可读结果。
**CLI 没有身份层**：`unblock --by` 只进审计不鉴权，`pending --actor` 只是「以谁的视角看」的读接口。信任边界 = 谁有宿主 shell 谁就能调。

## 飞书事件订阅 EventKey（研究证实为静态常量，不需 dev app 上下文）
- `card.action.trigger`（仅 bot）：卡片按钮点击。路由键塞按钮 `behaviors[].callback.value`，原样回传为 `action_value`（自描述 `{thread_id, interrupt_id, node_id, passed, reopen}`，与 gate 产出的 `passed`/`reopen` 逐字对齐）。⚠️ dev app 须在开发者后台开「事件与回调 → 回调配置」，否则静默零事件。
- `task.task.update_user_access_v2`（user|bot）：任务事件。完成 = `.event.event_types[]` 含 `task_completed_update`（自行 filter）；`.event.task_guid` 经关联表回映射到 `(thread_id, interrupt_id)`。
- human-produce 定稿信号：优先用 `task_complete` / `card_action` 结构化信号（ADR-021，无歧义）；发消息（message）变体走 IM 消息事件、推迟（待 dev app 定确切 EventKey，且消息无自描述封套、到中断的关联需另设计）。
- **入站归一化 as-built（`serve.normalize_event`，依据 lark-cli 内嵌 skill 字段表，真栈未验证）**：`card.action.trigger` 被 lark-cli 拍平（`operator_id` 在顶层，正是取身份的口径），但 `action_value` 是**开发者自定义值序列化成的 JSON 字符串**，必须先解开，否则每次点击都在 `_route` 里 AttributeError、整条入站通道对按钮永久失聪（而进程还活着，守护看不出问题）。`task.task.update_user_access_v2` 是 V2 信封、根在 `.event`，原样透传。**路由键一律用我们订阅的那个 EventKey**，绝不让 payload 里的同名字段改写它（payload 是外部输入）。任务通道按 `task_guid` 查关联表时**必须核对 `kind == "task"`**：关联表按 external_id 索引、不分种类，不核对就能拿一张卡的 message_id 冒充 task_guid，绕过卡片通道的身份判定（实测复现）。

## 待填（dev app 建好后验）
- 条件分支决策节点的**取值域声明字段**（护栏⑤全覆盖判据的前提，v1.3 定；v1 只校验守卫引用祖先）。
- 卡片视觉 schema（派单卡 / 门禁卡通过·打回·多选 reopen / 定稿确认卡的排版），assignee_role → open_id 通讯录解析。
- 共享协同拓扑的 docx block_id 跨 update 稳定性（v2）。
- ~~**escalation 的 approve / reject 契约**~~ → 已定并落码（ADR-040 引擎侧 + ADR-043 审批卡）。审批卡封套 = `{"kind": "escalation", "thread_id", "node_id": <门>, "seq", "decision": "approve"|"reject"}`，**不带 `interrupt_id`**；`_route` 按 `kind` 分流；裁决后 settle 卡片（ADR-037）。身份仍只取事件顶层 `operator_id`。
- 引擎读 / 命令 API（供前端，ADR-019；形态待原型后定）：
  - **读 / 命令 as-built 已列在〈引擎对外接口 as-built〉**（驱动层方法 + CLI，**没有网络接口**）。前端要的是把它包成网络 API，或退「命令走飞书原生轨」（ADR-019 命门）。
  - **读**：画布要整张 `dag`（节点 + 边 + pending 子图 + 状态），多维表格行式投影可能不够；定「整图读接口 + 返回字段 + 刷新 / 实时模型（轮询 / 推送）」。`dag_of(instance_id)` 已给出整图，缺的是传输与实时模型。
  - **改图命令 as-built（引擎侧已实现，前端形态仍待定）**：`LarkFlowService.edit_graph(instance_id, ops, *, by, reason)`，ops = `[{op: add_node, node:{…}} | {op: remove_node, id} | {op: update_node, id, set:{…}}]`；**鉴权 = owner-only + 必署名**（ADR-042：`by` 空或 `reason` 空 → `missing_audit`；`by != meta.reporter` → `unauthorized_edit`，两者都是结构化 return 不是抛异常）；引擎权威侧串校验「只触 pending 子图（挂起 human 节点并入冻结线当 running）→ 仍过 validate_template → 不用 v1 未实现语义 → 新增 tool 节点有 handler」（这四条抛 `GraphEditError` / `TemplateError`）→ `update_state` 写 dag channel + 往追加型 channel `edits["log"]` 记一条 `{by, at, reason, ops, nodes_after}`（`edit_log()` 读，被拒 / 被校验拦下的**不留痕**）→ 立刻 `invoke(None)` 推一步。**副作用（实测，见 MEMORY 2026-07-24）**：update_state 必让挂起中断换 id，故驱动层按 node 记 `interrupt_remap` 迁移链、旧卡继续有效且不重复派单。CLI 出口 `larkflow edit <实例> --ops <字面 JSON|@文件|-> --by --reason`。尚缺：乐观并发（读取时 checkpoint 版本）。
  - **改图命令（前端侧待定）**：报文 schema（op + 目标节点 + deps）；**校验在引擎权威侧**（复用 ADR-013：只改 pending / 仍是 DAG / 不删在跑节点）；乐观并发（命令带读取时 checkpoint 版本，冻结线已推进则拒、令前端重取）；命令经 checkpointer `update_state` 改 dag channel 并触发下一 dispatch。
  - **鉴权**：调用方认证（服务间 token / mTLS / 飞书身份透传择一）；命令带已验证操作人 open_id，供 gate `approval_policy=any/all` 按人归因去重；最小权限（前端只能对 pending 子图与本人有权的 gate 发命令）。**as-built 已有的那一半**：卡片 / 任务通道的裁决已按 ADR-023 / ADR-032 在引擎权威侧判身份（actor 取自事件顶层，不信封套）。**仍缺**：调用方认证本身（CLI 与进程内直调零鉴权）、`edit_graph` 的鉴权与乐观并发、`unblock` 的鉴权。
  - **cards 与 app 双输入面**：同一次决策跨两面去重（幂等键口径见上文「派单幂等键 = 实例:节点:轮次」，**不要再用 interrupt id**）；app 命令复用卡片自描述封套，引擎单处理器消费，身份仍只认引擎侧已验证的操作人。
