# SPEC · larkflow

> **As-built / Legacy Prototype。** 本文只描述当前 Python 原型可以执行的契约，不是目标产品规范。目标产品以 [PRD.md](PRD.md)、[ARCHITECTURE.md](ARCHITECTURE.md) 和 [DAG_TEMPLATE_SPEC.md](DAG_TEMPLATE_SPEC.md) 为准。
>
> 当前 legacy 契约部分定型：节点契约含投票 / 分支 / 打回权限，引擎契约和产出协议已跑通。Target 已另行实现 PostgreSQL、Template v0.2 与窄 Personal Agent Edge Proof，但仍没有三级父子实例或 Capability Registry。
>
> 2026-07-25：**legacy 对外契约的 as-built 面从「驱动层 Python 方法」扩到「CLI 子命令」**（ADR-031），见〈引擎对外接口 as-built〉。legacy 仍没有网络接口；2026-08-02 新增的 `/edge/v1` 只服务 Target Edge Proof。
>
> DAG Template 的产品目标契约见 [DAG_TEMPLATE_SPEC.md](DAG_TEMPLATE_SPEC.md)。下文“模板节点契约”描述的是当前引擎可执行的 legacy compact form，不代表 v0.1 已落码。

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

## legacy 引擎对外接口 as-built（驱动层方法 + CLI；**没有网络接口**）
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

### Target CLI 与 Task 入站 as-built

Target 使用独立 `larkflow-target` CLI，不复用上述 legacy 驱动层。控制面增加 `template-create / template-add-version / template-enable / template-disable / template-delete / template-list / template-show / create-from-template / preview / reconcile-projections / reconcile-completions / reconcile-instance-completion`，并保留 `migrate / create / confirm / show / submit-human` 和五类 Worker 的单步、常驻命令。`interact-once / interact` 专门处理凭据侧 IM 命令与人员分工交互；这些都是本机运维入口，不是公开网络 API。

六条 Target Worker 连接使用 PostgreSQL 通知缩短耐久阶段之间的空闲等待。服务启动时先建立专用监听连接，再执行首次队列扫描；`workflow_outbox_events`、`workflow_inbox_events`、`workflow_im_commands` 与 `workflow_role_binding_actions` 的可认领状态在事务提交后向固定 channel 发送空通知。通知不携带 tenant、人员、消息、Instance、Node 或任何业务状态，也不替代数据库 claim。连接、监听或等待失败时，Worker 继续按原有有界退避扫描，所以通知丢失只影响延迟，不影响最终处理。

Interactive Worker 依次访问 IM 命令验证、IM 回复、人员分工卡创建、人员分工回调验证与人员分工回复五条车道。配置契约要求 `LARKFLOW_TARGET_INTERACTIVE_CLAIM_LIMIT=1`，其他值拒绝启动；并行度只由独立进程副本数决定。开发拓扑固定两个副本，Projection 不再读取 `LARKFLOW_TARGET_ENABLE_IM_COMMANDS`，也不再认领上述五条车道。所有授权、幂等、租约、重试和数据库状态机保持原契约，双副本不能绕过服务端校验。

Target 自动节点按工作契约 kind 路由。Agent 当前只接受 `work.agent.kind=llm.generate`。Tool 由 `ToolExecutorRouter` 按 `work.tool.kind` 选择 adapter；`content.check` 读取直接依赖正文，接受 `min_chars`、`max_chars` 与 `required_terms`，返回 `verdict`、`evidence`、`suggestion`、`char_count`、`missing_terms`、`source` 和稳定 `request_id`。配置或输入错误使当前 Attempt 显式失败，未知 kind 在 claim 前保持未认领。

`create-from-template` 只接受 enabled 模板，以最新不可变版本解析参数和 `owner_role -> person_id` 绑定，生成含 `template_version_id` 与 `locked` 的完整 Snapshot。`preview` 仅允许 Instance Owner 读取并重新校验 draft，不写审计、不改变状态；`confirm` 仍需显式调用。

