# ROADMAP · larkflow

> 状态：Target Delivery Plan · 2026-08-05
>
> 原来的三级协作、个人 Agent Edge 产品化和完整能力治理路线已移出近期范围。现有代码作为 legacy 机制原型保留；一个不改变中央主线的只读 Edge Proof 单独验证架构边界。

## Completed · Phase 0 既有设计简化与一致性核验

目标：以既有设计为底稿，围绕最小闭环做减法，并切断 Target 与 As-built 的混写。

- 核对既有 AIREADME 的范围、依赖和实现边界。
- 固定单层 DAG、模板可选、草稿确认、未来区域编辑、节点重启和完整实例重启范围。
- 明确每节点唯一人类 Owner，Human、Agent、Tool 只表示执行器。
- 从近期产品范围移除子 DAG、个人 Agent Edge 产品化、通用 Capability Lease、RAG、Kafka 和复杂模板治理。
- 保留飞书 adapter、幂等、对账、权限纯函数和重算机制的迁移价值。
- 统一 README、PRD、架构、契约、路线图和包描述。

**Exit gate：** 每项 MVP 都有明确的产品理由和可判定验收；核心文档无范围冲突；市场结论仍标记为未知。

## Now · Phase 1 中央工作流基础

目标：一个企业可以从模板或无模板定义启动单层 DAG，并在飞书中可靠推进。

