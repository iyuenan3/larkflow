# RELATIONS · larkflow

> 状态：Target System Boundary · 2026-07-30

## 飞书：交互与内容底座

larkflow 复用飞书的：

- **通讯录**：同步人员、部门和基础组织关系，Role Slot 的候选来源。
- **IM / 卡片**：通知、选择执行方式、提交、验收和升级入口。
- **任务**：把工作包投影为分配给真实人员的持久待办。
- **云文档 / 云盘**：承载输入、协作内容和交付物。

飞书不是 DAG 状态、模板权限或审计的权威。所有客户端动作必须回到中央控制面校验，再投影结果。

## lark-cli：两种部署角色

1. **中央 Feishu Adapter**：运行在 ECS，使用企业应用身份收事件、写任务/消息/文档并执行对账。
2. **个人 Agent Edge**：运行在员工电脑，连接员工选择的 Claude/Codex，领取本人工作包并使用短时能力授权执行。

两者共享协议和飞书能力封装，但身份、凭证、权限和生命周期不同。不得用一个中央 lark-cli 进程假装所有员工的个人 Agent，也不得让个人端持有企业应用全局权限。

中央服务应提供签名安装包、版本要求、注册向导、设备撤销和升级策略。设备离线只影响 Agent 执行，不影响人的待办和工作流推进决策。

## Claude / Codex / other Agents

个人 Agent 是可替换执行器。larkflow 向其提供标准 Work Package Envelope 和 Capability Lease，接收标准 Result Envelope。Agent 不直接修改图、改派责任人、通过人类 Gate 或访问模板未声明的企业资源。

## Knowledge, Skill and MCP

中央 Capability Registry 管理逻辑名称、版本、租户范围、适用角色、数据策略和 Secret 引用。模板声明“需要什么”，运行时根据责任人和节点签发“这一次允许什么”。

- Knowledge：企业知识索引或飞书内容范围。
- Skill：可复用工作方法、prompt 资产或执行包。
- MCP：外部工具集合及允许调用的方法。

这些资源服务于模板和 Agent，但不成为 DAG 节点责任主体。

## LLM and LangGraph

中央 LLM 可执行无人值守的 AI 辅助节点；个人模型由员工 Agent 驱动。模型供应商通过可替换接口接入。LangGraph 是某些 AI Node Run 的可选内部实现，不是产品业务 DAG 的外部契约。

## Product consumers

- 飞书内的参与人、发起人、部门主管和管理员。
- 模板编辑/治理界面及运营控制台。
- 经授权的本地 Agent。
- 后续企业系统集成方；只能通过稳定 API / event contract，不读取数据库或 LangGraph checkpoint。

## 明确排除

- 自建 IM、网盘、在线文档和完整通讯录。
- 把 CC730 的“替代飞书”产品边界带入 larkflow。
- 把妙搭、某个前端框架、某个模型或 LangGraph 固化成不可替换的产品层。
