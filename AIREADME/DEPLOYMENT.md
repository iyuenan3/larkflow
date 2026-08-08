# DEPLOYMENT · larkflow

> **As-built / Legacy Prototype + Target Runtime 开发部署。** 本文保存 legacy LangGraph + SQLite 服务、Target PostgreSQL Runtime 与 Owner 只读中央控制台在单台 ECS 上的真实部署记录。它不是目标 SaaS 拓扑：目标架构是 PostgreSQL 中央控制面的模块化单体。Personal Agent Edge 已完成 loopback 部署、SSH 隧道验收和 HTTPS 源站配置，但公网设备链路受 ICP 接入备案阻断，见 [ARCHITECTURE.md](ARCHITECTURE.md)。
>
> 除修正事实错误外，不再给这套部署增加新的产品领域能力。个人端不得复用下文的企业 bot 全局凭证；中央端和个人端必须使用不同身份、权限与生命周期。

## 当前发布状态 · 2026-08-08

- 当前 Target 应用 wheel 对应内容提交 `db7651228e26055eb1229ae9f451e3e87c31df38`，管理员 allowlist 运维工具对应 `00b3c8f920e6b856d11d9d4a91678959de3da6a5`，systemd 出站边界对应 `bc961b6`。Caddy 通过公网 IP 的 80 / 443 端口提供 HTTPS，覆盖 `X-Larkflow-Client-IP` 并限制请求体、请求头与连接时间；Console 继续只监听 `127.0.0.1:8780`。本次重启的九个 Target 服务均为 `active / NRestarts=0`，legacy 消费者与 Caddy 继续为 `active / running`。公网与 loopback `/console/` 均返回 200，未认证管理员 API 返回 401。飞书 OAuth 应用凭证已由官方令牌端点验证，受限 env 保持 `0640 root:lf_target_dev`，管理员 allowlist 只保存于该受限 env。
- 飞书应用已发布网页应用与机器人两种能力，网页应用作为工作台默认入口。至少两名真实成员已完成网页授权登录、本人 Owner 数据读取与跨 Owner 隔离验证，机器人命令入口继续可用。完成登录后的会话摘要已保存到 PostgreSQL；真实会话在 Console 重启后仍有效，用户直接刷新保持登录。服务端 allowlist 命中的成员可读取当前企业管理员聚合，并通过五分钟耐久预览和显式确认撤销其他浏览器会话；当前会话只能注销，普通成员访问管理路由返回 404。用户已完成新会话治理面板的真实登录浏览器视觉验收。该证据关闭开发环境单进程会话、最小管理员观察面、单会话撤销和基础公网滥用防护缺口，不等于生产发布；入口仍使用公网 IP，也没有 allowlist 自助管理、批量撤销、正式域名、生产容量或跨区域容灾。
- 当前 Target 开发发布件对应内容提交 `db7651228e26055eb1229ae9f451e3e87c31df38`，在既有公网 Console 边界、生命周期、来源约束复核、待处理中心、PostgreSQL 耐久会话、管理员聚合和受审计的其他会话撤销上增加独立来源约束决策契约。从该提交构建的干净 wheel SHA-256 为 `54a4bbf4c96834d7d69a3434d01b083d2467f5df6dd129c9ac6e35876efb49ff`，位于 `/srv/larkflow/target/releases/20260808_040000_source_decision_db76512/`，权限为 `0640 root:lf_target_dev`。完整离线等价结果为 `988 passed, 21 skipped`，服务器 `pip check` 无断裂依赖。
- 升级前 PostgreSQL custom-format 备份 `/var/backups/larkflow-postgres/larkflow_target_dev-20260808T035842+0800.dump` 为 248626 bytes，SHA-256 为 `04bc4a17598a261375aeaa375d71d75049b2c0a1aac5434aaea282f6f56f8334`，权限为 `0600 lf_target_dev:lf_target_dev`；备份服务回读 `Result=success / ExecMainStatus=0`。本次没有 schema 变更，migration runner 返回空集，ledger 仍为 `21 / 0001_workflow / 0021_console_session_governance`。
- 本次重启 Runtime、Draft Generation Worker、Projection、两个 Interactive、凭据侧入站、领域侧入站、Edge 和 Console 九个 Target 服务。九个服务均回读 `active / running / NRestarts=0`，legacy 消费者与 Caddy 未重启且仍为 `active / running`，部署窗口 warning 为 0。5432、8765 与 8780 只监听 `127.0.0.1`，Caddy 管理口只监听 `127.0.0.1:2019`。Edge 公网链路仍保持关闭，不能把 Console 入口外推为 Personal Agent Edge 已具备公网 E2E。
- root 侧管理员工具安装在 `/usr/local/sbin/larkflow-console-admin-allowlist`，SHA-256 为 `072dc2c6e6f1d144fb2783f11c580af0dfe2892091455f452ccebb19f3f68f2c`。状态目录 `/var/lib/larkflow-console-admin-allowlist` 为 `0700 root:root`，审计为 `0600 root:root`。真实会话无变化确认没有改写 env 或重启 Console；唯一管理员移除被拒绝，公网与 loopback 页面仍为 200。实际添加、自动恢复和显式回滚已在离线故障注入中通过，但尚未在真实服务器改变成员权限。
- 真实认证 API 回读最近 30 个本人流程和 22 条待处理项，分类为失败恢复 2 条、本人 Human 1 条、暂停继续 0 条、草稿确认 19 条；序列化响应不含配置的人员 ID。应用仓储直接执行真实 PostgreSQL 查询，返回 30 条有界候选、两条失败节点和一条本人 Human 等待节点。未认证 API 继续返回 401。与部署静态资源同源的浏览器功能验收使用非敏感测试身份完成，“查看流程”点击后进入“已打开”，1280 像素视口无横向溢出，浏览器错误为 0。真实开发 token 没有进入自动化浏览器，因此该项不记为真实数据浏览器验收。
- 生命周期发布件曾在一次性 PostgreSQL 数据库执行两组真实双连接竞争。相同版本的两个取消确认只有一路执行，另一路幂等回放；aggregate version 只增加 1，取消审计恰好 1 条。暂停与 Human dispatch 竞争只允许一路成功，本轮结果为 dispatch 成功、pause 冲突，最终保持 `running / waiting_human`，没有产生“已经返回暂停但仍新增派单”的非法组合。测试库与三个远端临时上传件已删除并回读为 0，正式 release wheel 保留且哈希一致。真实飞书命令、普通 Human Task 和决定卡取消收口已在后续验收完成，证据继续保留在本节下方。
- `larkflow-target-console.service` 以 `lf_target_dev` 运行，env 为 `0640 root:lf_target_dev`，鉴权模式现为 `feishu`。App secret 与 OAuth token 不进入命令行、浏览器或日志；静态 token 只保留为 loopback 开发回退。未认证业务 API 返回 401；授权后的真实页面加载当前 Owner 实例，非 Owner 与不存在实例继续统一返回 404。早期 Chrome 在 `source_grounded_reject_20260806_001940` 回读“已完成 4/4、实例版本 16”、Agent、Tool 与最终 Human 三个 Attempt 2 节点，以及 00:53:27 由当前 Owner 发起且影响三节点的受控节点重启。`source_grounded_20260805_234517` 同时回读“未发现多轮执行节点”和“最近审计中未发现受控重启”。页面明确 Console 只读，回复与操作仍在 Agent 对话或飞书入口完成。既有标签页不会自动替换已加载的旧 JavaScript，部署后验收必须显式刷新。
- `source_grounded_review:1` 已导入并启用。真实飞书实例 `source_grounded_20260805_234517` 使用公开软件需求材料完成 Human-Agent-Tool-Human 4/4；Agent、Tool 与两个 Human 节点均为 Attempt 1。Tool 回读 4/4 条事实、3/3 个开放问题、零违规和 `quality=pass`。Owner 点击“接受”后，决定命令进入 `processed / human_decision_accepted / sent / updated`，卡片反馈写入耗时为 1098 ms，Instance 进入 `done / version 9 / graph_revision 1`。
- 第二个公开材料实例 `source_grounded_reject_20260806_001940` 首轮 Tool 回读 6/6 条事实、3/3 个开放问题与零违规。Owner 明确退回后，命令进入 `processed / human_decision_rejected / sent / updated`，卡片反馈写入耗时为 1041 ms，Instance 进入 `failed / version 9`。真实 `/larkflow restart` 预览只影响 Agent、Tool 与最终 Human 三个节点；确认后保留来源确认 Attempt 1，为三个受影响节点创建 Attempt 2。第二轮 Agent 与 Tool 均完成，Tool 仍为 6/6、3/3、零违规；新决定卡接受后 Instance 恢复为 `done / version 16 / graph_revision 1`。
- 该返工实例保留 Agent 与 Tool 两轮结果、Human Attempt 1 的 `failed / quality=fail`、Human Attempt 2 的接受结果、一次退回审计、一次接受审计和恰好一次节点重启审计。两张决定卡与四条自动结果消息使用不同外部 ID，完成文档、最终通知与原 Human Task 均有外部绑定；九个 Python 服务在整段验收窗口内无 warning。
- 真实实例 `im_5717aa5b9480d146239907d5` 验证具体意见返工。Human Attempt 1 保存原文意见“确定性检查未通过，请补齐 problem 和 acceptance_criteria，并确保内容符合来源约束。”，质量证据和 `node.human_decision_rejected` 审计保存同一语义；canonical 动作为 `processed / human_decision_rejected / sent / updated`，首个服务端反馈耗时 1155 ms。真实节点重启预览只影响 Agent、Tool 与最终 Human；确认后来源确认保持 Attempt 1，只有 Agent Attempt 2 的输入快照含 `rework_feedback={source_node_key, source_attempt_no, feedback}`，Tool 与 Human 的 Attempt 2 及冻结 Instance Snapshot 都没有该结构化字段。Agent 补出 1 项问题与 3 项验收条件，Tool 从首轮结构失败变为 `pass`，证据为 3/3 条来源事实与 2/2 个开放问题均已按类别引用；新的 Human Attempt 2 决定卡已从飞书服务端读回，实例当前为 `running / version 15` 并等待最终人工复核。
- 直接接受实例 `source_grounded_20260805_234517` 的 Task、Agent 消息、Tool 消息、决定卡、完成文档和最终通知均有外部投影绑定，追加型审计包含四次节点激活、两次自动完成、一次 Human 提交、一次明确接受和一次 Instance 完成。本条只证明开发测试组织中的接受路径和来源契约闭环，不证明外部事实真实、模型内容质量规模化、市场价值、生产容量或生产上线。
- `source_grounded_decision:1` 已导入并启用，模板内容哈希为 `0bf7d5495c9a7d043c0fd0150049712f46ce9116b44cba7776ce6f3889ac2490`；旧 `source_grounded_review:1` 保持启用且语义不变。新实例 `source_decision_20260808_0405` 复用被退回样本的同一份来源登记，四个 Human-Agent-Tool-Human 节点均在 Attempt 1 完成。Agent 产出一个优先级对象，回答 Q1、Q2、Q3，给出 4 条完成标准、4 个带重新评估条件的后置项和 3 项风险。确定性 Tool 回读 6/6 个 F、3/3 个 Q、零违规和 `verdict=pass`。
- Owner 明确接受后，该实例为 `done / version 9 / graph_revision 1`，四个 NodeInstance 和四个 Attempt 均为 `done`；追加型审计只有一条 `node.human_decision_accepted` 和一条 `instance.completed`。Task、两条自动结果消息、决定卡、完成文档和最终通知均有外部绑定。飞书服务端回读终态卡为已接受、无按钮且无 `%22`。这只关闭开发真栈的契约与交互门槛，不证明建议在业务上正确。