- 已完成领域内核：不可变 Instance Snapshot、DAG Contract 核验、草稿确认、NodeInstance、Attempt、显式状态迁移、Scheduler、中央 Node Runner、claim 和仓储 Port。
- 已完成 PostgreSQL 14 第一版 schema：Template、TemplateVersion、Instance、NodeInstance、Attempt、Projection、Audit、Outbox。
- 已完成 Instance 聚合事务仓储、JSONB 快照、乐观并发、追加型 Audit、带租约 Outbox 和 package-data migration；真实 PostgreSQL 14 一次性数据库集成验证已通过。
- 已完成单步 Runtime Worker、持久化 runnable scan、Worker 身份认领、精确租约到期恢复与稳定 Attempt 幂等键；真实 PostgreSQL 双 Worker 竞争和崩溃恢复验证已通过。
- 已完成独立 `larkflow-target` CLI、PostgreSQL 通知唤醒、轮询兜底、有界退避、SIGTERM 干净停机、adapter 能力过滤与 systemd 服务装配；SIGKILL 后新 Worker 接管同一 Attempt 已在真机验证。
- 已完成独立 Projection Worker、事件类型过滤、Task adapter、稳定幂等键、Projection 落库、失败重试和 systemd 服务装配；测试组织中的 Human Task 创建与完成闭环已真实通过。
- 已完成 Task 完成状态的耐久入站：Projection 周期扫描当前 Human Task，以稳定信号 ID 去重写入 PostgreSQL Inbox；legacy EventKey 事件保留为可选低延迟信号。无论入口来源，凭据侧都会重新读取飞书 Task，领域侧再校验 Projection 绑定、当前 Attempt、唯一 Owner 和完成人后提交 Human 节点。凭据验证默认最多尝试 24 次，耗尽后进入可审计且不可再认领的终态并暴露结构化告警信号。两个外部 Task 已完成而领域仍等待的开发实例，已由轮询自动推进到完成。
- 已完成首个 `llm.generate` Agent adapter、首个确定性 `content.check` Tool adapter、按 kind 路由、claim 与超时预算校验，以及直接依赖结果到下游 Human Task 的投影；开发云服务器与测试组织中的真实 Human-Agent-Tool-Human 四节点闭环已通过。
- 已完成 Template Service：`draft / enabled / disabled / deleted`、不可变版本、布尔锁、角色与参数绑定、追加型模板审计和 aggregate version 乐观并发；真实 PostgreSQL 同时启用竞争验证已通过。
- 已完成从启用模板生成冻结草稿、Owner 只读预览和正式 CLI 入口；开发环境已用合成输入创建、预览、确认模板实例，并完成正式模板的真实 `Human -> Agent -> Human` 闭环。
- `alicloud-sh` 已建立长期 Target 开发库、本机 peer authentication、每日备份、新库恢复演练和 enabled Target 服务；仍缺异机备份、PITR 与生产运行手册。
- 每个节点的唯一 Owner 解析与服务端授权。当前内核已拒绝非 Owner 提交，飞书 IM 命令发送者的活跃成员校验已在测试组织通过；草稿 Owner 全量企业目录校验已落码并部署但默认关闭。
- 独立业务 Scheduler 和 Human、Agent、Tool Node Runner。领域规则、持久化、常驻 Worker、Agent adapter、首个 Tool adapter、真实开发链路与恢复扫描已落码，更多业务 Tool 按验证需求增加。
- 已完成启动全量 Task 对账、缺失 Projection 补建和确认删除后的外部 Task 重建；一次性 PostgreSQL 与常驻开发服务均已验证补建、真实删除换绑、重入及修复后完成入站。
- 已完成飞书窄命令入口与完成投影：`/larkflow help / start / confirm / status / list / restart / restart-all / restart-confirm / edit / edit-confirm`、耐久发送者校验与回复、Agent / Tool 结果消息、完成 Docx 和最终通知已进入开发链路。`start` 的 `role=@成员` mention 角色绑定和单聊 Card 2.0 人员选择均已完成开发部署、目录再验证、冻结 Snapshot、幂等草稿创建与真实跨人员验收。人员选择卡和失败恢复卡统一在动作耐久落库后先尝试显示无按钮“处理中”，再收口为成功或拒绝；同一人员选择卡只有一个 canonical 动作。`status` 只允许 Instance Owner 查看单实例有界摘要，`list` 只返回本人最近十个实例摘要；两类重启和未来区域编辑只允许 Owner 预览和确认。既有十个命令均已完成 Owner 真实飞书闭环，编辑拒绝矩阵已覆盖冻结线、非法 DAG、陈旧预览与跨人员非 Owner。Task 事件在本轮仍未被 bot 长连接收到，周期状态轮询继续承担可靠完成发现。migration `0018_worker_wakeups` 已让六条 Worker 连接在耐久队列提交后收到数据库通知，原有有界轮询继续兜底。提交 `a506e7d` 把批次 Worker 的完成时间改为逐项结算；提交 `5312f6c` 又把五条凭据侧交互车道从 Projection 拆到两个 `claim_limit=1` 独立副本，并完成八服务开发部署。旧拓扑修正版五次真实人员选择卡的首反馈 P50 / P95 为 0.991 / 1.274 秒，最终回复为 12.670 / 19.298 秒；前四次突发点击的最终回复范围为 8.368 到 19.569 秒，第五次隔离点击为 4.044 秒。新双副本三次突发点击全部成功并由两个副本实际分流，最终回复 P50 / P95 为 4.793 / 5.498 秒；三张原卡片均从飞书服务端读回为不可再次提交的确认终态。双副本仍需隔离和更高强度限流回归；所有首反馈指标均不包含客户端渲染。
- 已完成节点安全重启：服务端计算目标及可达下游，耐久预览绑定 actor、版本和图 revision，确认事务创建新 Attempt、保留历史并收口旧 Human Task；重复确认 no-op。离线变异、一次性 PostgreSQL 14 双连接竞争及测试组织 Human-Agent-Human 真实闭环均已通过。
- 已完成完整实例安全重启：显式 instance scope 计算全图影响，确认后为所有节点创建新 Attempt，从全部根节点重新调度；旧 Attempt 与两轮完成投影保留。离线套件、一次性 PostgreSQL 14 双连接竞争和测试组织三节点第二轮闭环均已通过。
- 已完成运行中未来区域安全编辑：`add_node / update_node / remove_node` 只触及没有执行痕迹的 `pending / ready` 节点；耐久预览绑定 actor、aggregate version、`graph_revision` 与候选 Snapshot SHA-256，确认事务只递增一次 revision 并保留已执行历史。完整离线套件、一次性 PostgreSQL 14 双连接竞争、十四份 migration、开发服务器部署和 Owner 真实飞书闭环均已通过；真实命令还拒绝了冻结线修改、成环依赖和陈旧预览。
- 已完成自动节点失败恢复闭环：向节点 Owner 发送恢复卡，卡片回调进入耐久命令，领域侧重新校验 Owner 与精确 Instance / Node / Attempt 版本。重试和人工接管都创建新 Attempt，失败历史不覆盖。当前完整离线套件为 `807 passed, 17 skipped`；长期 PostgreSQL 十八份 migration、两个连续真实重试、人工接管、Human Task 完成、最终投影，以及恢复卡 0.990 秒首个服务端反馈均已在开发环境验收。
- 从 legacy 原型提炼 adapter、事件韧性和 Mock 测试资产。
- 已完成 Personal Agent Edge Proof v0 的代码扩展：一次性配对、哈希凭据、设备撤销、Owner 与 capability 双重过滤、现有 Attempt claim 续租、迟到结果拒绝、loopback Gateway、手工 `run-once`、前台 `serve` 和 Codex 只读适配器。`serve` 已具备单设备锁、有界长轮询、退避、应用心跳、续租失败取消和信号安全停止；内容提交 `fd6933a186bf115fe83adc5ac7d3a3b6153b0436` 已发布，完整离线套件与安装态 wheel 已通过，但员工设备真机 E2E 和开发部署尚未执行。既有一次性 PostgreSQL 14、长期开发库 migration、loopback Gateway systemd 部署、SSH 隧道跨机 Codex、Caddy 与受信任源站证书已通过；公网设备链路受 ICP 接入备案阻断，Caddy 验证后已停止，凭据系统存储和安全评审仍未完成。

