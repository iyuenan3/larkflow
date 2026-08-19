# SPEC · larkflow

> **As-built / Legacy Prototype。** 本文只描述当前 Python 原型可以执行的契约，不是目标产品规范。目标产品以 [PRD.md](PRD.md)、[ARCHITECTURE.md](ARCHITECTURE.md) 和 [DAG_TEMPLATE_SPEC.md](DAG_TEMPLATE_SPEC.md) 为准。
>
> 当前 legacy 契约部分定型：节点契约含投票 / 分支 / 打回权限，引擎契约和产出协议已跑通。Target 已另行实现 PostgreSQL、Template v0.2 与窄 Personal Agent Edge Proof，但仍没有三级父子实例或 Capability Registry。
>
> 2026-07-25：**legacy 对外契约的 as-built 面从「驱动层 Python 方法」扩到「CLI 子命令」**（ADR-031），见〈引擎对外接口 as-built〉。legacy 仍没有网络接口；Target 当前有只服务 Edge Proof 的私有 `/edge/v1`，以及 2026-08-06 新增的 loopback Owner 只读 `/console`，两者都不是公开网络 API。
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
- 交付物 = 飞书 handle（Docx document_id / 云盘 file token），模型 `(容器, region)`。面向人的文本交付物默认使用原生飞书 Docx；Markdown 仅作为写入与读取正文的交换格式，以及历史 handle 的兼容类型。
- **handle 权威登记（ADR-020）**：produce 末步把物化得到的 handle（+ region + type）写进 `state.outputs[node_id]`，它是交付物 handle 的**唯一权威登记表**；节点 schema 的 `deliverable.container` 是活图 dag 里的声明位 / create 后回填指针，非第二份权威。下游经 `outputs[dep]` 取 handle 再 fetch 正文；reopen 不清 outputs，故未重算旁支跨 overwrite 复用旧 handle。
- **produce**：文本首跑使用 `docs +create --doc-format markdown` 创建原生 Docx，重跑使用 `docs +update --command overwrite` 复用同一 document_id；二进制才走 `drive +upload`。
- **consume**（下游 llm 读上游正文）：Docx 使用 `docs +fetch --doc-format markdown`；升级前登记的 `type=markdown` handle 继续走历史 `markdown +fetch/+overwrite`，不迁移或覆盖旧对象。
- **审计 / 版本**：Docx 使用 `docs +history-*`，二进制使用 `drive +version-history`（引擎不自建版本）。
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

### Target Owner 中央控制台 v1

`larkflow-console` 使用与 Target 相同的 PostgreSQL 仓储。Owner 读取服务、`ConsoleActionService` 与 `ConsoleTaskService` 分离装配，写入服务只把当前服务端鉴权主体映射到既有 `WorkflowService` 命令，不实现第二套领域状态机。管理员仍只额外装配受限的其他会话撤销服务。HTTP 服务强制绑定 loopback。`/console/`、`/console/app.js` 和 `/console/styles.css` 提供静态页面；`GET /console/api/v1/auth` 返回当前鉴权模式、主体关系和安全能力对象，其中 `capabilities.attachment_planning` 只表示当前 Console 同时配置了附件存储且允许 `internal` 文本进入规划模型，不返回路径、策略来源或凭据；`GET /console/api/v1/instances?limit=<1..100>` 返回当前 Owner 的有界实例摘要和 `attention`；`GET /console/api/v1/instances/<instance_id>` 返回同一 Owner 实例的节点、历史 Attempt、最近审计和服务端提炼的 `insights`。`insights.reworked_nodes` 只列当前 Attempt 大于 1 的节点；`insights.latest_restart` 从最近 200 条审计中返回最近一次受控节点或实例重启的时间、操作者关系、目标与已验证影响节点，不返回原始 Audit payload。`POST /console/auth/logout` 只注销当前 Console 会话，不修改领域聚合。未知路由返回 404。

`attention` 是由当前 PostgreSQL 聚合即时派生的 Owner 待处理读模型，不单独保存状态。仓储先按 tenant、Instance Owner、`created_at DESC` 和 `limit` 限定最近实例，再只连接失败节点和归当前 Owner 的 `waiting_human` 节点。服务端生成四类有界提示：失败节点或失败实例为 `recover_failed`，当前 Owner 的 Human 待办为 `complete_human`，暂停实例为 `resume_flow`，草稿为 `confirm_draft`。失败项优先，其后依次为本人 Human、暂停与草稿；多个失败节点统一建议完整实例重启。DTO 不返回人员 ID、原始错误、claim、审计 payload、凭据或可执行飞书命令。普通 Human 待办打开工作台任务详情；草稿确认与继续直接调用中央节点；失败恢复先生成重启预览。

Owner 流程写接口均为无请求体 POST：`/console/api/v1/instances/<instance_id>/confirm`、`pause` 与 `resume` 直接执行；`cancel-preview` 返回当前 aggregate version 与完整影响集合，`cancel-confirm/<expected_instance_version>` 才确认取消；`restart-preview` 和 `nodes/<node_key>/restart-preview` 创建既有耐久 RestartPreview，`/console/api/v1/restart-previews/<preview_id>/confirm` 才创建新 Attempt。取消和重启预览不修改领域聚合。确认时重新校验 tenant、Instance Owner、当前状态、aggregate version 和既有预览约束；跨 Owner 与不存在实例统一返回 404，状态冲突、陈旧或过期预览返回安全的 409。重复确认、暂停、继续、取消与重启使用领域层既有幂等语义，不重复增加审计或 Attempt。

受控 DAG 画板使用 `POST /console/api/v1/instances/<instance_id>/graph-edit-preview` 创建结构编辑预览。请求体必须且只能是 `{"operations":[...]}`，操作沿用 `add_node / update_node / remove_node` 领域契约；服务端把 `owner_person_id=__current_user__` 翻译为当前认证主体，不接受浏览器声明任意当前用户身份。成功响应只返回预览 ID、当前与目标 graph revision、增删改节点摘要和过期时间。`POST /console/api/v1/graph-edit-previews/<preview_id>/confirm` 无请求体，只消费既有预览并返回最终 revision 与受影响节点。两条接口都重新校验 tenant、当前 Instance Owner、状态、完整 DAG、工作定义、aggregate version 和候选 Snapshot。草稿态允许修改完整定义，但确认只更新 Snapshot 与 graph revision，不创建 NodeInstance、Attempt、outbox 或外部资源；后续草稿确认启动才独立物化运行时。运行中实例仍重新校验冻结执行前沿，只能修改没有执行痕迹的未来节点，不能依据客户端影响集合执行。

新增节点的内部 key 不再由普通页面用户填写。`ConsoleActionService` 在认证和加载当前 Snapshot 后，根据标题的 ASCII slug 生成 key；标题无法形成 slug 时使用 `human_step / agent_step / tool_step`，冲突时增加数字后缀。页面可在新增操作中提交仅供 Console 翻译层使用的 `insert_before` 节点列表。服务端把它展开为一个 `add_node` 和若干 `update_node`，将新 key 加入目标节点依赖，并把目标 `work.inputs` 中旧的 `dependencies.*` 引用按最终依赖集合重写。原始 `insert_before` 不进入领域 Snapshot。整个候选仍经 GraphEditPreview、完整 DAG、冻结线、版本与哈希校验后才能确认。

