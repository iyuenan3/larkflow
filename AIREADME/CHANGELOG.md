# CHANGELOG · larkflow

## v0.18.0-draft · 2026-08-01 · Target Task 启动对账与缺失重建（ADR-063）

- Added：Projection 常驻循环在消费 Outbox 前按 Instance ID 分页对账；新增 `reconcile-projections` 运维命令与 `LARKFLOW_TARGET_PROJECTION_RECONCILE_BATCH_SIZE`。
- Recovery：当前 `waiting_human` 节点没有 Projection 时使用原稳定幂等键补建；已绑定 Task 只在飞书明确返回 `1470404` 时使用带 repair generation 的新稳定键重建，并以旧绑定为并发条件换绑。
- Safety：权限、网络、限流或服务端错误不会被误判为 Task 删除；单个实例失败不阻塞其他实例；已终止节点不补发历史 Task。
- Verified：完整离线套件 `617 passed, 7 skipped`；回归测试覆盖缺失记录、外部删除、丢失响应、逐实例容错、终态边界、启动顺序和错误码分流。一次性 PostgreSQL 14 数据库应用六份 migration，回读补建 1、重建 1、重入 1，随后删除数据库与临时上传件。
- Deployment：内容提交 `99af528` 构建 wheel，SHA-256 为 `ed5d597db3d593322a549e02700543f32ef317b2e0dfdab4a2605f7f9fb119e4`。部署前备份成功，四个 Target 服务与 legacy 消费者均回读 active、`NRestarts=0`；启动与手动对账均为 1 个实例、2 个节点、2 条绑定不变且 0 失败。
- Boundary：开发环境尚未对真实飞书 Task 执行删除后重建，也不代表生产发布。

## v0.17.1-draft · 2026-08-01 · Inbox 验证有限重试

- Fixed：凭据侧 Task 验证不再永久重试。默认最多尝试 24 次，达到预算后写入不可再认领的 `exhausted` 终态，保留终止时间、失败阶段、结果和最后错误。
- Added：`LARKFLOW_TARGET_INBOUND_VERIFICATION_MAX_ATTEMPTS` 配置、Verification Worker 的 `exhausted` 结构化计数，以及 PostgreSQL migration `0006_inbox_verification_exhaustion`。
- Verified：先用回归测试证明旧实现会无限重试，再完成 `608 passed, 6 skipped` 全量离线验证。一次性 PostgreSQL 14 数据库应用六份 migration，验证 `exhausted` 写入及隔天不可再次 claim，随后回读确认测试库与临时目录均已删除。
- Deployment：升级前备份成功；发布 wheel SHA-256 为 `42c83286964d4fd44f254cc85dc39714e62b953a155391b1faf1059e05287d27`，长期开发库已回读六份 migration，四个 Target 服务与 legacy 消费者均为 active。一条历史失败事件在真实退避到期后进入 `exhausted`，日志回读 `exhausted=1`。该验证仅代表开发环境，不是生产发布。

## v0.17.0-draft · 2026-08-01 · Target 模板生命周期与正式草稿入口（ADR-061）

- Added：`TemplateService`、Template aggregate、不可变 `TemplateVersion`、追加型模板审计、独立 aggregate version 乐观并发，以及 PostgreSQL migration `0005_template_lifecycle`。
- Added：模板创建、追加版本、启用、停用、逻辑删除、查询、从模板创建冻结草稿和 Owner 只读预览 CLI。模板参数和 `owner_role` 在实例化时解析，Snapshot 保存 `template_version_id` 与 `locked`。
- Changed：`target_agent_review.yaml` 从手填 Instance 和人员 ID 的样例改为可发布的 v0.2 Target 模板。启用模板固定使用最新版本，修改路径为 `disable -> append version -> enable`。
- Security：模板拒绝真实人员 ID、模型供应商配置和未知字段；尚未实现的语义不能静默进入版本后在实例化时丢失。已启用版本不可原地修改，草稿预览不写状态或审计，确认启动仍是独立的人类命令。
- Verified：完整离线套件 `607 passed, 6 skipped`。一次性 PostgreSQL 14 数据库验证五份 migration 重入、模板并发启用恰好一胜一冲突、不可变触发器、模板审计和冻结实例外键，随后删除测试库与脚本。
- Deployment：部署前备份成功；最终 wheel SHA-256 为 `8fb89a37e11fed5215a8b0177d262216ab3f13a89508929427ef1c8d6601dce3`，前一测试件与功能前 wheel 均作为受限回滚件保留，四个 Target 服务回读 active。正式 CLI 已用合成输入创建、预览并确认模板实例，首个 Human Task 已投影并等待处理。

## v0.16.1-draft · 2026-08-01 · Target Agent 真实三节点闭环与内容边界收口

- Fixed：飞书 Human 完成只向下游提交 `{confirmed: true}`；Task GUID、完成时间和事件元数据继续保存在 Projection、Inbox 与审计边界，不再混入 Agent 业务输入。
- Fixed：文本 Agent adapter 会从常见 `content` / `text` JSON 包装与整段 JSON 代码块中提取纯正文；prompt 同时明确禁止 JSON、代码块和字段包装，也不再向模型暴露内部执行标识。
- Verified：`alicloud-sh` 使用真实 PostgreSQL、真实飞书 Task 与真实 OpenAI 兼容模型完成 `Human -> Agent -> Human` 三节点实例。两个 Human Attempt、Agent Attempt 和 Instance 均为 `done`，最终 Task 精确包含 210 字 Agent 正文，未出现结构包装或内部字段。
- Resilience：真实完成变化事件曾在 Task 详情仍为 `todo` 时进入 Inbox，并被凭据侧持续拒绝；后续服务端可验证的完成事件正常处理，旧失败记录保留且没有重复推进。这验证了事件只作触发信号、详情读回才是授权依据。
- Deployment：Target Agent 已按明确授权启用，单线路 LLM timeout 为 240 秒，claim TTL 为 300 秒，安全余量为 30 秒；Runtime、Projection、入站校验和领域入站四个服务均回读 active。
- Verified：完整离线套件 592 项通过，5 项显式集成测试跳过；wheel 构建、上传哈希读回与服务器安装成功。
- Boundary：这是真实开发环境与测试组织验证，不是生产发布。有限 Agent 业务重试、人工接管、业务 Tool adapter 和生产迁移仍未实现。

## v0.16.0-draft · 2026-08-01 · Target LLM Agent 执行与下游人工复核（ADR-060）

