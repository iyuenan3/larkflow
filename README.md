# larkflow · 飞流

> 飞书原生的企业协作 DAG 系统。它把多人流程拆成有依赖、有唯一责任人、可验收和可追溯的节点。

## 当前状态

larkflow 已开始按收敛后的产品设计重建中央工作流，目前完成模板生命周期、草稿预览、领域内核、PostgreSQL 事务持久化、Runtime Worker、首个 LLM Agent executor、首个确定性 Tool executor、Task Projection Worker、飞书 Task 完成状态的耐久入站链路，以及带预览确认的节点重启、完整实例重启和运行中未来区域编辑。开发环境中的真实 Human-Agent-Tool-Human 四节点闭环、Human-Agent-Human 节点重启闭环、完整实例重启闭环和运行中未来区域编辑闭环已经完成。未来区域编辑的真实飞书验收覆盖未开始节点改名、幂等重复确认、冻结线拒绝、成环图拒绝和状态漂移后的陈旧预览拒绝；正向实例最终完成于 `graph_revision 2`，三类负向命令均未污染图或审计。开发应用发布所需通讯录数据范围后，跨人员非 Owner 真组织验收也已通过：中央应用可解析测试成员，当前登录用户对该成员持有实例发送的真实编辑命令被拒绝，图修订、预览和审计均未被污染。跨人员正向分工现已同时通过群聊 mention 和单聊 Card 2.0 人员选择两条真实链路，后者会冻结候选人与角色绑定、幂等创建一个草稿，并把原卡片更新为不可重复提交的已确认状态。所有可操作卡片现在遵循统一视觉反馈契约：回调先耐久落库，再尽快把原卡片替换为无按钮的“处理中”，最终收口为无按钮的成功或拒绝状态。六条 Target Worker 连接现通过 PostgreSQL `LISTEN/NOTIFY` 在耐久队列事务提交后立即唤醒，通知不携带业务状态，连接或等待失败时仍由原有有界轮询保证可靠性。修正逐项完成时间后，五次真实人员选择卡验收的首个服务端反馈 P50 / P95 为 0.991 / 1.274 秒，最终回复 P50 / P95 为 12.670 / 19.298 秒；前四次是 7.548 秒内的突发点击，第五次在约 19 分钟后单独点击并于 4.044 秒完成全链路。突发样本暴露了外部调用串行处理造成的队头阻塞。内容提交 `5312f6c` 已把五条凭据侧交互车道移出 Projection，以两个独立进程并行消费，每个进程在每条车道一次只领取一项。该拓扑已部署并完成新一轮三次真实飞书突发验收：三条动作均为唯一 canonical 记录并进入 `processed / draft_created / sent`，两个副本都实际承担校验和回复工作，最终回复 P50 / P95 降至 4.793 / 5.498 秒。三张原卡片均从飞书服务端读回为已冻结的确认终态。该小样本只证明开发环境突发链路改善，隔离样本与更高强度限流回归仍待完成。此前公布的首反馈数据仍有效，但提交 `a506e7d` 之前的身份校验、领域处理和最终回复精确耗时使用了批次开始时间，现已明确废止。既有失败恢复卡首反馈为 0.990 秒。Personal Agent Edge Proof v0 的内容提交 `fd6933a` 已部署到开发服务器，并通过临时 SSH 隧道在员工 Mac 上完成前台 `serve` 真机验收：空闲心跳、连续领取、真实 Codex、租约续期、单设备锁、SIGTERM 安全停止和设备撤销均已读回；专用开发子域名、Caddy 和受信任证书已经完成源站验证，但公网设备链路受阿里云中国内地 ICP 接入备案阻断，Caddy 已停止并禁用开机启动。

内容提交 `5113a59` 新增 `/larkflow draft <JSON定义>`，让结构化无模板定义进入与模板相同的草稿确认、Owner 授权和中央运行时。无模板定义使用严格 JSON，限制为 100 个节点，并拒绝调用方提供模型服务配置或 `personal.readonly` Edge capability。真实实例 `im_a9a43d1d4db354b31b798bb1` 已完成 Human-Agent-Tool-Human 4/4，PostgreSQL 回读为 `template_version_id IS NULL / status=done`。

内容提交 `244fb0c` 为裸 `/larkflow draft` 增加 Card 2.0 自然语言引导，`6ff0af2` 修正 Card 2.0 表单提交动作，`282ea51` 为未通过确定性校验的中央 Agent 候选增加一次有界重生成。用户填写目标、可选背景并选择一名协作者后，中央 Agent 只生成最多八个 Human / Agent 节点的候选图；服务端重新绑定原始输入、限制 Owner 角色并校验完整 Snapshot，随后只创建草稿。三个内容提交已推送，最终候选 wheel 已同时安装到 Target Runtime 与 legacy 飞书事件桥接虚拟环境。真实点击在 1056 ms 内把原卡片更新为无按钮“处理中”，中央 Agent 的首个非法依赖候选被拒绝，第二个候选通过校验并创建实例 `im_69af9ebdf241017341e5fee4`。PostgreSQL 回读该实例为 `draft / template_version_id IS NULL / 3 nodes / 0 NodeInstance / 0 Attempt`，唯一 canonical 动作为 `processed / draft_created / sent`；飞书服务端回读原卡片为无操作控件的“流程草稿已生成”。流程仍需独立 `/larkflow confirm` 才能启动，本轮没有确认该草稿。