页面画板由 React Flow 12.11.2 与 ELK.js 0.12.0 构建。节点拖动位置按 Instance 保存在当前浏览器，刷新后继续使用；恢复自动布局只清除本地位置，不访问领域写接口。画板上的增加、编辑、删除、拖动端点增加依赖和选中连线断开依赖都会生成服务端 GraphEditPreview，显式确认后才写入领域状态；节点表单继续作为依赖编辑回退入口。“打回到此节点”复用 RestartPreview。当前不支持任意自由图形或多人实时协同。

参与者任务接口为 `GET /console/api/v1/tasks`、`GET /console/api/v1/tasks/<instance_id>/nodes/<node_key>`、对应的 `POST .../submit`、`POST .../transfer` 与 `POST .../decision`，以及 `GET /console/api/v1/people`。任务列表和详情只返回当前 person 负责的 `waiting_human` 节点、任务类型、目标、验收条件、声明的 `work.outputs`、当前 Attempt、节点版本和有界依赖上下文；参与者访问同一实例的完整 Owner 详情仍返回 404。普通提交必须在 `content` 与结构化 `result` 中二选一。只要节点有 `required=true` 输出，页面按 `boolean / choice / date / integer / number / money / text / long_text / url / string_list / object / data / document / file` 渲染字段，服务端拒绝缺失、类型不符、未知字段、非法 URL、不可序列化或超限结果。Agent 和 Tool 完成也必须包含全部必填输出，但允许保留执行器生成的审计元数据。权限、责任人、Attempt、版本和 claim 校验先于交付物校验，校验失败不修改节点或 Attempt。转交同时校验 tenant、当前节点 Owner、executor、状态、Attempt 编号与节点版本；决定提交还必须携带并校验当前 Instance 版本，只接受 `accept` 或 `reject`，退回意见必填且最多 1000 字。页面入口与版本绑定的飞书决定卡调用同一领域命令，陈旧入口和重复提交返回 404 或 409。转交目标由服务端应用凭据目录解析，必须是同一租户内、应用可见且活跃的成员；领域事务只改变运行时 `NodeInstance.owner_person_id`，冻结 `InstanceSnapshot` 不变，并追加转交审计与 outbox。旧负责人随事务提交立即失去提交权，Projection 使用稳定幂等键更新既有飞书 Task 负责人。转交响应固定把外部投影标记为 `projection.kind=feishu_task / status=queued`，页面显示负责人已更换且飞书仍在同步；异步失败由既有 outbox 有界重试和管理员异常聚合承接。决定节点不可使用普通提交或转交接口，普通任务也不可调用决定接口。

普通 Human 节点只要声明必填输出，飞书原生 Task 完成入站就返回 `task_requires_deliverable`，不提交占位结果，也不推进领域状态。Projection 的 Task 描述列出必填交付物，并在配置了干净 HTTPS `LARKFLOW_CONSOLE_PUBLIC_BASE_URL` 时附加 `/console/?action=task&instance=<id>&node=<key>` 深链。Console 主页只接受精确的 `action=task + instance + node` 或既有 `auth_error` 查询；脚本、样式和其他静态资源仍拒绝 query。前端在 OAuth 往返前把深链暂存于当前标签页，登录后只从本人有权处理的 `attention` 中匹配并打开任务；找不到时使用不区分已处理、已转交和无权访问的统一提示。

所有工作流 POST 都拒绝 query 和 `Transfer-Encoding`，并要求 `X-Larkflow-Console-Action: workflow-action-v1`。需要正文的任务提交、任务转交、人工判断、草稿创建、图编辑预览和附件上传都要求 `Content-Type: application/json`、精确 `Content-Length` 与严格字段集合；除附件上传外最多 65536 字节，附件上传最多 262144 字节。流程控制确认、预览确认、附件撤销和附件开始生成拒绝请求体与非零 `Content-Length`。`feishu` 模式还要求 `Origin` 精确等于配置的 Console 公网 origin；客户端身份字段、版本正文或节点状态都不被接收。页面按钮在请求发出前立即进入“正在执行”或“正在生成预览”，完成后显示明确终态；取消、重启和图编辑在同页展示服务端预览，并要求第二次点击确认。

Console 支持 `static` 与 `feishu` 两种鉴权模式。`static` 仅供 loopback 开发，要求 `LARKFLOW_CONSOLE_ACCESS_TOKEN` 至少 32 字符，并在服务端把该 Bearer token 映射到 `LARKFLOW_TARGET_TENANT + LARKFLOW_CONSOLE_PERSON_ID`。`feishu` 使用 OAuth v3 authorization code 与 PKCE S256；服务端保存单次、五分钟有效的 state，并用 Secure、HttpOnly、SameSite=Lax 的 `__Host-` cookie 绑定发起浏览器。回调在服务端以 app secret 换取用户 access token，只调用一次用户信息接口，然后立即丢弃该 token。返回的飞书 `tenant_key` 必须等于 `LARKFLOW_CONSOLE_FEISHU_TENANT_KEY`，并显式映射到 `LARKFLOW_TARGET_TENANT`；`open_id` 成为当前 person。用户 OAuth 不申请业务 scope，不持久化 refresh token，也不依赖服务器或员工电脑的 `lark-cli` 登录。

`feishu` 模式只向浏览器发放随机不透明会话 cookie，服务端仅保存其 SHA-256 摘要。会话默认八小时，可配置在 300 到 86400 秒之间；migration `0020_console_sessions` 在 PostgreSQL 保存摘要、tenant、person、创建时间和过期时间，不保存原始凭据。migration `0021_console_session_governance` 为既有和新增会话增加独立的 32 位十六进制安全 ID，并保存耐久撤销预览和追加型撤销事件。签发前在事务级 advisory lock 内清理过期记录，并在全局 10000 条上限处逐出最早会话；摘要冲突不覆盖旧主体。每次认证只接受未过期摘要，过期记录会被删除；注销删除当前摘要。OAuth 发起态仍是五分钟有效的进程内短期状态，所以授权中途恰逢 Console 重启时需要重新发起授权，但已完成会话可跨 Console 重启继续使用。客户端不能提交 tenant 或 person。列表 SQL 同时限定 tenant 与 `owner_person_id`，详情读取完整聚合后再次校验 Instance Owner；不存在与非 Owner 都返回同一 404。返回 DTO 只使用 `you / collaborator / system` 表示人员关系，不含任何人员 ID、claim token、完整错误正文或原始审计 payload。列表最多 100 条，审计最多 200 条，单个结果超过 32000 字节时只返回截断预览。浏览器仅用 `textContent` 渲染服务端数据；`feishu` 模式不保存 Bearer token，`static` 模式的开发 token 只放当前标签页。