- Added：只接受 `work.agent.kind=llm.generate` 的 `LLMAgentExecutor`，从已提交的 Instance 输入与直接依赖结果构建单节点 prompt，通过 OpenAI 兼容逻辑角色生成正文。
- Added：Target Runtime 的显式 Agent 开关、prompt / result 大小上限，以及 LLM 主备路由总超时加安全余量必须小于 claim 租期的启动校验。
- Changed：Human Task 描述会展示节点明确声明的 Instance 输入，以及直接依赖中已提交的 Agent 正文；任务侧设置长度上限，完整内容保留在 Instance Snapshot 与 Attempt。
- Contract：增加 `work.agent.kind / model_role / instructions` 的 v0.2 数据契约与 `target_agent_review.yaml` Human-Agent-Human 示例。
- Resilience：LLM 调用继续发生在数据库 claim 提交后和结果事务前；OpenAI SDK 内层重试保持关闭，故障切换只由显式角色路由负责。迟到结果仍需通过版本、Worker、token、Attempt 与租期校验。
- Fixed：PostgreSQL draft 尚未物化 NodeInstance 时不再提前写 Dependency；确认事务会先写入全部节点，再写依赖。此前单节点真库测试未覆盖该顺序，首个真实三节点草稿创建时由外键约束揭示并整体回滚。
- Verified：完整离线套件 587 项通过，4 项显式集成测试跳过；wheel 构建成功并包含 Agent executor 与混合流程模板。
- Deployment：云端 wheel 已安装，四个 Target 服务回读 active，并保留完整虚拟环境回滚备份；Agent 配置未启用。复用旧服务 LLM key 会扩大凭证可读边界，需独立凭证或明确授权后才能完成真实 Human-Agent-Human 验证。
- Boundary：模型调用为 at-least-once，稳定请求标识不等于供应商提供计费幂等；有限业务重试、Agent 人工接管和业务 Tool adapter 尚未实现。

## v0.15.0-draft · 2026-08-01 · Feishu Task 耐久入站与凭据隔离（ADR-059）

- Added：PostgreSQL Inbox、以飞书 event ID 去重的 legacy 事件观察桥接、凭据侧 `TaskVerificationWorker` 与领域侧 `WorkflowInboundWorker`。
- Added：`larkflow-target verify-inbound-once / verify-inbound / inbound-once / inbound`，以及两个独立 systemd 服务与权限收紧的 env 模板。
- Changed：Human Task 创建改用原生 Task API，固定 `mode=1`、唯一 Owner assignee、稳定 client token 与绑定字段，为完成人校验提供可验证语义。
- Security：legacy 仍是 EventKey 单消费者，不写 Target 领域状态；凭据侧只能读 Task 并写 Inbox；领域侧不能读 lark-cli profile，只消费已验证 payload 并在服务端重算授权。
- Resilience：校验与领域处理分别持久 claim、租约、尝试次数和失败阶段；进程崩溃后可恢复，重复事件不重复提交。
- Verified：完整离线套件 580 项通过，4 项显式集成测试跳过；wheel 包含入站模块与四份 migration。
- Verified：一次性真实 PostgreSQL 数据库已验证 migration 重入、去重、两阶段双 Worker 竞争、无效 token 拒绝与崩溃恢复；长期开发库已应用四份 migration。
- Deployment：Runtime、Projection、入站校验、领域入站与 legacy 五个服务同时 enabled / active，`lf_target_dev` 已回读为无飞书凭据访问权。
- Boundary：当前只把飞书 Task 完成解释为 Human 节点的结构化确认，不承载任意结果内容；IM、Doc、通用命令入站与生产身份拓扑仍未完成。

## v0.14.0-draft · 2026-08-01 · Feishu Task Projection Worker 真栈闭环（ADR-058）

- Added：独立 `WorkflowProjectionWorker`、常驻 `ProjectionWorkerLoop`、Feishu Task adapter、Projection Store Port 与 PostgreSQL UPSERT。
- Added：`larkflow-target project-once / project`、独立 env 配置、只读 migration 验证和收紧权限的 systemd unit。
- Changed：Outbox claim 支持按事件类型过滤；Projection 只认领两类节点投影事件，不会占用未来其他消费者的事件。
- Resilience：Task 创建使用稳定幂等键；外部已创建但响应丢失时，重试复用同一任务。Task 完成按 GUID 调用，Projection 保存同步版本与完成状态，失败使用有界指数重试。
- Verified：完整离线套件 569 项通过，3 项真实 PostgreSQL 集成测试按默认配置跳过；wheel 包含 Projection、Feishu adapter 与 migration。
- Verified：`alicloud-sh` 上 6 条历史非 Human outbox 以 noop 发布；测试组织中的真实 Human 节点创建 1 条飞书任务，提交后实例、节点与 Projection 均为 done / completed，日志记录 `tasks_created=1` 与 `tasks_completed=1`。最终 9 条 outbox 全部 published。
- Deployment：`larkflow-target-projection.service` enabled / active，使用现有测试 profile 而不复制密钥；数据库身份只能更新 Outbox 与 Projection，不能更新领域状态。Runtime、Projection、legacy 与 PostgreSQL 同时 active。
- Boundary：当前只实现 Task 出站创建 / 完成，不包含飞书入站事件、IM / Doc 投影、启动全量对账、真实 Agent / Tool 或生产身份拓扑。

## v0.13.0-draft · 2026-08-01 · Target 常驻服务、CLI 与真机重启恢复（ADR-057）

- Added：独立 `larkflow-target` CLI，提供 migrate、create、confirm、show、submit-human、run-once 与 serve。
- Added：常驻 Worker loop、有界空闲退避、瞬时 tick 故障隔离、SIGINT / SIGTERM 干净停止、结构化 JSON 日志与进程级 Worker identity。
- Added：开发验证专用 `development.echo` Tool adapter、Target env 示例和收紧权限的 systemd unit。
- Changed：Runtime 在 claim 前按 adapter 能力筛选具体节点；未注册 executor 或未接受的 Tool kind 保持 ready，不会被错误认领后标记失败。
- Verified：完整离线套件 559 项通过；wheel 包含新 CLI 与 Runtime 模块；`alicloud-sh` 的 Target 服务 enabled / active，普通执行、SIGTERM 干净停机、SIGKILL 自动拉起、同一 Attempt 换 Worker 恢复均已真实通过。
- Verified：有效恢复中 Attempt ID 保持不变，Worker PID 与 claim token 轮换，节点版本递增，最终日志为 `recovered=1`、`completed=1`、`stale_results=0`，审计追加 `node.claim_recovered`。
- Boundary：开发服务只启用确定性测试 adapter，不是真实 Agent 或业务 Tool；6 条投影 outbox 仍为 pending，尚未连接飞书 Projection worker。

## v0.12.0-draft · 2026-08-01 · Runtime Worker、认领恢复与 Target 开发数据库（ADR-055..056）

