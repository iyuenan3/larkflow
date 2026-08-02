# RELATIONS · larkflow

> 状态：Target System Boundary + Experimental Edge · 2026-08-02

## 飞书

larkflow 复用飞书的：

- 通讯录：提供人员和组织输入。
- IM 与卡片：通知、确认、提交、异常处理和高影响操作入口。
- 任务：每个节点的责任投影。
- 云文档与云盘：输入、协作内容和交付物。

飞书不是 DAG 状态、模板权限或审计的权威。所有动作必须回到中央服务校验，再投影合法结果。

## lark-cli

MVP 只保留一种角色：中央 Feishu Adapter。它运行在服务端，以明确的企业应用身份收事件、写任务、消息和文档，并执行投影对账。

当前 As-built 已接入三个窄切片：独立 Projection Worker 从 PostgreSQL outbox 认领 Human 节点事件，通过 lark-cli 创建或完成飞书任务，并在启动时按 PostgreSQL 权威状态分页对账、补建缺失记录、重建经飞书确认已不存在的当前 Task；Task 完成事件由 legacy 单消费者写入耐久 Inbox，再由凭据侧和领域侧两个独立 Worker 先后校验飞书当前状态与 Target 领域状态；`llm.generate` Agent adapter 在提交 claim 后读取冻结输入并通过 OpenAI 兼容逻辑角色生成正文。外部调用不在数据库事务中，飞书任务不是流程真相。对账已在开发环境完成现有绑定重入、真实 Task 删除重建及修复后完成入站验收；IM、Doc 与通用命令入站仍未接入。

服务器使用自己的 lark-cli 与飞书应用 profile，不连接开发者电脑上的 lark-cli。持有 profile 的 OS 身份只负责飞书读写与 Inbox 校验，领域服务身份不获得飞书凭据。

员工电脑上的 Personal Agent Edge Proof 与中央 `lark-cli` 完全分离。Edge 使用一次性配对码取得可撤销设备凭据，再通过中央私有 HTTPS API 长轮询领取 `personal.readonly` 节点；它不登录飞书、不持有企业应用 profile，也不复用开发者电脑上的 `lark-cli`。员工若要在本机 Agent 内另行使用飞书能力，那是个人可选能力，不是 Edge 传输层或中央授权依据。

## Agent 与 Tool

Agent 和 Tool 是中央 Node Runner 的可替换执行器。它们收到单节点、单 Attempt 的工作输入，返回标准结果和证据。它们不能直接改图、改派 Owner、越过确认门或修改其他节点状态。

当前 Agent 实现只允许声明式 `llm.generate`，输入来自已提交的 Instance Snapshot 和直接依赖结果。稳定 Attempt 请求标识用于审计与 adapter 幂等；模型调用自身仍可能在 Worker 崩溃后重复计费，不能把请求标识描述为供应商已保证的幂等。

所有自动节点仍有唯一人类 Owner。执行失败、重试超限或需要业务判断时，由 Owner 接管。

## LLM 与 LangGraph

模型供应商通过可替换接口接入。LangGraph 只可以作为一个复杂 Agent 节点内部的实现，不是模板格式、业务 DAG 或跨人状态的权威。

## 外部集成边界

首个目标实现是模块化单体，不承诺 Kafka、公开事件总线或微服务接口。数据库事务通过 outbox 驱动飞书副作用，为未来拆分保留清晰边界。Edge v1 HTTP 面是私有、窄能力的设备控制边界，不是公开业务 API；Gateway 只允许监听 loopback，远程访问必须经独立 HTTPS 反向代理。

## 明确排除

- 自建 IM、网盘、在线文档、搜索和完整通讯录。
- 复制独立 Project、知识库、应用市场等完整平台边界。
- 把妙搭、某个前端框架、某个模型或 LangGraph 固化为产品层。
- 在 MVP 把个人 Agent Edge 产品化，或建设 Knowledge/Skill/MCP 注册表与通用 Capability Lease。
