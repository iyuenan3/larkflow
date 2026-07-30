# DAG Template Spec v0.1

> 状态：Draft · 产品目标契约 · 当前引擎尚未完整实现
>
> 本文中的 **必须 / 应该 / 可以** 分别表示 MUST / SHOULD / MAY。
>
> 产品范围见 [PRD.md](PRD.md)，运行时与数据权威见 [ARCHITECTURE.md](ARCHITECTURE.md)。当前 legacy YAML 兼容范围见 [SPEC.md](SPEC.md)。

## 1. 目的与边界

DAG Template 是企业可复用的工作流定义。它描述工作如何分解、由谁负责、输入输出、验收规则、是否允许展开子 DAG，以及哪些结构受治理约束。

本规范只定义**可执行 DAG 模板**，不定义企业全部业务关系。企业 Process Map 可以包含循环、汇报线和非正式协作；发布为模板前，必须收敛为有向无环图。重试、打回和返工属于实例运行记录，不得在模板中画回边。

模板不得绑定某位员工的本地 Agent。业务待办必须分配给具体的人；责任人自行决定亲自执行、使用个人 Agent，或在策略允许时创建子 DAG。

## 2. 核心概念

- **Template**：跨版本稳定的模板身份。
- **Template Version**：一次不可变的发布快照。
- **Instance**：从某个版本创建的运行中副本。
- **Role Slot**：实例化时解析为真实人员的责任角色。
- **Work Contract**：节点的目标、输入、输出和验收条件。
- **Expansion**：责任人把一个工作包展开为子 DAG。
- **Attempt**：节点或子 DAG 的一次提交与验收轮次。
- **Lock**：上游治理方禁止下游修改的结构或字段。

## 3. 强制不变量

1. 模板拓扑必须是 DAG；返工通过新 Attempt 表达。
2. 实例最多 3 层：L1 企业/跨部门，L2 部门，L3 团队/个人。L3 不得创建子 DAG。
3. `human` 节点必须有唯一的人类责任人；部门、群组和 Agent 均不能成为最终责任人。
4. Agent 可以执行人的待办，但不能替代责任人、验收人或审计身份。
5. 子 DAG 的创建和内部委派不转移父节点责任。
6. 已发布版本不可原地修改；任何修改必须生成新版本。
7. 实例必须保存完整模板快照，后续模板升级不得改变运行中实例。
8. 权限、锁、角色解析和图合法性必须由服务端校验，不信任卡片或客户端传入的身份。

## 4. 分类、生命周期与版本

`scope` 支持：

- `platform`：平台通用模板，租户只读，可 fork。
- `industry`：行业模板，租户只读，可 fork。
- `enterprise`：企业专有模板。
- `department`：部门专有模板。

生命周期为：

```text
draft -> in_review -> published -> deprecated -> archived
```

`version` 是同一 `template.id` 下从 1 开始递增的整数。发布后的修改先复制为新的 `draft`。Fork 必须记录 `source.template_id` 和 `source.version`；后续升级通过显式 diff/merge 完成，不做动态继承。

## 5. 顶层 Schema

```yaml
schema_version: "0.1"
template:
  id: contract_project
  version: 1
  name: 合同项目
  description: 跨部门完成合同起草、合并与审核
  status: draft
  scope: enterprise
  tenant_id: tenant_acme
  department_id: null
  owner_principal: "tenant_role:process_admin"
  allowed_instance_levels: [1]
  tags: [contract, cross_department]
  source: null

parameters: {}
role_slots: {}
permissions: []
locks: []
nodes: []
```

### 5.1 `template`

| 字段 | 规则 |
|---|---|
| `id` | 稳定的 lower `snake_case` 标识；租户与 scope 内唯一 |
| `version` | 正整数；发布后不可变 |
| `status` | 生命周期状态之一 |
| `tenant_id` | `enterprise/department` 必填 |
| `department_id` | `department` 必填，其余为 `null` |
| `owner_principal` | 模板治理责任主体，如 `tenant_role:process_admin` |
| `allowed_instance_levels` | 非空子集 `[1, 2, 3]` |
| `source` | Fork 时填写 `{template_id, version}` |