- Added：持久化 runnable scan、单步 `WorkflowWorker`、`AutomatedExecutor` Port 与不可变执行请求；请求包含已提交的实例输入、依赖结果、work 和 tenant-scoped Attempt 幂等键。
- Added：自动节点认领记录 Worker 身份；过期恢复保留同一 Attempt，轮换 token、Worker 与节点版本，旧 Worker 的迟到结果被拒绝。
- Added：`0002_runtime_claim_owner` migration，wheel 已验证同时包含 Runtime 模块与两份 SQL migration。
- Added：`alicloud-sh` 长期 Target 开发库、本机 peer authentication、每日 custom-format 备份、约 7 天保留与 systemd 沙箱；仓库增加对应 backup script、service 和 timer。
- Changed：同步 Worker 每个 tick 最多认领一个自动节点；Human 节点不占自动容量；外部调用始终发生在 claim 提交之后。
- Verified：完整离线套件 547 项通过；一次性 PostgreSQL 14 数据库的 3 项集成测试全部通过，覆盖 migration、聚合与 outbox、双 Worker 竞争、过期 claim 恢复；测试数据库与角色回读为 0。
- Verified：长期开发库已应用两份 migration；最新备份按管理员重建数据库默认值与 ACL、应用角色 `--no-acl` 的流程真实恢复到一次性新库，并回读 10 张 workflow 表、正确表所有者、收紧的 schema 权限、UTC 与三项 timeout，恢复库随后删除。
- Boundary：Target 常驻运行循环、真实 executor、Projection worker 和服务接线仍未实现；本机备份与数据库位于同一故障域，不构成生产级灾难恢复。

## v0.11.0-draft · 2026-08-01 · PostgreSQL 事务持久化与 outbox（ADR-054）

- Added：PostgreSQL 14 第一版 schema，覆盖 Template、TemplateVersion、Instance、NodeInstance、Dependency、Attempt、Projection、Audit 与 Outbox；TemplateVersion 不可变，Audit 只追加。
- Added：package-data migration 与 advisory lock runner；wheel 安装包已实测包含 migration SQL。
- Added：Instance 聚合事务仓储、稳定 JSONB Snapshot 序列化、tenant 复合键与实例版本乐观并发。
- Added：AuditEvent 与带租约的事务 outbox；状态、审计和投影请求在同一事务提交，worker 使用 `FOR UPDATE SKIP LOCKED` 认领、失败重试和过期回收。
- Changed：WorkflowService 的读写命令显式携带 tenant；草稿创建、确认、节点激活、Human 提交、自动完成与失败都会记录可关联审计。
- Boundary：Agent 与 Tool 的 NodeActivation 在数据库提交后直接交给 executor，不进入 outbox 排队；outbox 当前只承载可延迟、可重试的外部投影请求。
- Verified：完整离线套件 539 项通过，PostgreSQL 集成测试 1 项显式启用并在 PostgreSQL 14 真库通过；一次性数据库与角色已删除，未连接真实飞书、Agent 或 Tool。
- Commit：内容提交 `70d7abe`。

## v0.10.0-draft · 2026-08-01 · 中央工作流领域内核（ADR-053）

- Added：独立 `larkflow/workflow/` Target 内核，包含不可变 Instance Snapshot、NodeSpec、WorkflowInstance、NodeInstance、NodeAttempt 与简化质量结果。
- Added：v0.2 schema、目标、输出、验收、Tool kind、唯一节点、依赖存在、无环、稳定拓扑、就绪与可达下游校验。
- Added：显式实例、节点与 Attempt 状态迁移；草稿 Owner 确认或丢弃；根节点就绪、扇入依赖解锁和实例完成判定。
- Added：中央 Node Runner 对 Human Owner、Agent 和 Tool claim、过期 claim、当前 Attempt 和节点版本进行服务端校验。
- Added：仓储 Port 与仅用于离线测试的乐观并发内存仓储，结果和输入快照不可变保存。
- Separated：新内核不引用 legacy LangGraph、SQLite、飞书 adapter 或真实 executor，旧运行路径继续作为回归资产。
- Verified：完整离线套件 529 项通过，其中新 Target 内核 17 项；未连接 PostgreSQL、真实飞书、真实 Agent、Tool 或云服务器。

## v0.9.0-draft · 2026-08-01 · 既有设计收敛到最小闭环（ADR-051..052）

- Changed：Phase 0 从外部访谈门改为既有设计简化与一致性核验；访谈和飞书原生对照协议保留为 Deferred，不声称市场验证完成。
- Confirmed：当前工作以既有设计为底稿，不从零重建产品模型。
- Simplified：MVP 固定为单层 DAG；模板可选；草稿必须确认；每节点唯一人类 Owner；执行器为 Human、Agent 或 Tool；编辑和重启均先预览后确认；质量结果改为可解释的通过或失败。
- Deferred：三级子 DAG、个人 Agent Edge、Capability Lease、Knowledge/Skill/MCP 注册表、RAG、复杂模板治理、五维评分、Kafka 和微服务。
- Architecture：目标改为 PostgreSQL 模块化单体、独立 Scheduler、中央 Node Runner、飞书投影和 outbox；LangGraph 只用于单个复杂 Agent 节点内部。
- Docs：同步 CORE、PRODUCT_STRATEGY、PRD、ARCHITECTURE、DAG Contract v0.2、RELATIONS、CONVENTIONS、ROADMAP、README、pyproject.toml 和 research 路由。
- Implementation：本条只收敛产品和架构契约，未修改运行时代码，也未声称目标能力已实现。

## v0.8.0-draft · 2026-07-30 · 产品重定位与目标架构重置（ADR-045..050）

- Changed：产品从“合同类交付物流转 + 全局 LangGraph”重定位为飞书原生的企业协作 DAG；合同降为说明案例。
- Decided：待办只分配给真实人员，个人 Agent 是责任人选择的边缘执行方式；MVP 固定 L1/L2/L3；父子实例用 Work Contract 连接。
- Added：PRODUCT_STRATEGY 九段战略画布、重写 PRD/CORE/ARCHITECTURE/RELATIONS/ROADMAP/CONVENTIONS、DAG Template Spec v0.1、中央 Capability Registry / Lease 边界。
- Separated：目标业务真相迁到 PostgreSQL 中央控制面；LangGraph 限定为单个 AI 节点的可选运行时。SPEC 和 DEPLOYMENT 明确标为 legacy prototype as-built。
- Boundary：吸收模板库和分层 DAG 思路，但明确不复制完整办公平台、Project-DAG 1:1 或 Agent 直接领待办的边界。
- Implementation：本条只重置产品和架构契约，未声称目标能力已落码。