## Target PostgreSQL 开发验证状态（2026-08-08）

- 当前 Target 开发发布件对应内容提交 `db7651228e26055eb1229ae9f451e3e87c31df38`，在既有自然语言草稿、来源约束复核、返工上下文、生命周期、Owner Console、待处理中心、PostgreSQL 耐久会话、管理员聚合、受审计会话撤销和公网请求边界基础上增加来源约束决策契约。长期库仍为二十一份 migration，本次没有 schema 变更。干净 wheel 位于 `/srv/larkflow/target/releases/20260808_040000_source_decision_db76512/`，SHA-256 为 `54a4bbf4c96834d7d69a3434d01b083d2467f5df6dd129c9ac6e35876efb49ff`；上一版发布件与本次虚拟环境卸载备份均保留为可恢复回滚点。九个 Target 服务已重启并回读 `active / running / NRestarts=0`，legacy 消费者与 Caddy 未重启且继续 active，部署窗口 warning 为 0。新模板、真实接受实例和既有待处理中心、会话治理、生命周期竞争与飞书命令证据均已回读。
- 内容提交 `5b0c79b4d946441063d92970e8f0e9cac31b2ab3` 只增加员工 Mac 客户端的开发试用安装控制面与离线 `doctor`，不改变中央 Gateway 协议、migration 或开发服务器服务，因此本轮没有替换服务器 wheel。候选 `larkflow-0.0.2-py3-none-any.whl` 的 SHA-256 为 `f513a61c18a6fdd0c60d34c57dcb2f0121d814870ec8f2bcea8218986bd054d2`。员工 Mac 默认前缀为 `~/Library/Application Support/larkflow-edge`，稳定命令位于 `~/.local/bin`；真实执行 `0.0.1 -> 0.0.2 -> rollback -> 0.0.2` 后，当前 release 为 `0.0.2-f513a61c18a6`，previous 为 `0.0.1-d33241ba7328`。manager 源码与托管副本 SHA-256 均为 `2020ee85660f8623ca9f0b68caf7dde8f96555d9dd67c7c8894ffe7100bd548d`。完整离线套件为 `828 passed, 17 skipped`。
- 内容提交 `81bd43983598ff319150344e779223cd03731eba` 的离线 bundle 构建器与 manager 扩展不改变中央 Gateway、migration 或服务器服务。提交前构建的测试 bundle 固定 macOS arm64、CPython 3.12、完整 source commit、主 wheel、manager 和全部 wheel 元数据，共 45 个 wheel；manifest SHA-256 与每个文件哈希均在安装前验证。故意设置无效 pip index 与 HTTP、HTTPS、SOCKS 代理后，安装仍只读取本地 wheelhouse，先把 venv 自带的 pip 26.1 升级为哈希锁定的 26.2.1，再安装应用并通过 `pip check` 和 CLI 启动校验。`pip-audit 2.10.1` 首扫发现 pip 的 `CVE-2026-8643`，修复后复扫无已知漏洞，私有 `larkflow 0.0.2` 因不在 PyPI 被明确跳过。该测试候选来自提交前工作树，只证明机制，不是发布件。
- 安装器不读取或修改 Keychain。真实用户上下文中的 `larkflow-edge doctor` 已回读凭据 store 为 Keychain、Codex 可用、无后台服务和需要 loopback 隧道；临时 SSH 隧道中的 `run-once` 返回 `no_work`，服务器设备仍为 active，`last_seen_at` 推进至北京时间 2026-08-05 14:09:10。隧道随后关闭。受限工具上下文曾把 Keychain 查询误报为 item-not-found，真实用户上下文预检发现条目存在并阻止了不必要的重新配对与设备撤销。
- 双 Interactive 副本继续让五条凭据侧交互车道脱离 Projection，每个副本固定 `claim_limit=1`。一次性真实 PostgreSQL 竞争证明两个副本各领取一条不同记录；三次真实飞书突发点击也已由两个副本实际分流并全部成功。隔离与更高强度限流回归尚未完成。
- `alicloud-sh` 的 PostgreSQL 14.23 保持 active，`listen_addresses=localhost`，5432 只监听 `127.0.0.1`。宿主系统盘约有 33 GB 可用，内存约有 993 MB available。该数据库是自建 Target 开发环境，不是生产数据库，也不具备托管数据库的高可用能力。
- 一次性数据库与最小权限密码角色通过本机 SSH 隧道运行完整 `tests/test_workflow_postgres.py`，3 项全部通过：migration 重入、聚合与 outbox 往返、两个真实连接竞争同一节点、过期 claim 恢复。测试前先跑单 Worker 基线；完成后数据库与角色均已删除，并从系统目录回读为 0。
- 重启候选件另在一次性 PostgreSQL 14 数据库应用十一份 migration，并分别以节点和完整实例 scope 用两个真实连接同时确认同一 RestartPreview。两种 scope 都恰好一路执行、一路幂等回放，aggregate version 只增加 1，Attempt 从 1 增至 2，旧结果保留且对应重启审计只有 1 条；跨 tenant 读取预览被拒绝。测试库、安装目录和上传件随后删除并回读为 0。
- 长期开发库为 `larkflow_target_dev`，所有者是无密码角色 `lf_target_dev`。同名 Unix 系统用户通过本机 Unix socket 的 peer authentication 连接；角色不能超级管理、建库、建角色、复制或绕过 RLS，`PUBLIC` 没有数据库连接权。数据库默认 `timezone=UTC`、`statement_timeout=30s`、`lock_timeout=5s`、`idle_in_transaction_session_timeout=60s`。
- 长期开发库已应用 `0001_workflow` 到 `0021_console_session_governance`。第十九份 migration 增加自然语言草稿的独立生成租约、阶段进度 revision 与最终回复栅栏；第二十份保存 Console 会话凭据 SHA-256 摘要、服务端主体与有效期，并为过期清理和主体查询建立索引；第二十一份增加安全会话 ID、五分钟耐久撤销预览、追加型撤销事件和禁止事件更新删除的触发器。前十八份继续覆盖领域状态、投影、Inbox、模板、Edge、IM 命令、重启、编辑、人员分工、恢复卡、canonical 动作、首反馈指标和事务后通知。通知不携带业务状态，凭据侧默认验证上限仍为 24 次，结构化日志中的非零 `exhausted` 必须告警。
- `larkflow-target-edge.service` 以 `lf_target_dev` enabled / active，`NRestarts=0`，只监听 `127.0.0.1:8765`。运行态 `IPAddressAllow` 只有 loopback，`IPAddressDeny` 覆盖其他 IPv4 与 IPv6；无凭据 claim 返回 401。`/etc/larkflow-target-edge.env` 为 `0640 root:lf_target_dev`，只含本机 peer DSN、claim TTL 与结果大小限制，不含飞书或模型凭据。仓库 unit 与 env 模板为 `deploy/larkflow-target-edge.service` 和 `deploy/larkflow-target-edge.env.example`。
- Caddy 2.11.4 使用官方 Ubuntu stable 包安装。验收时 `NRestarts=0`，公网监听 80/443，管理端口只监听 `127.0.0.1:2019`；专用 DNS-only 子域名反向代理到 `127.0.0.1:8765`，请求体限制为 256 KB，并设置 HSTS、`nosniff` 与 `no-referrer`。确认 ICP 阻断后服务已 `disabled / inactive`，80/443 与 2019 均不再监听；软件、配置、证书和回滚备份保留。仓库脱敏模板为 `deploy/larkflow-edge.Caddyfile.example`，服务器旧配置备份为 `/etc/caddy/Caddyfile.pre-larkflow-20260802`。
- Runtime 使用 `/srv/larkflow/target/venv` 中的 wheel，以 `lf_target_dev` 运行并通过 Unix socket peer authentication 连接。`/etc/larkflow-target.env` 为 `0640 root:lf_target_dev`，systemd unit 为 `0644 root:root`。
- 来源复核发布时的 Target wheel 为 `larkflow 0.0.2`，SHA-256 为 `0dcccb7f674135dde8b44ab08d437ba397b92397b8456ede8a064f66f1eb2af1`，保存在 `releases/20260805_233701_source_review_b7e589b/larkflow-0.0.2-py3-none-any.whl`，对应内容提交 `b7e589ba4af0398573ec995254dd61e9b1a4508c`。决定卡回调需要 legacy 事件桥接进入耐久命令队列，因此该候选曾同时替换 `/srv/larkflow/target/venv` 与 `/srv/larkflow/dev/venv`。当时八个 Target 服务与一个 legacy 服务均回读 `active / running / NRestarts=0`。Gateway 与 PostgreSQL 继续只监听 loopback，Caddy 保持 `disabled / inactive`。
- 自然语言草稿真实验收使用新卡片消息 `om_x100b6805868bd8a8c2ed857d51a0a31`。点击后 1056 ms 内写入首个服务端反馈并把原卡片更新为“处理中”；中央 Agent 的首个候选因后向依赖被拒绝，第二个候选通过相同确定性校验。动作最终为 `processed / draft_created / sent`，同卡总记录数与 canonical 记录数均为 1；实例 `im_69af9ebdf241017341e5fee4` 为 `draft / template_version_id IS NULL / graph_revision 1 / version 0`，保留原始 brief 与空 context，共三个 `human / agent / human` 节点，NodeInstance 与 Attempt 均为 0。应用 bot 从飞书服务端回读原卡片为已更新、未删除、无输入或按钮的“流程草稿已生成”终态。本轮没有发送 `/larkflow confirm`，因此只证明草稿生成链路。
- 无模板开发真栈实例 `im_a9a43d1d4db354b31b798bb1` 由真实飞书用户消息创建草稿并确认启动，随后完成首个 Human Task、真实 Agent、`content.check` Tool 和最终 Human Task。最终飞书 `/larkflow status` 回读为 4/4 已完成；PostgreSQL 回读 `status=done`、`template_version_id IS NULL`、`graph_revision=1`、`completed_at IS NOT NULL`，四个 NodeInstance 全部为 `done`。本条证明开发环境入口与中央运行时闭环，不代表生产上线。
- 前台 Edge 真机验收使用本机 `127.0.0.1:18765` 到中央 `127.0.0.1:8765` 的临时 SSH 隧道和合成身份，不使用飞书或真实业务数据。候选客户端先连续返回 37 次无工作心跳，再领取实例 `edge_serve_acceptance_20260805_0043`；真实 Codex 执行期间追加 18 条 `node.claim_renewed`，结果以 `personal.readonly / codex.readonly` 完成，Instance、Node 与 Attempt 均为 `done`。同凭据第二个 Worker 立即被文件锁拒绝；空闲 Worker 收到 SIGTERM 后记录 `edge_agent_stop_requested` 并正常退出；设备进入 `revoked` 后再次领取返回 403 `device_revoked`。一次性配对文件、设备凭据、隧道和服务器临时命令输出随后删除。该证据不等于正式员工安装、后台常驻、公网可用或生产安全。
- 失败恢复专用实例在真实开发栈中连续产生三个可判定的 `executor_error`。Attempt 1 与 2 的不同恢复卡分别触发 Attempt 2 与 3，两个原卡片均成功收口；Attempt 3 的“人工接管”创建 Attempt 4 和真实飞书 Task。测试人员完成 Task 后，周期状态读回写入 Inbox，凭据侧验证和领域侧提交各成功一次，Instance 最终为 `done / version 11`，Attempt 4 为 `done` 且带结果与提交人。Attempts 1、2、3 的失败状态与错误、三条自动失败审计、两条重试审计、人工接管审计和全部投影均保留；Attempt 4 的完成文档、节点消息、Task 和最终通知均有外部绑定。验收数据为合成内容，不代表生产上线。
- 开发交互延迟配置保留 Runtime、Projection、Draft Generation Worker 与两个 Interactive 副本的 1 秒空闲退避上限。内容提交 `72d2e28` 最初为四个 Worker 增加 PostgreSQL `LISTEN/NOTIFY` 唤醒，`5312f6c` 增加两个 Interactive 监听，`1a80b403` 再增加 Draft Generation 监听，当前共七条。触发器只在可认领状态事务提交后发送空 payload。通知连接建立或等待失败时，循环只等待当前退避区间的剩余时间并继续扫描，所以队列表与轮询仍承担可靠性，通知只改善延迟。开发服务器已从 PostgreSQL 管理员视角回读 `lf-dev` 四条和 `lf_target_dev` 三条监听连接。
- 提交 `a506e7d` 修正了批次 Worker 在循环开始时只取一次 `now` 的缺陷。凭据验证、领域处理和回复投影现在都在每条工作实际完成后读取时钟并持久化；一次性真实 PostgreSQL 已验证同一批次两条记录分别保存 `+1000 ms` 与 `+2000 ms` 的完成时间。修正前记录的首个服务端反馈来自回调桥接器独立单调计时，仍然有效；修正前的凭据验证、领域处理和最终回复精确耗时属于批次开始时间，不能继续解释为逐项完成延迟。本节此前列出的 0.959、1.912、2.200、4.484、5.030、2.844 和 3.213 秒下游数据由本条明确废止。
- 修正版部署后的五次真实人员选择卡均只产生一个 canonical 动作和一个草稿，全部进入 `processed / draft_created / reply sent`；五张原卡片均由应用 bot 从飞书服务端读回为已确认且没有选择态、处理中状态或操作控件。五次首反馈、凭据验证、领域处理和最终回复的 P50 / P95 分别为 0.991 / 1.274 秒、4.757 / 12.358 秒、4.941 / 12.582 秒和 12.670 / 19.298 秒。前四次在 7.548 秒内到达，最终回复范围为 8.368 到 19.569 秒；第五次约 19 分钟后单独点击，全链路最终回复为 4.044 秒。该样本组成说明首反馈稳定在约 1 秒，但凭据校验与回复投影的串行外部调用会在突发时产生队头阻塞。五次合并统计不是同一并发负载，不包含客户端渲染，也不外推到生产容量。
- 双 Interactive 副本部署后的三次真实突发点击均只产生一个 canonical 动作和一个草稿，全部进入 `processed / draft_created / sent`，没有失败阶段或最后错误。首反馈、凭据验证、领域处理和最终回复的 P50 / P95 分别为 1.015 / 1.196 秒、2.373 / 2.425 秒、2.586 / 2.677 秒和 4.793 / 5.498 秒。副本 1 与 2 分别处理 2 / 1 条验证和 1 / 2 条回复，全部车道日志 `error_count=0`；两个服务保持 `active / NRestarts=0`，验收窗口 warning 级日志为 0。应用 bot 批量回读三张原卡片，全部为已更新、未删除、已冻结的“人员分工已确认”终态，不含原提交文案或处理中状态。该样本只覆盖测试组织中三次突发点击，不包含客户端渲染，不代表隔离性能、限流上限或生产容量。
- Edge 升级前备份已回读 `Result=success / ExecMainStatus=0`。当前候选件保存在 `releases/edge-7728894b1338/`，上一轮轮询发布件保存在 `releases/poll-e725f5ba39ee/`，其他受限回滚件保留在各自目录。wheel 为 `0640 root:lf_target_dev`，可按相同停服、安装、启动步骤回滚。
- 两个只含合成信息的单节点实例 `edge_remote_acceptance_20260802_180202` 与 `edge_remote_renewal_20260802_180554` 已通过临时 SSH 隧道由本机 Codex 只读执行。两者 Instance、Node 与 Attempt 均为 `done`；第二条 22.6 秒执行在同一 Attempt 上追加 10 条 `node.claim_renewed`。测试设备完成后进入 `revoked`，追加型 Edge 审计包含 issued、paired、revoked 各一条，旧凭据再次 claim 返回 `device has been revoked`。本机 `0600` 凭据、SSH 隧道和两端临时上传件随后删除。该验收不使用飞书或真实业务数据，也不证明公网 HTTPS。
- Cloudflare 权威 DNS 返回专用子域名的 DNS-only A 记录，TTL 为 300 秒；Caddy 通过 TLS-ALPN-01 取得 Let’s Encrypt 证书。源站直连验证了正确 SAN、可信链、HTTP/2 401、`Cache-Control: no-store` 与安全响应头。随后本机公网 TLS ClientHello 被连接重置，服务器抓包确认该连接没有到达 ECS，而服务器 loopback 与公网 hairpin 始终返回 401，两个服务均无重启。该现象与阿里云中国内地域名未完成 ICP 接入备案时的 80/443 阻断一致，不能描述为公网 HTTPS 已可用。
- 合成实例 `edge_https_acceptance_20260802_184636` 在公网验收前创建，因 TLS 阻断停留在 `running / ready / pending`，未被任何设备认领；本轮没有签发配对码、没有创建设备凭据，也没有启动 Codex。该实例作为失败前置条件证据保留，后续完成备案或迁移后可继续用于公网 E2E。
- 升级前已累计 28 次验证失败的历史 Inbox 事件，在下一次真实认领后于北京时间 22:36:28 原子写入 `status=exhausted`、`attempt_count=29`、`outcome=exhausted:verification_attempts` 和 `failure_stage=verification`；结构化日志同步回读 `exhausted=1`。
- 真实开发实例已在测试飞书组织完成 `Human -> Agent -> Human`：首个 Human Task 完成后只提交 `{confirmed: true}`，真实模型生成 210 字正文，最终 Human Task 精确展示该正文；第二次人工完成后 Instance 与三个 Node / Attempt 全部为 `done`。验证不代表生产上线。
- 正式模板 CLI 已用合成输入依次完成模板创建、启用、从模板创建草稿、只读预览和确认。实例 `template_entry_20260801_213749` 保存 `target_agent_review:1` 完整快照，并已用真实飞书 Task 与真实模型完成 `Human -> Agent -> Human`：Instance 与三个 Attempt 均为 `done`，两条 Task Projection 均完成，该实例八条 Outbox 均为 `published`。该流程仍是开发验证，不含用户业务数据。
- 正式混合模板 `target_checked_agent_review` 已用合成输入创建实例 `mixed_tool_acceptance_20260802_200044`。两个 Human Task 均由 Owner 在测试组织完成，Agent 生成正文后，`content.check` 返回 `pass`、172 字和全部必需词命中；最终 Task 同时展示 Agent 正文与 Tool JSON 证据。Instance、四个 Node 与 Attempt 均为 `done`，两条 Inbox 为 `processed`，10 条 Outbox 为 `published`。五个服务整体重启后状态不变，均为 active 且 `NRestarts=0`。
- 飞书 IM 真实链路已从 `/larkflow start` 创建模板草稿，再由 `/larkflow confirm` 启动。首个 Human Task 通过轮询推进，真实 Agent、`content.check` Tool 和最终 Human Task 依次完成，Instance 与四个 Node / Attempt 均为 `done`。完成 Docx 回读 revision 3，并确认包含 Instance ID、完成状态和四个节点结果；最终文本通知按消息 ID 回读，确认包含完成状态、Instance ID 和文档 token。再次执行单实例完成修复返回 `documents_created=0 / messages_sent=0 / noops=1`，没有重复资源。该验收只代表开发环境和测试组织。
- Owner 对同一已完成实例发送 `/larkflow status` 后，耐久命令记录为 `processed / status_shown`，回复投影为 `sent`。机器人身份从飞书服务端回读到唯一文本消息，确认包含“已完成”、`4/4` 与“责任人：你”，且不包含 open_id。该查询不修改 Instance、Node、Attempt 或 aggregate version，仅代表开发环境和测试组织。
- Owner 发送 `/larkflow list` 后，耐久命令记录为 `processed / instances_listed`，回复投影为 `sent`。机器人身份从飞书服务端按消息 ID 回读到十条本人拥有的最近实例，包含完成与进行中状态、进度和详情查询提示，不包含人员 ID。该查询按 `created_at DESC, id DESC` 读取有界摘要，不加载完整聚合，也不修改 Instance、Node、Attempt、审计或 aggregate version，仅代表开发环境和测试组织。
- 节点重启实例 `im_64450d61fa02de36f86bcedd` 由真实 `/larkflow start` 与 `/larkflow confirm` 创建并推进到最终 Human 节点等待。Owner 使用 `/larkflow restart` 得到只影响 `review_summary` 的 Attempt 1 预览，再以 `/larkflow restart-confirm` 创建 Attempt 2。旧 Attempt 进入 canceled、旧 Task 服务端为 done，新 Task 使用不同 GUID 且先为 todo；重复确认只返回当前 Attempt 2，aggregate version、Task 数和审计数不变。人工完成新 Task 后 Instance 与三个当前 Node 均为 done，旧 Attempt 保留，新 Attempt 有结果，两条完成 Inbox 均为 processed，11 条 Outbox 均为 published，重启审计恰好 1 条，完成文档与最终通知均已投影。三个验收 Task 从飞书服务端回读为 `done / mode=1 / one assignee / one completed assignee`。
- 同一实例随后由 Owner 使用 `/larkflow restart-all` 获得包含三个节点的完整实例预览，再用共享的 `/larkflow restart-confirm` 原子创建 confirm Attempt 2、draft Attempt 2 和 review Attempt 3。全部根节点重新进入可调度状态，其余节点等待依赖；重复确认只回读当前轮次，没有新增 aggregate version、Attempt、Task 或审计。两个人工 Task 依次完成后 Instance 为 `done / version 16 / graph_revision 1`，三个新 Attempt 均为 done 且有结果，所有旧 Attempt 与结果保留。两个新 Task 的 Inbox 均为 `processed / submitted:human_node`，实例相关的 19 条 Outbox 均为 published，最终 Task 从飞书服务端回读为 `done / mode=1 / one assignee / one completed assignee`。重启前后的完成文档和最终通知分别使用不同的幂等键与外部 ID；新文档从飞书服务端回读 revision 3，包含实例 ID 和三个节点结果。该验收只代表开发环境和测试组织。
- 跨人员 `collaborative_agent_review` 已完成两种正向真栈入口。群聊中的认证 mention 直接冻结 requester 与 reviewer 角色；单聊中的同一模板会返回 Card 2.0 人员选择表单，发起人和指定测试成员由服务端目录验证后冻结到一个草稿。卡片回调经过凭据侧与领域侧两阶段处理，实例 `im_7575ba0f48ef145a782a20c3` 只创建一次；成功回复为 `sent`，原卡片更新为绿色已确认状态，选择器与按钮均禁用。修复覆盖飞书微秒时间戳、开发数据库表 ACL、卡片 `config.update_multi`、更新失败可观测性，以及已禁用选择器不得同时声明 `required=true`。该验收只代表开发环境和测试组织，不代表生产上线。
- 本轮真实 Task 完成后，bot 长连接收到的对应 Task 事件计数仍为 0；30 秒周期状态轮询可靠推进了两个 Human 节点。事件路径没有通过验收，不能替代轮询或服务端详情再授权。
- 完成投影最初缺失的根因是一次部署只重启了 Runtime、Projection 和 legacy，遗漏两个 Target 入站服务；旧领域入站进程在代码升级前启动，因此只提交完成审计，没有生成新版本的完成投影 outbox。`deploy/restart-development-services` 现统一重启 Runtime、Draft Generation Worker、Projection、两个 Interactive、凭据侧入站、领域侧入站、Edge、Console 和 legacy 共十个服务，并逐个回读启动时间与 `NRestarts`。`reconcile-instance-completion` 只修复一个已知完成实例，不批量补发历史通知。
- Projection 使用同一 wheel 和独立 `larkflow-target-projection.service`，以持有测试飞书 profile 的 `lf-dev` 运行，不复制加密 app secret。PostgreSQL 同名角色只能 SELECT migration、Instance、Node、Attempt、Outbox 与 Projection，只能 UPDATE Outbox、INSERT / UPDATE Projection，并可 INSERT 完成观察信号到 Inbox，不能更新 Instance 领域状态。`/srv/larkflow/target` 保持 `0750`，只通过 ACL 给 `lf-dev` 路径穿越权限；Projection env 为 `0640 root:lf-dev`，不含飞书密钥。
- Projection 启动全量对账和 `reconcile-projections` 运维命令已部署。专用实例 `projection_repair_acceptance_20260801_235210` 创建初始 Task 后先完成未删除基线，对账为 3 条绑定全部不变；随后用 bot 身份删除且只删除该 GUID，服务端读取明确返回 `1470404`。下一次对账回读 `tasks_recreated=1`、`unchanged=2`、`failed=0`，Projection 换绑到不同 GUID、`repair_generation=1`，新 Task 为 `todo / mode=1 / source=6 / 1 member`；再次对账为 `tasks_recreated=0`、`unchanged=3`，稳定 repair key 与代码公式一致。人工完成新 Task 后，凭据侧日志为 `claimed=1 / verified=1 / failed=0`，领域侧为 `claimed=1 / submitted=1 / failed=0`；Inbox 为 `processed`、`attempt_count=2`、无失败阶段，Instance、Node、Attempt 均为 `done`，Projection 为已完成。数据库共有 9 条 Task Projection，均有 external ID，其中 8 条完成、1 条等待；五个服务保持 active、`NRestarts=0`，验收窗口无 warning 日志。
- Projection 完成轮询默认每 30 秒执行，启动立即扫描；`reconcile-completions` 可显式触发。部署后首轮读取 3 个当前 Human Task，观察到 2 个完成、1 个待办，写入 2 条 `feishu_task_poll` Inbox 信号。凭据侧 `claimed=2 / verified=2 / failed=0`，领域侧 `claimed=2 / submitted=2 / failed=0`。此前外部 Task 已完成但仍等待事件的 `minimal_scope_20260802_010613` 与 `minimal_scope_event_20260802_011644`，其 Instance、Node 与 Projection 均进入完成态。显式重跑只读取剩余 1 个待办 Task，新增信号为 0；两条轮询 Inbox 均为 `processed / submitted:human_node`。
- 凭据侧入站校验使用 `larkflow-target-inbound-adapter.service`，以 `lf-dev` 运行并只读飞书 Task 详情。它可以 SELECT / UPDATE Inbox，不能更新 Instance、Node 或 Attempt。领域入站使用 `larkflow-target-inbound.service`，以 `lf_target_dev` 运行，不能读取 legacy 飞书 profile 与应用凭据。
- `larkflow-target.service`、`larkflow-target-projection.service`、两个 `larkflow-target-interactive@.service` 实例、`larkflow-target-inbound-adapter.service`、`larkflow-target-inbound.service`、`larkflow-target-draft-generator.service`、`larkflow-target-edge.service`、`larkflow-target-console.service` 与 legacy `larkflow@dev` 均 enabled / active，回读 `NRestarts=0`。legacy 消费 Task 与 IM EventKey，只把原始 Task 提示或 `/larkflow` 命令写入 Target Inbox，不写 Target 领域表；Task 状态轮询是 Human 完成的可靠入口，Task 事件只是可选低延迟信号。
- 常驻验证覆盖普通 Tool 完成、SIGTERM 干净退出、SIGKILL 后 5 秒自动拉起，以及租约到期后由不同 Worker 恢复同一 Attempt。有效故障注入最终记录 `recovered=1`、`completed=1`、`stale_results=0` 和 `node.claim_recovered` 审计。
- `larkflow-target-backup.timer` 每天北京时间 03:20 后随机延迟不超过 15 分钟执行 custom-format `pg_dump`，本机保留约 7 天。备份目录权限为 `0700 lf_target_dev:lf_target_dev`，备份文件为 `0600`。2026-08-08 新备份 `larkflow_target_dev-20260808T021324+0800.dump` 已回读 `Result=success / ExecMainStatus=0`，大小 238220 bytes，SHA-256 为 `8ca267b7e1f28fefb2ef030aab8668e49c1fc64af637e86e8282efb8400612e4`。隔离库恢复后的 21 份 migration、22 张表、55 个流程实例、Console 会话与撤销审计计数均和源库一致；所有业务表与函数归 `lf_target_dev`，PUBLIC 没有数据库权限或 schema CREATE，UTC 与三项 timeout 生效，没有应用服务连接隔离库。
- 两阶段 Inbox 已在一次性真实 PostgreSQL 数据库中验证 migration 重入、event ID 去重、校验与领域两组双 Worker 竞争、无效 claim token 拒绝、阶段恢复和最终 `processed` 终态。一次性数据库已删除。
- Template Service 已在一次性真实 PostgreSQL 14 数据库中验证五份 migration 重入、两路同时启用时一条成功一条并发冲突、版本更新触发器拒绝修改、模板审计追加和冻结实例外键。一次性数据库与远端验证脚本均已删除。
- 备份目前只在同一块系统盘，能处理误操作和局部数据损坏，不能处理整机或云盘丢失，也没有 PITR。进入生产前必须增加异机或对象存储副本、恢复演练、容量告警和 PostgreSQL 升级流程。
- `larkflow@dev` 始终保持 active，仍运行 legacy SQLite 路径。Projection 开发服务只复用它的飞书 OS 身份和 profile，不读取 legacy SQLite；Target Runtime 与 legacy 领域状态没有混接。