`GET /console/api/v1/admin/overview` 是同一 Console 会话内的管理员只读接口。`GET /console/api/v1/auth` 只额外返回服务端计算的 `admin` 布尔值；浏览器不能提交或覆盖该值。管理员 allowlist 由 `LARKFLOW_CONSOLE_ADMIN_PERSON_IDS` 配置为最多 100 个非空 person ID，授权必须同时匹配当前 Target tenant 和当前会话 person。未命中 allowlist 时，管理员接口返回与未知路由相同的 404，页面也不显示“管理概览”页签。

管理员概览只聚合当前 tenant 的数据：流程总量与状态分布、不同 Owner 数量；有效、临近过期和已过期但尚未清理的 Console 会话数量；migration 已应用与安装态预期数量；Outbox、Inbox、IM 命令、IM 回复、人员分工动作、人员分工回复和人员分工进度七条耐久队列的总量、待处理量与需要关注量。仓储查询全部绑定服务端 tenant，不读取或返回人员 ID、原始错误、消息 payload、action payload、claim token 或单条业务记录。

`GET /console/api/v1/admin/sessions?limit=<1..100>` 只列当前 tenant 的有效 Console 会话，返回安全会话 ID、`you / member` 关系、创建时间、过期时间和 `current` 布尔值。当前浏览器会话不能从管理面撤销，必须调用注销。`POST /console/api/v1/admin/session-revocations/preview` 创建五分钟有效的耐久预览；`POST /console/api/v1/admin/session-revocations/confirm` 只接受预览 ID，并在同一 PostgreSQL 事务中锁定预览与目标会话、删除会话、消费预览和追加不可变事件。状态漂移、过期预览与目标缺失返回 409；同一已消费预览重复确认返回相同成功结果，不新增事件。两个连接竞争同一预览时只能一路执行。

上述两个管理 POST 都拒绝 query、请求体、`Content-Length` 和 `Transfer-Encoding`。`feishu` 模式还要求 `Origin` 精确等于配置的 Console 公网 origin，并携带 `X-Larkflow-Console-Action: session-governance-v1`；未授权成员仍得到与未知路由相同的 404。浏览器按钮在请求发出前立即进入“创建预览中”“正在撤销”或对应禁止状态，随后明确显示等待确认、已撤销或错误。该写面不提供 allowlist 修改、批量撤销、队列重放、配置修改或流程领域写命令。

开发 unit `larkflow-target-console.service` 以 `lf_target_dev` 运行，通过 Unix socket peer authentication 连接 PostgreSQL，只监听 `127.0.0.1:8780`，并使用 systemd 文件系统、设备、能力、命名空间和地址过滤。`feishu` 模式已位于 Caddy 公网 IP HTTPS 入口之后，精确回调地址、飞书网页应用主页、默认网页能力和可用范围均已发布；至少两名真实成员已完成授权登录、本人 Owner 可见性和跨 Owner 隔离验证。完成授权的真实会话已在 Console 重启后通过浏览器刷新回归。管理员概览和会话治理已在真实 PostgreSQL 与 HTTP 中验证管理员授权、普通成员 404、当前会话保护、预览确认、幂等竞争、撤销失效与追加型审计；新会话治理面板也已完成真实登录浏览器视觉验收。

`feishu` 模式在 HTTP server 边界启用线程安全的有界令牌桶，默认窗口为 60 秒。读取与静态资源每个来源容量为 300，`/console/auth/*` 和 `/console/api/v1/auth` 共用 30 的认证容量，`POST /console/api/v1/admin/*` 共用 30 的管理员写入容量，Owner 工作流 POST 共用 60 的工作流写入容量，所有来源共用 3000 的全局容量。超限返回 HTTP 429、`Retry-After` 和固定安全 JSON `{"error":{"code":"rate_limited","message":"request rate limit exceeded"}}`。客户端来源只在直接 peer 是 loopback 时读取 Caddy 覆盖的 `X-Larkflow-Client-IP`；其他 peer 始终使用 socket 地址。来源只用于可用性公平性，不参与 OAuth、tenant、person、Owner 或管理员授权，并只以进程随机密钥生成的 BLAKE2s 摘要保存在最多 10000 个 LRU key 的内存表中。`static` loopback 开发回退不启用该公网限流。Caddy 对精确的 `POST /console/api/v1/drafts/<32位小写十六进制ID>/attachments` 使用 262144 字节请求体限制，其他路径继续使用 65536 字节，并统一设置 32 KB 请求头、10 秒请求头、15 秒请求体、30 秒写入和 2 分钟空闲超时，关闭 0-RTT，返回 HSTS、CSP、能力禁用、跨源隔离、拒绝 framing、`nosniff` 和 `no-referrer` 等响应头。

`POST /console/api/v1/drafts` 接受严格 UTF-8 JSON 对象，必填字段是客户端生成的 32 位小写十六进制 `request_id`、非空 `brief`、可为空字符串的 `context` 和值为字符串或 `null` 的 `collaborator_person_id`；唯一可选字段是布尔值 `defer_generation`，省略或 `false` 时保持原有行为并直接进入 `pending`。`defer_generation=true` 只在 `capabilities.attachment_planning=true` 时接受，并创建 `collecting` 请求；能力未启用时在任何持久化之前返回 HTTP 409、`attachment_planning_unavailable`。不指定协作者时也必须显式传 `null`，服务端把该角色绑定为 requester。请求必须携带 `Content-Type: application/json`、正确的 `Content-Length` 和 `X-Larkflow-Console-Action: workflow-action-v1`，拒绝 query、分块传输、未知字段、越界文本和不在当前企业活跃目录中的协作者；`feishu` 模式还要求精确同源 `Origin`。服务端从当前会话取得 tenant 与 requester，浏览器不能提交或覆盖它们。相同 request ID 与相同输入幂等返回原请求，不同输入返回冲突。创建成功返回 202。

`GET /console/api/v1/drafts?limit=<1..20>` 只列当前 requester 的最近请求，`GET /console/api/v1/drafts/<request_id>` 只读取当前 requester 的单项状态。公开 DTO 只暴露安全状态、用户输入、时间、附件数量、可选实例 ID 和固定提示，不返回 person ID、claim、模型原文错误或内部定义。公开状态包含资料可变更期 `collecting`，把内部 `pending / generating / repairing / creating / failed` 映射为 `queued / generating / repairing / preparing / retrying`，终态为 `ready / rejected / failed`。

collecting 请求的附件接口为：`POST /console/api/v1/drafts/<request_id>/attachments` 上传，`GET /console/api/v1/drafts/<request_id>/attachments` 列表，`POST /console/api/v1/drafts/<request_id>/attachments/<attachment_id>/revoke` 逻辑撤销，`POST /console/api/v1/drafts/<request_id>/generate` 冻结当前 ready 清单并进入生成队列。四条接口只允许当前 requester；跨 tenant、同 tenant 非 Owner、collaborator 和不存在资源统一返回 404。上传 JSON 必须且只能包含 `display_filename / media_type / content`；media type 只允许 `text/plain` 或 `text/markdown`，正文必须是非空 UTF-8 字符串。客户端不能提交 object key、tenant、uploader、Instance、数据分级或模型外发决策。列表项只返回 `id / display_filename / media_type / size_bytes / content_sha256 / status / data_classification / model_egress_policy / created_at / revoked_at`。