## v0.7.0 · 2026-07-26 · 把「不修不敢拉真人进来」的那几条留白收口（ADR-040..043）
- 背景：真栈 e2e 跑通之后按「敢不敢让第一个非 Maxwell 的人碰它」重排留白，排出来最重的三条恰好都是**机制把人送进一个状态、却没给出口**这同一个病，只是换了地方。ADR-029 的 `blocked` 死局是第一次，这是第二、三、四次。
- Added: **escalation 的同意 / 拒绝通道**（ADR-040）：`approve_escalation` / `reject_escalation` + `larkflow approve/reject/escalations` 三个子命令。此前 `_escalate` 把申请写进权威 state，而全仓**没有任何 approve / reject 通道**（`status` 硬编码一处写入、reducer 只追加不覆盖，申请落库后物理上不可能再变），且它在**默认路径**上：v0.5.0 把卡上默认打回目标改成「保留要走审批的」之后，默认那颗「打回」按钮天然带跨界目标，一点就落进 escalation，人收到「等人拍板」而那个按钮不存在。
- Added: **打回那一刻关掉旧轮次的飞书待办**（ADR-041）。`complete_task` 三处定义、**零调用点**，真栈第一条 e2e 留下 2 条僵尸（本次手工清掉，并顺带验掉了这条真栈从没跑过的路径：bot 身份关得掉指派给别人的待办，返回带 `already_completed` 自带幂等语义）。最难受的孤儿不是「旧轮次里已点完的」，是**被卷进新一轮、但要等上游返工才轮到派单**的旁支节点，人手里那条死单和能干的活长得一模一样。
- Added: **`edit_graph` 的鉴权与审计**（ADR-042）：owner-only + 必署名 + 新的 `edits` 追加型 channel + `edit_log()`。此前它连 actor 都不收，比无鉴权的 `unblock` 更狠（`unblock` 最多让人返工，`edit_graph` 能**直接删掉一道还在等的门**让流程静默放行），而 `larkflow edit` 正要把这个入口开到命令行上。
- Added: **`larkflow edit`**（win 判据③「运行中改图」此前在真栈上**无从触发**，引擎侧早已落码却没有入口）。`--ops` 收字面 JSON / `@文件` / `-`（stdin）三种来源：报文里全是中文 label、prompt 常含 `$`，逼人在命令行裸写 JSON 是重踩「别 `source .env`」那个坑。CLI 只校验报文形状，合法性一律引擎权威侧算。
- Changed: **escalation 的状态改为派生**（`effective_status` ∈ pending/approved/rejected/expired）。追加型 channel 没有 UPDATE，所以「同意」不是改 `status` 而是**追加一条裁决记录**。这顺带修掉 v0.5.1 留下的「`escalations()` 旧记录 status 恒为 pending」那条 finding：它不是漏写，是存储模型决定的，只能改读法。
- Changed（**修订 ADR-023 ③**）：① **禁自批**。`approvers_for` = owner 令牌 ∪ 目标节点主负责人，而申请人完全可能正好是后者，不禁的话他自己提、自己批，那三条规则被整个绕开（owner 恒在审批人里且走不到 escalation 这条路，故禁自批不会造成无人可批）。② 审批人身份**两把尺**：令牌求交之外再认「当初真通知到的 open_id」，否则 `roles_of` 反解一旦静默失效（自定义 resolver 无该方法 / 角色映射改了 / assignee 配成飞书群），这笔申请就没人同意得了，死局原样复发。
- Fixed: **门已被答复的 escalation 申请不再显示为「待批」**。申请不是裁决（`_ack_escalation` 明说「你手里这张卡仍然有效」），所以提申请的人完全可能没等批下来就自己点了通过，这是常态。轮次那把尺在这里不管用（点通过不会让 `attempts` 变），必须另看门的状态，否则驾驶舱一直显示「等人拍板」而门早就过去了，真有人去点同意还会试着掀开一道已经放行的门。
- Fixed: `larkflow edit` 的两条拒绝出口口径不一致（CLI 侧发现）：`edit_graph` 对 `missing_audit` / `unauthorized_edit` 是 `return` 结构化拒绝而不是抛异常，只 catch 异常的写法会把**越权当成功**打印并退出 0。
- Reviewed: 5 维度对抗 review（escalation 语义 / 关旧待办 / edit 鉴权 / CLI / 测试有效性），逐条证伪后**坐实 18 条、证伪 5 条**，全部带实跑复现。引擎侧最重的三条已修并各配回归测试（见 ADR-040 / ADR-042 末尾）；教训是同一个：**「活性」这件事只许有一把尺**，自己再写一遍判据必错（我把三处口径统一了，唯独漏了 `_escalate` 里的去重，于是驳回之后申请人永远提不了同一笔且零反馈）。
- Reviewed: 变异测试补覆盖。review 用变异法证明了两处**零覆盖**：删掉 `_can_approve` 的令牌那把尺、去掉裁决通知幂等键里的 `:{seq}`，测试都全绿。更难堪的是 `test_the_audit_channel_is_append_only` **完全是空跑**：它拿 `reconcile` 当「后续推进」，而在那个现场 `reconcile` 一次 `_write_state` 都不调（我自己复现确认），于是它声称保护的不变量从未被执行到。已重写成打在不变量所在的那一层并**断言它真的被调到**，免得再退化。补完后 8 个变异体逐个验证**全部被杀**（基线 rc=0，先验尺再跑，不重蹈上次「基线 rc=4 导致 11/11 假阳性」）。
- Docs: 清掉全仓 23 处中文破折号（项目写作硬规范，此前是我自己破的）。CHANGELOG 补上 v0.6.0（真栈第一条 e2e 那个里程碑此前一条没记），ROADMAP 按真实完成度重标（原文还写着「真栈三件套一件没做」）。
- Fixed（CLI 侧，同一轮 review 坐实的 8 条）：`candidates` 是 `list[int]` 而 CLI 当 dict 遍历，多笔待批时 `approve` 直接 `AttributeError`、`--json` 的 stdout 变空串（**两份测试对同一契约打架，CLI 实现的是 stub 那份错的**，stub 形状已同步改成真 service 的）；`_ops` 不查 `node` / `id` / `set` 的类型，`apply_ops` 抛的 `AttributeError` / `TypeError` 不在认领清单里，同样让 `--json` 的 stdout 变空；漏认第 5 种异常 `UnsupportedInV1`（运行中给节点加 `when` 守卫这种最自然的改图就会撞上）；`LockBusy` 既没走 stderr 也不认 `--json`；非字符串 `node.id` 能混进权威 dag（`--json` 投影里 `nodes[].id` 是数字 `7`、`status` 的键是字符串 `"7"`，同一份报文里两种身份）；`escalations --all` 把裁决记录当申请渲染（拍板人被标成申请人、`seq` 显示 `?`）；`edit` 打错实例回 `illegal_edit` 而其余命令回 `no_such_instance`。另加一层兜底 except，认领清单过时也不会让 `--json` 的 stdout 空着。
- Added: **审批卡**（ADR-043），ADR-023 ③ 的「一键同意」这才名副其实：审批人收到带「同意 / 驳回」两颗按钮的卡，封套 `{kind: escalation, thread_id, node_id, seq, decision}` **刻意不带 `interrupt_id`**（拍板不是答复中断），`_route` 据 `kind` 走第三条分支。按钮文案与门禁卡的「通过 / 打回」用不同的字：两者挂在同一个 `node_id` 上并存。拍完把卡改成「已处理」，另一位审批人后来点他那张会得到「已由 X 处理过」而不是静默 no-op。
- Fixed: **停订阅没带走整棵进程树**（ADR-044），于是真机上**每一次**停机都报「10s 内没排空」而事件数是 0：`lark-cli event consume` 是两级进程，`terminate()` 只杀第一级，孙进程握着 stdout / stderr 让管道永不 EOF、泵线程卡死。代价不是多一行日志，而是**退出码恒定非 0**，把「这次停机干不干净」这个信号（v0.5.1 专门加的）淹在恒噪声里。改成 `start_new_session` + 按进程组发信号，真机复验「已停止（干净）／故障 0」。
- Verified: **v1.0 win 判据补齐 4/4**，③运行中改图在真栈上取得（实例跑到两道人工门时 `larkflow edit` 插节点 + 改依赖：`edited=2` / `remapped=2`（人手里的卡被重绑、仍有效）/ `attempts` 全空（改图不是打回）/ 审计落 `edits`；越权改图被 `unauthorized_edit` 挡住）。
- **469 tests pass（339 → 469，+130）**。仍然全程 Mock / Stub / `:memory:`，红线不破；新增 7 条 CLI 端到端走的是真 service（Mock 飞书 + Stub LLM），不再是「照抄实现自己声明的异常元组」那种结构上抓不到漏网的测试。
- 未做：`unblock` 仍无权限层（ADR-030 自己写的处方是「拿 `by` 当 actor 过一遍 `reopen_verdict`」）；human produce 配 `card_action` 时打回**无法主动作废旧卡**（`update_card` 只吃回调 token，没有按 message_id 改卡的能力），与任务通道不对称；`build_real_service` 的 `profile` 不从 env 取默认；`task.task.update_user_access_v2` 不推送的根因仍未查明；daemon 自己没有存活信号。

