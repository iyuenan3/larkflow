# RELATIONS · larkflow

> 状态：Cloud-first Target Boundary + Paused Edge Proof · 2026-08-14

## 飞书

larkflow 复用飞书的：

- 通讯录：提供人员和组织输入。
- IM 与卡片：通知、确认、提交、异常处理和高影响操作入口。
- 任务：每个节点的责任投影。
- 云文档与云盘：输入、协作内容和交付物。

飞书不是 DAG 状态、模板权限或审计的权威。所有动作必须回到中央服务校验，再投影合法结果。

## lark-cli

MVP 只保留一种角色：中央 Feishu Adapter。它运行在服务端，以明确的企业应用身份收事件、写任务、消息和文档，并执行投影对账。

当前 As-built 已接入四个窄切片：独立 Projection Worker 从 PostgreSQL outbox 认领 Human 节点事件，通过 lark-cli 创建或完成飞书任务，并在启动时按 PostgreSQL 权威状态分页对账、补建缺失记录、重建经飞书确认已不存在的当前 Task；Task 完成事件由 legacy 单消费者写入耐久 Inbox，再由凭据侧和领域侧两个独立 Worker 先后校验飞书当前状态与 Target 领域状态；IM 命令、人员选择卡、自动节点结果、完成文档和最终通知使用独立耐久队列与稳定幂等键；`llm.generate` Agent adapter 在提交 claim 后读取冻结输入并通过 OpenAI 兼容逻辑角色生成正文。外部调用不在数据库事务中，飞书任务不是流程真相。对账、IM 窄命令、mention 角色绑定、Card 2.0 人员选择和完成投影均已在开发环境闭环。

IM 角色绑定只把本条飞书事件的 mention key 作为命令文本引用，并把对应 open_id 作为最小身份元数据耐久保存。单聊人员选择卡由凭据侧先取得有界的活跃成员候选快照，卡片只提交候选中的 person ID，回调后凭据侧再次读取目录并验证操作人、候选人与角色绑定，领域侧才冻结 Snapshot。显示名称、手填 open_id、卡片内声明的身份和客户端声明的 Owner 均不是授权依据。

可操作卡片的快速视觉回写仍属于中央 Feishu Adapter 投影。桥接器只在回调动作成功耐久落库后，使用有界的 lark-cli 调用把原卡片改成无按钮“处理中”，随后立即释放耐久动作给凭据与领域 Worker；最终 Worker 再把同一张卡片收口为成功或拒绝。处理中不携带授权结论，最终合法性仍由中央 PostgreSQL 状态、企业成员、Owner、版本和 Attempt 共同决定。

服务器使用自己的 lark-cli 与飞书应用 profile，不连接开发者电脑上的 lark-cli。持有 profile 的 OS 身份只负责飞书读写与 Inbox 校验，领域服务身份不获得飞书凭据。

员工电脑上的 Personal Agent Edge Proof 与中央 `lark-cli` 完全分离。它已经验证配对、只读领取、续租、撤销和受控分发等窄机制，但自 2026-08-14 起暂停继续投入，不属于当前产品主线或默认部署。既有代码与证据只作为未来重新评估的历史基线，不能被描述为正式员工能力。

## 企业知识与项目资料

近期只接入两类知识来源：管理员明确发布为当前企业全员可用的共享资料，以及用户直接上传到当前项目工作区的资料。前者不能包含仍受部门或个人 ACL 约束的内容；后者只能授权给当前 Instance 的合法参与者和 Attempt。向量索引、检索命中和模型上下文都不是授权依据，服务端必须先按 tenant、Instance、actor、资料范围和数据分类过滤，再形成带来源 ID 与摘要指纹的 `ContextBundle`。

个人知识库、部门知识库、通用企业搜索和自动继承源文档复杂 ACL 均后置。如果企业无法维护一份明确可全员共享的资料集，MVP 只启用项目上传，不以放宽权限换取检索覆盖率。

## Agent 与 Tool

Agent 和 Tool 是中央 Node Runner 的可替换执行器。它们收到单节点、单 Attempt 的工作输入，返回标准结果和证据。它们不能直接改图、改派 Owner、越过确认门或修改其他节点状态。

目标架构把规划与节点执行拆成两个稳定端口。`PlannerRuntime` 只生成 `DAGCandidate + ValidationReport + PlanningEvidence + Usage`，`AgentRuntime` 只完成一个节点的一次 Attempt。两者通过 `Authorized Tool Gateway` 获得短时、窄范围能力，默认只允许知识检索、上传件读取、组织与模板查询、能力查询、DAG 校验、批评和调度模拟等只读工具。数据库写入、飞书操作和其他业务副作用仍由 DAG 上显式 Tool 节点承担。

当前 Agent 实现只允许声明式 `llm.generate`，输入来自已提交的 Instance Snapshot 和直接依赖结果。稳定 Attempt 请求标识用于审计与 adapter 幂等；模型调用自身仍可能在 Worker 崩溃后重复计费，不能把请求标识描述为供应商已保证的幂等。

所有自动节点仍有唯一人类 Owner。执行失败、重试超限或需要业务判断时，由 Owner 接管。

## LLM、Pi、DeepSeek Harness 与 LangGraph

模型供应商通过可替换接口接入。Pi 可参考其精简 Agent loop、provider 抽象与 session 事件模型；DeepSeek Harness 可实验其插件 seam、类型化工具组合、PTC 和受限 Subagent。它们都不是当前依赖，也不能直接接入领域仓储、飞书凭据或生产密钥。

PTC 只允许在隔离的 Planner Attempt 中组合只读工具并返回候选图，不能成为业务 DAG 或草稿写入者。Subagent 只属于一次 Planner 或 Agent Attempt，不成为业务节点、Owner 或 Human Gate。LangGraph 同样只可以作为一个复杂 Agent 节点内部的实现，不是模板格式、业务 DAG 或跨人状态的权威。

## 外部集成边界

首个目标实现是模块化单体，不承诺 Kafka、公开事件总线或微服务接口。数据库事务通过 outbox 驱动飞书副作用，为未来拆分保留清晰边界。PlannerRuntime 与 AgentRuntime 先以进程内 Python 基线实现，只有 A/B 证据证明质量或效率提升后，才考虑把 DSH 或 Pi 适配器部署成独立受限 sidecar。Edge v1 HTTP 面只作为暂停 Proof 的私有设备控制边界保留。

## 明确排除

- 自建 IM、网盘、在线文档、搜索和完整通讯录。
- 复制独立 Project、个人或部门知识库、应用市场等完整平台边界。
- 把妙搭、某个前端框架、某个模型或 LangGraph 固化为产品层。
- 在 MVP 把个人 Agent Edge 产品化，或建设通用 Knowledge、Skill、MCP 注册表与 Capability Marketplace。
- 把 Pi、DeepSeek Harness、LangGraph、模型供应商或向量数据库固化为产品层。