`project` 在进入 Outbox 循环前调用与 `reconcile-projections` 相同的全量对账路径。对账按 Instance ID 分页，默认每批 100，可用 `LARKFLOW_TARGET_PROJECTION_RECONCILE_BATCH_SIZE` 调整。它只对当前 `waiting_human` 节点补建缺失 Task；已有 Task 先只读查询，只有 Task v2 明确返回资源不存在码 `1470404` 才使用下一 repair generation 的稳定 client token 重建，并以旧 GUID 和旧幂等键为并发条件原子更换 Projection 绑定。任意其他读取失败只记入结构化错误并继续其他实例，不重建。终态 Human 节点不补发历史 Task；若已有 Projection 仍未完成，对账会收口其完成状态。

Target Task 完成发现以周期状态轮询为可靠路径。`project` 启动后立即扫描，此后默认每 30 秒读取当前 `waiting_human` 节点绑定的 Task；周期与每批实例数分别由 `LARKFLOW_TARGET_COMPLETION_POLL_SECONDS` 和 `LARKFLOW_TARGET_COMPLETION_POLL_BATCH_SIZE` 控制。只在 Task 详情明确为完成且存在完成时间时，以 tenant、Projection、Task GUID 和完成时间派生稳定信号 ID，写入 PostgreSQL Inbox。`reconcile-completions` 可显式执行同一次扫描。单个 Task 读取失败只进入结构化报告，不阻塞其他节点。

`task.task.update_user_access_v2` 中包含 `task_completed_update` 的事件仍可作为低延迟入口，但不再是 Human 节点推进的可靠性前提。轮询信号和原始事件都只提供 Task GUID 与变化提示，不作为 actor 证明。服务端必须重新读取 Task 详情，并校验以下条件：

- Task GUID 对应当前 Human Attempt 的 Projection。
- Task 绑定字段与 Projection 的稳定幂等键一致。
- Task 由当前企业应用创建，为 `mode=1`，且只有节点 Owner 一个 assignee。
- Task 状态已完成，完成人集合严格等于该 Owner。
- Node 仍是 `waiting_human`，Attempt 仍是当前轮次。

通过后以 Owner 作为经服务端核验的 actor 调用同一 Human 提交领域命令，入口信号 ID 同时作为 Inbox 幂等键与审计关联。旧的无绑定任务、`mode=2` 任务、非当前 Attempt 或非 Owner 完成均不能推进领域状态。

凭据侧详情读取失败或完成状态暂不可见时，按 `LARKFLOW_TARGET_INBOUND_RETRY_BASE_SECONDS` 到 `LARKFLOW_TARGET_INBOUND_RETRY_MAX_SECONDS` 做指数退避。`LARKFLOW_TARGET_INBOUND_VERIFICATION_MAX_ATTEMPTS` 默认 24；达到预算仍无法验证时，Inbox 进入 `exhausted` 终态，写入 `processed_at`、`outcome=exhausted:verification_attempts`、`failure_stage=verification` 与最后错误，不生成 verified payload，也不允许领域 Worker 认领。验证日志包含 `exhausted` 计数，运维必须对非零值告警并人工调查，不能静默丢弃。

### Target 飞书 IM 命令与完成投影 as-built

Target 订阅 `im.message.receive_v1`，处理以 `/larkflow` 开头的文本，也接受群聊中只有认证 mention token 位于命令前的 `@机器人 /larkflow ...` 形式。桥接层同时接受飞书原始 V2 信封和 lark-cli 拍平输出：原始事件的 `content` 是 JSON 字符串，拍平输出的 `content` 是普通文本，两者必须归一为同一个命令信号。桥接层只保存 mention 的 `key` 与 `open_id`，不保存显示名称；其他消息不进入 Target 命令 Inbox。