## v0.6.0 · 2026-07-26 · 真栈第一条 e2e 跑通（引擎不再只是「在 Mock 里跑通过」）
- 背景：此前所有「测绿」都是 Mock / Stub / `:memory:`，证的是逻辑自洽，**不证任何一条真栈路径**。这一版把 dev 飞书应用建起来、LLM 多角色 env 配起来，让策展合同图**八个节点在真飞书 + 真 LLM 上从头走到尾**。ADR-036..039 四条决策全部是接真栈才暴露出来的问题逼出来的，不是设计推演出来的。
- Verified: **v1.0 win 判据 3/4**（PRD 口径，真人 / 真项目版，此前为 0）。
  ① **真项目端到端 ✅**：`biz_draft` / `legal_draft` 双起草 → `finance_gate` / `legal_gate` 两个真人门（含一次真打回）→ `merge` 合并 → `finalize` 人定稿 → `checks` auto 机检 → `close` 收口，**八个节点全 done**，`outputs` 权威登记 8 条（其中 5 条是真实飞书文档，2 个人工门与 1 个机检产的是裁决不产文档）。
  ② **打回可感知省算 ✅**，三条独立证据：权威 state 的 `attempts`（= 各节点被打回重置进新一轮的次数）里**根本没有 `legal_draft` / `legal_gate`**，即法律那一支的 AI 长文起草与人工复核**一次都没重跑**；`legal_gate` 全程只发过 1 张卡，而被牵连的 `finance_gate` 发了 2 张；交付物 handle 不变、只做 overwrite（正文 1871 → 2545 字，文档 token 没变）。数字自洽：`finance_gate` 打回 1 次波及 `{biz_draft, finance_gate, merge, finalize, checks, close}` 各 +1，`checks` 再打回 3 次波及 `{finalize, checks, close}` 各 +3，正好凑出 `close/finalize/checks = 4`、`merge = 1`。
  ③ **运行中改图 ⬜**：`edit_graph` 引擎侧早已落码且有测试，但 **CLI 没有入口，真栈上无法触发**。
  ④ **auto 门 ✅ 双向**：同一道 `checks` 自动打回 3 次后自动放行，两个方向都验到。