内容提交 `1a80b4035d0a5ad5c634af7be957f4b7d1ee37d7` 已把可能连续调用两次模型的自然语言草稿生成移出凭据侧 Interactive 主循环，改由不持有飞书 profile 的独立 Draft Generation Worker 认领。凭据侧先把原卡片更新为“正在生成”，首次候选被确定性校验拒绝时再更新为“正在修复”，最终回复等待同 revision 的进度更新结算，避免旧进度覆盖终态。migration `0019_draft_generation_progress` 保存独立生成与进度租约；生成租约覆盖两次完整模型路由预算和安全余量。该拓扑已部署，第九个服务、migration 19、真实 PostgreSQL 双副本竞争和七条监听连接均已回读。内容提交 `2ed644e640f3c3834f82c464e05fe0b4c3a241cc` 又修正飞书延时更新 token 第三次使用导致终态卡片失败的问题：回调首次反馈继续使用 token，生成进度和最终结果改按原消息 ID 更新。完整离线套件为 `886 passed, 18 skipped`，两项隔离破坏测试均被捕获；旧卡片已修复，新实例 `im_74e775110afbd80aa598d3ae` 真实进入 `processed / draft_created / reply sent`，飞书服务端回读同一卡片为无按钮、无输入框的最终图预览。该实例随后由真实用户确认启动，Agent Attempt 1 经 86.0 秒模型调用完成，飞书 Human Task 的完成状态通过周期读回进入耐久 Inbox；实例最终为 `done / template_version_id IS NULL / 2 nodes done`。完成 Docx 和最终通知均已从飞书服务端读回，九个 Python 服务保持 `active / running / NRestarts=0`。这证明自然语言草稿到中央执行的开发环境技术闭环；本次输入不含真实业务数据，Agent 结果明确报告数据不足，因此不证明模型内容质量、业务价值或生产可用性。

内容提交 `b7e589ba4af0398573ec995254dd61e9b1a4508c` 新增来源约束型材料复核模板。输入用稳定 `F` 事实与 `Q` 开放问题登记来源；Agent 输出把来源事实、推断和开放问题显式分型并携带引用；确定性 `source_claims.check` 只校验结构、引用覆盖和来源 URL 一致性，不声称验证事实真伪。最终 Human 节点不使用飞书 Task 完成状态暗示接受，而是投影版本绑定的 Card 2.0，由唯一 Owner 明确选择接受或退回；退回保留旧 Attempt、结果和审计，并可通过既有节点重启从目标节点重新执行。完整离线套件为 `898 passed, 18 skipped`，wheel 已确认包含新模板。该提交现已部署到开发服务器，长期 PostgreSQL 回读仍为十九份 migration，九个 Python 服务均为 `active / running / NRestarts=0`。公开材料实例 `source_grounded_20260805_234517` 已完成首次接受路径；第二个实例 `source_grounded_reject_20260806_001940` 又完成真实退回、三节点重启、Attempt 2 重新执行和最终接受恢复。后者从 `failed / version 9` 恢复为 `done / version 16`，首个来源确认节点未重做，两轮 Agent 与 Tool 结果、退回与接受决定、两张独立决定卡和唯一重启审计均保留。这只证明开发测试组织中的窄材料复核与返工闭环，不证明事实真伪、内容质量规模化、市场价值、生产容量或生产上线。

内容提交 `0dc5359e990635c7b6aa16ec0bcd798eb8df39d0` 修复受控内部试用暴露的返工上下文缺口，内容提交 `f6125331aa541e824675e25f9cd2d756cd4c6b56` 又修正真实 Card 2.0 原生表单提交不携带 `action_value` 时的服务端绑定。决定卡继续允许一键接受，但退回必须填写不超过 1000 字的具体意见；服务端重新校验、裁剪空白，并把意见写入 Human Attempt 结果、质量证据和追加型审计。Instance Owner 确认 `reject_target` 节点重启后，意见只进入该目标节点的新 Attempt 输入快照，节点真正激活时仍会保留并交给 Agent；影响集合之外的上游节点、旧 Attempt 和冻结 Instance Snapshot 均不改写。接受路径忽略客户端额外提交的意见。完整离线套件为 `910 passed, 18 skipped`，实现复用既有 JSONB 字段，不增加 migration。开发服务器已部署 `f6125331aa541e824675e25f9cd2d756cd4c6b56`；真实实例 `im_5717aa5b9480d146239907d5` 已把具体退回意见写入 Human Attempt、质量证据和审计，卡片回调进入 `processed / human_decision_rejected / sent / updated`，首个服务端反馈为 1155 ms。节点重启只影响 Agent、Tool 与最终 Human，来源确认保持 Attempt 1；退回意见只进入 Agent Attempt 2，Agent 补出问题与验收条件后，确定性 Tool 从首轮失败变为 `pass`，新的 Attempt 2 决定卡已从飞书服务端读回。实例当前停在最终人工复核，不把本次开发验收外推为内容质量规模化、市场价值、生产容量或生产上线。

