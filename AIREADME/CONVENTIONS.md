# CONVENTIONS · larkflow

## 文档状态

每份设计文档必须标记以下一种状态：

- `Target`：目标产品契约，允许尚未实现。
- `As-built`：当前代码和已验证行为。
- `Target + Gap`：同时写目标和差距，必须逐项提供实现证据。
- `Historical`：旧决策或实验，不再指导新实现。

冲突优先级为：新 Accepted ADR、CORE / PRD、DAG Contract、ARCHITECTURE、SPEC。不要改写 append-only 历史来消除冲突，应新增 superseding ADR。

## Python

- Python 3.10+，四空格缩进，函数和模块使用 `snake_case`，类使用 `PascalCase`，常量使用大写。
- 公共接口和领域实体写类型标注；状态转换使用纯函数或显式事务边界。
- 测试不得访问真实飞书、网络、凭证或机器状态；复用 Mock Lark I/O、Stub LLM 和临时或内存数据库。
- 外部写必须有稳定幂等键；租户级查询、唯一键、缓存和事件包含 `tenant_id`。

## 领域命名

- `Template` 是跨版本身份，`TemplateVersion` 是不可变版本。
- `Instance.template_version_id` 可以为空；无模板实例仍保存完整 Instance Snapshot。
- `Instance.graph_revision` 用于编辑和高影响操作的乐观并发控制。
- `NodeInstance.owner_person_id` 是唯一人类责任人。
- `executor` 只使用 `human / agent / tool`，不得用 assignee 表示 Agent。
- `NodeAttempt` 表示一次执行、提交和质量判定轮次。
- `Projection` 表示飞书对象映射，不决定业务状态。
- `rejected` 或重启创建新 Attempt，图边不得指回祖先。

MVP 不使用 `parent_instance_id`、`CapabilityLease`、Knowledge/Skill/MCP registry 或 Kafka topic 作为必需领域概念。

## DAG Contract

- v0.2 ID 使用 lower `snake_case`，展示名可以中文。
- 新业务优先增加模板或结构化无模板定义，不新增按业务命名的 Python executor 类型。
- 每个节点必须有唯一 Owner、可判定目标、输出和验收条件。
- 模板禁止 Secret、token、真实人员 ID、个人设备和供应商运行时 state。
- 修改 [DAG_TEMPLATE_SPEC.md](DAG_TEMPLATE_SPEC.md) 的强制不变量时，必须新增 ADR，并同步 PRD 和 ARCHITECTURE。

当前 `larkflow/templates/*.yaml` 是 legacy compact form。代码迁移完成前，不得把它们描述为符合 v0.2；兼容范围以 [SPEC.md](SPEC.md) 为准。

## 状态与命令

- 状态转换命令携带 actor、tenant、instance、node、attempt 和 expected revision。
- 客户端传入的人员 ID、角色、权限、状态和影响范围都不可信，服务端必须重算。
- 生成、编辑和重启使用预览命令与确认命令两步完成。
- 审计在业务事务成功后追加，包含来源、correlation ID 和前后状态。
- 飞书事件按 at-least-once 处理；重复无副作用，乱序由版本检查拒绝或延后。
- 可操作卡片必须先耐久记录动作，再尽快撤下控件并显示处理中状态，最终收口为无控件的成功或拒绝状态。处理中只是接收回执，不能替代服务端授权或领域提交。
- 同一张卡片只允许一个有效提交动作。历史重复回调通过逻辑失效保留，不为建立唯一约束而物理删除。

## 安全

- 凭证、token、真实用户 ID 和生产数据库不得进入 git 或 AIREADME。
- Human、Agent 和 Tool 节点都保留唯一人类 Owner。
- Agent 与 Tool 只能提交当前节点、当前 Attempt 的结果，不能声明自身权限。
- 数据和历史使用逻辑失效或墓碑，不通过物理删除抹除审计。

## 写作与提交

- 产品结论同时写证据等级、边界、取舍和可判定验收。
- 合同、招聘等案例只作例子，通用规则必须能替换案例后仍成立。
- 不把功能设计写成市场验证，不把 legacy 测试写成 Target 已实现。
- 架构、公共契约、部署或产品范围变化要同步 AIREADME、ADR 和 CHANGELOG。