- Added: **LLM 备用线路**（ADR-036）：每角色一条有序链 `LLM_<ROLE>_BACKUP[N]_*`，缺项继承主配置（只写 `BACKUP_API_KEY` = 同端点换把 key，三项都填 = 换一家）；400 / 422 **不切换**（是我们自己的请求错了，换线路只会原样再错一次还多烧一次钱）；切换必须留痕。实测把主 key 换成假的，真实的方舟 401 被正确判成可切换并自动落到备用（3.7s）。
- Added: **超时按角色可配**（ADR-036 同条）：实测一次真实起草 **109.7s / 2570 字**，而当时默认 60s，`biz_draft` 必被掐断。默认提到 300s，加 `LLM_TIMEOUT` / `LLM_<ROLE>_TIMEOUT`。
- Added: **引擎自己读 `.env`**（`config.load_dotenv`）：`source .env` 走的是 shell 语义，会剥掉 `LARKFLOW_ROLES` 的 JSON 引号、把含 `$` 的 api_key 悄悄改写，而且不报错。加载器同时报出「因已被占用而未生效」的键，否则全被占用时一行日志都没有、看起来像加载器没工作。
- Added: **卡片「已处理」回写**（ADR-037）：裁决落地后把卡换成结论版（谁 / 何时 / 什么结论 / 打回到哪一环 / 意见），**按钮全部撤掉**；陈旧旧卡当场标「已失效」。用户原话：「点了通过或者打回，卡片没有任何变化，会让用户不知道点过了没、点了什么」。越权点击**不改卡**（卡可能已被转发，改卡会改掉所有人看到的内容），只走私信。
- Added: **对账轮询在等的飞书任务**（ADR-038）与**定期对账线程**（ADR-039，`LARKFLOW_SWEEP_SECONDS` 默认 120s，配 0 关掉）。
- Added: `LLM_NO_PROXY`：httpx 见到 `all_proxy=socks5://…` 会**急切构造** SOCKS 传输并直接报 `socksio` 未安装，`no_proxy` 救不了，只有 `trust_env=False` 能。
- Added: 真飞书报文钉成测试（`tests/test_real_payloads.py`，脱敏）。好消息：`normalize_event` / `_route` 照 lark-cli 字段表写的解包逻辑与真报文完全对上，**一行没改**。
- Fixed: **openai SDK 默认 `max_retries=2`**，且坐在我们自建的故障切换**里面**，把配置的超时乘 3（实测 `timeout=2` 实际耗 7.5s）。按当时配置换算 = 一条线路 15 分钟、主备两条最坏 30 分钟；现场表现是 `merge` 点完通过后 18 分钟毫无动静。改 `max_retries=0`（重试策略只许有一处），并加 `on_call` 让「正在等 LLM」可见：在此之前，一次 110s 的正常起草与一次 30 分钟的静默停摆，在日志里长得一模一样。
- Fixed: **长连接会静默死亡**：进程全活、TCP 显示 ESTABLISHED、`event status` 说 running、日志无异常，而它自己的账本写着 `RECEIVED 0`，**10 小时 48 分一条事件没收到**。`EventPump` 的退避重启只在子进程退出时触发，子进程不退就永远不重启。应对见 ADR-038 / ADR-039（轮询兜底），主动探测仍未做。
- Fixed: `_sweep_tasks` 第一版按 node_id 翻关联表，会拿第 1 轮的完成去 resume 第 3 轮，**每对账一次白烧一轮打回预算**（真栈实测把 `checks` 的预算从 1 烧到 3、实例直奔 blocked）。改按派单幂等键 `{实例}:{节点}:{轮次}` 定位，该键只在 `_dispatch_key` 一处拼，派单与轮询共用（两处各拼一次必然漂移：第一版漏了 `:kind` 段，后果是永远查不到、丢事件永远捞不回来，且没有任何症状）。
- Docs: DEPLOYMENT 补**飞书权限台账**（真正用到的 scope 逐条记；测试组织为方便开了全量，故必须单独记账）、「事件」与「回调」是控制台两栏各自订阅、**改完必须发布版本才生效**（一次误判成租户不对，实为版本没发）、长连接没有队列且不补投。`.env.example` 重写：只留代码真读的 key，每条标出读它的代码路径。
- 339 tests pass（280 → 339，新增 59）。**新增的仍然全程 Mock / Stub / `:memory:`**，红线不破（测试绝不构造 `build_real_service`）；真栈那一遍是手工跑的，证据在上面 Verified 一条。
- 未做：`larkflow edit` 子命令（win ③ 在真栈上无法触发）；`task.task.update_user_access_v2` **为什么根本不推送**未查明（ADR-039 标未验：隔离实验里 websocket 已连、以 bot 身份亲手建并完成任务、按提示加 app 为 follower 都试过，`RECEIVED` 始终 0，而卡片事件同一条 bus 正常）；打回时不关旧轮次的飞书待办，每打回一次给人留一条僵尸（真栈上留下 2 条，手工清掉）；`build_real_service` 的 `profile` 不从 env 取默认，调用方忘传会**静默**连到另一个 app；ADR-037 的卡片回写只有 Mock 测试，真栈没验过（那个实例后面没有卡片节点了）。

## v0.5.1 · 2026-07-26 · 收口上一轮没验完的 finding（假审计 / 静默失败 / 一个推进死角）
- 背景：v0.5.0 的对抗 review 出了 20 条 finding，为控成本只验了最重的 5 条，**15 条不是低价值、只是没看**。事后抽查全中，遂逐条复现后修掉；修的过程中又撞出一个 review 没人报的引擎 bug。
- Added: ADR-034（审计记录写在事情发生**之后**：投影侧事实与权威意图分离）、ADR-035（推进的收敛判据要看累加通道）。`tests/test_hardening.py` 13 条。
- Fixed: **假审计**：escalation 的 `notified` 原本在通知真发出去之前就写死，飞书失败时权威 state 留下「已通知」的假记录，会让审批人停止追查。改为先发后记，未送达进 `notify_failed`（ADR-034）。
- Fixed: **推进死角**（自查撞出，非 review 所报）：门重试再次失败时 `status` 快照前后逐字相同，`_advance` 判成「推不动了」提前返回，实例停在 `failed` 而非 `blocked`：通知不发、`unblock` 还以 `not_blocked` 拒绝它，ADR-029/030 的出口当场失效。判据补 `reopen_counts` / `attempts`（ADR-035）。
- Fixed: `blocked` 通知的幂等键只含「已解除次数」，而 `blocked` 不是真终态（别的门打回共同祖先就能把它拖回前沿），重新卡死时键没变、本地永久幂等表把它彻底吞掉。键补轮次。
- Fixed: `unblock` 不原子：额度只有 3 次且不可退，而重试要跑 LLM / 发飞书，基础设施抖一下就吃掉人的一次机会。失败补一条 `refund` 记录（审计仍只追加），`grants_used` / `granted_budget` 做减法，并尽力把实例推回稳定态。
- Fixed: 停机信号在 `startup_reconcile` 里只置位不生效（几百个实例照样对完，之后还白起一次泵）；`stop()` 排空超时照样关 SQLite（在飞的事件可能正握着实例锁写 checkpointer），且自认 `errors=0`、退出码 0。改为中止对账并报出没轮到的实例、没排空就不关库 + 记 drain 故障 + 退出码非 0；`EventPump.join` 返回是否真排空。
- Changed: 默认 DB 路径 `larkflow.sqlite`（cwd 相对）→ `~/.larkflow/larkflow.sqlite`，且 `--db` / `LARKFLOW_DB` 一律绝对化后回显。原来 systemd 起的 daemon（`WorkingDirectory=/`）与手敲的救场命令会**静默**落到两个库。
- Changed: 全局参数（`--db` / `--json` / …）子命令两侧都能写；子解析器那份一律 `default=SUPPRESS`，否则会把顶层已解析的值覆盖回默认（argparse 经典坑）。
- Reviewed: 11 个变异体逐个把修复退回缺陷态，**全部被测试杀掉**。第一次跑变异时基线 rc=4（`--timeout` 需要没装的插件），11/11「全抓住」是假阳性；**基线非 0 就是尺子坏了**，去掉后重跑才作数。
- 280 tests pass（267 → 280）。仍然全程 Mock / Stub / `:memory:`。
- 未做：15 条未验 finding 里剩下的（`escalations()` 旧记录 status 恒为 pending、锁文件与飞书侧的对账缺口等）；escalation 一键同意、`unblock` 权限层、群 assignee 无人可应答，三条留白照旧。