`parameters` 定义实例启动参数。每项可以声明 `type`、`required`、`default`、`description` 和非敏感校验规则。密钥只能引用服务端 Secret，不得写入模板。

## 6. Role Slot 与人员绑定

```yaml
role_slots:
  legal_head:
    description: 对法律工作包最终负责的人
    required: true
    cardinality: one
    constraints:
      department: legal
      relationship: department_head
  reviewers:
    description: 可参与会签的人员
    required: true
    cardinality: many
    min: 2
```

实例化时，所有必需 Role Slot 必须解析为当前租户的 `open_id`。节点 `owner_role` 只能引用 `cardinality: one` 的 Slot；`cardinality: many` 只用于会签等参与者集合。单人 Slot 解析为零人或多人时必须阻止启动，不得把待办降级为“发给部门”。

人员绑定属于 Instance Snapshot。人员离职或组织调整后，必须通过有审计记录的实例改派处理。

## 7. 节点 Schema

```yaml
- id: legal_work
  label: 完成法律部分
  executor: human
  role: produce
  deps: []
  owner_role: legal_head
  signal: task_complete
  contract:
    objective: 提交可合并的法律条款
    inputs:
      - ref: instance.inputs.deal_brief
    outputs:
      - id: legal_section
        type: document
        required: true
    acceptance:
      - 不得遗留未标注的法律风险
  execution_policy:
    allowed_modes: [manual, personal_agent, child_dag]
    decided_by: owner
  resources:
    knowledge:
      - {ref: "kb:historical_contracts", access: read}
    skills:
      - {ref: "skill:legal_drafting", version: 2}
    mcp:
      - {ref: "mcp:contract_archive", tools: [search]}
  expansion:
    policy: allowed
    allowed_parent_levels: [1, 2]
    parent_visibility: contract_summary
    child_template_selector:
      scopes: [enterprise, department]
      tags_any: [legal]
  deliverable:
    output: legal_section
    region: whole
```

### 7.1 通用字段

| 字段 | 规则 |
|---|---|
| `id` | 模板内唯一的 lower `snake_case` 标识 |
| `executor` | `human \| llm \| tool` |
| `role` | `produce \| gate` |
| `deps` | 已存在节点 ID 数组；共同构成 DAG 边 |
| `owner_role` | `human` 必填；引用单人 Role Slot |
| `contract` | `produce` 必填；定义目标、输入、输出、验收 |
| `when` | 可选，沿用 `{upstream_decision_id: value}`；只能引用祖先节点 |

### 7.2 人、Agent 与工具

- `human`：服务端向 `owner_role` 解析出的人员创建飞书待办。`execution_policy` 只控制其可选执行方式。
- `personal_agent`：人的本地 Agent 代为执行，提交必须记录责任人、Agent 身份、设备和结果来源；电脑离线不影响待办存在。
- `llm`：中央云端 AI 节点，必须声明 `prompt` 与 `model_role`。它不能作为最终放行者。
- `tool`：确定性能力节点，必须声明 `tool.kind`；外部副作用应该声明人类 `owner_role` 或由后续人工 Gate 验收。

人类 Gate 的 `allowed_modes` 只能是 `[manual]`。个人 Agent 可以提供建议，但批准或打回必须由责任人本人提交。

`execution_policy` 省略时，`human/produce` 和 `human/gate` 均默认为 `[manual]`；`llm/tool` 不允许声明该字段。`expansion` 省略时视为 `forbidden`。出现 `child_dag` 时必须同时提供非 `forbidden` 的 Expansion；`required` 时唯一执行方式必须是 `child_dag`。

### 7.3 Work Contract、引用与交付物

`contract.inputs[].ref` 只允许：

- `instance.inputs.<parameter_id>`
- `nodes.<ancestor_id>.outputs.<output_id>`