- `/larkflow help`：返回当前十一个命令的用法。
- `/larkflow start <template_id> [JSON对象] [role=@成员 ...]`：以 tenant 和 message ID 派生稳定 Instance ID，验证发送者属于当前企业且状态活跃，再把发送者绑定为 Instance Owner。显式角色绑定使用 lower snake case 角色名和本条消息的 mention key；凭据侧通过企业目录验证每名被引用人员仍在当前 tenant 且状态活跃，领域侧再把 mention key 映射到冻结 Snapshot。原始文本中的 open_id、显示名称或不存在于 mention 元数据的 token 均不能授权。未显式绑定的模板角色继续归发送者。命令只创建草稿并返回确认命令，不自动启动。
- `/larkflow draft` 与 `/larkflow draft <JSON定义> [role=@成员 ...]`：两种形式都不查找模板版本，只创建 `template_version_id=NULL`、`locked=false` 的 Instance Snapshot 草稿，仍需独立 `confirm` 才启动。带 JSON 的高级入口沿用严格解析、100 节点上限、完整 work 契约与 mention Owner 验证，拒绝重复键、非有限数、模型服务配置和 `personal.readonly`。裸命令打开 Card 2.0 引导，必填目标、可选背景，并从冻结的活跃人员快照中选择一名协作者。回调只接受原发起人、原消息、原卡片和服务端允许的字段；协作者在领域处理前再次验证。中央 Agent 只能生成最多八个 `human / agent` 节点，Owner 角色只能是 `requester / collaborator`，Agent 节点后必须直接有人类复核。服务端覆盖模型返回的 schema 与原始输入，并重新校验完整 Snapshot。首个候选未通过确定性校验时最多重生成一次，第二个候选仍不合法就拒绝。动作落库后由无飞书凭据的 Draft Generation Worker 认领；首次无按钮反馈使用卡片回调 token，`generating / repairing` 阶段和最终结果按原消息 ID 更新，所有卡片在更新前后都保持 `config.update_multi=true`。最终回复等待当前进度 revision 结算，旧 revision 不能覆盖终态。该独立拓扑、旧卡修复和新卡完整收口均已在开发测试组织真实通过。
- `/larkflow confirm <instance_id>`：重新校验发送者与草稿 Owner，确认并启动实例。
- `/larkflow status <instance_id>`：重新校验发送者后，仅允许 Instance Owner 读取流程状态。实例不存在与非 Owner 统一返回“实例不存在或你无权查看”，避免枚举。回复最多列出 20 个节点，每个可变字段最多 120 个字符；只包含状态、进度、节点、executor 和相对责任人，不包含结果正文或人员 ID。该命令只读，不追加领域审计，也不改变 aggregate version。
- `/larkflow list`：重新校验发送者后，只查询该发送者作为 Instance Owner 的最近实例。仓储按 `created_at DESC, id DESC` 排序，命令最多展示十条，并额外查询一条用于提示仍有更多结果。每条只包含 Instance ID、目标摘要、实例状态和完成节点数，不读取完整聚合，不包含节点结果或人员 ID。该命令只读，不追加领域审计，也不改变 aggregate version。
- `/larkflow restart <instance_id> <node_key>`：重新校验发送者为 Instance Owner，服务端计算目标节点及全部可达下游，并持久化默认 15 分钟有效的只读预览。回复列出完整影响集合和当前 Attempt；预览绑定 tenant、Instance、actor、目标节点、影响集合、aggregate version 与 `graph_revision`，不改变 Instance、Node、Attempt 或领域审计。
- `/larkflow restart-all <instance_id>`：重新校验发送者为 Instance Owner，以显式 `instance` scope 创建完整实例重启预览。影响集合固定为拓扑排序后的全部节点，预览的节点键为空；它不以特殊节点值模拟完整重启，也不改变领域状态。
- `/larkflow restart-confirm <preview_id>`：只允许创建预览的当前 Instance Owner 确认。服务端重新校验 scope、有效期、aggregate version、`graph_revision` 和影响集合，在一个 PostgreSQL 事务内取消受影响的活动旧 Attempt、清除 claim、创建新 Attempt、消费预览并写入审计与投影 outbox。节点 scope 只把目标节点置为 `ready`；instance scope 把每个根节点置为 `ready`，其余节点置为 `pending`。旧 Attempt、结果与质量记录保留；重复确认返回已执行状态，不再增加版本、Attempt、Task 或审计。过期或状态漂移的预览必须重新创建。
- `/larkflow edit <instance_id> <JSON操作数组>`：只允许当前 Instance Owner 对 `running` 且未锁定实例创建未来区域编辑预览。操作数组只接受 `add_node / update_node / remove_node`；既有节点必须仍为没有任何执行痕迹的 `pending / ready` 当前 Attempt。服务端重新校验完整 DAG、Owner 与工作定义，并把规范化操作、增删改集合、aggregate version、`graph_revision` 和候选 Snapshot SHA-256 写入默认 15 分钟有效的耐久预览。客户端提交的身份、revision 或影响集合不是授权事实。
- `/larkflow edit-confirm <preview_id>`：只允许创建预览的当前 Instance Owner 确认。服务端重新执行预览中的操作，拒绝过期、版本漂移、revision 漂移或候选图语义漂移，并在一个 PostgreSQL 事务内保存聚合、消费预览、递增一次 `graph_revision`、追加一条审计及必要 outbox。新增节点创建 Attempt 1，更新节点保留并刷新未开始的当前 Attempt，删除节点只移除未开始 Node 与 Attempt；Template 和已执行历史保持不变。重复确认只回读已应用状态。