## v0.5.0 · 2026-07-25 · 从「跑得通的引擎」到「起得来的服务」（服务层 + 权限层 + blocked 出口）
- Added: **常驻服务形态**（ADR-031）：`larkflow/serve.py`（启动全实例对账 + 事件泵接线 + SIGINT/SIGTERM + 优雅退出）、`larkflow/__main__.py`（CLI：`serve / start / status / pending / unblock / reconcile`，含 `--json` 与退出码约定，`[project.scripts]` 已挂）、`larkflow/store.py`（多进程共用一个 SQLite：WAL + busy_timeout + 跨进程实例 flock + daemon 单例锁）。
- Added: **打回权限层落码**（ADR-023 as-built）：`larkflow/engine/permissions.py` 纯图函数（`allowed_reopen` / `reopen_verdict` / `collateral_humans` / `primary_owner` / `approvers_for`）+ 跨界打回 escalation 申请（新 state channel `escalations`，追加型）+ `RoleResolver.roles_of` 反向角色解析 + `pending(actor=)` 按人过滤。
- Added: **`blocked` 的解除通道**（ADR-030）：`unblock()` / `unblock_log()`，追加预算而非重置计数，两层额度上界，审计落新 state channel `unblocks`（追加型）；`larkflow unblock` 与 demo 的 `un` 命令同步暴露。
- Added: **应答权**（ADR-032）：`permissions.can_answer`，放行 / 定稿也在引擎权威侧判身份；卡片事件缺 `operator_id` 一律 fail closed。
- Added: 只读接口 `dag_of` / `finished` / `escalations` / `pending_escalations`（驾驶舱 / 对账按**实例自己的活图**算，不拿装配期模板当所有实例的图）。
- Changed: 外部写动作的幂等性从飞书的 1 小时窗口**收回本地**（ADR-033，`_once` + `idem_store`，与交付物 `markdown +create` 同一张表）；`LarkFlowService` 新增 `lock_factory` 注入点（默认仍是进程内锁，真栈注跨进程锁）；`EventPump` 补 `join()`、`stop()` 后正常退出不再报「重启达上限」假故障；`normalize_event` 解开 lark-cli 把 `action_value` 序列化成的 **JSON 字符串**（依据 lark-event / lark-im 内嵌 skill 字段表，**真栈未验证**，接真栈第一件事就是盯它）；卡片默认打回目标改为「只剔 denied、保留要走审批的」，与「全或无」不变量对齐。
- Fixed（都实测复现过，每条先写红测试）：陌生人改一个封套字段就替别人把定稿签了（非 gate 的 fail 落在身份判定的两支之外）；卡片默认「打回」按钮静默只退回一半目标（发卡时削掉跨界目标，把「全或无」架空，申请没落、谁都没被告知）；拿一张卡的 message_id 冒充 task_guid 就绕过整条卡片通道的身份判定；语义相同的重复点击各占一格审批配额（去重键拿前端原始列表逐字比）且配额按整条历史算导致一道门此后永久提不了申请；每次 serve 重启 / 每次 `reconcile` 都真的再发一遍卡、再建一条待办（重复待办没有任何代码去关）；第二次卡死 / 第二次停摆的通知被幂等键静默吞掉（键里没有区分「第几次」的东西）。
- Reviewed: 四轮落地各自跑变异测试验测试有效性（记数的 69 个变异体全部被杀），最后**专起一轮攻击自己刚落地的修复**，找到并修 4 条（其中 2 条是前一轮修复本身引入的语义冲突）；另跑跨进程锁的真子进程探针、8 线程与 4 线程并发探针；把 4 个「回归时会挂死而不是变红」的等待型测试改成带上限。
- 267 tests pass（140 → 267，新增 127；新增 `tests/test_unblock.py` / `test_permissions.py` / `test_serve.py`）。**全程 Mock / Stub / `:memory:`**：证的是逻辑自洽，不证任何真栈路径。
- 未做（明确留下）：escalation 的一键同意（ADR-023 ③，`status` 永远 pending）；`unblock` 的权限层（`by` 只进审计，`unblock(reopen=…)` 是绕过 ADR-023 的路）；受控活图换负责人不重新派单；`assignee_role` 解析成飞书群时该节点无人可应答；真飞书 / 真 LLM e2e 与妙搭三命门仍是 0（见 ROADMAP v1.0）。

## v0.4.0 · 2026-07-25 · 从「合同流引擎」改回通用引擎（对抗 review 收口）
- Reviewed: 6 维度 62 agent 对抗 review（通用性 / 引擎不变式 / 真实栈 / 文档符合度 / 测试有效性 / 产品泛化），逐条自行复现。**根因不是代码质量，是上一轮围绕合同图做 TDD，把这一个用户故事的假设焊进了准入层与绑定层。**
- Added: **tool 数据化能力库**（ADR-026，`tool: {kind, args}` + 与模板无关的全局注册表）；`lint_template`（ADR-027）；打回意见回流（进 llm prompt + 回喂上一稿 + 进人工卡片）；打回预算与 `blocked` 终态（ADR-029）；`reconcile()`；`blocked()`；`templates/hiring.yaml`（招聘接力，**零 Python** 的第二个业务场景）。
- Changed: 护栏①「三型齐全」降级为 lint（ADR-027）；`produce` 的 `deliverable` 改为可省（纯动作节点）；human gate 禁用 `task_complete`（否则审批门是橡皮图章）；驱动层绕开 super-step 屏障（ADR-028，保值写回 + 借位重排）；`pending()` 过滤已答复者；`recursion_limit` 按运行时 dag 现算；派单 per-interrupt 隔离；EventPump 异常隔离 / stderr drain / 退避重启；真栈角色严格解析 + `LARKFLOW_ROLES` JSON。
- Removed: `templates/contract_handlers.py`、`templates/defect_handlers.py`（模板目录只剩 yaml）；`app.HANDLERS` 按模板名的注册表。
- Fixed（都实测复现过）：并行门先打回时打回不落地、改图吞掉刚做出的裁决、不相干并行分支被人工节点卡死、打回后 AI 用逐字节相同的 prompt 重跑（等于空转）、跨模板 node id 撞名静默跑错业务、auto 门无限重算撞 recursion limit。
- 127 tests pass（新增 25，其中 `tests/test_generality.py` 是「通用产品」的可执行断言）。