**Demo：** 已从真实飞书消息创建并确认模板草稿，在测试组织完成 Human-Agent-Tool-Human，最终 Docx 包含四个节点结果，Owner 收到带链接的完成通知；重复修复为 no-op。另一个 Human-Agent-Human 实例先完成最终节点重启，再完成全图实例重启；第二轮从根节点重新调度，三个新 Attempt 均完成，新旧 Task、结果、文档和最终通知均保留，重复确认 no-op。未来区域编辑实例在首个 Human 节点等待时修改最终 Human 节点标题，幂等确认后完成 Agent、更新标题 Task、Docx 与最终通知；独立负向实例拒绝冻结线、成环依赖和陈旧预览。失败恢复实例连续重试两次后由 Owner 人工接管，真实 Task 完成后第四个 Attempt 和 Instance 进入 `done`，前三次失败历史与全部投影仍可追溯。测试成员持有的合成实例还验证了当前登录用户的真实跨人员编辑命令被拒绝，且图修订、预览和审计保持不变。跨人员正向分工分别通过群聊 mention 和单聊人员选择卡创建冻结草稿；卡片成功后回写为已确认状态。该证据仅覆盖开发环境，无模板定义的同等真栈入口仍待补充。

## Next · Phase 2 受控变化与恢复

- 针对五次卡片验收暴露的队头阻塞，已部署两个独立 Interactive 副本，并把每条车道的单次 claim 固定为 1。三次真实飞书突发点击已确认共享 bot profile、租约、幂等、双副本分流和卡片终态均正常。后续只在真实业务或功能验收自然产生点击时继续采集耐久指标，不再把反复人工点击计时设为独立门槛；现有小样本仍不得外推生产容量。
- 下一项工程验收是把内容提交 `fd6933a` 部署到受控员工设备链路，通过 SSH 隧道验证前台 `serve` 连续领取、应用心跳、续租、SIGTERM 停止、设备撤销和同凭据双进程拒绝。公网入口仍不是该验收的前置条件。

目标：让运行中流程可以安全修改、重做和运营，而不覆盖历史。

- 未来区域编辑、影响预览、确认和 `graph_revision` 乐观并发已经落地；继续补齐图形化 diff、批量操作体验与真实组织回归矩阵。
- 两类重启已保留 Attempt 历史、结果、质量记录和完成投影；继续补全交付物引用与跨轮次浏览体验。
- `pass/fail + evidence + suggestion` 质量结果与有限 Agent 重试。
- 暂停、流程级恢复、取消和运维告警；继续补齐可配置自动重试策略与人工接管运营视图。
- 投影缺失重建、重复事件与乱序事件验证。

**Demo：** 通过真实飞书命令修改未开始分支、幂等确认并拒绝过期确认；重启中间节点后只重做其下游；旧 Attempt 和审计保持可查。

## Later · 证据驱动的扩展

只有真实使用证明必要时，再评估：

- 模板子 DAG、临时子 DAG和最多三级父子契约。
- 个人 Agent Edge 的产品化、后台常驻、写能力、离在线状态和通用 Capability Lease。
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