首批三项真实项目小样本现已覆盖直接退回、带具体意见返工和直接接受。第三项 `pilot_console_value_20260806_164925` 使用固定版本路线图作为来源，Human-Agent-Tool-Human 四节点均在 Attempt 1 完成，Tool 覆盖 5/5 条来源事实与 3/3 个开放问题，Owner 接受首次结果；从确认到接受用时 12 分 41 秒，完成文档、通知和其他外部投影均保持唯一。该小样本说明流程能记录结果可用性、返工与人工干预，不证明稳定模型质量、市场价值或生产容量。本轮状态仍由开发操作者通过 PostgreSQL 与聊天追踪，没有证明用户独立使用中央控制台能降低追踪成本，下一门槛见 [`AIREADME/ROADMAP.md`](AIREADME/ROADMAP.md)。

Personal Agent Edge 的 macOS 客户端现已接入登录 Keychain：设备密钥只写入系统钥匙串，`0600` 元数据文件只保存服务器地址和设备 ID。除隔离合成项外，这台员工 Mac 已通过临时 SSH 隧道，以真实流程 Owner 身份完成默认 Keychain 槽位的一次性配对；随后 `run-once` 返回 `no_work`，服务器回读设备为 active、配对审计存在且认证后的 `last_seen_at` 已推进。隧道已关闭，设备凭据和非敏感元数据继续保留，重新建立受控隧道后可再次使用。该证据关闭真实设备 Keychain 配对缺口，但不等于员工安装分发、安全评审、可持续公网链路或生产上线。

- **目标产品**：单企业、单层 DAG 的最小闭环，支持模板可选、草稿确认、Human / Agent / Tool 节点、受控编辑、重启、审计和飞书投影。
- **新内核**：`larkflow/workflow/` 已实现模板生命周期和不可变版本、角色绑定和冻结 Instance Snapshot、草稿预览与确认、DAG 校验、节点状态迁移、依赖解锁、Human / Agent / Tool Node Runner、Attempt、claim、过期认领恢复、节点与完整实例重启预览及原子确认、未来区域编辑预览及原子确认、Runtime / Projection / Interactive / Inbound / Draft Generation Worker、PostgreSQL 通知唤醒与轮询兜底、乐观并发、PostgreSQL 仓储、追加型审计、事务 outbox 与耐久 Inbox。慢模型生成与凭据侧卡片更新使用不同领取车道和 revision 栅栏；普通人员分工 Worker 不再认领自然语言草稿动作。凭据侧 Task 验证默认最多尝试 24 次，超限进入不可再认领的 `exhausted` 终态并保留终止时间、失败阶段、结果和最后错误。
- **Edge Proof v0**：已实现一次性配对、设备哈希凭据、撤销、Owner 与 `personal.readonly` 双重过滤、租约续期、迟到结果拒绝、loopback Gateway、手工 `run-once`、前台 `serve` 和 Codex 只读适配器。`serve` 在一个用户主动启动的会话中固定单工作区，使用长轮询、有界退避、应用心跳、单设备锁和信号安全停止；续租失败会取消整个 Codex 进程组，不回传失去租约的结果。内容提交 `fd6933a` 构建的 wheel 已部署到 `alicloud-sh`，并以同一候选件的临时安装态在员工 Mac 上完成前台真机验收。实例在 37 次空闲心跳后被领取，真实 Codex 执行期间产生 18 次续租并完成；同凭据第二个 Worker 被拒绝，SIGTERM 安全退出，设备撤销后再次领取返回 403。临时凭据和隧道均已删除。专用 DNS 记录、Caddy、Let’s Encrypt 证书、源站反向代理和未认证 401 已验证；公网 TLS 随后被 ICP 接入备案阻断，因此公网配对、领取、续租和回传仍未完成。
- **失败恢复 as-built**：自动 Agent / Tool 节点失败会向节点 Owner 投影 Card 2.0，可选择“重新执行”或“人工接管”。卡片回调先进入耐久 IM 命令队列，并立即尝试撤下按钮、显示“处理中”；凭据侧随后重新校验当前企业成员，领域侧精确校验 Owner、Instance version、Node version 与 Attempt 编号，最终卡片显示成功或拒绝。重试创建新自动 Attempt；人工接管创建 `waiting_human` Attempt 和飞书 Task；原失败 Attempt、结果与审计均保留。该能力已在开发服务器与测试组织完成真实闭环：两个不同失败卡片分别创建 Attempt 2 和 3，人工接管创建 Attempt 4 与真实飞书 Task，完成 Task 后 Instance 与 Attempt 4 进入 `done`，前三次失败历史、审计和投影全部保留。新一轮恢复卡真实点击的首个服务端反馈耗时为 0.990 秒，飞书服务端读回终态标题为“恢复操作已处理”且不再包含按钮。
- **legacy 原型**：LangGraph + SQLite + lark-cli 路径继续保留，用于回归已验证的飞书投影、打回、幂等和恢复机制。
- **飞书入口 as-built**：已实现 `/larkflow help`、`/larkflow start`、`/larkflow draft`、`/larkflow confirm`、`/larkflow status`、`/larkflow list`、`/larkflow restart`、`/larkflow restart-all`、`/larkflow restart-confirm`、`/larkflow edit`、`/larkflow edit-confirm` 十一个窄命令，以及命令回执、Agent / Tool 结果消息、完成文档和最终通知。`start` 从启用模板创建草稿；`draft <JSON定义>` 是最多 100 个节点的结构化高级入口；裸 `draft` 打开 Card 2.0 自然语言引导，收集目标、可选背景和一名协作者，再由中央 Agent 生成最多八个 Human / Agent 节点的受限候选图。自然语言回调复用现有耐久动作链，重新校验操作人和冻结候选人；服务端覆盖模型返回的输入，限制 Owner 角色，拒绝 Tool、服务配置与 Personal Edge capability，并在最终卡片上展示无按钮图预览。首次候选校验失败时只允许同一中央 Agent 有界重生成一次，第二次失败仍拒绝，绝不绕过确定性校验。`start` 与两种 `draft` 都只创建草稿，`confirm` 才启动实例。人员选择卡、自然语言引导卡与失败恢复卡在动作耐久落库后立即尝试显示无按钮的“处理中”，最终再替换为成功或拒绝状态；服务端用单调时钟记录有效回调被接受到直接更新返回的耗时，不把它误写为客户端渲染耗时。`status` 只向 Instance Owner 返回单实例有界状态摘要，`list` 只返回本人拥有的最近十个实例摘要，restart 和 edit 命令只创建短期影响预览，对应 confirm 命令才执行原子变更。模板、结构化无模板、自然语言引导和跨人员正向分工均已在开发测试组织完成真实闭环。
- **尚未实现**：上述十一类命令之外的通用飞书控制面、更多业务 Tool adapter、图形化编辑体验和生产装配。企业目录草稿 Owner 全量校验已落码并部署但默认关闭；IM 命令发送者、mention 角色成员和 Card 2.0 候选人的活跃成员校验已完成开发真栈验证。Edge 已具备版本化开发安装、回滚、Keychain、哈希锁定离线 bundle 与依赖清单，但正式员工分发仍为 No-Go：当前没有 Apple Developer ID 身份与公证凭据，员工端仍携带完整中央依赖栈，目录级读取隔离和可复现构建证明也未完成。当前设计不提供操作系统级守护或隐藏后台常驻。可持续使用的公网 HTTPS 入口还必须先完成 ICP 接入备案，或迁移到合规的非中国内地环境。真实 Agent、确定性内容检查、模板入口和飞书 IM / Card / Doc 投影只在开发环境和测试组织验证，不能据此描述为生产上线。
- **证据边界**：本轮完成的是既有设计简化与一致性核验，不是访谈、市场或商业验证。
- **重要边界**：`alicloud-sh` 已运行 Target Runtime、Projection、两个凭据侧 Interactive、凭据侧入站校验、领域侧入站、Draft Generation Worker、loopback Edge Gateway 和 Owner Console 九个 Target 服务，并保留一个 legacy 事件消费者，共十个 Python 服务。七条 PostgreSQL `LISTEN` 连接分属 `lf-dev` 四条和 `lf_target_dev` 三条。Caddy 仍因备案阻断处于 disabled / inactive，服务器只有 SSH 对公网监听。Projection 只负责飞书投影与 Task 状态读取；两个 Interactive 副本持有受限 bot profile；Draft Generation Worker 不持有飞书 profile。凭据侧重新读取外部资源后只写已验证 Inbox，领域侧不能读取 lark-cli profile。legacy 服务继续使用 SQLite，并仅作为事件桥接时写入 Target Inbox，不能把 checkpointer 或全局 LangGraph state 扩展为新产品领域模型。

