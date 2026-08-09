# ROADMAP · larkflow

> 状态：Target Delivery Plan · 2026-08-10
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

## Now · Phase 1 首次成功路径与可用性收口

目标：一名普通员工不阅读说明、不复制飞书命令，也能在五分钟内完成“描述目标、核对草稿、确认启动、处理人工任务或判断、查看结果”的首次闭环。

当前明确不进入邀请测试阶段。开发真栈已经覆盖中央状态、飞书投影、工作台和画板，但历史测试数据、内部术语、网页与飞书之间的入口切换仍使首次使用成本过高。当前发布门槛是清理默认工作区、压平动作层级、把接受或退回收回工作台、用业务语言展示上下文和结果，并由开发者独立完成一条不依赖口头指导的首次成功路径；在此之前不得描述为可推荐给其他人使用。

- 内容提交 `1063b29eece4b0dea05467982e5b581af3e79ff2` 已把首次发起压缩为一个必填目标，背景、约束和协作者默认收起；三个安全示例只填表不提交；流程记录默认只展示仍需跟进的项目，已结束历史仍可显式筛选；草稿打开后先进入步骤概览，而非复杂画板，并按状态展示单一下一步。本地受控页面已检查深浅色、无横向溢出和示例无副作用，完整离线套件为 `1037 passed, 24 skipped`。开发部署、真实飞书登录可见验收和五分钟独立首次成功路径尚未完成，邀请测试门槛保持关闭。

- 内容提交 `64424f1de429f051af58739962f504e62337755d` 已把版本绑定的 Human 决定纳入参与者任务面，普通任务继续提交或转交，决定任务可直接接受或填写意见后退回；飞书决定卡继续作为通知与备用入口。工作台新增三步首次使用引导，并把主路径中的 `DAG / Human / Agent / Attempt / Graph revision` 改为业务语言。完整离线套件为 `1037 passed, 24 skipped`，wheel SHA-256 为 `a79d55caa8398806c826acf6ede4f0a625d48637de3a525e6bbc2d57c1f44ed0`；开发部署、23 份 migration、十个 Python 服务、Caddy 和公网资源哈希已经回读。真实登录标签页刷新时浏览器控制连接超时，可见验收仍未完成，不能据此关闭可用性门槛。

