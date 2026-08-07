# ROADMAP · larkflow

> 状态：Target Delivery Plan · 2026-08-07
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
- 已完成 `llm.generate` Agent adapter、确定性 `content.check` 与 `source_claims.check` Tool adapter、按 kind 路由、claim 与超时预算校验，以及直接依赖结果到下游 Human 责任入口的投影。来源约束型结果和明确接受或退回决定已在内容提交 `b7e589ba4af0398573ec995254dd61e9b1a4508c` 落码并通过完整离线套件；开发部署、真实 PostgreSQL 回读和公开软件需求材料的真实飞书接受路径均已完成。既有 Human-Agent-Tool-Human 四节点闭环也已在开发云服务器与测试组织真实通过。
- 已完成 Template Service：`draft / enabled / disabled / deleted`、不可变版本、布尔锁、角色与参数绑定、追加型模板审计和 aggregate version 乐观并发；真实 PostgreSQL 同时启用竞争验证已通过。
- 已完成从启用模板生成冻结草稿、Owner 只读预览和正式 CLI 入口；开发环境已用合成输入创建、预览、确认模板实例，并完成正式模板的真实 `Human -> Agent -> Human` 闭环。
- `alicloud-sh` 已建立长期 Target 开发库、本机 peer authentication、每日备份、新库恢复演练和 enabled Target 服务；仍缺异机备份、PITR 与生产运行手册。
- 已完成 Owner 中央控制台 v0：真实 PostgreSQL 可展示本人最近流程、运行中或草稿 DAG、跨轮次 Attempt 与追加型审计；非 Owner 真实实例返回 404。内容提交 `623b9b6228caa52b4680eb30ad2fee723e8921b6` 已按真实 `deps` 分层绘制依赖，`b153c5311771eaa5b98d964fe6ffd448b62cf49d` 与 `c3e23fcbf3bf9e66eeb9cf97bf8bbbc1bb2eefc3` 增加拖动缩放操作并隔离节点点击，`efc1dff935d21918517d73c0d10fd15336516d9a` 又在详情顶部直接汇总最终状态、返工节点与最近重启。内容提交 `30dc7ee` 与查询边界加固 `b6eda8c` 进一步实现 Owner 待处理中心，从有界实例集合派生失败恢复、本人 Human、暂停继续和草稿确认提示；页面只能复制既有飞书命令并打开只读详情。该读模型已完成开发部署，真实认证 API 回读 30 个本人流程和 22 条待处理项，PostgreSQL 直接查询与静态资源哈希均通过；同一前端发布内容的非敏感浏览器功能验收确认按钮终态、无横向溢出和无浏览器错误。飞书 OAuth 登录、至少两名真实成员 Owner 隔离和内容提交 `a6f5babb07623590e9be2a2b8c523857cce56ff7` 的 PostgreSQL 耐久会话也已部署，真实浏览器登录态跨 Console 重启保持。内容提交 `e15f47942fcc01bc85ecbbfa822acd00558c06f0` 增加服务端 allowlist 授权的当前企业管理员只读概览；内容提交 `8ba0ab9d93554b7958a650492e0282ad40db0d2e` 又增加其他浏览器会话的列表、五分钟耐久预览、显式确认和追加型审计。真实 PostgreSQL 竞争、管理员授权、普通成员 404、当前会话保护、撤销失效、幂等、服务部署和会话治理面板真实登录浏览器目视均已验收。内容提交 `66b2c12d3ea27a61e5a1cdc21332ed03adb516ac` 又完成 Caddy 可信来源覆盖、连接边界、完整安全响应头和 Console 有界令牌桶的真实公网回归。Owner 领域资源继续只读，静态 token 只保留为 loopback 开发回退。下一门槛是把管理员 allowlist 变更收敛为有回读和回滚的运维流程，并重跑开发恢复演练；不优先引入通用画板依赖或可写领域前端。
- 每个节点的唯一 Owner 解析与服务端授权。当前内核已拒绝非 Owner 提交，飞书 IM 命令发送者的活跃成员校验已在测试组织通过；草稿 Owner 全量企业目录校验已落码并部署但默认关闭。
- 独立业务 Scheduler 和 Human、Agent、Tool Node Runner。领域规则、持久化、常驻 Worker、Agent adapter、首个 Tool adapter、真实开发链路与恢复扫描已落码，更多业务 Tool 按验证需求增加。
- 已完成启动全量 Task 对账、缺失 Projection 补建和确认删除后的外部 Task 重建；一次性 PostgreSQL 与常驻开发服务均已验证补建、真实删除换绑、重入及修复后完成入站。
- 已完成飞书窄命令入口与完成投影：`/larkflow help / start / draft / confirm / status / list / pause / resume / cancel / cancel-confirm / restart / restart-all / restart-confirm / edit / edit-confirm`、耐久发送者校验与回复、Agent / Tool 结果消息、完成 Docx 和最终通知已进入开发链路。`start` 的模板入口和两种 `draft` 都只创建草稿，仍需独立确认。带 JSON 的无模板高级入口沿用严格解析、100 节点上限和 `role=@成员` mention 绑定；裸 `draft` 打开自然语言 Card 2.0，收集目标、可选背景与一名协作者，再由中央 Agent 生成最多八个 Human / Agent 节点。服务端覆盖原始输入、限制 Owner 角色并重新校验 Snapshot，首个非法候选最多有界重生成一次，原卡片最终显示无按钮图预览。结构化无模板实例 `im_a9a43d1d4db354b31b798bb1` 已完成 Human-Agent-Tool-Human 4/4，PostgreSQL 回读 `template_version_id IS NULL / status=done`；自然语言实例 `im_69af9ebdf241017341e5fee4` 已完成真实飞书草稿验收，保持 `draft / 3 nodes / 0 Attempt`，唯一 canonical 动作为 `processed / draft_created / sent`。另一自然语言实例 `im_74e775110afbd80aa598d3ae` 已由真实用户确认启动，Agent 和 Human Attempt 1 全部完成，周期 Task 读回经耐久 Inbox 提交流程，最终为 `done / template_version_id IS NULL / 2 nodes done`；完成 Docx 与最终通知均已从飞书服务端读回。人员选择卡、自然语言引导卡和失败恢复卡统一在动作耐久落库后先尝试显示无按钮“处理中”，再收口为成功或拒绝；同一张卡只有一个 canonical 动作。`status` 只允许 Instance Owner 查看单实例有界摘要，`list` 只返回本人最近十个实例摘要；两类重启和未来区域编辑只允许 Owner 预览和确认。编辑拒绝矩阵已覆盖冻结线、非法 DAG、陈旧预览与跨人员非 Owner。Task 事件在本轮仍未被 bot 长连接收到，周期状态轮询继续承担可靠完成发现。migration `0018_worker_wakeups` 已让六条 Worker 连接在耐久队列提交后收到数据库通知，原有有界轮询继续兜底。提交 `a506e7d` 把批次 Worker 的完成时间改为逐项结算；提交 `5312f6c` 又把五条凭据侧交互车道从 Projection 拆到两个 `claim_limit=1` 独立副本，并完成八服务开发部署。旧拓扑修正版五次人员选择卡的首反馈 P50 / P95 为 0.991 / 1.274 秒，最终回复为 12.670 / 19.298 秒；新双副本三次突发点击的最终回复 P50 / P95 为 4.793 / 5.498 秒。双副本仍需隔离和更高强度限流回归；所有首反馈指标均不包含客户端渲染。
- 已完成节点安全重启：服务端计算目标及可达下游，耐久预览绑定 actor、版本和图 revision，确认事务创建新 Attempt、保留历史并收口旧 Human Task；重复确认 no-op。离线变异、一次性 PostgreSQL 14 双连接竞争及测试组织 Human-Agent-Human 真实闭环均已通过。
- 已完成完整实例安全重启：显式 instance scope 计算全图影响，确认后为所有节点创建新 Attempt，从全部根节点重新调度；旧 Attempt 与两轮完成投影保留。离线套件、一次性 PostgreSQL 14 双连接竞争和测试组织三节点第二轮闭环均已通过。
- 已完成运行中未来区域安全编辑：`add_node / update_node / remove_node` 只触及没有执行痕迹的 `pending / ready` 节点；耐久预览绑定 actor、aggregate version、`graph_revision` 与候选 Snapshot SHA-256，确认事务只递增一次 revision 并保留已执行历史。完整离线套件、一次性 PostgreSQL 14 双连接竞争、十四份 migration、开发服务器部署和 Owner 真实飞书闭环均已通过；真实命令还拒绝了冻结线修改、成环依赖和陈旧预览。
- 历史说明：本节较早条目中的六条监听连接、八个 Python 服务与 migration 18 是当时验收快照，当前 As-built 由下一条覆盖。
- 内容提交 `1a80b4035d0a5ad5c634af7be957f4b7d1ee37d7` 已把自然语言草稿的慢模型调用隔离到无飞书凭据的 Draft Generation Worker，并增加 `generating / repairing` 进度 revision、最终回复栅栏和覆盖两次模型调用的生成租约预算。migration 19、第九服务、真实 PostgreSQL 竞争和真实飞书阶段变化均已完成。内容提交 `2ed644e640f3c3834f82c464e05fe0b4c3a241cc` 进一步把后续阶段与终态改为按消息 ID 更新；旧卡修复、新卡完整收口，以及自然语言候选图确认后的 Agent、Human Task、完成 Docx 与最终通知闭环均已通过。下一门槛是带真实材料的窄业务功能和产品价值验证，不再单独反复点击计时。
- 内容提交 `b7e589ba4af0398573ec995254dd61e9b1a4508c` 已为该门槛实现首个来源约束型材料复核模板，并完成真实 PostgreSQL migration 回读、模板启用、Human 来源确认、Agent 生成、确定性来源契约检查和 Human 明确接受的飞书闭环。实例 `source_grounded_20260805_234517` 最终为 4/4 完成，Tool 回读 4/4 条事实与 3/3 个开放问题覆盖、零违规。第二个公开材料实例 `source_grounded_reject_20260806_001940` 又完成明确退回、三节点重启预览与确认、Attempt 2 重新执行和最终接受恢复；旧结果、退回决定、两张决定卡和唯一重启审计均保留。接受与退回返工两条路径已关闭开发真栈门槛，后续不再用更多合成实例代替真实使用。
- 已完成自动节点失败恢复闭环：向节点 Owner 发送恢复卡，卡片回调进入耐久命令，领域侧重新校验 Owner 与精确 Instance / Node / Attempt 版本。重试和人工接管都创建新 Attempt，失败历史不覆盖。该链路在 `1a80b4035d0a5ad5c634af7be957f4b7d1ee37d7` 下的完整离线套件为 `884 passed, 18 skipped`；长期 PostgreSQL 在候选部署前仍为十八份 migration，两个连续真实重试、人工接管、Human Task 完成、最终投影，以及恢复卡 0.990 秒首个服务端反馈均已在开发环境验收。
- 从 legacy 原型提炼 adapter、事件韧性和 Mock 测试资产。
- 已完成 Personal Agent Edge Proof v0 的代码扩展与受控员工 Mac 前台真机验收：一次性配对、哈希凭据、设备撤销、Owner 与 capability 双重过滤、现有 Attempt claim 续租、迟到结果拒绝、loopback Gateway、手工 `run-once`、前台 `serve` 和 Codex 只读适配器。内容提交 `fd6933a186bf115fe83adc5ac7d3a3b6153b0436` 已部署到开发服务器；同一候选 wheel 通过 SSH 隧道完成空闲心跳、连续领取、真实 Codex、18 次续租、单设备锁、SIGTERM 安全停止与撤销后拒绝。该临时测试设备、凭据和隧道已删除。macOS 默认 Keychain 密钥存储、非敏感元数据引用和旧明文文件校验迁移已落码，并先通过合成登录钥匙串创建、回读和删除，再以真实流程 Owner 身份完成员工 Mac 默认槽位的一次性配对。内容提交 `5b0c79b4d946441063d92970e8f0e9cac31b2ab3` 又实现独立 manager、wheel SHA-256 校验、版本化 venv、原子 `current / previous` 切换、回滚和离线 `doctor`；员工 Mac 已真实完成 `0.0.1 -> 0.0.2 -> rollback -> 0.0.2`。内容提交 `81bd43983598ff319150344e779223cd03731eba` 进一步实现哈希锁定离线 wheelhouse、目标绑定、精确依赖清单与修复版 bootstrap pip，故意破坏索引和代理后仍完成真实安装；安全评审已形成，正式员工分发结论为 No-Go。公网设备链路仍受 ICP 接入备案阻断，Caddy 保持停止。