每个 collecting 请求最多保留 8 个附件对象、每个最多 32768 字节、实际保留总量最多 131072 字节；每个 tenant 最多保留 1024 个附件对象和 33554432 字节。逻辑撤销只使附件退出冻结清单，不删除元数据或 blob，也不释放 request 或 tenant 保留配额。PostgreSQL 仓储在 tenant advisory transaction lock 内计算保留量，避免不同请求并发绕过 tenant 配额。超出文件数返回 `too_many_attachments`，超出 request 字节数返回 `attachments_too_large`，超出 tenant 上限返回 `tenant_attachment_quota_exceeded`，均为 HTTP 409。collecting 结束后的上传或撤销返回 `draft_not_collecting`。若部署策略在 collecting 期间改为 deny，存在 ready 附件时 generate 返回 `egress_denied`；Owner 可以撤销全部 ready 附件，再以空 manifest 进入原有无附件生成路径，因此不会被锁死。

附件 Blob 缺失、非普通文件、大小或 SHA-256 不匹配、UTF-8 失败、manifest 漂移、越界绑定与确定性策略违规会使请求进入 `rejected`，不会静默丢弃单个来源继续规划。权限、I/O、挂载或存储读取故障属于可重试基础设施错误，进入 Draft Worker 的既有 `failed` 与有界 backoff 路径；这类错误不能伪装成附件不存在，也不能调用 Planner 后再补救。

附件支持的候选图由服务端为每个 Agent 节点写入 `work.inputs` 中的 `instance_inputs.project_attachments`，模型不能删除、伪造或替换这一声明。Agent Runtime 只有在节点显式声明该输入时才可请求附件上下文；未声明时不得读取 Blob，声明后却没有装配 Context resolver 时以 `agent_context_unavailable` 失败。Context resolver 必须把冻结 manifest 与 tenant、Instance、Node、Attempt、Owner、原始 planning fingerprint、附件 ready 状态、origin request、分级、外发决策、大小、SHA-256、UTF-8 和字符预算逐项复验，任一确定性失败以 `agent_context_rejected` 结束当前 Attempt，临时存储故障沿用 Worker 的可重试失败路径。

通过复验后，Runtime 只获得 `purpose=agent_execution` 的有界 ContextBundle 和短时 `CapabilityEnvelope`。能力信封仅允许 `context.read.project_attachments`，并绑定 tenant、Instance、Node、Attempt、scope、上下文 fingerprint、签发与过期时间；不包含 claim token、expected node version、数据库句柄、飞书凭据、object key 或真实存储路径。附件正文只位于 Runtime 的独立“不可信项目资料”提示区块，其中指令不能改变系统规则、DAG、Owner、Human Gate、工具权限或确定性校验。Attempt 成功结果的 `_runtime_evidence` 只保存能力信封、安全 manifest 和运行时摘要，不保存正文、object key、路径或 uploader。

migration `0023_console_draft_requests` 保存请求、输入、生成状态、尝试次数、可用时间、短租约、冻结候选定义、实例绑定、安全错误码与完成时间；migration `0024_console_project_attachments` 增加 collecting、冻结 manifest 和 tenant-first 附件元数据，并已应用到开发环境。独立 Draft Generation Worker 在同一 PostgreSQL 事务中通过 `FOR UPDATE SKIP LOCKED` 领取；候选一旦通过确定性校验，就必须先在当前 claim 下保存，再用 `console_draft_<request_id>` 作为稳定实例 ID 创建未启动 Snapshot。Worker 在保存候选后崩溃时，接管者复用同一冻结候选，不能再次调用模型。基础设施失败采用有界退避，默认最多五次后进入 `exhausted`；确定性拒绝直接进入 `rejected`。当前没有带 URL 引用的搜索线路时，内部记录保留 `DraftCapabilityUnavailable` 分类，公开 DTO 只返回固定的可操作说明，不暴露原始异常或模型路由。成功请求只创建 `draft / 0 Attempt`，仍由既有确认接口启动。

migration `0025_enterprise_knowledge_catalog` 增加 tenant-first 企业共享资料版本目录与追加审计。目录只保存安全元数据，不保存正文或对象 key；同一 tenant 与 source 同时最多一个 `published` 版本，既有版本除一次 `published -> revoked` 外不可修改，版本和审计都禁止物理删除。仓储对 tenant 与 source 使用事务级 advisory lock，使重复发布幂等、竞争版本只有一个成功；撤销重复调用不追加第二条审计。

migration `0026_enterprise_knowledge_content_authorization` 增加 tenant-first、内容绑定且追加型的服务器授权证明。证明绑定 tenant、source、version、content SHA-256、授权管理员、固定 `tenant_all_members` scope、策略版本、声明摘要和授权时间；禁止更新和物理删除。版本行只保存 proof ID 与 fingerprint，不保存正文、object key、原始授权声明或存储路径。正文位于独立 `enterprise` namespace BlobStore，object key 由服务端根据 tenant、source、version 和内容哈希派生。

migration `0027_console_enterprise_knowledge_selection` 为既有 DraftRequest 增加默认空 source 清单、单调 `selection_version`、冻结企业安全 manifest 与 selection fingerprint。0027 的数据库函数只校验数组形状、数量和 source ID 格式，应用层继续对重复 ID fail closed；向前 migration `0028_console_enterprise_source_uniqueness` 必须重新验证既有行，并在数据库层拒绝重复 source ID。选择只能在 collecting 状态修改，冻结 manifest 只能随 collecting 原子转为 pending 写入，写入后不可改。既有请求、已冻结 Instance、历史 Attempt 和 canonical v1 fingerprint 不被重写。

企业资料管理复用 `LARKFLOW_CONSOLE_ADMIN_PERSON_IDS` 与当前 Target tenant，不新增浏览器可声明的管理员身份。`GET /console/api/v1/admin/knowledge?limit=<1..100>` 返回当前 tenant 的 published 与 revoked 版本安全元数据；`GET /console/api/v1/admin/knowledge/sources/<source_id>/versions/<version_id>/audit?limit=<1..100>` 返回追加审计，但 actor 只表示 `you / member`。`POST /console/api/v1/admin/knowledge/publications` 只接受 `source_id / version_id / display_label / media_type / content / content_sha256 / egress_decision / authorization_statement / authorization_policy_version` 九个字段。正文必须为非空 UTF-8，媒体类型只能是 `text/plain` 或 `text/markdown`，正文最多 131072 bytes，提交哈希必须与正文相等；授权声明和策略版本必须精确等于服务端公开的当前合同。tenant、publisher、发布时间、分级、object key、proof ID 与状态全部由服务端产生。`POST /console/api/v1/admin/knowledge/sources/<source_id>/versions/<version_id>/revoke` 不接受 query 或正文，重复撤销保持幂等且不新增审计。两个写端点都要求 `X-Larkflow-Console-Action: knowledge-governance-v1`；发布还要求严格 JSON、精确 `Content-Length`、不超过 262144 bytes 和正确 `Content-Type`，`feishu` 模式继续要求精确同源 `Origin`。非管理员与跨 tenant 统一 404，版本冲突为 409 `knowledge_conflict`，内容存储不可用为 503 `knowledge_content_unavailable`。响应不含正文、object key、存储路径、tenant、管理员 person ID、原始授权声明、源系统 locator 或凭据。`GET /console/api/v1/auth` 只对已认证管理员返回 `capabilities.enterprise_knowledge_catalog=true`；仅当 BlobStore 已配置时同时返回 `enterprise_knowledge_content_publication=true`。