- 已完成领域内核：不可变 Instance Snapshot、DAG Contract 核验、草稿确认、NodeInstance、Attempt、显式状态迁移、Scheduler、中央 Node Runner、claim 和仓储 Port。
- 已完成 PostgreSQL 14 第一版 schema：Template、TemplateVersion、Instance、NodeInstance、Attempt、Projection、Audit、Outbox。
- 已完成 Instance 聚合事务仓储、JSONB 快照、乐观并发、追加型 Audit、带租约 Outbox 和 package-data migration；真实 PostgreSQL 14 一次性数据库集成验证已通过。
- 已完成单步 Runtime Worker、持久化 runnable scan、Worker 身份认领、精确租约到期恢复与稳定 Attempt 幂等键；真实 PostgreSQL 双 Worker 竞争和崩溃恢复验证已通过。
- 已完成独立 `larkflow-target` CLI、PostgreSQL 通知唤醒、轮询兜底、有界退避、SIGTERM 干净停机、adapter 能力过滤与 systemd 服务装配；SIGKILL 后新 Worker 接管同一 Attempt 已在真机验证。
- 已完成独立 Projection Worker、事件类型过滤、Task adapter、稳定幂等键、Projection 落库、失败重试和 systemd 服务装配；测试组织中的 Human Task 创建与完成闭环已真实通过。
- 已完成 Task 完成状态的耐久入站：Projection 周期扫描当前 Human Task，以稳定信号 ID 去重写入 PostgreSQL Inbox；legacy EventKey 事件保留为可选低延迟信号。无论入口来源，凭据侧都会重新读取飞书 Task，领域侧再校验 Projection 绑定、当前 Attempt、唯一 Owner 和完成人后提交 Human 节点。凭据验证默认最多尝试 24 次，耗尽后进入可审计且不可再认领的终态并暴露结构化告警信号。两个外部 Task 已完成而领域仍等待的开发实例，已由轮询自动推进到完成。
- 已完成 `llm.generate` Agent adapter、确定性 `content.check`、`source_claims.check` 与 `source_decision.check` Tool adapter、按 kind 路由、claim 与超时预算校验，以及直接依赖结果到下游 Human 责任入口的投影。来源约束型材料复核和明确接受或退回决定已在内容提交 `b7e589ba4af0398573ec995254dd61e9b1a4508c` 落码并完成开发真栈验证。真实内部样本 `im_fb85651d34e24c9789304715` 又暴露材料复核契约无法代替决策契约：旧 Agent 按设计保留 Q，最终没有回答 Q1、Q2、Q3，Owner 已明确退回。内容提交 `db7651228e26055eb1229ae9f451e3e87c31df38` 已新增独立 `source_decision.v1 + source_decision.check` 与 `source_grounded_decision` 模板，完成开发部署、真实 PostgreSQL 回读和飞书接受闭环。
- 已完成 Template Service：`draft / enabled / disabled / deleted`、不可变版本、布尔锁、角色与参数绑定、追加型模板审计和 aggregate version 乐观并发；真实 PostgreSQL 同时启用竞争验证已通过。
- 已完成从启用模板生成冻结草稿、Owner 只读预览和正式 CLI 入口；开发环境已用合成输入创建、预览、确认模板实例，并完成正式模板的真实 `Human -> Agent -> Human` 闭环。
- `alicloud-sh` 已建立长期 Target 开发库、本机 peer authentication、每日备份、新库恢复演练和 enabled Target 服务；仍缺异机备份、PITR 与生产运行手册。
- 已完成 Owner 中央控制台 v1：真实 PostgreSQL 可展示本人最近流程、真实 DAG、跨轮次 Attempt、追加型审计、返工摘要与派生待处理提示；非 Owner 与不存在实例统一返回 404。既有提交已完成拖动、缩放、适配、节点选择、浅色与深色主题、飞书 OAuth、至少两名真实成员 Owner 隔离、PostgreSQL 耐久会话、管理员聚合、其他浏览器会话撤销、Caddy 安全边界、allowlist 运维与隔离恢复。内容提交 `da94891f5e6d01ecee6082a98bab6148abba12ee` 又把确认草稿、暂停和继续接入既有领域服务，把取消与节点或完整实例重启接入同页预览确认；Human 输入与决定仍留在飞书。按钮在请求前立即显示执行中或生成预览，高风险确认保留 aggregate version、耐久 RestartPreview、幂等、旧 Attempt、结果和审计。完整离线套件为 `995 passed, 21 skipped`，wheel SHA-256 为 `fca2eee16d3af57dcfb4bb78409a0b6f9e23b7d3d29aa7d7435cc1f26dd3063a`，已部署到 `/srv/larkflow/target/releases/20260808_235309_console_actions_da94891/`；长期库保持二十一份 migration，本次只重启 Console，全部服务与 Caddy 均为 active、`NRestarts=0`，公网静态资源哈希与源码一致。真实登录 Owner 已在公网工作台直接确认并启动 `internal_trial_20260808_155244`；三节点均为 Attempt 1，实例终态 `done / version 7`，所有 Task、Agent 结果、完成文档和通知均有外部绑定。下一产品门槛改为网页 Human 责任入口和待办转交契约；暂停、继续、取消和重启仍需逐项网页验收，通用画板依赖和运行中图编辑继续后置。
- 内容提交 `b60cbbd8beb98742cc80082df78ac185274e3a8a` 已关闭第一版受控 DAG 画板的代码和开发部署门槛。React Flow 与 ELK.js 提供真实依赖图、拖动、缩放与自动布局；个人节点位置只保存在浏览器。Owner 可从画板增加、修改或删除未开始节点，也可从选中节点发起返工；结构修改复用 GraphEditPreview，返工复用 RestartPreview，服务端继续重验冻结线、DAG、Owner、版本和影响集合。真实登录 Owner 随后在服务器纯合成实例完成未来节点修改、三节点返工、末尾 Human 节点新增和循环依赖拒绝，Graph 从 r1 进入 r3。内容提交 `f320fd5b9b200fae24cefeb6a853c684a38e7565` 继续让草稿态接入同一画板，并增加节点端点连边和选中连线断开。一次性真实 PostgreSQL 验证两次草稿编辑不创建运行时，独立启动后才物化节点与 Attempt。完整离线套件为 `1034 passed, 24 skipped`，新 wheel 已部署并完成公网资源、服务和授权边界回读。下一门槛回到小范围真实业务试用；多人实时协同、任意自由图形和生产装配继续后置。
- 内容提交 `3d438bb476ad9b9f98cd4c2873802a2894718fe4` 已关闭网页普通 Human 责任入口与待办转交的代码和开发部署门槛。任务参与者只看有界任务上下文，可以提交结果或转交给同租户活跃成员，不能读取完整协作者实例；决定节点继续使用飞书决定卡。转交保留冻结 Snapshot，只移动运行时 NodeInstance Owner，并追加审计与 outbox、同步既有飞书 Task 负责人。完整离线套件为 `1003 passed, 22 skipped`，真实 PostgreSQL 双连接竞争只有一路成功。内容提交 `3fd42df8740825482eb3bbebd5cf69715f37df5b` 又完成真实浏览器提交和跨成员转交验收，并把页面终态修正为“中央已转交，飞书同步中”。飞书 Task、中央 NodeInstance 与 Projection 负责人一致，`sync_version=2`；下一产品门槛转为受控流程输入框。
- 内容提交 `432fea77c210e7a2cfa5344054eb30d01706bf87` 已关闭受控流程输入的代码、迁移与开发部署门槛。工作台填写的目标、背景和可选协作者先写入 PostgreSQL `DraftRequest`，无飞书凭据的中央 Worker 领取后生成并确定性校验候选 DAG，成功结果只创建草稿并复用现有“确认并启动”。重复提交幂等、候选冻结、租约接管和最多五次失败终止均已覆盖。完整离线套件为 `1015 passed, 23 skipped`；长期库应用第二十三份 migration，真实双 Worker 竞争只有一路领取，部署服务与公网边界正常。同一真实成员主体的临时会话先用纯合成文本通过真实 API 和中央模型创建三节点草稿，随后真实 Chrome 会话又从公网工作台完成点击生成并自动打开新草稿。数据库回读两条验收实例均保持 `draft / 0 Attempt / 0 Projection`，未创建外部待办。下一产品门槛转为使用真实业务材料开展小范围受控试用，观察生成质量、返工原因与人工接管体验。
- 内容提交 `d879a280d49e584d2d7e5927a498e7947544bb63` 已关闭自然语言 Agent 候选缺少明确验收出口的结构缺口。服务端要求所有终端节点均为 `accept_reject` Human 决定，且退回目标是直接上游 Agent；纯 Human 流程保持普通待办。完整离线套件为 `1023 passed, 23 skipped`，开发发布与真实合成闭环均已完成。实例 `console_draft_80707de5ea8149809d15433510e67128` 先退回 Agent Attempt 1，再只重启 Agent 与最终 Human，Agent Attempt 2 收到具体意见并补充回滚条件与监控窗口，最终明确接受；旧结果、两张决定卡、完成文档、通知和审计均保留。后续不再为该状态机路径创建纯合成点击样本，转向真实内部工作中的首次结果可用性和返工质量。
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