**Demo：** 已从真实飞书消息分别创建并确认模板草稿与两类无模板草稿。结构化无模板实例 `im_a9a43d1d4db354b31b798bb1` 在测试组织完成 Human-Agent-Tool-Human 4/4，数据库确认没有模板版本引用。自然语言实例 `im_74e775110afbd80aa598d3ae` 从受限候选图经独立确认完成 Agent 与 Human 两个节点，Task 完成信号经耐久 Inbox 提交，完成 Docx 与最终通知均可从飞书服务端读取；该测试没有真实业务数据，只验证技术闭环。来源约束型实例 `source_grounded_20260805_234517` 使用公开软件需求材料完成 Human-Agent-Tool-Human 4/4，确定性来源检查通过，Owner 明确接受，决定卡、完成文档、最终通知和 PostgreSQL 审计均已回读。模板实例的最终 Docx 包含四个节点结果，Owner 收到带链接的完成通知；重复修复为 no-op。另一个 Human-Agent-Human 实例先完成最终节点重启，再完成全图实例重启；第二轮从根节点重新调度，三个新 Attempt 均完成，新旧 Task、结果、文档和最终通知均保留，重复确认 no-op。未来区域编辑实例在首个 Human 节点等待时修改最终 Human 节点标题，幂等确认后完成 Agent、更新标题 Task、Docx 与最终通知；独立负向实例拒绝冻结线、成环依赖和陈旧预览。失败恢复实例连续重试两次后由 Owner 人工接管，真实 Task 完成后第四个 Attempt 和 Instance 进入 `done`，前三次失败历史与全部投影仍可追溯。测试成员持有的合成实例还验证了当前登录用户的真实跨人员编辑命令被拒绝，且图修订、预览和审计保持不变。跨人员正向分工分别通过群聊 mention 和单聊人员选择卡创建冻结草稿；卡片成功后回写为已确认状态。以上证据仅覆盖开发环境。

