# DAG Contract v0.2

> 状态：Target + Partial As-built · 既有契约简化版 · 模板生命周期、实例化和草稿预览已实现，编辑与重启仍待实现
>
> 本文中的“必须、应该、可以”分别表示 MUST、SHOULD、MAY。当前 legacy YAML 兼容范围见 [SPEC.md](SPEC.md)。

## 1. 目的

本契约定义 larkflow MVP 的模板、实例和节点图。模板是可选的复用来源；Instance Snapshot 才是运行时契约。模板和实例图都必须是有向无环图，重试、打回和重启通过 Attempt 表达。

v0.2 是此前 v0.1 设计的简化，不引入子 DAG、资源注册表、字段级锁或复杂模板治理。

## 2. 核心概念

- **Template**：跨版本稳定的模板身份。
- **TemplateVersion**：不可变的模板图版本。
- **Instance**：从模板或无模板结构化定义创建的运行副本。
- **NodeInstance**：实例内的节点及其当前状态。
- **Owner**：对节点结果负责的唯一真实人员。
- **Executor**：`human / agent / tool` 三种执行方式。
- **Attempt**：节点一次执行、提交和质量判定轮次。
- **Projection**：节点在飞书中的任务、卡片或等价责任入口。

## 3. 强制不变量

1. 节点 ID 在图内唯一，所有依赖存在，拓扑无环。
2. 每个节点必须绑定一个唯一人类 Owner。
3. Agent 和 Tool 可以执行节点，但不能成为 Owner 或可信身份来源。
4. 实例必须先是草稿，经人确认后才能运行。
5. 模板版本不可变，实例保存完整图、输入和责任绑定快照。
6. 运行中编辑只触及未开始区域，并经过预览、确认和 revision 校验。
7. 重启不增加回边，历史 Attempt 不可被覆盖。
8. 权限、状态、责任和图合法性必须由服务端计算。

## 4. 模板生命周期

```text
draft -> enabled -> disabled -> deleted
```

- `draft` 可以追加新版本，但已存在版本本身不可修改。
- `enabled` 可以创建新实例，但版本内容不可变。
- `disabled` 不再创建新实例，可以追加新版本，已有实例不受影响。
- `deleted` 是逻辑终态，历史和审计继续保留。
- 对现有版本的任何内容修改都生成新的 `version`。
- `locked: true` 表示由该版本创建的实例不允许结构编辑。v0.2 不支持字段级锁。
- 当前启用入口始终使用最新版本，不另存可漂移的 active pointer。要修改已启用模板，必须按 `disable -> append version -> enable` 执行。
- 模板 aggregate 以独立 version 做 compare-and-swap，每次生命周期变化和版本追加都写入不可变审计事件。

## 5. 模板 Schema

```yaml
schema_version: "0.2"
template:
  id: contract_review
  version: 1
  name: 合同审核
  status: draft
  locked: false

parameters:
  contract_ref:
    type: document_ref
    required: true

nodes:
  - id: legal_review
    title: 法务审核
    deps: []
    executor: human
    owner_role: legal_owner
    work:
      objective: 给出法律风险意见
      inputs:
        - instance_inputs.contract_ref
      outputs:
        - id: legal_opinion
          type: document
          required: true
      acceptance:
        - 风险与处理建议明确
```

### 顶层字段

| 字段 | 规则 |
|---|---|
| `schema_version` | v0.2 固定为 `"0.2"` |
| `template.id` | lower `snake_case`，在租户内稳定 |
| `template.version` | 从 1 开始递增的正整数 |
| `template.status` | 模板生命周期之一 |
| `template.locked` | 布尔值，默认 `false` |
| `parameters` | 非敏感启动参数定义，可以为空 |
| `nodes` | 非空节点数组 |

Secret、token、真实人员 ID、设备 ID、本地路径和供应商运行时 state 不得写入模板。

## 6. 节点 Schema

```yaml
- id: merge_result
  title: 汇总结果
  deps: [legal_review, finance_review]
  executor: agent
  owner_role: project_owner
  work:
    objective: 汇总两份审核意见，不自行消除冲突
    inputs:
      - dependencies.legal_review
      - dependencies.finance_review
    outputs:
      - id: merged_result
        type: document
        required: true
    acceptance:
      - 两份意见均被引用
      - 冲突被明确标注
    agent:
      kind: llm.generate
      model_role: default
      instructions: 形成供项目 Owner 复核的合并意见，不能自行消除冲突
  retry:
    max_attempts: 2
```