已提交合同要求企业资料只能由 Console collecting 草稿发起人显式选择，默认空选择；飞书 wizard 与其他没有选择 UI 的入口不得自动使用企业资料。`GET /console/api/v1/knowledge` 返回当前认证成员所在 tenant 的 published 安全元数据：`source_id / version_id / display_label / media_type / size_bytes / published_at / data_classification / egress_decision / authorization_proof_id / selectable / unavailable_reason`。响应不得包含正文、content hash、proof fingerprint、object key、管理员身份、授权声明、tenant、数据库字段或跨 tenant 信息。未认证返回 401，已认证成员均可读取当前 tenant 安全目录；服务未配置时返回空目录和稳定的不可用能力状态，不回退为自动选择。

`GET /console/api/v1/drafts/<request_id>/knowledge-selection` 只允许当前 requester 在 collecting 阶段读取 `source_ids / selection_version / selected / unavailable_selected` 安全元数据。`selected` 只包含当前仍可选的公开目录项；`unavailable_selected` 为已经保留在选择中、但因撤销或不再发布而不可使用的安全墓碑，只返回 `source_id / selectable=false / unavailable_reason`。服务端和页面不得静默清空墓碑，也不得返回正文、版本哈希、proof fingerprint、管理员或 tenant；Owner 必须能明确取消勾选并保存，成功后 `selection_version` 增加，再以剩余有效选择或空选择开始生成。`POST` 只接受严格 JSON `{"source_ids":[...],"expected_version":N}`，source ID 必须有界、去重且最多 16 个。客户端不能提交 tenant、version、hash、proof、egress、classification、正文、object key 或 Instance。相同清单重放幂等；不同清单必须匹配当前 `selection_version`，成功后版本加一。不存在请求、非 Owner、跨 tenant 或新加入不可见 source 统一 404；从已有选择中移除不可见 source 合法。请求已 pending 或终态、版本冲突和重复输入返回稳定 409。写请求要求 `X-Larkflow-Console-Action: knowledge-selection-v1`，`feishu` 模式继续要求精确同源 `Origin`，正文上限 16384 bytes。

`POST /console/api/v1/drafts/<request_id>/generate` 在服务端事务中锁定当前 collecting request，按 source ID 解析当时唯一 published、正文已授权且外发允许的精确版本，并与 ready 项目附件一起冻结。冻结结果写入 DraftRequest 的 `enterprise_knowledge_manifest`、`enterprise_selection_fingerprint` 与附件 manifest 后才原子转为 pending；撤销、缺失、重复、超限、proof 或 egress 漂移、并发选择变化均整体 fail closed。已成功冻结后的同一生成动作幂等；生成 Worker 重试必须复用冻结 refs，不得改选后续版本。Instance 只冻结 `enterprise_knowledge` 安全 refs、`context_manifest` 与 canonical fingerprint，不保存正文。Agent 节点必须显式声明服务器拥有的 `instance_inputs.enterprise_knowledge`；Worker 再按 tenant、Instance、Node、Attempt、版本、撤销、proof、哈希、大小、媒体、分级、外发、预算和 TTL 重新授权。Runtime 只收到有界 ContextBundle 和 `context.read.enterprise_knowledge` 能力，未配置 resolver 或任一复验失败时 fail closed。撤销不改写旧 Attempt 的安全 manifest 与 fingerprint，但新规划和新 Attempt 不得读取已撤销版本。

企业正文读取完成并通过长度、SHA-256、UTF-8 与预算校验后，Knowledge Context Service 必须在发行 ContextBundle 前重新读取并原子复验精确 tenant、source、version、完整 ref、published 状态、授权证明和外发决定；PostgreSQL 实现使用与发布、撤销一致的 source advisory transaction lock，把该事务完成点定义为最终授权线性化点。撤销若在最终授权点之前完成，本次 planning 或 Agent bundle 必须拒绝且正文不得交给下游 Runtime；最终授权点之后才完成的撤销视为该 bundle 已经发行，只阻断后续 bundle，不能撤销已经发生的模型外发。项目附件保持冻结 manifest 顺序，企业资料按 source/version 稳定排序，合并顺序固定为项目附件区块后接企业资料区块；canonical fingerprint 覆盖这一顺序，不对既有冻结 manifest 做全局重排。

`DraftDefinitionGenerator` 对新候选强制以下交付物不变量：每个节点输出必须包含 `id / type / label / required=true`；每条 `deps` 必须在 `work.inputs` 中以 `dependencies.<node_id>` 精确消费；Agent 必须消费至少一个上游并输出 `content`；含 Agent 的图必须从普通 Human 根节点开始，并以 `accept_reject` Human 决定结束。提示词要求先识别日期、人数、预算、来源、范围、限制和验收口径等缺失事实，并把不同来源或独立交付物拆成可并行节点。受控联网开启时，模型只能生成 `work.tool.kind=web.search` 的公开信息研究节点，参数只允许服务端模型角色和研究指令，输出固定为必填 `content(text) + sources(string_list)`。旅游规划由服务端按 evidence policy 选择路径：允许联网、资料不足且已配置带引用搜索能力时，根节点必须收集出发地、出行日期、出行人数和预算，景点与交通必须拆成两个独立 `web.search` 节点，后续 Agent 同时消费研究结果；请求明确 no-web 或数据策略禁止外发，且授权附件提供可解析的正向出发地、有效日期、正整数人数、正数预算以及无否定词的景点和交通资料时，允许 `Human 来源确认 -> Agent -> Human 决定`。被最终 Human 复核的每个 Agent 必须直接依赖并通过 `work.inputs` 消费至少一个提供完整已确认来源交付物的 Human 根节点；与 Agent 无依赖边的旁路根节点不能满足校验。“未知、没有、缺失、待定、未确认”等明显否定文本不算正向证据；任一类资料不足时都要明确列出缺失证据，不得静默联网。需要外部资料但没有声明 `responses_citations` 的可用线路时，在模型生成候选前返回 `DraftCapabilityUnavailable`，不得先创建一个会等待数分钟后失败的搜索节点。

该开发入口没有正式域名，进程内令牌桶也不是多副本或分布式生产限流。当前仍没有任意自由图形、多人实时协同、allowlist 自助管理、批量撤销、设备命名、跨区域容灾、生产容量证明或正式员工交付方案。工作台只提供参与者任务面，不提供完整协作者实例视图、分页游标、筛选或跨轮次 diff；受控 DAG 画板只允许 Owner 修改草稿定义或运行中未来区域，并发起节点返工。