第四项真实内部样本 `im_fb85651d34e24c9789304715` 使用当前路线图回答未来一个工作周期的优先级、完成条件和后置项。实例完整走到 Human 决定，但旧 `source_claims.v1` 把 Q 继续保留为开放问题，未形成可执行答案；风险条目还因类别不符被确定性 Tool 判为失败。Owner 已退回，卡片、Human Attempt 意见、旧 Agent 和 Tool 结果及审计均已保存。该失败证明状态机可以保留否定信号，也证明材料整理与决策生成必须使用不同结果契约。同一登记输入随后在新模板实例 `source_decision_20260808_0405` 中完成四个 Attempt 1，Q1、Q2、Q3 全部回答，Tool 为 `pass`，Owner 明确接受。该对照关闭了决策契约的开发真栈门槛，但不证明建议在业务上正确。

## Next · 受控内部试用与 Phase 2 恢复

- Owner 流程操作、普通 Human 任务页面、受控流程输入与受控 DAG 画板均已提交和部署。公网纯合成实例已覆盖运行中未来节点修改与新增、图编辑确认、节点返工、旧 Attempt 保留、循环依赖拒绝和 PostgreSQL 审计回读；草稿态又完成真实登录拖拽连接、两次预览确认、选边断开和恢复原图。最终实例保持 `draft / graph_revision 3 / 0 NodeInstance / 0 Attempt / 0 Projection`，草稿依赖连线可见手势门槛已经关闭。下一步回到项目自身的真实工作，继续观察首次结果可用性、退回率、人工干预和重复副作用，不再为该结构路径创建纯合成点击样本。
- 飞书应用内员工工作台登录、PostgreSQL 耐久会话、最小管理员聚合、其他会话撤销、公网有界限流与安全响应头均已通过开发真栈。root 侧 allowlist 工具现提供活跃会话解析、十分钟预览、env 指纹栅栏、原子更新、健康回读、失败自动恢复、显式回滚和追加型运维审计；真实服务器已通过无变化确认与唯一管理员保护，但尚未在没有明确授权对象的情况下执行真实提权。包含 Console 会话表的二十一份 migration 异库恢复也已完成，并验证暴露前清空会话不会删除撤销审计或流程数据。下一步回到真实内部工作：由 Owner 独立使用待处理中心处理一项自然产生的工作，并在确有第二名管理员需求时完成一次真实添加、普通成员与管理员回读及撤销闭环。正式域名因近期不备案保持后置；公网 IP、静态开发 token、进程内限流和单机部署都不作为正式员工交付方案。
- 具体退回意见与目标 Attempt 返工上下文已完成真实飞书和 PostgreSQL 验收。下一批内部试用不再重复验证该状态机路径，转而观察 Agent 是否稳定理解意见、返工结果是否可直接接受，以及人类需要多少次补充说明。
- 将 `source_decision_20260808_0405` 中 Owner 已接受的唯一优先级作为下一个真实工作周期的输入，用新流程记录实际完成标准、人工澄清次数和是否触发后置项重新评估。不再为已通过的结构契约创建合成点击样本，也不把本次 Tool 通过描述为业务正确。
- 首批三项真实工作基线已经建立，不再把样本数量本身当作门槛，也不为已通过的状态机路径创建纯合成点击任务。后续真实工作应围绕尚未回答的产品问题选择，并继续保留失败与返工作为有效信号。测试成员有空时再加入跨人员样本，不把其响应设为阻断条件。
- 针对五次卡片验收暴露的队头阻塞，已部署两个独立 Interactive 副本，并把每条车道的单次 claim 固定为 1。三次真实飞书突发点击已确认共享 bot profile、租约、幂等、双副本分流和卡片终态均正常。后续只在真实业务或功能验收自然产生点击时继续采集耐久指标，不再把反复人工点击计时设为独立门槛；现有小样本仍不得外推生产容量。
- Edge 的 macOS 开发试用已经完成最小 artifact、离线安装、升级、回滚、目录级只读、会话级模型外发确认、`edge-data-v0.1` 默认拒绝策略和安全卸载。内容提交 `e6106e5f218f9c520928bef0293899ace7a2395f` 又在一台初始无 Edge 状态的员工 Mac 上完成无效 manifest 故障保护、真实合成执行、中央 3/3 收口、设备撤销和精确清理。下一步先由员工在可见登录会话中完成一次首次 Keychain 配对与卸载后凭据处置体验，再建立可信摘要发布和上游 beta 版本兼容门禁；非公开数据继续默认拒绝，直到供应商条款、管理员批准和事件响应责任都明确。Developer ID 签名与公证本阶段后置，但正式分发前仍必须关闭。可持续远程连接仍需完成 ICP 接入备案或迁移合规地域。

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