节点重启、完整实例重启和运行中未来区域编辑都已实现。可重启实例状态为 `running / done / failed`。节点 scope 的目标节点状态必须为 `running / waiting_human / done / failed`，且目标节点直接依赖必须完成；失败实例若存在影响集合之外的失败节点会拒绝节点重启，避免把仍有未处理失败的实例错误恢复为 running。完整实例 scope 覆盖全图，因此不需要外部失败节点例外。图编辑只允许 `running` 实例的未开始区域，已开始节点和锁定实例都拒绝。编辑代码、离线套件、一次性 PostgreSQL 竞争、开发服务器部署和 Owner 真实飞书闭环已经验证；真实命令也拒绝了冻结线修改、成环图和状态漂移后的陈旧预览。开发应用发布所需通讯录数据范围后，当前登录用户对测试成员持有实例发送的真实 `/larkflow edit` 也被统一拒绝，回复成功发送，实例没有新增预览、图修订或编辑审计。

原始 event ID 与 message ID 都参与去重；验证、领域执行和回复各自使用可认领的耐久状态，不依赖单个进程在线。mention 元数据随命令写入 PostgreSQL，凭据侧与领域侧读取同一耐久记录。客户端 payload 中的身份、Owner 或状态不作为授权事实。命令回复、Agent / Tool 节点结果、完成文档与最终通知都由服务端状态生成，并以稳定幂等键落为 Projection。Instance 进入 `done` 后，Projection Worker 汇总节点结果创建 Docx，再向 Owner 发送含文档链接的最终消息。首次完成的幂等键保持原格式；重启后的完成投影按当前终端节点 Attempt 编号分代，因此同一实例再次完成会产生新的文档和最终通知，同时保留旧轮次 Projection。

跨人员角色绑定、`collaborative_agent_review` 双角色模板和 migration `0013_im_command_mentions` 已完成开发部署与真实群聊验收。单聊中，若模板含发送者之外的未绑定角色，`start` 会返回 Card 2.0 人员选择表单；候选人是凭据侧冻结的有界活跃成员快照，回调只接受创建命令的操作人和快照内人员，经目录再次验证后由领域侧幂等创建一个草稿。migration `0014_role_binding_cards` 保存候选快照、回调验证、领域处理、卡片更新和文本回复状态。动作耐久落库后，原卡片先替换为蓝色无按钮“处理中”；成功后再变为绿色已确认状态，选择器和按钮全部撤下。migration `0016_role_card_single_action` 增加 `is_canonical` 与部分唯一索引，一张卡片只有一个有效动作；迁移前的重复回调保留为非 canonical 历史，不物理删除。