产品与架构真相源从 [AIREADME/INDEX.md](AIREADME/INDEX.md) 开始。判断“目标是什么”和“现在做到了什么”时，必须区分 Target 与 As-built。

## 简化后的产品闭环

1. 用户从启用模板、自然语言引导或结构化无模板定义创建实例草稿。
2. 系统展示节点、依赖、唯一人类 Owner、执行器和验收条件。
3. 用户明确确认启动或丢弃，草稿不会自动执行。
4. 中央 Scheduler 按依赖调度 Human、Agent 和 Tool 节点，并把责任入口投影到飞书。
5. 项目 Owner 可以预览并确认只影响未来节点的编辑。
6. 节点重启会重置该节点及全部可达下游，历史通过 Attempt 保留。
7. 完整实例重启会为全图创建新 Attempt，从所有根节点重新调度，历史 Attempt 和交付物保留。
8. 自动节点失败后，责任人可以重试或人工接管，两条路径都创建新 Attempt 并保留原失败记录。
9. PostgreSQL 保存业务状态、revision、投影记录和审计，飞书对象可以对账和重建。

既有设计的取舍记录见 [research/design-simplification.md](research/design-simplification.md)。

## 产品不变量

1. 每个节点必须有唯一人类 Owner，Agent 和 Tool 只是执行器。
2. 实例先是草稿，经人确认后才能运行。
3. 模板可选，模板版本不可变，实例保存完整快照。
4. 运行中编辑只影响未开始区域，并经过预览、确认和 revision 校验。
5. DAG 保持无环，重做与重启创建新的 Attempt。
6. 中央数据库是业务真相源，飞书是交互入口和可恢复投影。
7. 权限、责任、状态和图修改合法性由服务端计算。
8. LangGraph 只可用于单个复杂 Agent 节点内部。

## MVP 明确不包含

- 独立 Project、IM、搜索、知识库和应用市场全套平台。
- 模板子 DAG、临时子 DAG和三级下钻。
- 个人 Agent Edge 的产品化、后台常驻、写能力和通用 Capability Lease。仓库中的只读 Proof 不属于默认 MVP 交付。
- Knowledge、Skill、MCP 注册表和 RAG 模板匹配。
- 字段级锁、复杂 ACL、五维评分、Kafka、微服务和完整图形化编辑器。

