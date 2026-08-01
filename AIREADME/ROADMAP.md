# ROADMAP · larkflow

> 状态：Target Delivery Plan · 2026-08-01
>
> 原来的三级协作、个人 Agent Edge 和完整能力治理路线已移出近期范围。现有代码作为 legacy 机制原型保留。

## Completed · Phase 0 既有设计简化与一致性核验

目标：以既有设计为底稿，围绕最小闭环做减法，并切断 Target 与 As-built 的混写。

- 核对既有 AIREADME 的范围、依赖和实现边界。
- 固定单层 DAG、模板可选、草稿确认、未来区域编辑和节点重启范围。
- 明确每节点唯一人类 Owner，Human、Agent、Tool 只表示执行器。
- 从近期范围移除子 DAG、个人 Agent Edge、Capability Lease、RAG、Kafka 和复杂模板治理。
- 保留飞书 adapter、幂等、对账、权限纯函数和重算机制的迁移价值。
- 统一 README、PRD、架构、契约、路线图和包描述。

**Exit gate：** 每项 MVP 都有明确的产品理由和可判定验收；核心文档无范围冲突；市场结论仍标记为未知。

## Now · Phase 1 中央工作流基础

目标：一个企业可以从模板或无模板定义启动单层 DAG，并在飞书中可靠推进。

- 已完成领域内核：不可变 Instance Snapshot、DAG Contract 核验、草稿确认、NodeInstance、Attempt、显式状态迁移、Scheduler、中央 Node Runner、claim 和仓储 Port。
- 已完成 PostgreSQL 14 第一版 schema：Template、TemplateVersion、Instance、NodeInstance、Attempt、Projection、Audit、Outbox。
- 已完成 Instance 聚合事务仓储、JSONB 快照、乐观并发、追加型 Audit、带租约 Outbox 和 package-data migration；真实 PostgreSQL 14 一次性数据库集成验证已通过。
- 已完成单步 Runtime Worker、持久化 runnable scan、Worker 身份认领、精确租约到期恢复与稳定 Attempt 幂等键；真实 PostgreSQL 双 Worker 竞争和崩溃恢复验证已通过。
- 已完成独立 `larkflow-target` CLI、常驻轮询、有界退避、SIGTERM 干净停机、adapter 能力过滤与 systemd 服务装配；SIGKILL 后新 Worker 接管同一 Attempt 已在真机验证。
- 已完成独立 Projection Worker、事件类型过滤、Task adapter、稳定幂等键、Projection 落库、失败重试和 systemd 服务装配；测试组织中的 Human Task 创建与完成闭环已真实通过。
- 已完成 Task 完成事件的耐久入站：legacy 保持 EventKey 单消费者，以 event ID 去重写入 PostgreSQL Inbox；凭据侧回读飞书 Task，领域侧再校验 Projection 绑定、当前 Attempt、唯一 Owner 和完成人后提交 Human 节点。
- `alicloud-sh` 已建立长期 Target 开发库、本机 peer authentication、每日备份、新库恢复演练和 enabled Target 服务；仍缺异机备份、PITR 与生产运行手册。
- 模板 `draft / enabled / disabled / deleted`、不可变版本和布尔锁。
- 实例 `draft`、预览、确认启动和丢弃。当前内核已实现确认与丢弃，预览接口待接入。
- 每个节点的唯一 Owner 解析与服务端授权。当前内核已拒绝非 Owner 提交，企业目录校验待接入。
- 独立业务 Scheduler 和 Human、Agent、Tool Node Runner。领域规则、持久化、常驻 Worker 与恢复扫描已落码，真实 executor adapter 待接入。
- 通用飞书命令入站、IM / Doc 投影、启动全量对账和缺失对象重建。Outbox Worker、Task 创建 / 完成与 Task 完成入站已实现。
- 从 legacy 原型提炼 adapter、事件韧性和 Mock 测试资产。

**Demo：** 从启用模板和无模板定义各创建一个草稿；确认后在飞书完成 Human、Agent、Tool 混合流程；服务重启后状态和投影一致。

## Next · Phase 2 受控变化与恢复

目标：让运行中流程可以安全修改、重做和运营，而不覆盖历史。

- 未来区域编辑、影响预览、确认和 `graph_revision` 乐观并发。
- 节点重启和完整重启的下游影响计算。
- Attempt 历史、交付物引用和质量记录。
- `pass/fail + evidence + suggestion` 质量结果与有限 Agent 重试。
- 暂停、恢复、取消、失败处理、人工接管和运维告警。
- 投影缺失重建、重复事件与乱序事件验证。

**Demo：** 修改未开始分支并拒绝过期确认；重启中间节点后只重做其下游；旧 Attempt 和审计保持可查。

## Later · 证据驱动的扩展

只有真实使用证明必要时，再评估：

- 模板子 DAG、临时子 DAG和最多三级父子契约。
- 个人 Agent Edge、设备注册、离在线状态和 Capability Lease。
- Knowledge、Skill、MCP 注册表及 RAG 模板匹配。
- 字段级锁、复杂 ACL、模板 Fork、行业分发和图形化编辑器。
- 数值评分、独立质量服务、Kafka、微服务和公开事件 API。
- 企业访谈、飞书原生对照、首个场景与商业验证。

未来研究协议保留在 [`research/phase-0/`](../research/phase-0/README.md)，目前不是工程启动门，也不能被本轮设计核验视为已经完成。

## 明确禁止

- 继续把新产品语义写入 legacy LangGraph 全局 state。
- 把 Agent 当组织责任人或可信权限来源。
- 用代码量、文档数量或测试通过数替代用户和市场证据。
- 为尚未出现的规模提前引入 Kafka、微服务或复杂多租户治理。