Target 自动节点按工作契约 kind 路由。Agent 当前只接受 `work.agent.kind=llm.generate`。普通 `plain_text` 结果不再接受裸正文：支持 metadata 的 provider 必须返回正常结束原因，当前只接受 `stop / completed / end_turn`；模型正文必须是严格 JSON envelope，包含非空 `content`，以及 `completion.status=complete` 和覆盖全部验收 ID 的 `acceptance_evidence`。每项证据必须标记 `satisfied`，并提供 1 到 12 个、每个不超过 80 字符且可在正文中精确定位的 `content_anchors`。长度截断、缺失或未知结束原因、完成标记缺失、验收项缺失、锚点不存在和超出结果上限都使 Automated Attempt 以 `agent_result_incomplete` 失败，不能保存结果或标为 done；成功结果保存安全的 `finish_reason / usage / provider_model`。未实现 metadata 的 legacy 兼容 client 仍沿用旧正文路径，避免改变既有本地 adapter 合同；Target 生产装配使用 metadata 路径。`work.agent.result_format=source_claims.v1` 用于整理材料，要求模型返回 `problem / target_users / functional_requirements / acceptance_criteria / risks / open_questions / source_url`，其中声明必须标记为 `source_fact / inference / open_question` 并引用服务端登记的稳定 `F` 或 `Q` ID。`work.agent.result_format=source_decision.v1` 用于形成决定，要求返回唯一 `priority`、`rationale`、3 到 5 条 `acceptance_criteria`、带 `reconsider_when` 的 `not_now`、`risks`、逐一回答全部 Q 的 `answers` 与原样 `source_url`；除问题编号外，全部条目只引用 F，并在渲染中标记为建议推断。Tool 由 `ToolExecutorRouter` 按 `work.tool.kind` 选择 adapter；`web.search` 优先选择已配置的豆包 Custom `SearchProvider`，未配置时才兼容显式声明 `web_search_capability=responses_citations` 的 provider/model 路线。豆包请求只发送服务端生成的 `Query / SearchType=web / Count / NeedSummary` 和 API Key，不发送 Bot ID、API ID、数据库句柄、飞书凭据或业务写能力；查询不得为空或超过 100 字符，返回条数固定在 1 到 50。搜索 adapter 只渲染检索证据，不在内部做模型综合；下游 Agent 必须显式消费结果。缺少开关、API Key 或完整路线时返回 unavailable，不调用远端；既有 Responses 路线仍只接受供应商结构化 citation，正文中的裸 URL 不能成为 `sources`；备用线路改变 base URL 或 model 时不会继承能力声明。两条搜索路径都禁止登录、提交表单、预订和购买。普通 Agent 一旦消费 `web.search` 结果，提示必须禁止绝对真实性声明，服务端还要固定附加未独立验证与时效复核提示。`content.check`、`source_claims.check` 与 `source_decision.check` 继续执行既有结构和覆盖校验。配置或输入错误使当前 Attempt 显式失败，未知 kind 在 claim 前保持未认领。Agent 与 Tool 的不可变嵌套结果在进入统一交付物 validator 前规范化为普通 JSON 值，保证运行时只读快照不会因序列化形状导致合法 Attempt 失败。

搜索来源质量合同要求成功 Tool 结果保存 `content / sources / source_records / provider / query / usage / error=null / model_role / request_id`。每条 `source_records` 必须包含有界 `title / snippet / source_url / published_at / published_at_status`，并增加 `url_status=valid|invalid`、`health=reachable|unreachable|unknown`、`freshness=current|stale|unknown`、可解释 `authority` 类别或 unknown，以及 `support=supported|unsupported|unknown`。URL 集合与 `sources` 必须精确一致并去重。默认没有安全出站 adapter 时 health 必须为 unknown，不能伪装 reachable；发布时间不明时 freshness 也为 unknown。authority 只表示来源类别与服务端可解释依据，不表示事实权威或正确。

真实 URL 健康检查只能通过 `SafeOutboundFetcher` Port，生产默认实现为 unavailable。任何未来启用的 adapter 都必须在连接前和每次重定向后拒绝 localhost、私网、链路本地、凭据 URL、非 HTTP(S)、DNS 越界、登录态和超大响应；普通搜索 adapter 不得直接向任意返回 URL 发起 `httpx` 请求。额度耗尽、HTTP 429、超时、传输失败、协议错误、无合法 URL和全部不可用来源必须映射为稳定、安全且可操作的错误分类或质量状态，不保存原始响应、key 或内部异常。

`source_evidence.check` 是只读确定性 Tool，只接受 `claims` 与 `source_records` 两个直接依赖路径。运行器必须在输入快照的服务器保留区域中，为每个直接依赖冻结由 NodeSpec 和已提交 Attempt 产生的 `node_key / executor / tool_kind / attempt_id / attempt_no` provenance；上游结果、模型、浏览器和 DAG 输出都不能声明或覆盖这些字段。`source_records` 路径必须属于 provenance 明确标记为 `executor=tool` 且 `tool_kind=web.search` 的同一个直接依赖，缺失 provenance 的旧快照、普通 Agent 自报 `tool_kind`、嵌套伪造、跨依赖复制 URL 或 excerpt 均 fail closed。每条 claim 必须有有界 `claim_id / text / source_url / supporting_excerpt`，source URL 规范化后仍须属于该真实搜索 Attempt，excerpt 必须是对应 provider snippet 的非空有界原文片段；所有必需 claim 都满足才标记 `support=supported`。替换 URL、重复 claim ID、伪造或扩写 excerpt、空来源、超长字段和结果预算超限均 fail closed。该结果只证明当前检索片段支持该表述，不证明页面可访问、官方域名事实正确、供应商摘要真实或结论在现实世界成立，最终判断仍留给 Human Gate。

上述来源质量合同已由 `fba57583af164d5a39077d7979b751e604cc3382` 落码并部署到开发环境。默认出站 adapter 没有网络实现，安装态探针把 health unavailable/unknown 与搜索 provider 可用性分开回读；不得把尚未实现的安全 URL 抓取写成已验证。

完成文档投影对普通 Markdown 使用服务端的安全子集转换，只接受标题、无序列表、有序列表、管道表格和粗体，并先转义原始 XML。支持的结构会写成飞书 Docx 原生块；未支持的 Markdown 继续作为普通段落文本，不执行模型提供的任意 XML。

Human 责任卡中的结构化 Instance 输入和结构化依赖结果使用带围栏的 JSON 代码块展示。这样既保留可读结构，也避免 URL 后面的 JSON 引号被 Card Markdown 自动链接解析器吞入目标地址。字符串正文仍按普通 Markdown 展示，所有上下文继续受既有长度上限约束。

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