这些能力只有在真实使用证据证明必要时才重新评估。

## 目标架构

```mermaid
flowchart LR
    F["飞书<br/>IM / Task / Doc / Drive / Directory"] <-->|"事件、命令、投影、对账"| C["larkflow 模块化单体"]
    E["员工电脑<br/>Personal Agent Edge"] -->|"私有 HTTPS<br/>短时租约"| C
    C --> P[("PostgreSQL<br/>Template / Instance / Attempt / Audit")]
    C --> S["DAG Scheduler"]
    C --> R["Human / Agent / Tool Node Runner"]
    R -.-> L["可选 LangGraph<br/>单个 Agent 节点内部"]
```

详细模型和迁移差距见 [AIREADME/ARCHITECTURE.md](AIREADME/ARCHITECTURE.md)。目标契约见 [AIREADME/DAG_TEMPLATE_SPEC.md](AIREADME/DAG_TEMPLATE_SPEC.md)。

## 仓库结构

```text
AIREADME/                         产品、架构、契约、路线和决策真相源
research/design-simplification.md 既有设计简化与取舍记录
research/phase-0/                Deferred 的访谈、对照实验协议与迁移清单
larkflow/
  workflow/                      Target 模板服务、领域内核、CLI、Runtime / Projection / Interactive / Inbound / Edge、PostgreSQL migration、事务仓储、outbox 与 Inbox
  engine/                        legacy LangGraph 编排、门禁、返工和活图机制
  model/                         legacy YAML 节点和模板校验
  io/                            lark-cli、飞书投影、事件和关联表适配
  llm/                           Stub 与 OpenAI 兼容多角色路由
  templates/                     legacy YAML 模板、Target 协作流程、来源约束型材料复核与 Personal Edge 示例
  service.py                     legacy interrupt/resume、投影、权限与对账驱动层
  serve.py                       legacy 常驻服务和启动对账
  store.py                       legacy SQLite、WAL 和跨进程锁
tests/                           离线 pytest 套件
deploy/                          legacy 单机服务、Target Runtime / Projection / Interactive / Inbound、PostgreSQL 备份资产
```

迁移资产的逐模块处理方式见 [research/phase-0/migration-inventory.md](research/phase-0/migration-inventory.md)。

## 当前阶段

Phase 0 的设计一致性核验已经完成，当前进入 Phase 1 中央工作流基础实现。现有代码建立可离线验证的领域边界，并在开发环境打通 PostgreSQL 与测试飞书组织中的 Task 投影：