节点输出引用必须指向其传递祖先。v0.1 基础类型为 `text | number | boolean | document | document_ref | file | record | json`。`deliverable.output` 把一个 document/file 输出映射到飞书容器及 `region`；结构化输出直接登记到实例 Output Registry。

节点可以用 `sla` 声明 `target_duration`、`warn_before` 和 `escalation_role`，时长使用 ISO 8601，例如 `PT24H`。没有 SLA 不阻止发布，但校验器应该提示。

### 7.4 企业资源与能力

`resources` 可以声明 `knowledge`、`skills` 和 `mcp` 依赖。引用必须指向租户 Capability Registry 中存在且当前版本可用的资源。

资源声明只表示运行所需能力，不构成授权，也不得包含 token、endpoint secret 或本地路径。中央或个人 Agent 开始执行时，服务端根据当前责任人权限签发短期 Capability Lease；Lease 必须绑定 `tenant + instance + node + attempt + actor/agent + device`，并限制到声明的知识域、Skill 版本和 MCP tools。

### 7.5 Gate

Gate 的 `approval_policy` 支持 `auto | single | any | all | {threshold: <positive_int>}`。`auto` 只能用于确定性 `tool`；其余 Gate 必须由 `human` 执行并使用 `card_action`。

`single` 只由 `owner_role` 决定。`any/all/threshold` 必须增加：

```yaml
vote:
  voter_roles: [reviewers]
  primary_role: legal_head
```

`voter_roles` 可以引用单人或多人 Slot，实例化后按 `open_id` 去重。`primary_role` 必须是单人 Slot，默认等于节点 `owner_role`，负责处理平票、撤回和异常升级，但不得绕过既定阈值。

打回目标在实例运行时选择，必须是 Gate 的传递祖先且通过权限校验。模板不得用回边表示“审核不通过”。

## 8. 子 DAG 与三层模型

子 DAG 是人类责任节点的展开，不是一种执行人：

- `expansion.policy` 为 `forbidden | allowed | required`。
- `allowed` 表示责任人可在手工、个人 Agent、子 DAG 之间选择。
- `required` 表示父节点只能在子 DAG 验收通过后完成。
- 一个父节点同一 Attempt 最多关联一个活动的 `child_instance_id`。
- 子实例层级恒为 `parent.level + 1`，且必须满足子模板的 `allowed_instance_levels`。
- 父级默认只看子 DAG 的责任人、总体状态、阻塞原因、最终交付物和审计摘要；内部节点按子模板权限展示。
- 父级不得直接改动子 DAG 内部节点。父节点拒收时，子 DAG 新增 Attempt，由子 DAG 责任人决定内部打回范围。

父节点与子实例通过 `contract.inputs/outputs` 连接。创建子实例时冻结输入映射；子实例只有在必需输出齐全后才能提交父级验收。

`parent_visibility` 支持 `contract_summary | full`，默认前者；`full` 仍需同时通过子模板 ACL。非 `forbidden` 的 Expansion 必须提供 `allowed_parent_levels` 与 `child_template_selector`。

父级可见的子实例状态固定为 `created | running | blocked | submitted | accepted | rejected | cancelled`。子实例 `submitted` 后由父节点责任人验收；只有 `accepted` 才能完成父节点。`rejected` 开启新的子实例 Attempt，不创建模板回边。

## 9. 权限与锁

模板权限动作固定为：

```text
view, instantiate, edit_bindings, edit_node_config, edit_structure,
review, publish, deprecate, manage_locks
```

```yaml
permissions:
  - principal: "tenant_role:process_admin"
    actions: [view, instantiate, edit_bindings, edit_node_config,
              edit_structure, review, publish, deprecate]
  - principal: "department:legal"
    actions: [view, instantiate]

locks:
  - id: keep_legal_gate
    owner_scope: enterprise
    targets:
      - "node:legal_review"
      - "edge:legal_work->legal_review"
      - "field:legal_review.approval_policy"
```