自动 Agent 或 Tool 节点的当前 Attempt 失败后，Projection 向该节点唯一 Owner 发送 Card 2.0，只显示稳定的 `error_code`，不显示原始 `error_message`。卡片提供“重新执行”和“人工接管”两个显式操作：

- 恢复回调的操作人只从飞书顶层认证字段取值，卡片 payload 只能表达 action 和目标快照，不能自证身份或权限。
- lark-cli 真实回调允许 `action_value` 为 JSON 字符串且省略 `action_name`；桥接层先归一化动作值，若名称存在则必须交叉一致。事件时间统一接受秒、毫秒或微秒精度。以上兼容只处理报文形状，不替代服务端成员、Owner、版本和 Attempt 授权。
- 凭据侧重新确认操作人是当前企业活跃成员；领域侧只允许当前节点 Owner，并精确比较 Instance version、Node version 和 Attempt 编号。实例已变化、节点已重启、旧 Attempt 或非 Owner 操作均 fail closed。
- “重新执行”为目标节点及可达下游创建新 Attempt，语义与受控节点重启一致。“人工接管”只为失败节点创建新 `waiting_human` Attempt，然后复用当前 Human Task、凭据侧完成验证和领域提交链路。
- 原失败 Attempt、结果、错误代码和审计保留。新操作成功后原卡片收口为无可点击按钮的结果卡，重复回调只返回首次结果。

人员选择卡与失败恢复卡共享两阶段视觉契约。桥接器严格校验回调形状并耐久插入后，将动作的可认领时间延后 10 秒作为崩溃兜底；随后用最长 3 秒的直接卡片更新显示“处理中”，无论更新成功或失败都立即把动作释放为可认领。这样可以避免最终绿色或橙色状态先写入后，又被较慢的蓝色处理中覆盖。若桥接进程在释放前崩溃，10 秒兜底保证动作仍会进入 Worker。快速回写失败不得回滚已落库动作，最终 Worker 仍会继续处理和收口。migration `0017_card_feedback_metrics` 在两类动作表保存 `feedback_status`、`feedback_elapsed_ms` 与 `feedback_completed_at`；单调计时从有效回调被接受开始，覆盖耐久插入和直接卡片更新，直到更新调用返回。该指标是服务端反馈区间，不包含客户端渲染时间。任何批次 Worker 都必须在每条工作实际完成后单独读取时钟并持久化完成时间，禁止把批次开始时间写成所有条目的验证、处理或回复完成时间。

migration `0015_recovery_cards` 为耐久命令保存卡片更新 token，`0016_role_card_single_action` 限制同一人员选择卡只有一个 canonical 动作，`0017_card_feedback_metrics` 增加首个服务端反馈指标。长期开发库已应用这三份 migration；开发真栈验收覆盖两个连续重试、人工接管、真实 Human Task 完成、完成投影和失败历史保留。提交 `a506e7d` 修正批次 Worker 的逐项完成时间后，五次真实人员选择卡点击均只接受一个 canonical 动作并创建一个草稿。首反馈、凭据验证、领域处理和最终回复的 P50 / P95 分别为 0.991 / 1.274 秒、4.757 / 12.358 秒、4.941 / 12.582 秒和 12.670 / 19.298 秒。前四次是突发样本，第五次是隔离样本，后者全链路为 4.044 秒；五张原卡片均为已确认终态且不含操作控件。提交 `a506e7d` 之前公布的首反馈数据仍有效，但身份校验、领域处理和最终回复精确耗时因使用批次开始时间而废止。以上证据只适用于开发环境与测试组织，也不把服务端反馈耗时表述为用户点击到客户端渲染的耗时。

`reconcile-instance-completion <instance_id>` 是显式的单实例恢复命令。它只接受已完成实例，只补齐缺失的完成文档或最终通知；重复执行返回 no-op，不批量扫描历史实例，也不复制已存在的外部资源。

### Target Personal Agent Edge Proof v0 的 `/edge/v1` HTTP