- Instance Snapshot 无论来自模板还是无模板定义，都进入同一套运行时。
- Template Service 已实现 `draft / enabled / disabled / deleted`、不可变版本、布尔锁、追加型模板审计和 aggregate version 乐观并发。启用模板固定使用最新版本，已启用模板必须先停用才能追加版本。
- 启用模板可按参数和逻辑 Owner 角色绑定生成冻结草稿；`preview` 只读校验完整图，确认仍是独立的人类动作。
- 草稿只能由项目 Owner 确认或丢弃，确认后才创建节点与初始 Attempt。
- Human 节点只接受唯一 Owner 提交，Agent 和 Tool 结果必须匹配当前 claim、Worker 身份、Attempt 和节点版本。
- Scheduler 只在全部依赖完成后解锁节点，任何迟到或陈旧结果都不得改写当前状态。
- Runtime Worker 每次只认领一个已被 adapter 明确接受的自动节点，先提交 claim 再调用 executor；进程中断后，其他 Worker 可在租约到期时用新 token 接管同一 Attempt。
- `LLMAgentExecutor` 只接受 `work.agent.kind=llm.generate`，使用已提交的实例输入和直接依赖结果生成正文；可选 `result_format=source_claims.v1` 要求输出来源事实、推断和开放问题的结构化声明。启动装配会强制最长 LLM 路由预算加安全余量小于节点 claim 租期。
- `ToolExecutorRouter` 按 `work.tool.kind` 选择内部 adapter；`content.check` 对直接依赖正文执行长度和必需词检查，`source_claims.check` 对结构、类型、引用覆盖和来源 URL 一致性执行确定性检查。两者都不代替 Human 的业务语义判断；未知 kind 在 claim 前被过滤，不会被错误 Worker 认领。
- 可选企业目录边界会在草稿入库前校验 Instance Owner 与全部节点 Owner。无法证明 open_id 属于当前租户活跃成员时 fail closed；开发环境默认关闭，启用需额外的通讯录只读 scope。
- PostgreSQL 14 schema、migration runner、事务仓储、追加型 Audit 和带租约的 outbox 已落码；领域状态、审计和 outbox 在同一事务提交。
- migration SQL 已进入 wheel，仓库与长期开发库均为十九份。`0013_im_command_mentions` 到 `0019_draft_generation_progress` 依次覆盖 mention、人员选择卡、恢复卡、canonical 动作、首反馈指标、通知唤醒和独立草稿生成进度。migration 19、真实 PostgreSQL 双副本竞争和通知连接均已验收；既有节点重启、完整实例重启和未来区域编辑预览继续保持恰好一路执行、一路幂等回放。
- 当前完整离线套件为 `922 passed, 18 skipped`；跳过项是需要显式外部环境的集成验证，不会在默认测试中访问网络、凭据或真实飞书。
- `larkflow-target` CLI 已提供模板创建、追加版本、启用、停用、逻辑删除、查询，从模板创建草稿和预览，以及实例确认、状态、Human 提交、既有 Worker 命令和 `generate-drafts-once / generate-drafts`；环境配置由项目 dotenv 解析器读取，不使用 shell `source`。
- `alicloud-sh` 的长期 Target 开发库只接受本机 peer authentication，已应用十九份 migration。九个 Target systemd 服务与一个 legacy 事件消费者组成十个 Python 服务，均回读 `active / running / NRestarts=0`。Edge Gateway 与 Owner Console 分别只监听 `127.0.0.1:8765` 和 `127.0.0.1:8780`；七条 PostgreSQL 通知连接分别由凭据侧持有四条、领域侧持有三条。两个 Interactive 副本固定 `claim_limit=1`，Draft Generation Worker 不持有飞书 profile。队列表仍是业务权威，通知失败时继续使用有界轮询。当前中央开发发布件对应内容提交 `623b9b6228caa52b4680eb30ad2fee723e8921b6`，包含既有流程能力与 Owner 只读 Console 的真实 DAG 依赖渲染；既有接受路径和带具体意见的退回到 Agent 重做路径均已通过。既有小样本延迟只描述开发环境，不能外推生产容量。
- Projection Worker 只认领明确的投影事件，在数据库 claim 提交后调用 lark-cli，以稳定幂等键创建任务，并把 Task GUID、URL、同步版本和完成状态写回 Projection 记录。启动全量对账以 PostgreSQL 为权威分页扫描当前 Human 责任入口，补建缺失记录，并在飞书明确返回 Task 不存在时使用新一代稳定幂等键重建；权限或网络错误不会被误判为删除。该版本已部署到常驻开发服务。专用开发实例已完成真实删除重建及后续完成验收：旧 Task 读回 `1470404` 后只重建 1 条，Projection 换绑到新 GUID、`repair_generation=1`，第二次对账 3 条绑定全部不变；人工完成新 Task 后，凭据侧验证 1 条、领域侧提交 1 条且均无失败，Instance、Node、Attempt 与 Projection 一致进入完成态。
- Human Task 会展示节点明确声明的 Instance 输入；下游任务还会展示直接依赖中已提交的 Agent 正文。超长内容只在任务描述中截断，完整输入与结果仍保存在 PostgreSQL。
- 每日 custom-format 备份保留约 7 天，并完成过一次新库恢复演练。
- 自动执行采用 at-least-once 语义，executor 必须使用请求中的稳定幂等键消除重复副作用。
- 飞书 IM 命令链路已接入：真实消息创建草稿、确认启动、Human Task、Agent、`content.check` 与 `source_claims.check` Tool、人类明确决定、完成文档和最终通知均在开发云服务器与测试组织闭环。Owner 查询、两类重启、未来区域编辑、跨人员授权拒绝和失败恢复均有真实飞书与 PostgreSQL 回读。自然语言草稿入口现额外覆盖独立生成 Worker、阶段进度、按消息 ID 的最终卡片更新，以及从受限候选图确认启动到 Agent、Human Task、完成 Docx 和最终通知的真实闭环。公开材料接受路径与退回后重启恢复路径均已通过；两轮 Agent、Tool、决定卡、文档、通知与审计可同时追溯。Task 完成事件在本轮仍未被 bot 长连接收到，周期状态轮询通过耐久 Inbox 承担可靠入口。九个 Python 服务保持 active 且 `NRestarts=0`。下一阶段不再继续堆叠合成技术验收，而是使用真实内部工作记录完成率、返工、人工干预和结果可用性；更多业务 Tool、图形化控制面和生产迁移仍缺，因此不能描述为目标产品已经上线。
- 无模板飞书入口已在开发真栈完成真实验收：`/larkflow draft` 创建草稿，`confirm` 后依次完成 Human、Agent、`content.check` Tool 和最终 Human 节点。实例 `im_a9a43d1d4db354b31b798bb1` 的四个节点均为 `done`，中央 PostgreSQL 回读 `template_version_id IS NULL`，证明该实例没有借用模板版本。
- Personal Edge 不通过飞书 `lark-cli` 与中央节点交互。中央 Gateway 复用 PostgreSQL Node claim，员工电脑仅保存可撤销设备凭据，并在 `run-once` 或用户主动启动的前台 `serve` 会话中显式固定一个 Codex 只读工作区。前台真机实例已通过 SSH 隧道在员工 Mac 上完成：候选 Edge 先持续报告无任务心跳，再领取一个合成 `personal.readonly` 节点；真实 Codex 完成结果并写入 18 次续租审计，同凭据第二进程被本机锁拒绝，空闲进程收到 SIGTERM 后安全退出。测试设备随后撤销，旧凭据再次领取返回 403；本机凭据、隧道与临时上传件均已删除。专用开发子域名的权威 DNS、源站证书和反向代理也已验证，但员工电脑的公网 TLS 握手随后被阿里云备案系统重置，尚未产生公网配对设备。Human 节点和 gate 不会被 Edge 领取。
- macOS 上 `--credential-store auto` 默认使用登录 Keychain。钥匙串保存设备密钥，`~/.config/larkflow/edge-device.json` 只保存非敏感连接元数据并继续充当单设备锁定位。其他系统维持 `0600` 文件兼容路径，也可显式使用 `--credential-store file`。旧明文文件可在 Keychain 回读一致后迁移；加 `--delete-source` 会把原文件原子替换成非敏感元数据，而不是删除连接配置。