Human 节点可声明 `work.decision.kind=accept_reject`，并用 `reject_target` 指向直接依赖的待复核节点。该节点不创建可被“完成”绕过的飞书 Task，而是投影 Card 2.0 决定入口。接受按钮位于表单外，可保持一键提交；退回必须通过表单填写 `rejection_feedback`，非空且最多 1000 字。回调先耐久落入既有 IM 命令队列并尽快把原卡片更新为无按钮“处理中”；桥接层和领域服务都重新校验退回意见，接受路径忽略客户端附带的额外意见。凭据侧从飞书顶层字段取得操作人并重新验证活跃成员，领域侧再校验 Instance、Node、Attempt 版本和唯一 Owner。`accept` 正常完成 Human Attempt；`reject` 把规范化意见写入 Human Attempt 结果、质量证据和追加型审计，使当前 Human Attempt 与 Instance 失败，并提示 Owner 使用既有节点重启修订目标节点。重复操作、旧版本卡片、非 Owner 和 legacy Task 完成信号均不能提交决定。

凭据侧详情读取失败或完成状态暂不可见时，按 `LARKFLOW_TARGET_INBOUND_RETRY_BASE_SECONDS` 到 `LARKFLOW_TARGET_INBOUND_RETRY_MAX_SECONDS` 做指数退避。`LARKFLOW_TARGET_INBOUND_VERIFICATION_MAX_ATTEMPTS` 默认 24；达到预算仍无法验证时，Inbox 进入 `exhausted` 终态，写入 `processed_at`、`outcome=exhausted:verification_attempts`、`failure_stage=verification` 与最后错误，不生成 verified payload，也不允许领域 Worker 认领。验证日志包含 `exhausted` 计数，运维必须对非零值告警并人工调查，不能静默丢弃。

### Target 飞书 IM 命令与完成投影 as-built

Target 订阅 `im.message.receive_v1`，处理以 `/larkflow` 开头的文本，也接受群聊中只有认证 mention token 位于命令前的 `@机器人 /larkflow ...` 形式。桥接层同时接受飞书原始 V2 信封和 lark-cli 拍平输出：原始事件的 `content` 是 JSON 字符串，拍平输出的 `content` 是普通文本，两者必须归一为同一个命令信号。桥接层只保存 mention 的 `key` 与 `open_id`，不保存显示名称；其他消息不进入 Target 命令 Inbox。

- `/larkflow help`：返回当前十五个命令的用法。
- `/larkflow start <template_id> [JSON对象] [role=@成员 ...]`：以 tenant 和 message ID 派生稳定 Instance ID，验证发送者属于当前企业且状态活跃，再把发送者绑定为 Instance Owner。显式角色绑定使用 lower snake case 角色名和本条消息的 mention key；凭据侧通过企业目录验证每名被引用人员仍在当前 tenant 且状态活跃，领域侧再把 mention key 映射到冻结 Snapshot。原始文本中的 open_id、显示名称或不存在于 mention 元数据的 token 均不能授权。未显式绑定的模板角色继续归发送者。命令只创建草稿并返回确认命令，不自动启动。
- `/larkflow draft` 与 `/larkflow draft <JSON定义> [role=@成员 ...]`：两种形式都不查找模板版本，只创建 `template_version_id=NULL`、`locked=false` 的 Instance Snapshot 草稿，仍需独立 `confirm` 才启动。带 JSON 的高级入口沿用严格解析、100 节点上限、完整 work 契约与 mention Owner 验证，拒绝重复键、非有限数、模型服务配置和 `personal.readonly`。裸命令打开 Card 2.0 引导，必填目标、可选背景，并从冻结的活跃人员快照中选择一名协作者。回调只接受原发起人、原消息、原卡片和服务端允许的字段；协作者在领域处理前再次验证。中央 Agent 只能生成最多八个 `human / agent` 节点，Owner 角色只能是 `requester / collaborator`，Agent 节点后必须直接有人类复核。服务端覆盖模型返回的 schema 与原始输入，并重新校验完整 Snapshot。首个候选未通过确定性校验时最多重生成一次，第二个候选仍不合法就拒绝。动作落库后由无飞书凭据的 Draft Generation Worker 认领；首次无按钮反馈使用卡片回调 token，`generating / repairing` 阶段和最终结果按原消息 ID 更新，所有卡片在更新前后都保持 `config.update_multi=true`。最终回复等待当前进度 revision 结算，旧 revision 不能覆盖终态。该独立拓扑、旧卡修复和新卡完整收口均已在开发测试组织真实通过。
- `/larkflow confirm <instance_id>`：重新校验发送者与草稿 Owner，确认并启动实例。
- `/larkflow status <instance_id>`：重新校验发送者后，仅允许 Instance Owner 读取流程状态。实例不存在与非 Owner 统一返回“实例不存在或你无权查看”，避免枚举。回复最多列出 20 个节点，每个可变字段最多 120 个字符；只包含状态、进度、节点、executor 和相对责任人，不包含结果正文或人员 ID。该命令只读，不追加领域审计，也不改变 aggregate version。
- `/larkflow list`：重新校验发送者后，只查询该发送者作为 Instance Owner 的最近实例。仓储按 `created_at DESC, id DESC` 排序，命令最多展示十条，并额外查询一条用于提示仍有更多结果。每条只包含 Instance ID、目标摘要、实例状态和完成节点数，不读取完整聚合，不包含节点结果或人员 ID。该命令只读，不追加领域审计，也不改变 aggregate version。
- `/larkflow pause <instance_id>`：只允许当前 Instance Owner 把 `running` 实例转为 `paused`。暂停只阻止新的节点领取；已经提交的 outbox 继续投影，已经进入 `running / waiting_human` 的 Human、Agent 与 Tool 允许按原 Attempt 收口。收口后只把后继节点置为 `ready`，在继续前不会调度。若最后一个活动节点完成或失败，实例可从 `paused` 直接进入对应终态。重复暂停不增加版本或审计。
- `/larkflow resume <instance_id>`：只允许当前 Instance Owner 把 `paused` 实例恢复为 `running`，不创建新 Attempt、不改变 `graph_revision`，Scheduler 随后从既有 `ready` 节点继续。重复继续不增加版本或审计。
- `/larkflow cancel <instance_id>`：只允许当前 Instance Owner 读取 `running / paused` 实例的完整取消影响。回复列出未完成节点、已经发出的节点和当前 aggregate version；该步骤不改变 Instance、Node、Attempt 或审计。
- `/larkflow cancel-confirm <instance_id> <instance_version>`：再次验证当前 Instance Owner 和预览版本。确认事务把所有 `pending / ready / running / waiting_human` Node 与当前非终态 Attempt 置为 `canceled`，清除自动 claim，并通过 outbox 关闭已有 Human Task、把已有 Human 决定卡替换为无控件“复核已取消”；`done / failed` 节点、旧 Attempt、结果和审计保持不变。取消后的迟到结果因实例终态、Node version 和 claim 撤销而拒绝。已经发生的外部副作用不自动回滚。状态漂移必须重新预览；重复确认只回读当前取消状态，不增加版本或审计。
- `/larkflow restart <instance_id> <node_key>`：重新校验发送者为 Instance Owner，服务端计算目标节点及全部可达下游，并持久化默认 15 分钟有效的只读预览。回复列出完整影响集合和当前 Attempt；预览绑定 tenant、Instance、actor、目标节点、影响集合、aggregate version 与 `graph_revision`，不改变 Instance、Node、Attempt 或领域审计。
- `/larkflow restart-all <instance_id>`：重新校验发送者为 Instance Owner，以显式 `instance` scope 创建完整实例重启预览。影响集合固定为拓扑排序后的全部节点，预览的节点键为空；它不以特殊节点值模拟完整重启，也不改变领域状态。
- `/larkflow restart-confirm <preview_id>`：只允许创建预览的当前 Instance Owner 确认。服务端重新校验 scope、有效期、aggregate version、`graph_revision` 和影响集合，在一个 PostgreSQL 事务内取消受影响的活动旧 Attempt、清除 claim、创建新 Attempt、消费预览并写入审计与投影 outbox。节点 scope 只把目标节点置为 `ready`；若当前失败决定的 `reject_target` 恰好等于目标，服务端只向目标的新 Attempt 输入快照注入 `rework_feedback={source_node_key, source_attempt_no, feedback}`，Runner 激活时继续保留。范围外上游、受影响下游的占位 Attempt 与冻结 Instance Snapshot 均不复制该字段。instance scope 把每个根节点置为 `ready`，其余节点置为 `pending`，不会自动继承某次局部退回意见。旧 Attempt、结果与质量记录保留；重复确认返回已执行状态，不再增加版本、Attempt、Task 或审计。过期或状态漂移的预览必须重新创建。
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