| 字段 | 规则 |
| `id` | 图内唯一的 lower `snake_case` |
| `title` | 面向用户的工作名称 |
| `deps` | 已存在节点 ID 数组 |
| `executor` | `human / agent / tool` |
| `owner_role` | 必填，启动时解析到唯一人员 |
| `work.objective` | 必填，可判定的节点目标 |
| `work.inputs` | 只能引用实例输入或祖先节点输出 |
| `work.outputs` | 节点承诺产生的结构化结果或材料引用 |
| `work.acceptance` | 非空验收条件 |
| `work.agent.kind` | Agent 必填，当前实现只接受 `llm.generate` |
| `work.agent.model_role` | 非空逻辑模型角色，默认 `default` |
| `work.agent.instructions` | 非空节点指令，不得包含长期凭证 |
| `retry.max_attempts` | Target 字段，当前 Template Service 尚未实现，提交时会按未知字段拒绝 |

Tool 节点还应声明数据化的 `tool.kind` 和非敏感参数。Agent 节点通过 `work.agent` 声明逻辑 kind、`model_role` 和节点指令，不得把模型供应商、base URL、长期凭证或 LangGraph checkpoint 固化为业务契约。

## 7. Owner 解析

模板中的 `owner_role` 是逻辑角色。创建草稿时，调用方必须提供角色到人员的绑定。确认启动前，每个角色必须解析到当前企业中的唯一人员，否则阻止启动。

无模板实例可以直接提交 `owner_person_id`，但服务端仍要验证人员有效且调用方有权分派。人员变化只能通过有审计记录的实例操作完成。

## 8. Instance Snapshot

模板不是必需输入。实例草稿至少保存：

```yaml
instance:
  id: ins_123
  template_ref: null
  status: draft
  graph_revision: 1
  project_owner_person_id: person_001
  inputs: {}
  nodes: []
```

若来自模板，`template_version_id` 保存 `template_id:version`，并把该版本的 `locked` 值写入快照。无论来源如何，创建草稿时都必须冻结完整图、输入、Owner 绑定和验收字段，后续模板变化不回写实例。

草稿确认流程：

1. 服务端校验结构、依赖、责任和权限。
2. 返回图、Owner、执行器、材料和风险预览。
3. 用户明确确认或丢弃。
4. 确认成功后进入 `running`，创建初始 Attempt 和投影 outbox。

## 9. 状态

实例状态：

```text
draft, running, paused, done, failed, canceled, discarded
```

节点状态：

```text
pending, ready, running, waiting_human, done, failed, canceled
```

Agent 或 Tool 节点执行中使用 `running`。Human 节点可从 `ready` 进入 `waiting_human`。状态转换以服务端领域规则为准，客户端不能直接覆写状态。

## 10. 编辑

运行中编辑只改变 Instance Snapshot，不反写 Template。

1. 客户端提交目标变更和 `expected_graph_revision`。
2. 服务端验证只影响未开始节点，并返回变更 diff、影响节点与风险。
3. 用户以同一 revision 确认。
4. revision 未变化时事务提交，成功后递增。

`running`、`waiting_human`、`done` 或已有审计结果的节点不得被静默删除或改写。`locked` 模板实例不接受结构编辑。

## 11. 重启与 Attempt

节点重启的影响集合为目标节点加所有可达下游。完整重启的影响集合为所有节点。

确认重启后：

- 当前活动执行被结束或失效。
- 受影响节点创建新的 Attempt。
- 历史 Attempt、责任人、交付物和质量记录保持只读。
- 节点按当前依赖重新进入 `pending` 或 `ready`。
- 重启动作和影响集合进入审计。

## 12. 质量结果

MVP 质量契约为：

```yaml
quality:
  result: pass
  evidence:
    - 所有必需输出存在
  suggestion: null
```

`result` 只能是 `pass / fail`。失败必须说明 evidence，应该提供可执行 suggestion。Agent 失败时可以有限自动重试；Human 失败时由 Owner 明确重做。评分权重和百分制后置。

## 13. 发布和启动校验

模板启用或无模板草稿确认前必须检查：

1. Schema 与必填字段合法。
2. 节点 ID 唯一，依赖存在，图无环。
3. 所有输入只引用实例参数或已声明的直接依赖；更远祖先必须通过依赖节点显式传递。
4. 所有必需输出和验收条件存在。
5. 每个节点有唯一有效 Owner。
6. Tool kind、Agent kind、模型逻辑角色和 retry 配置在服务端允许列表内。
7. 不包含 Secret、token、真实人员 ID 或供应商运行时 state。

校验失败只能返回草稿级错误，不得部分启动。

## 14. 当前兼容性

Target 的 `target_agent_review.yaml` 已转换为本文 v0.2 形状，并通过 Template Service 实例化。其余 YAML 仍是 legacy compact form，只由 legacy 加载器消费。旧模板不能被静默标记为符合 v0.2，迁移必须经过显式转换和校验。

## 15. 后置能力

子 DAG、三级父子契约、个人 Agent Edge、Capability Lease、Knowledge/Skill/MCP 注册表、RAG 模板匹配、字段级锁、复杂 ACL、模板 Fork、Kafka 和公开事件协议均不属于 v0.2。