该接口是设备控制边界，不是业务前端 API。`larkflow-edge-gateway serve` 强制监听 loopback，远程使用必须由外部 HTTPS 反向代理终止 TLS；客户端拒绝非 loopback 的明文 HTTP、重定向、URL 内凭据和带路径的 server URL，并默认 `trust_env=False`。

| 方法与路径 | 认证 | 语义 |
|---|---|---|
| `POST /edge/v1/devices/pair` | 一次性配对码 | 绑定设备名和固定 `personal.readonly` capability，原始设备 secret 只返回一次 |
| `POST /edge/v1/leases/claim` | Bearer 设备凭据 | 最多领取一个本人拥有且 kind 匹配的 Agent 节点，可有界长轮询，无工作返回 204 |
| `POST /edge/v1/leases/renew` | Bearer 设备凭据 | 在当前租期内延长同一 Attempt，不改变 Worker、token 或节点版本 |
| `POST /edge/v1/leases/complete` | Bearer 设备凭据 | 回传有大小上限的 JSON 结果，服务端重验 Owner、capability、Attempt、版本、Worker、token 与租期 |
| `POST /edge/v1/leases/fail` | Bearer 设备凭据 | 显式报告已领取工作的领域失败；本机适配器基础设施异常不会调用它 |

配对码有效期最多 1 小时且只能消费一次。服务端只保存 SHA-256 purpose-separated hash，不保存原始配对 secret 或设备 secret。设备撤销后认证、续租和结果回传均拒绝。macOS 客户端的 `auto` 凭据模式默认把设备密钥保存为当前用户登录 Keychain 中 service `com.larkflow.edge.device`、account `default` 的 generic password；密钥不进入命令行参数、环境变量、日志或磁盘元数据。`~/.config/larkflow/edge-device.json` 仍以 `0600` 保存 `credential_store=keychain`、server URL 与 device ID，并拒绝符号链接。非 macOS 客户端和显式 `--credential-store file` 保留当前用户所有的 `0600` 完整凭据文件兼容路径；硬件密钥存储尚未实现。

本机 `larkflow-edge` 提供 `pair`、`credential-migrate`、`doctor`、`run-once` 与前台 `serve`。`credential-migrate` 先把旧文件中的密钥写入 Keychain 并回读完整设备凭据；只有回读一致时，`--delete-source` 才把原明文文件原子替换为非敏感 Keychain 元数据。迁移校验或替换失败会回滚本次新建的 Keychain 项，旧文件保持可恢复。`doctor` 只在本机加载并校验所选凭据、检查 Codex 命令和报告传输模式，不连接中央节点、不领取工作，也不输出 server URL、device ID 或 secret。两种执行命令都显式固定工作区并启动 `codex exec --sandbox read-only --ephemeral --ignore-user-config --skip-git-repo-check`；`serve` 使用最长 25 秒的有界长轮询持续领取，同一凭据通过 POSIX 非阻塞文件锁限制为一个本机 Worker。瞬时网络与执行错误使用带抖动的有界指数退避，撤销或无效设备凭据立即停止；结构化日志提供启动、应用心跳、续租、单任务结果、故障和停止摘要。SIGINT 或 SIGTERM 会传递停止信号，续租失败也会取消整个 Codex 进程组；两种情况均不提交可能失去租约的结果。本机执行器异常仍不调用领域 `fail`。