## v0.3.0 · 2026-07-24 · 引擎 v1.0 核心 headless 跑通（代码追上第二 / 三轮设计）
- Added: v1 节点契约落码（`executor × role + 配置`，护栏①..⑤ + 字段级）；交付物层（`Deliverable{type,token,url,region}` + `DeliverableIO` create/overwrite/fetch + handle 权威登记 `outputs[node_id]`）；通用 produce/gate 执行体（per-role 取代 per-node-id）；选择性重算 v1（运行时手选 reopen 组 + 合法域校验 + 结构性终止）；auto 门短路；merge 扇入（引擎零改）；受控活图 `edit_graph`；首张策展合同图 `templates/contract.yaml` + 机检 / 收口 handler；驱动泛化 `start(template, inputs)` + `build_service`；真实栈（`CliDeliverableIO` 走 lark-cli markdown、多角色 LLM env 装配、`build_real_service`）。
- Changed: `type→executor` / 旧 `role→assignee_role` / 去 `on_fail`；`defect.yaml` 迁 v1 作回归载体；`LLMClient` 主接口改 `complete(prompt, model_role)`；`service` 删掉最后一处模板硬编码（动态指派留 v1.1）；`read_upstream` 透过不产交付物的节点看上游。
- Verified: v1.0 win 的 headless 判定版一次跑通（交付物真流转 + 打回**可感知省算**：旁支 AI 长文不重跑、旧 handle 复用、全程不新建文档 + auto 门自动放行 / 打回 + 运行中改图）。102 测绿，全程 Mock/Stub/`:memory:`。
- Learned: 挂起时 `update_state` **必让中断换 id**（实测四种情形一律换）→ 加 `interrupt_remap` 重绑，否则改一次图就废掉在等的人手里的卡（见 MEMORY）。
- 未做（明确留下）：真飞书 / 真 LLM e2e（需 dev app + 事件回调）；ADR-023 权限层 `allowed_reopen`（v1.0 只做机制层）；崩溃对账 `reconcile`（MEMORY finding D）；reopen 预算（finding C）。

## v0.2.0 · 2026-07-24 · 产品最终形态定型（入口 / 生成 / 打回权限 / 投票分支 / 子项目 / 实现分层）
- Added: ADR-021 入口与意图路由（结构化 + @bot NL 双入口，确认步）；ADR-022 模板生成升为主路径（受控活图 + 确认降低 ADR-003 生成风险）；ADR-023 打回权限模型（机制 × 权限两层，防踢皮球精确判据，节点负责人 / 主负责人，escalation）；ADR-024 子项目 spawn（交付物流转递归 + 回填 + 边界隔离）；ADR-025 多人节点投票门(A) / 决策表决(B) + 条件分支（when 守卫 / skipped）。
- Changed: 前端呈现 → 两视角两表面（参与人 chat-first 可只读看全貌 / 发起人 app 驾驶舱可编辑，可见 ≠ 可操作）+ 页面 P1-P4；节点契约补 assignee_role / vote / when，状态机加 skipped；生成从 ADR-003 路线 1 优先升为主路径。理由见 DECISIONS ADR-021..025。
- Changed: ROADMAP 从「Now v1」细化为**实现分层** v1.0(第一个 win) → v1.1 生成 → v1.2 子项目 → v1.3 投票分支，v2 共享协同 + 前端可编辑；子项目 / 会签从 v2 提前到 v1.2 / v1.3。
- Reviewed: 两轮对抗性 workflow review：内部一致性（修 12 项）+ PM 产品视角（6 把 pm-skill 尺子）。据 PM review 补：v1.0/v1.1 间加**采用 gate**、win 判据改「可感知省算」、修 win↔画布（v1.0 改图走命令 / 卡片）矛盾、PRD 补频次假设 + vs 飞书原生一节、ADR-024/025 加暂定头。
- 注：本版为纯设计 / 文档定稿（未动代码），代码仍 seg-1 契约、待 v1.0 step 1 迁移。

## v0.1.2 · 2026-07-24 · 开写就绪度复盘 + 交付物 handle 权威定家
- Decided: 交付物 handle 权威登记表 = `state.outputs[node_id]`（`deliverable.container` 降为活图声明位 / 回填指针）；固化 `on_fail`（静态单目标）→ `reopen`（运行时多选 + 运行时祖先校验）代码契约；澄清 v1 `role`（produce|gate）与 as-built `role`（业务指派串 = `assignee_role`）撞名。理由见 DECISIONS ADR-020。
- Reviewed: v1 开写就绪度审查（6 维找缺口 + 对抗验证 + 合成）无硬阻塞：seg-1 引擎原语全复用，剩下是照 SPEC 落码；net-new 集中在交付物 IO 层 + 执行体泛化 + merge + 活图 edit_graph；前端 spike 不在关键路上。

## v0.1.1 · 2026-07-24 · 前端形态定：真前端（妙搭为主）
- Changed: 修订 cards-only（ADR-011）为真前端；妙搭（Miaoda，本地开发）为主、开放平台自建 H5 备选；前端 = 引擎投影 + 客户端；松动 ADR-007（引擎将暴露读 / 命令 API）；README / About 已写。理由见 DECISIONS ADR-019。

## v0.1.0 · 2026-07-24 · 第一段引擎跑通 + 第二轮设计（交付物流转）
- Added: seg-1 本地引擎跑通（8 节点缺陷流，固定编排器解释数据 DAG + SQLite checkpointer + 驱动层 LarkFlowService，15 测试绿，`larkflow/`）；对抗性审查 9 项（见 MEMORY）；交付物产出协议在测试组织实测（markdown create/fetch/overwrite/版本）；公开仓库 github.com/iyuenan3/larkflow。
- Changed: 定位升格为交付物流转引擎（缺陷流降退化特例）；节点模型 → executor×role + approval_policy；门禁 win 核心从五维评分修正为「可换执行体 + auto/会签 + 打回流转」；引入受控活图 + 选择性重算打回；交付物 → (容器,region) 统一飞书文档 + 飞书原生版本；LLM 从 newapi 改为通用多角色 OpenAI 兼容路由。理由见 DECISIONS ADR-012..018。
- Removed: newapi-proxy 依赖（LLM 改多角色路由）。

## v0.0.0 · 2026-07-23 · 立项
- Added: 项目立项（larkflow / 飞流）；git init（main）；AIREADME 骨架（INDEX / CORE / RELATIONS / ARCHITECTURE / PRD / DECISIONS / CONVENTIONS / ROADMAP 实填，SPEC / DEPLOYMENT / MEMORY 语义占位）；CLAUDE.md router。
- 定架构：两层（领域 DAG 数据 + LangGraph 有环引擎）+ 单一事实源（checkpointer）+ 飞书投影；路线 1 策展模板起步；飞书原语复用、MVP 零自建前端。理由见 DECISIONS ADR-001..006。
- 设计定稿（同日）：入口 lark-cli event consume（NDJSON，不接 SDK）/ 宿主 alicloud-sh + SQLite / dev 独立飞书租户 / 第一张模板 = 缺陷生命周期（分两段建）/ 模板生成走 few-shot（种子库 + 3 护栏）/ 工作台 cards-only；win = 证采用 + 门禁。理由见 DECISIONS ADR-005（结论）+ ADR-007..011。