**当前产品门槛：** 使用 larkflow 项目自身的真实开发、复核或发布工作做受控内部试用。每个实例记录是否完成、首次结果是否可用、是否退回、人工干预次数、从启动到明确决定的耗时，以及是否出现重复外部副作用。首轮只建立小样本基线，不预设市场收益或生产容量结论。

首个真实内部试用样本 `im_ff8b47359aedfd7360896404` 已完成至最终 Human 明确退回，从启动到决定用时 22 分 38 秒。首次 Agent 调用遇到 429 后发生一次例外人工重试，没有观察到重复外部副作用。该样本暴露的退回意见缺口已由内容提交 `0dc5359e990635c7b6aa16ec0bcd798eb8df39d0` 修复，并由 `f6125331aa541e824675e25f9cd2d756cd4c6b56` 补齐原生 Card 2.0 表单绑定。真实实例 `im_5717aa5b9480d146239907d5` 已证明具体意见进入 Agent Attempt 2、未污染上游和下游占位 Attempt，并使确定性 Tool 从首轮失败转为通过；该发布门槛已关闭。

第三个真实内部试用样本 `pilot_console_value_20260806_164925` 使用本路线图的固定提交版本作为来源登记，围绕中央只读控制台的试用判断生成研发简报。Human 来源确认、Agent、Tool 和最终 Human 均在 Attempt 1 完成，确定性 Tool 覆盖 5/5 条来源事实与 3/3 个开放问题，Owner 明确接受首次结果。实例从确认启动到接受用时 12 分 41 秒，接受卡首个服务端反馈为 1235 ms；Task、两条自动结果消息、决定卡、完成文档和最终通知均只有一个外部绑定，没有重启、例外人工干预或已观察到的重复外部副作用。