### Target PostgreSQL 运维入口

- 应用身份：systemd 服务以 `lf_target_dev` 运行，通过 `postgresql:///larkflow_target_dev` 连接，不配置数据库密码，不改用 TCP。
- Target CLI：`/srv/larkflow/target/venv/bin/larkflow-target --env-file /etc/larkflow-target.env <command>`；模板控制面支持 template-create、template-add-version、template-enable、template-disable、template-delete、template-list、template-show、create-from-template 与 preview，并保留实例和五类 Worker 命令。Projection 身份使用 `/etc/larkflow-target-projection.env reconcile-projections` 显式执行启动对账，使用同一 env 的 `reconcile-completions` 立即扫描完成状态；`reconcile-instance-completion <instance_id>` 只补齐一个已完成实例缺失的文档或最终通知。凭据侧交互使用 `/etc/larkflow-target-interactive.env interact`，开发环境固定启用 `larkflow-target-interactive@1.service` 与 `@2.service`。
- Agent 开关：`LARKFLOW_TARGET_ENABLE_AGENT_EXECUTOR=true`。路由使用 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`，单线路可用 `LLM_TIMEOUT` 收紧。启动时会计算主线路与全部备用线路的超时总和，并要求该值加 `LARKFLOW_TARGET_AGENT_CLAIM_SAFETY_SECONDS` 后严格小于 `LARKFLOW_TARGET_CLAIM_TTL_SECONDS`；不满足时服务拒绝启动。
- Tool 开关：`LARKFLOW_TARGET_ENABLE_CONTENT_CHECK_EXECUTOR=true`。`LARKFLOW_TARGET_CONTENT_CHECK_MAX_CHARS` 限制检查输入上界；Tool 不读取模型或飞书凭据。
- 目录开关：`LARKFLOW_TARGET_VALIDATE_DIRECTORY=false`。草稿入库前的 Instance / Node Owner 全量校验仍默认关闭；IM 命令发送者、mention 绑定成员和人员选择卡候选人的当前企业活跃成员校验是独立的必经步骤，已在测试组织真栈通过。后续启用全量开关前仍需单独回归异常成员状态。
- Runtime 服务：`systemctl status larkflow-target.service`；日志看 `journalctl -u larkflow-target.service`。
- Projection 服务：`systemctl status larkflow-target-projection.service`；日志看 `journalctl -u larkflow-target-projection.service`。仓库 unit 与 env 模板为 `deploy/larkflow-target-projection.service`、`deploy/larkflow-target-projection.env.example`。
- Interactive 服务：`systemctl status larkflow-target-interactive@1.service larkflow-target-interactive@2.service`；日志按同名 unit 查看。两个副本使用稳定的主机名与实例号 Worker ID，共享 `/srv/larkflow/dev` 下的受限 bot profile。仓库 unit 与 env 模板为 `deploy/larkflow-target-interactive@.service`、`deploy/larkflow-target-interactive.env.example`。
- 入站校验服务：`systemctl status larkflow-target-inbound-adapter.service`；日志看 `journalctl -u larkflow-target-inbound-adapter.service`。
- 领域入站服务：`systemctl status larkflow-target-inbound.service`；日志看 `journalctl -u larkflow-target-inbound.service`。
- Edge Gateway：`systemctl status larkflow-target-edge.service`；日志看 `journalctl -u larkflow-target-edge.service`。服务只允许 loopback。
- Owner Console：`systemctl status larkflow-target-console.service`；日志看 `journalctl -u larkflow-target-console.service`。应用服务只允许 loopback，Caddy 通过公网 IP HTTPS 反向代理 `/console/`；必要时仍可用 `ssh -N -L 127.0.0.1:18780:127.0.0.1:8780 alicloud-sh` 做隔离诊断。仓库 unit 与 env 模板为 `deploy/larkflow-target-console.service`、`deploy/larkflow-target-console.env.example`；env 由 larkflow 自己解析，禁止在 shell 中 source。开发服务器运行 `feishu` 模式，公开 origin、`/console/auth/callback`、工作台主页、app secret、飞书 `tenant_key` 与 Target tenant 映射均已配置。完成登录后的会话摘要保存到 PostgreSQL，Console 重启不再要求重新登录；授权中途的短期 OAuth state 仍可能因重启失效。`LARKFLOW_CONSOLE_ADMIN_PERSON_IDS` 是最多 100 个 person ID 的服务端 allowlist，必须保留在 `0640 root:lf_target_dev` env，不进入浏览器、日志或文档；修改后只需重启 Console，并分别回归管理员 200 与普通成员 404。五项 `LARKFLOW_CONSOLE_RATE_LIMIT_*` 配置同样只由服务端读取，调整后必须同时回归正常请求、429、`Retry-After`、不同伪造来源仍共享 Caddy 覆盖后的预算，以及十个 Python 服务与 Caddy 的 `NRestarts=0`。
- 管理员 allowlist：先从管理员会话治理面板取得目标成员的安全会话 ID，再运行 `sudo larkflow-console-admin-allowlist preview add <session_id>` 或 `preview remove <session_id>`。只核对公开输出中的操作、数量、是否需要变更、会话后缀和过期时间，不复制内部 person ID。确认使用 `sudo larkflow-console-admin-allowlist confirm <preview_id>`；已应用操作需要撤销时使用 `rollback <operation_id>`。预览十分钟失效，env、tenant、allowlist 或会话漂移都会拒绝确认，最后一名管理员不能移除。实际变更后工具会自行重启并回读 Console，禁止再手工 source env 或跳过健康检查直接编辑。
- Edge HTTPS：Caddy 当前为员工工作台保持 `enabled / active`，不能再把 Caddy 服务状态等同于 Edge 公网链路状态。Personal Agent Edge 的专用公网路由仍受 ICP 接入备案阻断，不对员工设备宣称可用；恢复 Edge 公网链路前必须单独核对路由、DNS、证书和外部设备 E2E。源站 loopback 成功不能替代 ICP 接入与外部链路验收。
- 手工只读连接：`sudo -u lf_target_dev env --chdir=/ psql -X --dbname=larkflow_target_dev`。
- migration：由目标应用启动入口调用 package-data migration runner。长期库的二十一份 migration 已落地，后续不得手工改 schema 后跳过 migration ledger。
- 立即备份：`sudo systemctl start larkflow-target-backup.service`；结果看 `systemctl show larkflow-target-backup.service --property=Result,ExecMainStatus`。
- 定时器：`systemctl show larkflow-target-backup.timer --property=ActiveState,UnitFileState,NextElapseUSecRealtime`。
- 恢复：先由 postgres 管理员创建目标库，重建 UTC 与三项 timeout，撤销 `PUBLIC` 的数据库权限和对 `public` schema 的 CREATE，再授予 `lf_target_dev` CONNECT、TEMPORARY、USAGE 与 CREATE；之后以 `lf_target_dev` 执行 `pg_restore --exit-on-error --single-transaction --no-acl`。不能直接让应用角色恢复 ACL，`public` schema 不归它所有，pg_restore 只会 warning，目标库会保留默认 PUBLIC CREATE。最终验收同时回读 migration、关键表计数、表与函数所有者、ACL、时区、timeout 和目标库应用连接数。
- 恢复会原样带回未过期的 `workflow_console_sessions`。默认暴露前门禁是在任何服务指向恢复库之前，以应用角色在一个事务中清空该表和所有未消费的会话撤销预览，随后确认有效会话为 0，而已消费预览、追加型撤销事件、流程实例和 migration 均保留。若恢复库是同一安全域的权威替换且确需保留会话，必须单独记录事故决策；并行演练或临时环境不得复用原会话。隔离恢复库在取证完成后必须删除，原 custom-format 备份继续保留。
- 仓库资产：`deploy/larkflow-target-backup`、`deploy/larkflow-target-backup.service`、`deploy/larkflow-target-backup.timer`、`deploy/larkflow-console-admin-allowlist.py`。服务器安装位置分别是 `/usr/local/sbin/` 与 `/etc/systemd/system/`。

✅ **已真部署**（alicloud-sh，2026-07-27）。ADR-007 从立项欠到现在的那笔债还上了：租户 `dev` 以 systemd 常驻，真飞书凭证，**入站长连接已建立**：

```
[21:19:44] 启动对账：实例 0｜已对账 0｜已完成 0｜失败 0
[21:19:47] 入站通道已就绪：['card.action.trigger', 'task.task.update_user_access_v2']
[21:19:47] 定期对账已起：每 120s 一轮
```

实测资源：**6 进程 / 282 MB RSS**（1 个 python + 每个 EventKey 一条两级 node consume + 1 个 per-(HOME,appId) 的事件总线守护进程）。宿主 1.6G 内存，起完还剩约 1.0G 可用。

产物在 `deploy/`（systemd 模板单元 + 租户 env 模板 + bootstrap 脚本）+ `larkflow doctor`（只读体检）+ 文末〈runbook〉。

**仍未做**：在这台机器上跑一条真实例走完八个节点（引擎能力本身已在 Mac 上验过，见 CHANGELOG v0.6.0）；长连接跨小时级的存活（那正是 ADR-039 记的「静默死亡」，只能靠时间验）。

这一趟换来 6 条真机才看得见的东西，全部已修回代码，逐条记在下面各节：模板 yaml 根本没被打进包（装出来的引擎一条流程都跑不起来）、`requires-python` 过严、`Path.exists()` 不吞 EACCES、`ensurepip` 单独成包、`StartLimit*` 写错段被静默忽略、以及凭证隔离只认 `HOME`。

## Legacy 原型目标形态
- **宿主**：alicloud-sh（Ubuntu 22.04.5 / x86_64 / 2 核 / **1.6G 内存**（可用约 1.15G）/ 40G 盘（34G 空闲）；只开 22 端口）。Python 自带 3.10.12，**`ensurepip` 在单独的 `python3.10-venv` 包里**（`python3 -m venv --help` 会过、真建 venv 才报错）。内存这条是硬约束：实测每租户约 6 进程 / 300MB，而定期对账的全量枚举会再吃几百 MB 峰值，**这台机器一个租户就接近满**。
- **持久化**：LangGraph checkpointer 用 **SQLite**（省内存，单租户 MVP 够）。同一个文件里还有关联表与幂等表。**必须放本地盘**（网络盘上 WAL 与 flock 都不可靠，见〈多进程〉）。
- **事件入口**：引擎 spawn `lark-cli event consume <EventKey>` 子进程收 NDJSON，出站长连接。**无需任何入站端口**（ADR-007）。
- **前端↔引擎传输（命门，ADR-019）**：前端要引擎的读 / 命令 API = 入站。但妙搭云托管（`aiforce.cloud`）能否 egress 到本机、且本机能否公网可达（公网 IP / 域名 / 证书 / 反代 / 隧道）**未确认**。若不能，**退「命令走飞书原生轨」**：app 写多维表格 / 发消息 / 触发自动化 → 引擎经 `event consume` 消费（保 ADR-007「无入站」）。**列为妙搭原型第一必验项，排在画布之前。**
- **LLM**：OpenAI 兼容多角色路由（火山方舟 / 中转站 / 直连，见 RELATIONS / DECISIONS ADR-017）。
- **飞书应用**：独立 dev 飞书租户（ADR-008）的企业自建应用，挂工作台 = 妙搭前端（本地开发，飞书托管 `aiforce.cloud`，ADR-019）+ bot + 卡片。凭证（app_id / secret / lark-cli token）走 env / keychain，绝不入库。

## 进程拓扑（ADR-031）
一台宿主上跑的东西：

- **一个常驻 daemon**：`larkflow serve`。它自己再 spawn 每个 EventKey 一条 `lark-cli event consume` 子进程（v1 订阅两个：卡片按钮 `card.action.trigger` + 任务事件 `task.task.update_user_access_v2`）。
- **若干一次性 CLI 命令**：`larkflow start / status / pending / unblock / reconcile`，人或脚本按需敲，与 daemon 是**不同的进程、同一个 SQLite 文件**。
- 意图路由层（v1.1，ADR-021）落地后是第三个进程，可共宿；纳入同一套进程守护。

```
larkflow serve ──┬── lark-cli event consume card.action.trigger   （子进程，NDJSON → stdout）
                 └── lark-cli event consume task.task.update_...  （子进程）
                 └── SQLite（checkpointer + 关联表 + 幂等表）