macOS 开发试用安装由独立 `larkflow-edge-manager` 管理。推荐入口 `install --bundle <dir> --manifest-sha256 <hex>` 要求通过独立可信渠道取得完整 64 位 manifest SHA-256；manifest 固定 macOS 架构、Python 实现与次版本、完整 source commit、主 wheel、manager 和全部 wheel 的包名、版本、大小与 SHA-256。manager 在修改安装目录前验证精确文件集、拒绝额外文件、缺失文件、符号链接、重复包和目标漂移。离线 bundle 必须携带 pip 26.1.2 或更高且低于 27 的 bootstrap wheel，manager 先以哈希验证后的本地 wheel 离线升级 pip，再强制 `--no-index --only-binary=:all:` 安装应用。`install --wheel <path> --sha256 <hex>` 继续作为可能联网解析依赖的兼容开发入口。release ID 固定为 `<package-version>-<sha12>`；直接 wheel 使用 wheel SHA-256，离线 bundle 使用覆盖全部依赖和 manager 的 manifest SHA-256，避免相同主 wheel 的不同依赖集合复用旧环境。虚拟环境必须直接创建在最终 release 路径，`pip check` 与安装态 CLI 验证成功前不得切换。`status` 只输出非敏感安装状态；`rollback` 交换 `current / previous`，保留 release 内容。稳定命令为 `~/.local/bin/larkflow-edge` 与 `~/.local/bin/larkflow-edge-manager`。manager 拒绝 root、符号链接目录和已有的无关同名命令，不读取或迁移 Keychain，不注册后台服务，也不提供联网自动更新。当前 bundle 未签名、未公证，依赖面未最小化，不是正式员工分发件。

`serve` 表示用户主动启动并保持可见的会话，不提供操作系统 daemon、开机启动或隐藏后台驻留。启动时拒绝文件系统根目录、用户主目录以及包含设备凭据的工作区。子进程环境使用最小 allowlist，不继承任意 API key、代理、SSH agent、Edge、Target 或飞书变量。显式 `--inherit-loopback-proxy` 只传递无用户名和密码的 loopback HTTP / HTTPS / SOCKS URL，远程或带凭据代理仍丢弃。这限制文件写入、会话持久化和环境凭据暴露，但当前没有证据证明目录级读取被限制在所选工作区；恶意任务输入仍可能诱导读取其他可读文件，也不代表模型调用无数据外发风险。

## 飞书事件订阅 EventKey（研究证实为静态常量，不需 dev app 上下文）
- `card.action.trigger`（仅 bot）：卡片按钮点击。路由键塞按钮 `behaviors[].callback.value`，原样回传为 `action_value`（自描述 `{thread_id, interrupt_id, node_id, passed, reopen}`，与 gate 产出的 `passed`/`reopen` 逐字对齐）。⚠️ dev app 须在开发者后台开「事件与回调 → 回调配置」，否则静默零事件。
- `task.task.update_user_access_v2`（user|bot）：任务事件。完成 = `.event.event_types[]` 含 `task_completed_update`（自行 filter）；`.event.task_guid` 经关联表回映射到 `(thread_id, interrupt_id)`。
- human-produce 定稿信号：优先用 `task_complete` / `card_action` 结构化信号（ADR-021，无歧义）。Target 的 `im.message.receive_v1` 已用于自描述 `/larkflow` 控制命令；把任意普通消息关联到某个 Human 中断并当作业务结果仍未实现。
- **入站归一化 as-built（`serve.normalize_event`，依据 lark-cli 内嵌 skill 字段表，真栈未验证）**：`card.action.trigger` 被 lark-cli 拍平（`operator_id` 在顶层，正是取身份的口径），但 `action_value` 是**开发者自定义值序列化成的 JSON 字符串**，必须先解开，否则每次点击都在 `_route` 里 AttributeError、整条入站通道对按钮永久失聪（而进程还活着，守护看不出问题）。`task.task.update_user_access_v2` 是 V2 信封、根在 `.event`，原样透传。**路由键一律用我们订阅的那个 EventKey**，绝不让 payload 里的同名字段改写它（payload 是外部输入）。任务通道按 `task_guid` 查关联表时**必须核对 `kind == "task"`**：关联表按 external_id 索引、不分种类，不核对就能拿一张卡的 message_id 冒充 task_guid，绕过卡片通道的身份判定（实测复现）。