Lock target 支持 `node:<id>`、`edge:<from>-><to>`、`field:<id>.<path>` 和 `template:<path>`。Fork 可以增加节点和边，但不得删除或修改继承的 Lock target。只有 Lock 所有作用域中拥有 `manage_locks` 的主体，才能在新的源模板版本中解除锁。

`principal` 支持 `tenant_role:<id>`、`department:<id>` 和 `user:<open_id>`。授权时必须结合当前组织成员关系；租户安全管理员的实时禁用或拒绝规则优先于模板和实例权限。

## 10. 实例化与运行时变更

实例化顺序必须为：

1. 选择一个状态为 `published` 的版本。
2. 校验参数与当前操作者的 `instantiate` 权限。
3. 解析 Role Slot 到真实人员。
4. 根据父实例计算层级并校验三层限制。
5. 保存模板、参数、人员绑定、权限策略与锁的不可变快照。
6. 在同一业务事务提交后创建飞书待办和通知。

运行中改图只改变 Instance，不反写 Template。它必须只触及尚未开始的子图，遵守原有 Lock 和权限，并记录 `by`、`reason`、变更前后 revision。已开始、已完成或已审计的节点不得被静默删除。

## 11. 发布校验

进入 `published` 前必须通过：

1. Schema 版本和必填字段合法；未知扩展字段只能使用 `x-` 前缀。
2. 节点 ID 唯一，`deps` 均存在，图中无环且至少有一个根节点和终点。
3. 所有 `when` 只引用传递祖先。
4. 所有 Role Slot、参数、输入和输出引用可解析。
5. 每个 `human` 节点有唯一责任人；每个 `produce` 节点有 Work Contract。
6. Gate 的执行器、信号、投票人和放行策略符合第 7.5 节。
7. Expansion 只出现在 `human` 节点，且不会产生超过 L3 的实例。
8. 声明的 Knowledge、Skill 与 MCP 资源存在，且未把资源声明误作授权。
9. Lock target 和权限主体语法有效；Fork 未破坏继承锁。
10. 不包含密钥、真实访问令牌或跨租户人员 ID。

校验器应该同时返回错误和非阻断 lint，例如缺少 SLA、验收标准过于模糊、存在无人工验收的高风险 AI 输出。

## 12. 合同项目示例