原有访谈和飞书基线协议保留在 [research/phase-0/README.md](research/phase-0/README.md)，当前状态为 Deferred，不阻塞本轮简化设计，也不能被描述为已完成。

## 运行 legacy 原型

下面的命令用于回归当前机制原型，不代表目标产品已经实现。测试使用 Mock Lark I/O、Stub LLM 和临时或内存 SQLite，不访问真实飞书。

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

pytest -q
pytest -q tests/test_workflow_kernel.py
pytest -q tests/test_workflow_persistence.py
pytest -q tests/test_workflow_runtime.py
python -m larkflow.demo --auto
python -m larkflow.demo --template hiring
```

`tests/test_workflow_postgres.py` 是显式启用的集成测试，只能指向可销毁数据库，并通过 `LARKFLOW_TEST_POSTGRES_DSN` 提供连接。

Target 运维入口是独立命令 `larkflow-target`。下面只展示不会调用飞书的控制面命令，真实 DSN 和 tenant 应通过权限收紧的 env 文件提供：

```bash
larkflow-target --env-file /etc/larkflow-target.env migrate
larkflow-target --env-file /etc/larkflow-target.env template-create template.yaml --actor <owner>
larkflow-target --env-file /etc/larkflow-target.env template-enable <template> --actor <owner>
larkflow-target --env-file /etc/larkflow-target.env create-from-template <template> --instance-id <instance> --owner <owner> --bindings bindings.yaml --inputs inputs.yaml
larkflow-target --env-file /etc/larkflow-target.env preview <instance> --actor <owner>
larkflow-target --env-file /etc/larkflow-target.env create draft.yaml
larkflow-target --env-file /etc/larkflow-target.env confirm <instance> --actor <owner>
larkflow-target --env-file /etc/larkflow-target.env show <instance>
larkflow-target --env-file /etc/larkflow-target.env serve
```

中央控制台 v0 是独立的 Owner 只读入口，用于查看本人发起的最近流程、DAG 状态、历史 Attempt 结果和审计时间线。内容提交 `623b9b6228caa52b4680eb30ad2fee723e8921b6` 修正了首版把所有节点画成线性链的问题：浏览器现在依据服务端 `deps` 计算拓扑层级、绘制真实依赖箭头，并在节点上标注直接依赖。它不提供确认、重启或改图操作，也不复用 Personal Agent Edge API。服务强制绑定 loopback，当前开发鉴权把一个至少 32 字符的 Bearer token 映射到服务端配置的 tenant 与 person；令牌只在浏览器当前标签页保存。代码、wheel、服务与 API 技术回读已通过；真实 Chrome 页面刷新后又确认 4 条依赖边、分叉汇合、关联高亮、依赖标签和横向滚动均可见。生产部署前仍需替换为飞书登录态或企业 SSO，并重新设计反向代理边界。

```bash
# env 文件同时提供 LARKFLOW_TARGET_DSN、LARKFLOW_TARGET_TENANT、
# LARKFLOW_CONSOLE_PERSON_ID 与 LARKFLOW_CONSOLE_ACCESS_TOKEN
larkflow-console --env-file /etc/larkflow-target-console.env \
  --host 127.0.0.1 --port 8780

# 仅在本机浏览器打开
# http://127.0.0.1:8780/console/
```

`reconcile-projections` 会只读查询并可能重建飞书 Task，因此只能使用持有开发 profile 的 Projection env 显式执行：`larkflow-target --env-file /etc/larkflow-target-projection.env reconcile-projections`。`reconcile-completions` 使用同一身份立即扫描当前 Human Task，只把已完成状态写入耐久 Inbox，不直接提交节点。`reconcile-instance-completion <instance_id>` 只修复一个已完成实例缺失的完成文档或最终通知，依赖稳定幂等键，重复执行不会复制外部资源。

Edge Proof v0 有两个独立入口。开发服务器已把 Gateway 作为仅监听 loopback 的 systemd 服务部署，并完成独立 Caddy HTTPS 反向代理的源站验证。本机 Edge 默认只接受 HTTPS，只有 loopback 可以使用明文 HTTP。当前 ECS 位于阿里云中国内地，专用域名尚未完成 ICP 接入备案，因此公网 TLS 会被接入侧阻断；Caddy 已停止并禁用开机启动。完成备案或迁移到合规的非中国内地环境前，下面的 HTTPS 入口只是配置形状，不能作为已通过的公网验收：

macOS 开发试用版使用 `deploy/larkflow-edge-manager.py` 管理独立版本目录，不修改 Homebrew 或系统 Python。推荐由发布方先构建哈希锁定的离线 bundle。构建时需要联网，员工安装时不联网；manifest 固定目标 Mac 与 Python、完整 source commit、主 wheel、manager、全部 wheel 的包名、版本、大小和 SHA-256。员工必须从独立可信渠道取得 manifest SHA-256：

```bash
# 发布方，在 clean commit 上构建 wheel 后生成 macOS 离线 bundle
python deploy/build-larkflow-edge-bundle.py \
  --wheel <larkflow wheel> \
  --output <绝对路径>/larkflow-edge-bundle \
  --source-commit <完整四十位 Git SHA> \
  --python <员工 Mac 对应的 Python 3.10+>