## 待填（dev app 建好后验）
- 条件分支决策节点的**取值域声明字段**（护栏⑤全覆盖判据的前提，v1.3 定；v1 只校验守卫引用祖先）。
- 卡片视觉 schema（派单卡 / 门禁卡通过·打回·多选 reopen / 定稿确认卡的排版），assignee_role → open_id 通讯录解析。
- 共享协同拓扑的 docx block_id 跨 update 稳定性（v2）。
- ~~**escalation 的 approve / reject 契约**~~ → 已定并落码（ADR-040 引擎侧 + ADR-043 审批卡）。审批卡封套 = `{"kind": "escalation", "thread_id", "node_id": <门>, "seq", "decision": "approve"|"reject"}`，**不带 `interrupt_id`**；`_route` 按 `kind` 分流；裁决后 settle 卡片（ADR-037）。身份仍只取事件顶层 `operator_id`。
- 引擎读 / 命令 API（供前端，ADR-019；形态待原型后定）：
  - **读 / 命令 as-built 已列在〈legacy 引擎对外接口 as-built〉**（驱动层方法 + CLI，**没有业务网络接口**）。Edge HTTP 只允许设备领取窄 Agent 工作，不能作为前端读写 DAG 的替代。前端要的是把业务命令包成网络 API，或退「命令走飞书原生轨」（ADR-019 命门）。
  - **读**：画布要整张 `dag`（节点 + 边 + pending 子图 + 状态），多维表格行式投影可能不够；定「整图读接口 + 返回字段 + 刷新 / 实时模型（轮询 / 推送）」。`dag_of(instance_id)` 已给出整图，缺的是传输与实时模型。
  - **改图命令 as-built（引擎侧已实现，前端形态仍待定）**：`LarkFlowService.edit_graph(instance_id, ops, *, by, reason)`，ops = `[{op: add_node, node:{…}} | {op: remove_node, id} | {op: update_node, id, set:{…}}]`；**鉴权 = owner-only + 必署名**（ADR-042：`by` 空或 `reason` 空 → `missing_audit`；`by != meta.reporter` → `unauthorized_edit`，两者都是结构化 return 不是抛异常）；引擎权威侧串校验「只触 pending 子图（挂起 human 节点并入冻结线当 running）→ 仍过 validate_template → 不用 v1 未实现语义 → 新增 tool 节点有 handler」（这四条抛 `GraphEditError` / `TemplateError`）→ `update_state` 写 dag channel + 往追加型 channel `edits["log"]` 记一条 `{by, at, reason, ops, nodes_after}`（`edit_log()` 读，被拒 / 被校验拦下的**不留痕**）→ 立刻 `invoke(None)` 推一步。**副作用（实测，见 MEMORY 2026-07-24）**：update_state 必让挂起中断换 id，故驱动层按 node 记 `interrupt_remap` 迁移链、旧卡继续有效且不重复派单。CLI 出口 `larkflow edit <实例> --ops <字面 JSON|@文件|-> --by --reason`。尚缺：乐观并发（读取时 checkpoint 版本）。
  - **改图命令（前端侧待定）**：报文 schema（op + 目标节点 + deps）；**校验在引擎权威侧**（复用 ADR-013：只改 pending / 仍是 DAG / 不删在跑节点）；乐观并发（命令带读取时 checkpoint 版本，冻结线已推进则拒、令前端重取）；命令经 checkpointer `update_state` 改 dag channel 并触发下一 dispatch。
  - **鉴权**：调用方认证（服务间 token / mTLS / 飞书身份透传择一）；命令带已验证操作人 open_id，供 gate `approval_policy=any/all` 按人归因去重；最小权限（前端只能对 pending 子图与本人有权的 gate 发命令）。**as-built 已有的那一半**：卡片 / 任务通道的裁决已按 ADR-023 / ADR-032 在引擎权威侧判身份（actor 取自事件顶层，不信封套）。**仍缺**：调用方认证本身（CLI 与进程内直调零鉴权）、`edit_graph` 的鉴权与乐观并发、`unblock` 的鉴权。
  - **cards 与 app 双输入面**：同一次决策跨两面去重（幂等键口径见上文「派单幂等键 = 实例:节点:轮次」，**不要再用 interrupt id**）；app 命令复用卡片自描述封套，引擎单处理器消费，身份仍只认引擎侧已验证的操作人。