```yaml
schema_version: "0.1"
template:
  id: contract_project
  version: 1
  name: 合同项目
  description: 商务、法务和财务并行交付后合并并由发起人审核
  status: draft
  scope: enterprise
  tenant_id: tenant_acme
  department_id: null
  owner_principal: "tenant_role:process_admin"
  allowed_instance_levels: [1]
  tags: [contract]
  source: null

parameters:
  deal_brief:
    type: document_ref
    required: true

role_slots:
  sponsor: {required: true, cardinality: one}
  business_head: {required: true, cardinality: one}
  legal_head: {required: true, cardinality: one}
  finance_head: {required: true, cardinality: one}

permissions:
  - principal: "tenant_role:process_admin"
    actions: [view, instantiate, edit_bindings, edit_node_config,
              edit_structure, review, publish, deprecate, manage_locks]
  - principal: "tenant_role:employee"
    actions: [view, instantiate]
locks: []
nodes:
  - id: business_part
    label: 完成商务部分
    executor: human
    role: produce
    deps: []
    owner_role: business_head
    signal: task_complete
    contract:
      objective: 提交商务条款
      inputs: [{ref: instance.inputs.deal_brief}]
      outputs: [{id: business_section, type: document, required: true}]
      acceptance: [金额、交付和验收条件明确]
    execution_policy:
      allowed_modes: [manual, personal_agent, child_dag]
      decided_by: owner
    expansion: &department_expansion
      policy: allowed
      allowed_parent_levels: [1, 2]
      parent_visibility: contract_summary
      child_template_selector: {scopes: [enterprise, department]}
    deliverable: {output: business_section, region: whole}

  - id: legal_part
    label: 完成法务部分
    executor: human
    role: produce
    deps: []
    owner_role: legal_head
    signal: task_complete
    contract:
      objective: 提交法律条款
      inputs: [{ref: instance.inputs.deal_brief}]
      outputs: [{id: legal_section, type: document, required: true}]
      acceptance: [风险、违约责任和争议解决条款明确]
    execution_policy:
      allowed_modes: [manual, personal_agent, child_dag]
      decided_by: owner
    expansion: *department_expansion
    resources:
      knowledge:
        - {ref: "kb:historical_contracts", access: read}
      skills:
        - {ref: "skill:legal_drafting", version: 2}
    deliverable: {output: legal_section, region: whole}

  - id: finance_part
    label: 完成财务部分
    executor: human
    role: produce
    deps: []
    owner_role: finance_head
    signal: task_complete
    contract:
      objective: 提交财务与税务意见
      inputs: [{ref: instance.inputs.deal_brief}]
      outputs: [{id: finance_section, type: document, required: true}]
      acceptance: [付款、税务和票据要求明确]
    execution_policy:
      allowed_modes: [manual, personal_agent, child_dag]
      decided_by: owner
    expansion: *department_expansion
    deliverable: {output: finance_section, region: whole}

  - id: merge_contract
    label: AI 合并合同
    executor: llm
    role: produce
    deps: [business_part, legal_part, finance_part]
    prompt: 合并三部分，保留冲突标记，不得自行裁决实质冲突。
    model_role: editor
    contract:
      objective: 生成供发起人审核的合同草案
      inputs:
        - {ref: nodes.business_part.outputs.business_section}
        - {ref: nodes.legal_part.outputs.legal_section}
        - {ref: nodes.finance_part.outputs.finance_section}
      outputs: [{id: merged_contract, type: document, required: true}]
      acceptance: [三部分均被引用, 所有冲突均显式标记]
    deliverable: {output: merged_contract, region: whole}

  - id: sponsor_review
    label: 发起人审核
    executor: human
    role: gate
    deps: [merge_contract]
    owner_role: sponsor
    signal: card_action
    approval_policy: single

  - id: deliver_contract
    label: 交付合同
    executor: tool
    role: produce
    deps: [sponsor_review]
    tool: {kind: notify, args: {recipient: reporter}}
    contract:
      objective: 通知发起人并登记最终合同
      inputs: [{ref: nodes.merge_contract.outputs.merged_contract}]
      outputs: [{id: delivery_receipt, type: record, required: true}]
      acceptance: [通知已送达且合同链接可访问]
```

`sponsor_review` 审核不通过时，由实例命令选择 `merge_contract` 或三个部门节点中的合法祖先打回；它不是模板中的第二条回边。

## 13. 与 LangGraph 的边界

DAG Template 是产品层契约，不暴露 `StateGraph`、`Send`、reducer、checkpoint 或 `thread_id`。目标产品由独立业务 Scheduler 解释模板，不能要求某个 Agent 框架才能读取或推进实例。

Template、Instance、Task、Attempt、权限和审计的权威状态存放在产品数据库。LangGraph 仅可实现某次复杂 AI Node Run，checkpoint 只保存该次节点执行的内部状态，不作为整个企业工作流的事实来源。legacy 原型把业务 DAG 编译/解释到 LangGraph 的做法只用于迁移，不是 v0.1 目标能力。

## 14. 当前兼容性与 v0.2 候选

当前 `larkflow/templates/*.yaml` 是 legacy compact form：加载器只消费 `id/name/nodes`，执行器只支持 `human/llm/tool`，尚不识别 Template Version、Role Slot、权限、锁和子 DAG 契约。旧模板不得被静默标记为符合 v0.1；迁移应经过显式转换和发布校验。

v0.2 再决定：通用条件表达式、SLA/升级策略的完整语法、动态参与人及加签/转签、模板签名与跨租户分发、Fork 的三方 diff/merge 协议。