首批三项小样本已同时出现直接退回、带意见返工和直接接受，证明当前流程能保留这些真实产品信号。其中一项首次结果可直接接受，一项需要明确返工，一项因早期 429 发生例外人工重试。样本数量小、任务不同，不能推出稳定内容质量、市场收益或生产容量。第一次 Owner 独立 Console 使用已成功定位指定返工实例，但用户无法从图中发现回复位置，也无法拖动或缩放执行图。前者明确为只读观察面边界，后者已按真实反馈修复并完成开发者真机组合验收；尚未完成修复后的 Owner 再次独立使用，因此控制台降低追踪成本的假设仍未验证。

## Next · 受控内部试用与 Phase 2 恢复

- Owner 待处理中心的提交、部署、真实 PostgreSQL 范围核对、响应隐私检查和同发布内容浏览器功能验收已经完成。下一步由 Owner 不依赖聊天 `status` 命令或开发者代查，从真实待处理卡判断失败恢复、本人 Human、暂停或草稿的下一步，复制命令或打开详情，并确认按钮点击即时反馈。取得这项独立使用证据后，再决定分页筛选、跨轮次对比或继续保持现状。任何可写前端仍必须复用既有预览确认、Owner 重授权和版本栅栏，不能直接改数据库。
- 飞书应用内员工工作台登录、PostgreSQL 耐久会话、最小管理员聚合和其他会话撤销已通过公网 IP HTTPS 开发部署。至少两名真实成员已完成 OAuth 登录、本人 Owner 可见性和跨 Owner 隔离验证，机器人能力继续可用；完成登录的真实会话已通过 Console 重启后直接刷新仍保持登录。管理员会话治理的真实 HTTP、PostgreSQL 竞争、真实登录浏览器目视，以及公网入口有界限流和完整安全响应头回归均已完成。接下来把 allowlist 变更整理为最小受控运维动作，覆盖授权前预览、服务重启、管理员与普通成员回读、失败回滚和审计记录，再重跑包含 Console 会话表的异库恢复演练。正式域名因近期不备案保持后置；公网 IP、静态开发 token、进程内限流和单机部署都不作为正式员工交付方案。
- 具体退回意见与目标 Attempt 返工上下文已完成真实飞书和 PostgreSQL 验收。下一批内部试用不再重复验证该状态机路径，转而观察 Agent 是否稳定理解意见、返工结果是否可直接接受，以及人类需要多少次补充说明。
- 首批三项真实工作基线已经建立，不再把样本数量本身当作门槛，也不为已通过的状态机路径创建纯合成点击任务。后续真实工作应围绕尚未回答的产品问题选择，并继续保留失败与返工作为有效信号。测试成员有空时再加入跨人员样本，不把其响应设为阻断条件。
- 针对五次卡片验收暴露的队头阻塞，已部署两个独立 Interactive 副本，并把每条车道的单次 claim 固定为 1。三次真实飞书突发点击已确认共享 bot profile、租约、幂等、双副本分流和卡片终态均正常。后续只在真实业务或功能验收自然产生点击时继续采集耐久指标，不再把反复人工点击计时设为独立门槛；现有小样本仍不得外推生产容量。
- Edge 的 macOS 开发试用最小安装、升级、回滚与离线依赖交付已经落地，且没有增加隐藏后台常驻。下一步按安全评审 P0 顺序推进：先拆出不含中央运行时的最小 Edge 包并生成固定 lock、SBOM 和构建证明，再取得 Developer ID 身份完成签名、公证与可信摘要发布，最后在全新员工 Mac 验证首装、故障回滚、撤销和数据边界。当前持久设备通过受控 SSH 隧道使用；若要形成可持续远程连接，仍需完成 ICP 接入备案或迁移合规地域。

目标：让运行中流程可以安全修改、重做和运营，而不覆盖历史。

- 未来区域编辑、影响预览、确认和 `graph_revision` 乐观并发已经落地；继续补齐图形化 diff、批量操作体验与真实组织回归矩阵。
- 两类重启已保留 Attempt 历史、结果、质量记录和完成投影；继续补全交付物引用与跨轮次浏览体验。
- `pass/fail + evidence + suggestion` 质量结果与有限 Agent 重试。
- 暂停、流程级继续和版本绑定取消已经落码、部署并完成真实飞书验收，采用“停止新调度、允许已发出节点收口”的 drain 语义。真实 PostgreSQL 双连接竞争、Owner 命令、普通 Human Task 完成收口与 Human 决定卡原位冻结均已通过；下一步补运维告警、可配置自动重试策略与人工接管运营视图。
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