# 员工首次安装或升级，manager 会验证 bundle 后再修改安装目录
python3 larkflow-edge-bundle/larkflow-edge-manager install \
  --bundle larkflow-edge-bundle \
  --manifest-sha256 <独立可信渠道提供的六十四位 SHA-256>

# 非敏感安装状态与本机离线诊断
~/.local/bin/larkflow-edge-manager status
~/.local/bin/larkflow-edge doctor

# 需要时切回 previous
~/.local/bin/larkflow-edge-manager rollback
```

默认版本目录是 `~/Library/Application Support/larkflow-edge/releases/`。直接 wheel 安装以 wheel 摘要标识 release，离线安装以覆盖全部依赖和 manager 的 manifest 摘要标识 release，因此相同主 wheel 的不同依赖集合不会误用旧环境。安装器先离线安装 bundle 内哈希锁定且已修复 `CVE-2026-8643` 的 pip，再强制 `--no-index --only-binary=:all:` 安装应用与依赖；只有 `pip check` 和 CLI 启动校验通过才原子切换，旧版本保存在 `previous`。安装器拒绝不匹配目标、文件、wheel 元数据、符号链接目录、额外文件和已有的无关同名命令，也不读取或迁移 Keychain，不注册后台服务。

当前 bundle 仍未代码签名或公证，而且完整应用包会携带中央节点依赖。它只适合明确批准的开发试用，不能作为正式员工分发。详细结论和放行门禁见 [`research/edge-distribution-security-review.md`](research/edge-distribution-security-review.md)。直接 `--wheel <path> --sha256 <hex>` 的旧开发入口仍保留，但可能联网解析依赖，不属于推荐的离线交付路径。

```bash
# 中央节点，数据库已执行 larkflow-target migrate
larkflow-edge-gateway --env-file /etc/larkflow-target-edge.env pairing-create \
  --tenant <tenant> --person <person> --actor <admin>
larkflow-edge-gateway --env-file /etc/larkflow-target-edge.env serve \
  --host 127.0.0.1 --port 8765

# 开发验收隧道，不是公网入口
ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 alicloud-sh
larkflow-edge pair --server http://127.0.0.1:18765 --name "My Mac"

# 配置 HTTPS 后的员工电脑入口，pair 默认无回显读取一次性 code
larkflow-edge pair --server https://edge.example.com --name "My Mac"
larkflow-edge run-once --workspace /absolute/path/to/approved/workspace \
  --wait-seconds 20
larkflow-edge serve --workspace /absolute/path/to/approved/workspace \
  --wait-seconds 20

# 已有明文凭据的 macOS 客户端，验证 Keychain 回读后移除磁盘密钥
larkflow-edge credential-migrate --delete-source
```

`serve` 是用户主动启动并保持可见的前台会话，不会注册操作系统后台服务，也不会扩大配对时固定的 `personal.readonly` 能力。SIGINT 或 SIGTERM 会停止长轮询并取消在途 Codex；同一设备凭据的第二个 `serve` 或 `run-once` 会被本机锁拒绝。

macOS 默认把设备密钥保存在当前用户登录 Keychain，密钥不会进入命令行参数、环境变量、日志或磁盘元数据；非敏感引用文件仍以 `0600` 保存并拒绝符号链接。显式文件模式和非 macOS 兼容路径仍会把完整凭据写入 `0600` 文件。Codex 使用 `read-only + ephemeral + ignore-user-config`，子进程环境采用最小 allowlist，不继承任意 API key、代理、SSH agent、Edge、Target 或飞书变量。本机必须依赖 Clash 等环境代理时，可显式增加 `--inherit-loopback-proxy`；它只传递无用户名和密码的 loopback HTTP / HTTPS / SOCKS URL，远程或带凭据代理仍被丢弃。只读当前只证明写入受限，没有证据证明目录级读取被限制在所选工作区；恶意任务输入仍可能诱导读取其他可读文件，也不等于无数据外发。该 Proof 只能用于明确批准的测试工作区。

真实飞书部署会创建任务、卡片和文档，只能在明确配置的开发环境中运行。现有单机部署是 legacy 原型实录，操作前先读 [AIREADME/DEPLOYMENT.md](AIREADME/DEPLOYMENT.md)。

## 文档路由

- 产品定位与红线：[CORE](AIREADME/CORE.md)
- 简化依据与目标：[PRODUCT_STRATEGY](AIREADME/PRODUCT_STRATEGY.md)
- MVP 功能契约：[PRD](AIREADME/PRD.md)
- 目标架构与实现差距：[ARCHITECTURE](AIREADME/ARCHITECTURE.md)
- DAG 目标契约：[DAG_TEMPLATE_SPEC](AIREADME/DAG_TEMPLATE_SPEC.md)
- 当前 legacy 接口：[SPEC](AIREADME/SPEC.md)
- 推进顺序：[ROADMAP](AIREADME/ROADMAP.md)
- 决策历史：[DECISIONS](AIREADME/DECISIONS.md)

## 安全

- 不提交凭证、token、真实人员 ID 或生产数据库。
- 飞书对象和执行器回传都必须经过服务端授权、版本与幂等校验。
- 测试不得构造 `build_real_service`，不得访问网络或真实飞书资源。
