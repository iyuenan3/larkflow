# CONVENTIONS · larkflow

## 文档状态

每份设计文档必须在开头标记以下一种状态：

- `Target`：目标产品契约，允许尚未实现。
- `As-built`：当前代码和已验证行为。
- `Target + Gap`：同时写目标和差距，必须逐项给实现证据。
- `Historical`：保留的旧决策或实验，不再指导新实现。

发现冲突时，优先级为：新 Accepted ADR → CORE / PRD → DAG Template Spec → ARCHITECTURE → SPEC(as-built)。不要通过改写 append-only 历史消除冲突，应新增 superseding ADR。

## Python

- Python 3.10+，四空格缩进，函数/模块用 `snake_case`，类用 `PascalCase`，常量用大写。
- 公共接口和领域实体写类型标注；状态转换保持纯函数或显式事务边界。
- 测试不得访问真实飞书、网络、凭证或机器状态；复用 mock Lark I/O、stub LLM 和内存数据库。
- 外部写动作必须有稳定幂等键；租户级查询、唯一键、缓存和事件都必须包含 `tenant_id`。

## 领域命名

- `Template` 是跨版本身份，`TemplateVersion` 是不可变发布快照。
- `Instance` 从一个版本创建；`NodeAttempt` 表示一次执行/提交轮次。
- `Assignment.owner_person_id` 是唯一人类责任人；不要命名为 `agent_assignee`。
- `AgentRun` / `NodeRun` 是执行记录，不是责任主体。
- `parent_instance_id` 与 `level` 表达三级关系；不要把子流程隐藏在 LangGraph state。
- `rejected` 创建新 Attempt；模板边和实例边不得指回祖先。

## DAG Template

- v0.1 ID 使用 lower `snake_case`，业务展示名可以中文。
- 新业务优先增加模板和能力注册项，不新增按业务命名的 Python executor 类型。
- 人工节点必须引用 `cardinality: one` 的 Role Slot。
- 模板只引用逻辑 Knowledge/Skill/MCP 资源，禁止 secret、个人设备 ID 和供应商运行时 state。
- 修改 [DAG_TEMPLATE_SPEC.md](DAG_TEMPLATE_SPEC.md) 的强制不变量时，必须同时新增 ADR，并更新 PRD/ARCHITECTURE。

当前 `larkflow/templates/*.yaml` 是 legacy compact form。代码迁移完成前，新 v0.1 示例不要假装可由 `load_template` 执行；兼容范围以 [SPEC.md](SPEC.md) 为准。

## 状态与事件

- 状态转换命令必须携带 actor、tenant、instance、node、attempt 和 expected version。
- 客户端传入的 `open_id`、角色、权限和可打回节点都不可信，服务端必须重算。
- 审计记录在动作成功后追加，包含来源（Feishu、web、edge、system）和 correlation ID。
- 飞书与边缘事件按 at-least-once 处理；重复应无副作用，乱序应被版本检查拒绝或延后。

## 安全

- 凭证、token、真实用户 ID 和生产数据库不得进入 git 或 AIREADME。
- Capability Lease 必须短时、可撤销并限定到单节点/Attempt/资源集合。
- 人类 Gate 不接受 Agent 代签；个人 Agent 的提交必须保留责任人和 Agent 来源。
- 所有跨父子 DAG 的读取都经过 Contract Summary / explicit grant，不默认继承父层权限。

## 写作与提交

- 写产品结论时同时写边界、取舍和可判定验收，不使用“智能化”“赋能”等不可验证措辞。
- 合同、招聘等案例只作例子；通用规则必须能替换案例后仍成立。
- 架构、公共契约、部署或产品范围变化要同步 AIREADME、相关 ADR 和 CHANGELOG。