本机 `larkflow-edge` 提供 `pair`、`credential-migrate`、`doctor`、`run-once` 与前台 `serve`。`credential-migrate` 先把旧文件中的密钥写入 Keychain 并回读完整设备凭据；只有回读一致时，`--delete-source` 才把原明文文件原子替换为非敏感 Keychain 元数据。迁移校验或替换失败会回滚本次新建的 Keychain 项，旧文件保持可恢复。`doctor` 必须接收 `--workspace`，只在本机校验凭据、Codex 命令和传输模式，并运行两项真实 sandbox 探针：所选工作区必须可读，工作区外临时哨兵必须不可读；它不连接中央节点、不领取工作、不调用模型，也不输出 server URL、device ID 或 secret。两种执行命令都显式固定工作区，要求当前会话同时提供 `--allow-model-egress` 和 `--data-classification synthetic|public`，并启动使用 `larkflow_edge_readonly` permission Profile 的临时 Codex 会话。`edge-data-v0.1` 默认拒绝 `internal / confidential / restricted`，且没有客户端绕过参数；结果执行策略保存版本与分类但不复制正文。Profile 设定根路径默认拒绝、最小系统路径只读、临时目录拒绝、所选工作区只读，并在工作区内排除 Agent 配置、环境文件、证书与常见私钥名；网页搜索、浏览器、Computer Use、应用、图片生成和命令网络均禁用。`serve` 使用最长 25 秒的有界长轮询持续领取，同一凭据通过 POSIX 非阻塞文件锁限制为一个本机 Worker。瞬时网络与执行错误使用带抖动的有界指数退避，撤销或无效设备凭据立即停止；结构化日志提供启动、应用心跳、续租、单任务结果、故障和停止摘要。SIGINT 或 SIGTERM 会传递停止信号，续租失败也会取消整个 Codex 进程组；两种情况均不提交可能失去租约的结果。本机执行器异常仍不调用领域 `fail`。

macOS 开发试用安装由独立 `larkflow-edge-manager` 管理。推荐入口 `install --bundle <dir> --manifest-sha256 <hex>` 要求通过独立可信渠道取得完整 64 位 manifest SHA-256。schema v2 manifest 的 `package` 固定为 `larkflow-personal-edge`，主 artifact 只能包含 `edge_contract.py`、`edge_client.py`、`edge_agent.py`、`edge_cli.py` 与两个最小 package initializer，不能携带中央控制面模块。manifest 绑定 macOS 架构、Python 实现与次版本、完整 source commit、manager、全部 wheel 的包名、版本、大小和 SHA-256，以及 `requirements.lock`、`sbom.spdx.json`、`build-proof.json` 三项构建证据。manager 在修改安装目录前验证精确文件集、wheel metadata、lock 与 wheel 清单一致性、SPDX 包清单、构建证明、目标和 artifact，并拒绝额外文件、缺失文件、符号链接和重复包。离线 bundle 必须携带 pip 26.1.2 或更高且低于 27 的 bootstrap wheel，manager 先离线升级到已验证 pip，再使用 `--require-hashes --no-index --only-binary=:all:` 安装锁定依赖。schema v1 完整 `larkflow` bundle 只保留为旧开发兼容；`install --wheel <path> --sha256 <hex>` 仍可能联网解析依赖。release ID 固定为 `<package-version>-<sha12>`；直接 wheel 使用 wheel SHA-256，离线 bundle 使用覆盖全部依赖、manager 与证据的 manifest SHA-256。虚拟环境必须直接创建在最终 release 路径，`pip check` 与安装态 CLI 验证成功前不得切换。`status` 只输出非敏感安装状态；`rollback` 交换 `current / previous`，保留 release 内容。`uninstall --confirm-prefix <绝对前缀>` 只删除经过结构校验的受管前缀和精确稳定命令，拒绝根目录、用户主目录、符号链接布局和无关同名命令，重复执行返回 `already_absent`。稳定命令为 `~/.local/bin/larkflow-edge` 与 `~/.local/bin/larkflow-edge-manager`。manager 拒绝 root，不读取或迁移 Keychain、设备元数据或中央撤销状态，不注册后台服务，也不提供联网自动更新。当前 bundle 未签名、未公证，可信摘要发布、真实登录 Keychain 首次体验和合规公网入口仍缺，不是正式员工分发件。

`serve` 表示用户主动启动并保持可见的会话，不提供操作系统 daemon、开机启动或隐藏后台驻留。启动时拒绝文件系统根目录、用户主目录以及包含设备凭据的工作区。子进程环境使用最小 allowlist，不继承任意 API key、代理、SSH agent、Edge、Target 或飞书变量。显式 `--inherit-loopback-proxy` 只传递无用户名和密码的 loopback HTTP / HTTPS / SOCKS URL，远程或带凭据代理仍丢弃。目录级只读探针只限制模型可调用的本机命令读取面，不阻止中央提示、节点输入和完成任务所需的工作区内容发送给模型服务商。`--allow-model-egress` 是当前前台会话的显式确认，不是永久同意；数据分类是独立门禁，二者缺一不可。成功结果必须保存 `workspace_access`、`sensitive_paths`、`command_network`、`model_egress`、`data_policy_version`、`data_classification` 与 permission Profile 摘要。当前证据只覆盖 macOS 上的 Codex 0.147 beta permission Profile；干净 Mac 自动化使用临时测试 Keychain，其他版本、工具和真实登录 Keychain 首次交互必须重新验收。

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