larkflow unblock / start / status / …  ← 另一个进程，写同一个 SQLite
```

## 飞书 app 需要什么（权限台账）

**为什么要有这张表**：dev 阶段图省事会把「我能开的都开了」，上线前没人说得清哪些是真用到的。这张表按**引擎实际 spawn 的 lark-cli 命令**倒推，一条命令一行；`确认` 一列区分「查过 `lark-cli schema` / 事件 schema」与「按命令语义推断、待第一条真链路验证」。**收敛权限时以这张表为准**，跑通后把没用上的关掉再回归一遍。

| 引擎的动作 | lark-cli 命令 | 需要的权限 | 确认 |
|---|---|---|---|
| 派人工任务 | `task +create --summary --description --assignee --idempotency-key --as bot` | `task:task:writeonly` | ✅ Target 最小 scope 真栈 |
| 关任务 | `task +complete --task-id --as bot` | `task:task:writeonly` | ✅ Target 最小 scope 真栈 |
| 读当前任务与完成状态 | `task get --task-id --as bot` | `task:task:read` | ✅ Target 最小 scope 真栈 |
| 可选收「任务完成」事件 | `event consume task.task.update_user_access_v2 --as bot` | `task:task:read` + **控制台事件** `task.task.update_user_access_v2` | ⚠️ 当前事件为 user 身份，bot profile 未收到 |
| 收 Target 文本命令 | `event consume im.message.receive_v1 --as bot` | 控制台事件 `im.message.receive_v1` 与接收消息所需 IM scope | ✅ 测试组织已发布并收到真实命令 |
| 发命令回执、节点结果与最终通知 | `im +messages-send --user-id … --msg-type text --as bot` | `im:message:send_as_bot` | ✅ Target 真栈 |
| 创建完成文档 | `docs +create --title … --content … --as bot` | `docx:document` + `docx:document:create` | ✅ Target 真栈 |
| 回读完成文档做验收 | `docs +fetch --document-id … --as bot` | 文档读取能力 | ✅ 仅验收使用，不是 Runtime 必需动作 |
| 发门禁卡片 | `im +messages-send --user-id\|--chat-id --msg-type interactive` | 发消息权限（`im:message` 一族） | ⚠️ 推断 |
| 发通知（打回回执 / 卡死告警） | `im +messages-send … --msg-type text` | 同上 | ⚠️ 推断 |
| 收卡片按钮点击 | `event consume card.action.trigger --as bot` | `im:message:readonly` + **控制台回调** `card.action.trigger` | ✅ 事件 schema |
| 建交付物 | `markdown +create --name --content -` | Drive 文件写入（`drive:drive` / `drive:file:upload` 一族） | ⚠️ 推断 |
| 覆盖交付物 | `markdown +overwrite --file-token --content -` | 同上 | ⚠️ 推断 |
| 读交付物正文 | `markdown +fetch --file-token` | Drive 文件读取 | ⚠️ 推断 |

**已在真栈实测通过（测试组织）**：2026-07-26 的交互消息与卡片回调；2026-08-02 的 Target Human Task 创建、完成和详情读取；2026-08-03 的 `im.message.receive_v1` 十个窄命令、当前企业活跃成员验证、Owner 专属状态查询、文本回执、Agent / Tool 结果消息、节点与完整实例重启、运行中未来区域编辑、完成 Docx 与最终通知。编辑正向实例 `im_7590aae6bf8d067e74d44909` 从 revision 1 变为 2，重复确认 no-op，更新标题的最终 Task、revision 3 Docx 与最终通知均已从飞书服务端回读，Instance 最终为 `done / version 8`。负向实例 `im_93e7e95aadba9ded17190542` 真实拒绝冻结线修改、成环依赖和状态漂移后的陈旧预览；陈旧预览保持未消费，Instance 最终为 `done / version 7 / graph_revision 1`，没有图编辑审计。开发应用发布所需通讯录数据范围后，中央应用从根部门读取到五名活跃成员，并能解析选定测试成员；无部门参数的独立成员查询仍只返回当前用户，符合该接口语义。随后以该测试成员为 Owner 创建合成实例并生成真实 Human Task 投影，当前登录用户从飞书会话发送 `/larkflow edit` 后得到合并拒绝回复。命令记录为 `processed / rejected:command / sent`，实例保持 `running / graph_revision 1`，GraphEditPreview 与 `instance.graph_edited` 审计均为 0，目标节点标题不变；测试成员无需完成该 Task。文档权限在新增 `docx:document + docx:document:create` 并发布后生效。五个 Target 服务与 legacy 事件消费者均为 active 且 `NRestarts=0`，本轮验收窗口没有 warning 级日志。Task 完成轮询已完成真实闭环，Task 事件路径本轮仍为零事件，不能标记为通过。权限台账记录已知运行所需能力，不等于当前控制台完整权限清单；上线前仍需从已发布版本重新导出并做最小权限回归。

**「事件」与「回调」是两个东西，别在同一个页签里找**（2026-07-26 实测踩过）：开发者后台「事件与回调」下分**事件配置 / 回调配置**两栏，各自有**各自的订阅方式**。`task.task.update_user_access_v2` 在**事件**里，`card.action.trigger`（卡片回传交互）在**回调**里。只订了事件时，`lark-cli event consume card.action.trigger` 以 `failed_precondition` 直接拒绝（文案用词是 callbacks 不是 events）；在飞书里点按钮则弹「该应用尚未配置卡片回调」。

**改完必须发布版本才生效**（同上，实测的真实根因）：回调加好了、订阅方式也对，但没发版本时行为与「没配」一模一样，包括 `event consume` 仍报 `failed_precondition`。**排查顺序：先确认版本发布了，再怀疑配置本身。**（当时我先怀疑的是「浏览器登录的租户不对」，错了。那条一键配置链接点进去显示「该应用不存在」，是未发布版本的连带表现，不是租户问题。）

**两栏的订阅方式都要选长连接**，不要 webhook：`lark-cli event consume` 走长连接，这正是 ADR-007「引擎无需任何入站端口」的来源；选了 webhook 整条入站链路不通。

**长连接没有队列**（ADR-007 的实测修正，可用性上很硬的一条）：daemon 不在线时人点按钮**当场失败**（飞书弹「目标回调服务当前未在线」），该回调**不补投**。webhook 模式下飞书会 POST 并重试，长连接没有这个兜底。所以 `Restart=always` 不是可选项；启动对账能补回投影，**补不回丢掉的点击**。好在它失败得响，用户会再点一次。

**身份**：卡片回调只有 **bot** 收得到，故常驻服务 `LARKFLOW_IDENTITY=bot`。

**代理**：本机 shell 有 Clash 全局代理，lark-cli 会警告凭证经由代理传输。飞书是境内服务，建议 `LARK_CLI_NO_PROXY=1` 绕开。注意这条警告与 `failed_precondition` 类报错**无关**，后者是发请求之前的本地前置校验，别把它误诊成网络问题。

## 飞书凭证在宿主上到底落在哪（2026-07-27 查实）

开发机上 `~/.lark-cli/config.json` 里是 `"appSecret": {"source": "keychain", "id": "appsecret:<appId>"}`，真值在 macOS Keychain 里。**这个结论不能带到 Linux 宿主上**，而红线「key / 凭证不入库」在真宿主上成不成立就取决于这一条。查法与结论：

- lark-cli 是 Go 写的单体二进制，npm 包按平台分发。取官方 `lark-cli-1.0.77-linux-amd64.tar.gz`（校验和对过），扫符号与字符串：
  - darwin 构建链的是 `github.com/zalando/go-keyring`，只编进 `macOSXKeychain` 那条实现。
  - **linux 构建里 `keyring` / `freedesktop` 零命中**（同一份二进制里 `open-apis` 有 365 处，证明字符串提取是有效的），即**没有走 D-Bus Secret Service**，没有 gnome-keyring / KWallet 这条路。
  - linux 构建里有 `internal/keychain/keychain_other.go`、`keychain.getMasterKey`、常量 `master.key`，以及一句面向用户的原话：**「command is only supported on macOS; on this platform the keychain layer already uses local files.」**
**在真宿主上实测坐实**（alicloud-sh，Ubuntu 22.04.5，无桌面、无 gnome-keyring、无 user D-Bus、`org.freedesktop.secrets` 不存在）。用一个**假** app secret 建 profile，落盘的是：

```
-rw------- 240 <CONFIG_DIR>/config.json                       {"appSecret":{"source":"keychain","id":"appsecret:<appId>"}}
-rw-------  32 <DATA_DIR>/lark-cli/master.key                 32 字节 = AES-256 主密钥
-rw-------  56 <DATA_DIR>/lark-cli/appsecret_<appId>.enc      密文
```

- **结论**：Linux 上 app secret = **密文文件 + 同目录的 `master.key`**。注意 `config.json` 里仍写着 `"source": "keychain"`，那只是内部抽象的名字，这台机器上根本没有任何 keychain。它是**静态混淆，不是 OS 级保护**：谁读得到这个目录谁就解得开。

三条直接后果：

1. 红线仍然成立（凭证不在 larkflow 的 SQLite 里），但**安全边界从「OS 钥匙串」降级成「文件权限 + 谁能登这台机」**。所以每租户独立 Unix 用户 + `0700` 目录不是洁癖，那是这一层唯一的防线。
2. **隔离只认 `HOME` 一个变量。** 实测三组对照：只设 `HOME` 时 config 与密文**都**跟着 HOME 走（`$HOME/.lark-cli/` 与 `$HOME/.local/share/lark-cli/`）；`HOME` + `LARKSUITE_CLI_CONFIG_DIR` 时 config.json 分开了、**密文仍留在 HOME 下**；再加 `LARKSUITE_CLI_DATA_DIR` 密文才跟着走。结论是**只设 HOME 就已经把两样都隔离干净了**，多设那两个不增加任何隔离，只增加一处「建 profile 时的 env 与服务运行时的 env 不一致」的失败模式，而那个失败极其隐蔽：profile 建出来看着正常、`profile list` 里也在，服务却在 `auth status` 报 `bot: not_configured`，然后照常启动、静默地没有凭证（当天踩了两次）。所以 `deploy/` 里只留 `HOME`，并给每个租户放一个 `<租户目录>/lark` 包装器，人手工敲 lark-cli 时一律走它、不碰 env。
3. 建 profile 不需要浏览器：`lark-cli profile add --app-secret-stdin` 完全非交互（真机实测通过），secret 走 stdin 不进命令行、不进 shell 历史。

**未验的一条**：二进制里还有 `LARKSUITE_CLI_APP_ID` / `LARKSUITE_CLI_APP_SECRET` 这对**未文档化**的环境变量。设上之后 lark-cli 进入 external credential provider 模式（`auth` 子命令被拒，原话 "credentials are provided externally and do not support interactive management"），而且**配置目录里一个文件都不落**。若它能真正完成 token 交换，凭证就可以完全不落盘、只存在于 0600 的 env 文件里，比上面那套文件加密强一个档次。但 README 里没有它、也就没有兼容承诺，**验通之前不要依赖**。

## 启动 / 退出行为
`larkflow serve` 的一生（顺序是硬的，理由见 ADR-031）：装 SIGINT/SIGTERM → **启动全实例对账** → 起泵 → block 到收到信号 → 停订阅 → 等在飞的那条事件跑完 → 关 DB。

**启动对账**做什么：按 checkpointer 里的实例逐个 `reconcile`（重建崩溃时丢掉的卡 / 待办投影，把被 super-step 屏障挡住的分支推到位）。三条要知道的性质：
- 实例枚举的真相源就是 checkpointer，**没有第二张实例表**；换掉 checkpointer 时优雅降级（报告里带 `degraded`），服务照起。
- **逐实例容错**：一个坏实例不阻塞启动，失败进报告与 stderr 日志。
- **跳过已跑完的实例**（没有投影要重建，重推只会重发通知）。
- 实例多了启动会变慢（无并发、无分批），这段时间入站通道还没起。**对账期间收到 SIGTERM 会当场中止**，没轮到的实例进报告与日志（不会把剩下几百个跑完，也不会再白起一次泵）。
- `larkflow reconcile`（不带实例）与它走**同一条代码路径**。

**退出**：停订阅 → 等在飞的那条事件跑完 → 关 DB。**没排空就不关 DB**（那条事件可能正握着实例锁写 checkpointer，关连接等于把桌子从它手底下抽走），改为记一笔 `drain` 故障、把半截写留给下次启动对账兜，并让进程**退出码 1**。所以 systemd 里 `Restart=always` 配合 `TimeoutStopSec` 要给足（一条事件里可能在跑 LLM）；日志里出现 `drain` 就说明上次没收干净，下次启动会补。

**配置加载**：CLI 启动时自动读**当前目录的 `.env`**（`--env-file` 可改），已存在的环境变量优先。**绝不要用 `source .env`**：`.env` 长得像 shell 赋值但不是 shell 脚本，`source` 会做引号剥离与 `$` 展开：实测把 `LARKFLOW_ROLES={"法务":"ou_…"}` 吃成 `{法务:ou_…}` 当场炸，而含 `$` 的 api_key 会被悄悄改写**且不报错**。systemd 用 `EnvironmentFile=`（它按 KEY=VALUE 解析，不走 shell），或让工作目录指到项目根、靠自动加载。

**DB 路径**：默认 `~/.larkflow/larkflow.sqlite`，`--db` / `LARKFLOW_DB` 给相对路径也会**落成绝对路径并回显**。默认值曾经是 cwd 相对的 `larkflow.sqlite`，于是 systemd 起的 daemon（`WorkingDirectory=/`）与你在 home 敲的救场命令各开各的库：两边都不报错、都「正常」，只是各看各的实例，而这个分叉没有任何症状。

**幂等**：派单与通知的幂等键记在本地幂等表里（ADR-033），所以重启 / 反复对账**不会**再给还在等的人发第二遍卡、建第二条待办。

## 多进程写同一个 SQLite
daemon 常驻握着 DB，而运维的一次性命令（尤其 `unblock`，那是 `blocked` 门的唯一出口）必须能同时执行。做法与边界：

- **保证**：走这套 API 的进程，对同一实例的状态变更严格串行（跨进程 flock，锁文件在 `<DB>.locks/`）；同一个 DB 只允许一个 daemon（`<DB>.serve.lock`）；SQLite 层不再有「database is locked」这类伪故障（WAL + busy_timeout）。
- **不保证**：flock 是**建议锁**，裸 sqlite3 或别的工具照样能进来写；NFS / SMB 上语义不可靠（故开不了 WAL 时 `open_db` 直接拒绝启动，不降级）；不是事务；不保证公平；对方握锁超过 `--lock-timeout`（默认 120s）时这边报错而不是硬闯。
- 拿不到实例锁时 daemon 会**丢掉那一条事件**（记一笔故障、继续下一条）。人手里的卡还在，再点一次即可，但那一次点击确实没被处理。
- Windows 跑不了（flock 依赖 fcntl，构造时直接抛，不静默降级成「没有锁」）。

## 环境变量（只列 key 名，真值走 `.env` / keychain，绝不入库；完整注释见仓库 `.env.example`）
- 飞书应用：`LARK_PROFILE`（lark-cli profile，认哪个应用）、`LARKFLOW_IDENTITY`（bot | user，卡片回调只有 bot 收得到）。**凭证不在这里**：app_id / secret / token 由 lark-cli 自己保管，引擎只透传 `--profile`。
- 引擎：`LARKFLOW_DB`（SQLite 路径，**本地盘**）、`LARKFLOW_TEMPLATE`（默认模板名）、`LARKFLOW_DRIVE_FOLDER`（交付物落哪个云空间文件夹）。
- 角色映射：`LARKFLOW_ROLES`（JSON，`assignee_role → open_id`；中文角色名当环境变量名 export 不进去，故以 JSON 为主）、`LARKFLOW_ROLE_<ASCII 别名>`（辅，会合并）。真栈 strict：模板里出现的角色没配全会在**装配期直接抛**，绝不伪造 `ou_<角色名>` 发给飞书。
- LLM（ADR-017，按角色一组三元组）：`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`（默认角色兜底），以及 `LLM_<ROLE>_BASE_URL` / `_API_KEY` / `_MODEL`（如 writer / legal / editor / triage）。三元组缺项的角色会被跳过。

## runbook：一台机器上开一个租户（目标 30 分钟，尚未在真机上验证）

产物在 `deploy/`：`larkflow@.service`（systemd 模板单元，`%i` = 租户名）、`tenant.env.example`、`bootstrap.sh`。一台机器可以跑多个租户，**每租户 = 一个进程 + 一个 SQLite + 一个 lark-cli 配置目录 + 一个 Unix 用户**，引擎代码零改动（全仓 `grep -rni tenant larkflow/` = 0，隔离全靠这四样物理分区）。实测每租户约 6 个进程 / 300MB（1 个 python + 每个 EventKey 一条两级 node consume + 1 个事件总线守护进程）。

**为什么不用 systemd 的 `EnvironmentFile=`**：它有自己的引号规则，而要塞进去的恰好是最容易被引号规则改坏的两类值（`LARKFLOW_ROLES` 是内含双引号的 JSON、LLM 的 api_key 常含 `$`）。这个项目已经被「`source .env` 把 JSON 引号吃掉」坑过一次，不该换个解析器再坑一次。改用 `larkflow --env-file`，走本项目自己那套有测试钉着的解析器；systemd 侧只放纯 ASCII 的 `HOME` 与 `LARKSUITE_CLI_*`。

**国内宿主先解决源**（阿里云上海实测：github / npm registry / npmmirror / open.feishu.cn 都直连通，**唯独 `pypi.org` 超时**）。所以：

```bash
# node：apt 里的太老（lark-cli 要 >=16），NodeSource 域内不稳，直接取 npmmirror 的二进制
curl -sSL -o node.tar.xz https://registry.npmmirror.com/-/binary/node/v24.18.0/node-v24.18.0-linux-x64.tar.xz
sudo tar -xJf node.tar.xz -C /usr/local --strip-components=1     # curl 必须带 -L，不然拿到的是重定向页
sudo npm i -g @larksuite/cli --registry=https://registry.npmmirror.com
# pip：bootstrap.sh 默认已经指到阿里云镜像，可用 PIP_INDEX_URL 覆盖
```

| # | 做 | 验收（做完立刻确认，别攒到最后） |
|---|---|---|
| 0 | 客户侧建飞书**自建应用**、开权限（见上面的权限台账）、**事件与回调两栏分别订阅且都选长连接**、**发布版本** | 控制台能看到已发布版本；漏发版本时行为与「没配」一模一样，排查顺序永远是先确认版本 |
| 1 | 装 node + lark-cli（见上）；`sudo ./deploy/bootstrap.sh <租户名>` | 脚本自己会报每一步是新建还是跳过（幂等，重复跑不覆盖 env 与凭证） |
| 2 | 配 profile（secret 走 stdin）：`lark-cli profile add --name <租户> --app-id cli_xxx --app-secret-stdin` | `lark-cli --profile <租户> auth status --json` 的 `appId` 对得上、`identities.bot.status == ready` |
| 3 | 填 `/srv/larkflow/<租户>/larkflow.env` | 至少 `LARK_PROFILE` / `LARKFLOW_APP_ID` / `LARKFLOW_ROLES` / 一组 `LLM_*` |
| 4 | `larkflow --env-file <那个文件> doctor` | 全绿或只剩 ⚠️。**这一步是整套 runbook 的意义所在**：它把「起不来 / 起来了但静默不干活」的已知成因一次查完，而不是到现场逐个撞 |
| 5 | `sudo systemctl enable --now larkflow@<租户>` | `journalctl -u larkflow@<租户> -f` 看到启动对账跑完 + 入站通道就绪 |
| 6 | 起一条真实例走一遍 | `larkflow start …` → 人在飞书里收到卡 → 点一下 → `larkflow status` 变了 |

**演练时验到的正常失败长什么样**（假凭证下，第 5 步）：ExecStartPre 的 doctor 输出整段进 journald，接着 `启动对账：实例 0`，然后 `故障 startup: RuntimeError: event consume card.action.trigger 未在 30.0s 内就绪`，`已停止（干净）`，退出码 1，systemd 5 秒后重启。**这是好的失败**：响、有指向、退出码非 0。真凭证下这一条应该变成入站通道就绪且不再退出。顺带这次也在真 Linux 上验到了 `已停止（干净）`，即 ADR-044 的进程组修复成立（Mac 上那条路一直报「10s 内没排空」）。

**踩过的两个 systemd / 打包坑**（已修，写在这里免得下次重踩）：
- `StartLimitIntervalSec` / `StartLimitBurst` **必须写在 `[Unit]` 段**。写进 `[Service]` 时 systemd 249 只对前者报 `Unknown key name ... ignoring`、却把后者收下，于是配上默认的 10 秒窗口，而每轮失败要 9 秒，永远凑不满次数：实测连重启 16 次仍在 `activating`。改到 `[Unit]` 后 10 次即停进 `failed`，日志写 `Start request repeated too quickly`。
- **模板 yaml 要在 `pyproject.toml` 里显式声明 package-data**，否则 `pip install` 装出来的包里 `templates/` 只有 `__init__.py`，`load_template` 抛「模板文件不存在」，引擎一条流程都跑不起来。从源码树跑永远发现不了。`tests/test_packaging.py` 现在钉着它。

**`auth status` 是纯本地判断**（拿伪造 secret 建的 profile 它照样说 `ready`）：它证明「配成了哪个 app」，不证明「凭证还能用」。真正的凭证验证只能靠第 6 步跑一条真链路。

**`ExecStartPre` 用 `-` 前缀跑 doctor**：失败也不挡启动。这是有意的，长连接会静默死亡、`Restart=always` 不能被一次体检失败卡住；但每次启动都把体检结果留进 journald，出事时第一屏就是「当时配置长什么样」。doctor 全程本地只读，不会因为网络抖动假红。

**多租户时最容易犯的错**：复制 systemd 单元忘了改 `LARK_PROFILE`。写错 profile 名 lark-cli 会 fail loud；写成**另一家的合法 profile 名**不会，那就是无声的跨租户串号。`LARKFLOW_APP_ID` 这颗钉子专治这个，doctor 每次启动都对一遍。

## 进程守护要点（细节已落在 `deploy/larkflow@.service`）
- `Restart=always` + `RestartSec=5`。**不是可选项**：长连接没有队列，daemon 不在线时人点按钮当场失败且不补投。同时给 `StartLimitBurst=10` / `StartLimitIntervalSec=300`，配置写错时停进 failed 状态说人话，而不是每 5 秒假装重启。
- `TimeoutStopSec=330`：停机是「停订阅 → 等在飞的那条事件跑完 → 关 DB」，一条事件里可能正在跑 LLM（默认超时 300s）。没排空 daemon 会拒绝关 DB 并以非 0 退出，那是设计如此。
- `lark-cli event consume` 子进程的拉起与断线重启由 daemon 自己管（退避重启 + 上限，达上限会喊出来），systemd 只管 daemon 本身。
- 观测：目前只有 stderr 日志 + 进程内计数（`server.stats` / `server.errors`），**没有 HTTP 探针、不落盘指标**（ADR-007 无入站端口下有意为之）。运维靠 `journalctl` 与 `larkflow status <实例>`。
- 备份：直接备份 SQLite 文件（WAL 模式下连 `-wal` / `-shm` 一起，或先 `sqlite3 .backup`）。锁文件（`<DB>.locks/`、`<DB>.serve.lock`）不必备份，已进 `.gitignore`。

## 还缺什么才能真跑（宿主侧已就位；缺的全在飞书那一侧 + 真凭证）
1. **建 dev 飞书自建应用**（ADR-008 独立租户）：权限（`task:task` / `im:message` / `docx:document` + `drive:drive`）、**事件与回调 → 回调配置里开 `card.action.trigger`**（不开就一个按钮点击也收不到，且消费端不报错、只是永远静默）、事件订阅方式选长连接、`lark-cli auth login` 把凭证配进 profile。
2. **配 env**（上一节清单）+ 角色 open_id 映射齐全。
3. **在宿主上真起一次 `larkflow serve`**：systemd 单元没写过、没跑过；`build_real_service` 这条路**零测试覆盖**（红线：测试绝不构造真栈，只把它的调用方测穿了）。
4. **盯两处第一次上线才见得到的东西**：① `lark-cli event consume` 的真实 NDJSON 与 `normalize_event` 的字段假设（`action_value` 是 JSON 字符串、任务事件根在 `.event`、身份取顶层 `operator_id`）对不对得上，对不上只改这一个纯函数；② 新增的 state channel（`unblocks` / `escalations`）在旧 checkpoint 里不存在，读侧全走默认值、`update_state` 会补，但从没在真库上回放验证过（全程 `:memory:`）。
5. 妙搭前端的三命门（ADR-019）与前端↔引擎传输方式，仍是 0/3。

## 待落地后填
- 升级 / 回滚流程（当前只有「换代码 + 重启 daemon」，没有 schema 迁移方案：state channel 是新增字段，旧实例靠默认值兜）。
- 内存吃紧的观测阈值与升配 / 迁 Postgres 触发条件。
- 妙搭前端部署（本地开发 → 发布 `aiforce.cloud`）+ 引擎读 / 命令 API 的暴露方式（ADR-019）。
- 意图路由层（v1.1，ADR-021）的进程拓扑与守护配置（复用已列 LLM 多角色路由，无新增外部依赖）。
- 事件级去重：飞书是 at-least-once，目前靠陈旧判定挡住重放（够用但不是显式幂等），真栈重连后的重放行为未验证。
