# CHANGELOG · larkflow

## v0.70.0-draft · 2026-08-09 · 员工工作台受控流程发起

- Added：内容提交 `432fea77c210e7a2cfa5344054eb30d01706bf87` 在工作台增加“发起流程”。员工填写目标、可选背景与可选协作者后，请求先进入 PostgreSQL 耐久队列；不持有飞书凭据的中央草稿 Worker 生成并确定性校验最多八个 Human 或 Agent 节点的候选 DAG。成功后页面自动打开既有草稿预览，只有本人再次确认才启动。
- Safety：服务端从当前飞书会话取得 tenant 与 requester，浏览器不能声明可信身份或 Owner。32 位 request ID 保证重复提交幂等；`FOR UPDATE SKIP LOCKED`、短租约和稳定实例 ID 保证双 Worker 只有一路领取。候选在创建实例前先冻结到请求记录，崩溃接管不会再次调用模型。确定性拒绝进入 `rejected`，基础设施失败最多尝试五次后进入 `exhausted`，原始错误不返回前端。
- UI：页面提交前立即显示“正在进入生成队列”，随后轮询 `queued / generating / repairing / creating / retrying`，并明确收口为可打开草稿或安全失败。最近请求保留在独立列表。浅色、深色与 390 像素移动视口已完成本地真实 HTTP 验收，无横向溢出；失败后按钮可重新提交。
- Verified：定向套件为 `69 passed`。首次完整运行因继承本机 SOCKS 代理出现两个环境性失败，清空六项代理变量并允许进程树读取后，完整离线套件为 `1015 passed, 23 skipped`。最终 wheel 包含 `console_drafts.py`、`0023_console_draft_requests.sql` 和三项静态资源，SHA-256 为 `6b320b22804c02eaa2840d9a101bcf1b4ffe75287509816486727588ccdc0198`。
- Deployment：发布件位于 `/srv/larkflow/target/releases/20260809_0357_console_drafts_432fea7/`。升级前 custom-format 备份为 285292 bytes，SHA-256 为 `ffb091224db79ab6bab92b9f42ad0b3af8963c1380f940b5fad52329777b1b61`，并已通过 `pg_restore --list`。长期库应用 `0023_console_draft_requests`，migration 总数为 23。真实 PostgreSQL 双连接竞争只得到一个 `generating / attempt 1` claim，测试行清理一条。十个 Python 服务与 Caddy 均为 `active / NRestarts=0`；公网 200、未登录草稿 API 401、安全响应头、安装资源哈希与部署窗口零 warning 均已回读。
- Boundary：当前证据关闭代码、持久化、竞争、部署、本地界面和真实成员主体 API 到中央模型的门槛。真实浏览器仍停在飞书授权入口，尚未完成可见点击验收；本版本也不证明模型内容质量、生产容量或组织采用。
- Acceptance addendum：同一真实成员主体签发的五分钟临时会话，以纯合成会议摘要文本调用正式草稿 API。首次请求因未显式携带空协作者字段返回 400，临时会话已注销且没有落库；补齐 `collaborator_person_id: null` 后，请求从 `queued` 一次领取进入 `ready`，生成三节点草稿 `console_draft_0dc8215ed17b411288ca451be615a074`。独立数据库回读为 `draft / version 0 / template_version_id NULL / confirmed_at NULL / 0 NodeInstance / 0 Attempt / 0 Projection`，第二条临时会话也已注销。验收文本不含内部提交、迁移、人员信息或真实业务数据。

## v0.69.0-draft · 2026-08-09 · 真实 Human Task 转交与异步状态表达

- Fixed：内容提交 `3fd42df8740825482eb3bbebd5cf69715f37df5b` 把中央转交事务与飞书 Task 投影分开表达。转交响应新增 `projection.kind=feishu_task / status=queued`；页面立即显示“中央已转交，飞书同步中”，不再把 outbox 入队描述为外部负责人已经更新。异步失败继续由既有有界重试和管理员异常聚合承接。
- Acceptance：真实登录浏览器分别完成一次普通 Human 节点提交和一次跨成员转交。提交后中央流程继续调度；转交后旧负责人立即失权。精确所需 Task 权限开通后，既有同步事件在第 8 次尝试发布。飞书 Task 回读为 `todo / mode=1 / 单一负责人`，Task 负责人、中央 NodeInstance Owner 与 Projection Owner 一致，`sync_version=2`。
- Verified：聚焦套件为 `41 passed`。首次全量运行暴露本机 SOCKS 代理注入和沙箱禁止 `ps` 的三个环境性失败；清空六项代理变量并允许进程树读取后，完整套件为 `1005 passed, 22 skipped`。Git whitespace 和 wheel 内外三项文件哈希一致。
- Deployment：wheel SHA-256 为 `79ac572f4feb160db835d8a26b25d77b84e57916542367c347f0df0b65426ee1`，位于 `/srv/larkflow/target/releases/20260809_0259_transfer_sync_3fd42df/`。升级前备份为 285292 bytes，SHA-256 为 `f3593021322de5b87aace25b61b8b3edf711dbb42c9726efae054f2d7438357d`；migration runner 返回空集，ledger 保持 22 份。本次只重启 Console；十个 Python 服务与 Caddy 均为 `active / NRestarts=0`，公网与 loopback 200、未登录 401、安全响应头、静态资源哈希和零 warning 均已读回。
- Boundary：这关闭开发测试组织中的普通 Human 页面提交和跨成员 Task 转交门槛，不等于生产容量、正式域名、异机容灾、分布式限流或完整协作者实例视图已经完成。

## v0.68.0-draft · 2026-08-09 · Projection outbox 有界终止

- Fixed：内容提交 `ed118e7b3a9eeb5b5daed52e3d7b0296896f12f1` 为 Projection outbox 增加默认 24 次尝试上限。达到上限的外部投影失败原子进入 `exhausted`，停止再次领取，同时保留事件内容、累计尝试次数、最后错误和终止时间；临时失败继续使用原有指数退避。
- Data：migration `0022_outbox_exhaustion` 扩展 outbox 状态约束并增加 `exhausted_at` 与状态形状检查。领取 SQL 仍只接受 `pending`、`failed` 或租约过期的 `processing`，所以终止记录不会重新进入租约竞争。管理员队列聚合和 Projection 结构化日志独立暴露终止计数。
- Verified：完整离线套件为 `1005 passed, 22 skipped`。一次性真实 PostgreSQL 应用 migration 22 后，两个连接竞争同一投影事件的领取结果为 `1 / 0`；唯一领取者写入终态后，未来一天再次领取为 0，错误与尝试次数保留。测试库和临时文件均已删除并回读不存在。
- Deployment：wheel SHA-256 为 `a9f68581294ac65e71b2eae5f97940618289194eedd77c5943c40f539e4f6245`，位于 `/srv/larkflow/target/releases/20260809_0201_outbox_exhaustion_ed118e7/`。升级前备份 SHA-256 为 `a75bdd632f7e14b7df1e22616741dbd03e834ccb438c9ec07d75ddb5d18f5ecf`，长期库应用第二十二份 migration。两条历史永久失败投影从 1171 次失败进入 `exhausted / 1172`，日志回读 `claimed=2 / failed=2 / exhausted=2`；十个 Python 服务与 Caddy 均为 `active / NRestarts=0`，公网 200、未登录 401 与安全响应头正常。
- Boundary：终止状态阻止无意义的永久重试，但当前没有 Console 队列重放或处置入口。需要人工修复外部身份或配置后重放的场景仍需后续设计版本绑定、权限校验和审计，不能直接把 `exhausted` 改回 `pending`。

## v0.67.0-draft · 2026-08-09 · Human Task 页面提交与转交

- Added：内容提交 `3d438bb476ad9b9f98cd4c2873802a2894718fe4` 新增参与者范围的任务列表、任务详情、结果输入框、提交和转交接口。参与者只读取分配给自己的普通 Human Task 有界上下文，不能打开其他 Owner 的完整实例；明确接受或退回的决定节点继续使用飞书决定卡。
- Domain：转交绑定当前 Attempt 与节点版本，服务端重新校验当前负责人和目标活跃成员。事务只修改运行时 `NodeInstance.owner_person_id`，不改写冻结 `InstanceSnapshot`；旧负责人立即失去提交权限，并追加 `node.human_task_transferred` 审计与 outbox。Projection 使用稳定幂等键更新既有飞书 Task 负责人。
- UI：本人待处理页合并 Owner 关注项与参与者任务。任务弹窗展示目标、验收条件和有界上下文，允许填写结果或选择成员转交；按钮在请求发出前立即显示提交中、加载成员中或转交中。通用流程输入框仍保持后置。
- Verified：聚焦测试与完整离线套件通过，完整结果为 `1003 passed, 22 skipped`。真实 PostgreSQL 双连接竞争只有一路转交成功，审计与 outbox 均恰好一条，冻结 Snapshot Owner 不变，运行时 Owner 已改变，新负责人可见任务。JavaScript 语法、Python 编译、Git whitespace 与敏感文字扫描通过。
- Deployment：wheel SHA-256 为 `8373a9f18377abf7068b53e362158714168078327934d44ab9d3b3330f75e736`，位于 `/srv/larkflow/target/releases/20260809_0120_human_tasks_3d438bb/`。升级前备份 SHA-256 为 `3e6511c0e1a622e63ac53e08a3402709daaaa7095eca09eb7caca2438623e499`。migration ledger 保持二十一份；十个 Python 服务与 Caddy 均为 `active / NRestarts=0`，部署窗口 warning 为 0。公网静态资源哈希、安全响应头、未认证 401、登录态任务与成员目录 API 200、临时会话撤销失效均已读回。一次性 PostgreSQL 库与临时文件已删除。
- Boundary：当前开发库没有自然等待中的普通 Human Task，所以本版本尚未完成真实浏览器结果提交和真实飞书 Task 转交。它不包含通用流程输入框、决定节点网页提交、完整协作者实例视图、生产容量、正式域名或生产发布。

## v0.66.0-draft · 2026-08-09 · Owner 工作台受控流程操作

- Added：内容提交 `da94891f5e6d01ecee6082a98bab6148abba12ee` 新增 `ConsoleActionService`，把当前服务端鉴权主体映射到既有 `WorkflowService`。草稿确认、暂停和继续可以直接执行；取消使用 aggregate version 预览确认；节点与完整实例重启复用耐久 RestartPreview。Human 正文与最终判断仍在飞书责任入口完成。
- Safety：所有工作流 POST 都拒绝 query、请求体、非零 `Content-Length` 与 `Transfer-Encoding`，并要求 `X-Larkflow-Console-Action: workflow-action-v1`。`feishu` 模式还要求 `Origin` 精确等于配置的公网 origin。服务端重新校验 tenant、Instance Owner、当前状态、版本和预览；跨 Owner 与不存在实例统一返回 404，状态漂移返回 409，重复确认复用领域层幂等语义。
- UI：本人待处理卡和详情操作栏在请求发出前立即进入“正在执行”或“正在生成预览”。取消和重启在同页列出影响节点，再要求明确确认；旧 Attempt、结果与审计保留。浅色与深色主题均完成本地视觉检查，检查发现并修正浅色模式主要操作按钮对比度，浏览器错误与告警为 0。
- Verified：工作流操作、Console、限流、鉴权、管理员会话与部署聚焦套件为 `53 passed`；清除本机代理变量并允许进程树检查后的完整离线套件为 `995 passed, 21 skipped`。JavaScript 语法、Git whitespace、敏感文字扫描均通过。干净 wheel SHA-256 为 `fca2eee16d3af57dcfb4bb78409a0b6f9e23b7d3d29aa7d7435cc1f26dd3063a`，wheel、临时安装、服务器安装与公网下载后的操作服务和三项静态资源哈希逐项一致。
- Deployment：发布件位于 `/srv/larkflow/target/releases/20260808_235309_console_actions_da94891/`。升级前备份 `/var/backups/larkflow-postgres/larkflow_target_dev-20260808T235220+0800.dump` 为 259098 bytes，SHA-256 为 `459ecac443e2eb6cad9034691fa07b21dec947b7944213ed6e88f5cdbca4db7e`，权限为 `0600 lf_target_dev:lf_target_dev`；migration runner 返回空集，ledger 保持 21 份。本次只重启 Console，九个 Target 服务、legacy 消费者与 Caddy 均为 `active / NRestarts=0`，公网和 loopback 页面返回 200，未认证读取与工作流 POST 返回 401，公网安全响应头齐全，部署窗口 warning 为 0。
- Boundary：本轮关闭的是开发环境中“复制命令再去飞书发送”的交互绕行和服务端写入边界。尚未使用真实登录 Owner 对已部署版本执行首轮确认、暂停或继续，以及取消或重启；也不包含 Human 正文提交、运行中图编辑、协作者视图、生产容量、正式域名或生产发布。
- Acceptance addendum · 2026-08-09：真实登录 Owner 已在公网工作台直接确认并启动 `internal_trial_20260808_155244`。三个节点均在 Attempt 1 完成，实例终态为 `done / version 7`；两个飞书 Task、Agent 结果消息、完成文档与最终通知均已外部绑定。该补充证据只关闭首个真实登录 Owner 写操作门槛，不改写本版本发布时的 Boundary，也不宣称其他网页操作已逐项验收。

## v0.65.0-draft · 2026-08-08 · Owner 工作台重构与双主题

- Changed：内容提交 `e3bd98d155a446a66bdb2c947e124f7ba7fc9c31` 重构 Owner 中央工作台的信息架构。默认入口改为按优先级分组的本人待处理队列，流程库独立承载全部流程，详情页拆分为概览、执行过程和审计三个页签；状态、责任人、进度和下一步行动在首屏直接可见。
- Accessibility：全局正文、标签、按钮与表格字号上调，并增加浅色和深色主题。首次访问跟随操作系统，用户选择只保存为浏览器本地偏好，不进入服务端会话、流程数据或审计。
- Safety：Owner 领域接口继续只读，身份、租户、Owner 可见性、管理员 allowlist、会话治理和写操作边界均未改变。前端仍不提供确认、重启、编辑或人类决定操作。
- Verified：JavaScript 语法检查通过，Console 聚焦套件为 `16 passed`，完整离线等价结果为 `988 passed, 21 skipped`。干净 wheel SHA-256 为 `7b7b5318c4f94b096210629f5c94db1d2369ee2cb94f5e66cfc146ab8a2a5178`；wheel、临时安装和服务器安装后的三项静态资源 SHA-256 与源码逐项一致，服务器 `pip check` 无断裂依赖。
- Deployment：发布件位于 `/srv/larkflow/target/releases/20260808_231502_console_ui_e3bd98d/`。升级前备份 `/var/backups/larkflow-postgres/larkflow_target_dev-20260808T231502+0800.dump` 为 259027 bytes，SHA-256 为 `3018163deb374eb4a7376e56c26eb381e6b78772cd267451adc9dfa742e5ff3a`；migration runner 返回空集。本次只重启 Console，九个 Target 服务、legacy 消费者与 Caddy 均回读 active，Console `NRestarts=0`。公网与 loopback `/console/` 返回 200，未认证业务与管理员 API 返回 401，公网安全响应头齐全，部署窗口 warning 为 0。
- Boundary：该版本是开发环境界面发布，尚未完成部署后真实登录浏览器的新版视觉复验，不构成生产发布、稳定内容质量或市场价值证据。

## v0.64.0-draft · 2026-08-08 · 来源约束型决策契约

- Signal：真实内部样本 `im_fb85651d34e24c9789304715` 完整进入 Human 决定后被 Owner 退回。旧 `source_claims.v1` 按设计保留开放问题，因此没有回答 Q1、Q2、Q3；该结果证明材料复核契约不能代替决策契约。退回意见、旧 Agent 与 Tool 结果和审计均保留。
- Added：内容提交 `db7651228e26055eb1229ae9f451e3e87c31df38` 新增 `source_grounded_decision` 模板、`source_decision.v1` Agent 结果与 `source_decision.check`。结果必须只给一项优先级，回答每个登记 Q，提供 3 到 5 条完成标准、不做事项、重新评估条件和风险；所有条目只引用登记 F，并以建议推断展示。确定性 Tool 检查结构、覆盖和来源 URL，不判断事实真伪或建议正确性。
- Fixed：Human 责任卡的结构化输入与依赖结果改用 JSON 代码块，避免 Card Markdown 把 URL 后的 JSON 引号编码为链接末尾的 `%22`。普通字符串正文保持原有展示与长度限制。
- Compatibility：旧 `source_grounded_review`、`source_claims.v1` 与 `source_claims.check` 保持原语义和兼容性，不会被自动迁移或重启。
- Verified：Agent、Tool、模板和决定卡聚焦套件为 `44 passed`。完整离线套件在沙箱内除既有进程树权限用例外为 `987 passed, 21 skipped`，该用例在沙箱外单独通过，因此完整等价结果为 `988 passed, 21 skipped`。从内容提交构建的干净 wheel SHA-256 为 `54a4bbf4c96834d7d69a3434d01b083d2467f5df6dd129c9ac6e35876efb49ff`，位于 `/srv/larkflow/target/releases/20260808_040000_source_decision_db76512/`；新旧来源模板均在包内，`pip check` 无断裂依赖。`source_grounded_decision:1` 已启用，内容哈希为 `0bf7d5495c9a7d043c0fd0150049712f46ce9116b44cba7776ce6f3889ac2490`。真实实例 `source_decision_20260808_0405` 的四个节点均在 Attempt 1 完成；Agent 回答 Q1、Q2、Q3，Tool 覆盖 6/6 个 F 和 3/3 个 Q、零违规且 `verdict=pass`。Owner 接受后实例为 `done / version 9`，接受审计恰好一条，完成文档、最终通知和各外部投影均有绑定。飞书服务端回读终态卡为已接受、无按钮且无 `%22`。
- Boundary：该版本已完成开发环境部署与真实飞书复验，不增加 migration，不证明 Agent 建议正确、内容质量规模化、市场价值、生产容量或生产上线。

## v0.63.0-draft · 2026-08-08 · 管理员 allowlist 运维与会话恢复门禁

- Added：内容提交 `c1340ca21f13ed3f543df8f1411b94e46d9e6b7e` 增加 root 侧 `larkflow-console-admin-allowlist`。操作者只能用当前租户内仍有效的 Console 会话创建十分钟预览，不能直接提交 person ID；确认时重新校验会话、tenant、env SHA-256 和 allowlist。`abc4f5e7ad8c3617cef641efc01523055e9b695e` 修正 `psql --command` 不执行变量替换的问题，`00b3c8f920e6b856d11d9d4a91678959de3da6a5` 区分未检查健康与服务异常。
- Safety：工具拒绝移除最后一名管理员，以同目录临时文件、`fsync` 和 `os.replace` 原子更新 env，并保留原权限、所有者和独立备份。实际变更后重启 Console，回读页面、鉴权和未认证管理员端点；任何失败都会恢复原 env 并再次验证，已应用操作还可在 env 指纹未漂移时显式回滚。预览与历史为 `0700 / 0600 root:root`，公开输出和追加型审计不含人员 ID。
- Verified：定向套件为 `17 passed`，清除本机代理并允许进程树检查后的完整离线套件为 `982 passed, 21 skipped`。回归覆盖实际添加、无变化确认、重复确认、最后一名管理员保护、陈旧和过期预览、健康失败自动恢复、显式回滚，以及生产 `psql` 标准输入适配器。
- Deployment：服务器脚本 `/usr/local/sbin/larkflow-console-admin-allowlist` 的 SHA-256 为 `072dc2c6e6f1d144fb2783f11c580af0dfe2892091455f452ccebb19f3f68f2c`。当前真实管理员的重复添加返回 `confirmed_no_change`，env 指纹和 Console 重启计数保持不变；移除唯一管理员被拒绝。loopback 与公网页面均返回 200。本轮没有给其他成员提权，因此真实服务器上的实际变更、重启与人工回滚路径仍待明确授权对象出现时验证。
- Recovery：新备份 `/var/backups/larkflow-postgres/larkflow_target_dev-20260808T021324+0800.dump` 为 238220 bytes，SHA-256 为 `8ca267b7e1f28fefb2ef030aab8668e49c1fc64af637e86e8282efb8400612e4`。固定名称隔离库恢复后，21 份 migration、22 张表、55 个流程实例、1 条有效 Console 会话、1 条撤销预览和 1 条撤销事件与源库一致；对象所有者、PUBLIC ACL、UTC 和三项 timeout 全部通过。暴露前清空会话后，撤销审计、流程与 migration 均保留；隔离库随后已删除，源库和备份未改变。
- Boundary：备份会原样保留仍有效的 Console 会话。恢复副本接入任何服务前默认必须清空会话并强制重新登录；若要作为同一安全域的权威替换保留会话，必须单独做事故决策。备份仍只在同一系统盘，没有 PITR、异机副本或生产容灾。本版本不增加员工前台提权入口，也不代表产品已生产上线。

## v0.62.0-draft · 2026-08-08 · 公网 Console 边界加固 v0

- Added：内容提交 `66b2c12d3ea27a61e5a1cdc21332ed03adb516ac` 为 `feishu` Console 增加读取、认证、管理员写入和全局四类有界令牌桶。来源地址只以进程随机密钥生成的 BLAKE2s 摘要保存在最多 10000 个 LRU key 中；429 返回固定安全 JSON 与 `Retry-After`。
- Security：Caddy 无条件覆盖 `X-Larkflow-Client-IP`，Console 只在 immediate peer 为 loopback 时信任该头，且来源绝不参与身份或授权。Caddy 同时限制 64 KB 请求体、32 KB 请求头，设置 10 秒请求头、15 秒请求体、30 秒写入和 2 分钟空闲超时，关闭 0-RTT，并补齐 HSTS、CSP、Permissions-Policy、COOP、CORP、拒绝 framing、`nosniff` 与 `no-referrer`。OAuth callback 仍不启用访问日志。
- Verified：限流与部署聚焦套件为 `14 passed`，完整离线套件为 `972 passed, 21 skipped`。故意移除 loopback 代理头信任后，定向测试按预期失败；恢复实现后通过。最终 wheel 已确认包含 `console_rate_limit.py`、Console 静态资源和全部模板，安装态导入与资源读取通过。
- Deployment：wheel SHA-256 为 `3ff1d97317bf4c72e4040622e747bc16d7ca98709ecf2525371f894b9fa1b9df`，保存在 `/srv/larkflow/target/releases/20260808_004500_console_public_66b2c12/`。env、Caddyfile 和 systemd unit 均创建带同一发布标记的可恢复备份，上一版 wheel 继续保留。本次没有 migration，只重启 Console 并 reload Caddy；十个 Python 服务与 Caddy 均保持 `active / NRestarts=0`，部署窗口 warning 为 0。
- Acceptance：公网与 loopback `/console/` 均返回 200，未认证管理员接口返回 401。31 个并发认证请求分别携带不同伪造来源值，仍共享 Caddy 覆盖后的预算，结果为 30 次 200 和 1 次 429，`Retry-After: 2`。公网安全响应头和 Caddy 运行时超时均已回读，原有真实登录会话仍为一条。上一版本待完成的会话治理面板也已由用户在真实登录浏览器中完成视觉验收。
- Boundary：该版本只关闭单机开发入口的基础滥用防护与浏览器响应头缺口。进程内预算会随重启重置，也不跨副本共享，不能替代生产容量测试、上游 DDoS 防护、正式域名、跨区域容灾或生产发布。下一步转向 allowlist 变更运维和包含 Console 会话表的恢复演练。设计理由见 ADR-098。

## v0.61.0-draft · 2026-08-07 · 管理员会话治理 v0

- Added：内容提交 `8ba0ab9d93554b7958a650492e0282ad40db0d2e` 为管理员增加当前企业有效 Console 会话列表，以及其他会话撤销的五分钟耐久预览和显式确认。列表只返回安全会话 ID、`you / member` 关系、创建时间、过期时间与当前会话标记；当前浏览器会话只能注销。
- Safety：确认在同一 PostgreSQL 事务中锁定预览与目标会话，原子删除目标、消费预览并追加不可变事件。状态漂移、过期和目标缺失返回 409；已消费预览重复确认返回相同成功结果。飞书会话的管理 POST 拒绝 query、请求体、`Content-Length` 和 `Transfer-Encoding`，并要求精确同源 `Origin` 与专用动作头。普通成员与不存在路由同样返回 404。
- UI：会话治理按钮在发出请求前立即进入“创建预览中”或“正在撤销”，随后明确显示等待确认、已撤销或错误。该面板不显示人员 ID、凭据或会话摘要，也不扩展到 allowlist、队列、配置或流程领域写操作。
- Verified：会话治理聚焦套件为 `29 passed`，JavaScript 语法、Python 编译与 Git whitespace 检查通过；清除本机代理并取得进程树检查所需权限后，完整离线套件为 `965 passed, 21 skipped`。一次性真实 PostgreSQL 应用二十一份 migration，双连接竞争同一预览得到一路执行、一路幂等回放，审计只有一条且更新删除均被拒绝；测试数据库与临时文件随后清理并回读为零。
- Deployment：候选 wheel SHA-256 为 `b2cff677a419f7151f6ceb6dc8986fcd061999406cbd8212ac2cdde7504fecc8`，保存在 `/srv/larkflow/target/releases/20260807_212230_session_gov_8ba0ab9/`。升级前备份 `/var/backups/larkflow-postgres/larkflow_target_dev-20260807T212320+0800.dump` 成功，大小 230122 bytes，模式 `0600`。长期库已应用 `0021_console_session_governance`，本次只重启 Console；十个 Python 服务与 Caddy 均保持 `active / NRestarts=0`，部署窗口无 warning。
- Acceptance：真实 HTTP 验收返回列表 200、当前会话撤销拒绝 409、预览 201、确认 200、重复确认幂等、被撤销会话 401、普通成员 404 和一条撤销审计。短期验收会话已清理，原有真实登录会话仍保留。新面板的真实登录浏览器视觉验收仍待完成。
- Boundary：该版本只关闭开发环境的单会话集中撤销与审计缺口，不等于通用可写管理员后台或生产发布。allowlist 自助管理、批量撤销、设备命名、队列处置、正式域名、生产限流、安全响应头回归、跨区域容灾和容量验证仍未完成。设计理由见 ADR-097。

## v0.60.0-draft · 2026-08-07 · 管理员只读概览

- Added：内容提交 `e15f47942fcc01bc85ecbbfa822acd00558c06f0` 在现有飞书身份与 PostgreSQL 耐久会话上增加当前企业管理员只读概览。`GET /console/api/v1/auth` 返回服务端计算的 `admin` 布尔值；授权后页面显示“管理概览”，读取流程状态、会话、migration 和七条耐久队列聚合。
- Safety：管理员资格只由受限服务器 env 中的 `tenant + person` allowlist 计算，浏览器不能提交身份或权限。普通成员访问管理员路由与未知路由同样返回 404。仓储查询全部绑定当前 tenant，响应不含人员 ID、原始错误、payload、claim 或单条敏感记录；页面没有会话撤销、队列重放、配置修改和流程写操作。
- Verified：Console 管理、HTTP、CLI 与 PostgreSQL 聚焦套件为 `44 passed`，JavaScript 语法与 Git whitespace 检查通过；清除本机代理并取得进程树检查所需权限后，完整离线套件为 `960 passed, 20 skipped`。候选 wheel 已确认包含 `console_admin.py`、静态资源和 migration `0020_console_sessions`。
- Deployment：升级前数据库备份返回 `Result=success / ExecMainStatus=0`，env 备份为 `/etc/larkflow-target-console.env.bak.20260807_204031_admin_e15f479`。wheel SHA-256 为 `fbdd2e325d57fb595362c4aac8c32b10ae734843014c4bbef2da71480bbe418b`，保存在 `/srv/larkflow/target/releases/20260807_204031_admin_e15f479/`。本次只重启 Console，十个 Python 服务与 Caddy 均保持 `active / NRestarts=0`，公网工作台返回 200，验收窗口无 warning。
- Acceptance：真实 HTTP 验收回读管理员 200、普通成员 404、`read_only=true`、`scope=current_tenant`、七条队列、55 个流程和二十份已对齐 migration。两条短期验收会话已撤销，原有真实登录会话仍为一条；聚合响应不含真实 person ID。真实登录浏览器中的管理员页签视觉验收仍待完成。
- Boundary：当前只关闭开发环境管理员观察面和 HTTP 权限隔离，不等于管理员会话治理或生产发布。allowlist 自助管理、集中会话撤销、队列处置、正式域名、生产限流、安全响应头回归、跨区域容灾与容量验证仍未完成。设计理由见 ADR-096。

## v0.59.0-draft · 2026-08-07 · PostgreSQL 耐久员工工作台会话

- Changed：内容提交 `a6f5babb07623590e9be2a2b8c523857cce56ff7` 把完成登录后的 Console 会话从单进程内存迁入 PostgreSQL。浏览器继续只持有随机不透明 HttpOnly 凭据；migration `0020_console_sessions` 只保存 SHA-256 摘要、tenant、person、创建时间和过期时间，不保存原始凭据或飞书用户 token。
- Safety：签发事务使用 advisory lock 串行化过期清理、全局 10000 条容量约束和新摘要写入；摘要冲突不会覆盖旧主体。认证只接受未过期摘要，过期记录会被删除；注销删除当前摘要。五分钟 OAuth 发起态仍为短期进程内状态，Console 在授权中途重启时需要重新发起授权，但不影响已经完成的登录会话。
- Verified：完整离线套件等价结果为 `955 passed, 19 skipped`。一次性真实 PostgreSQL 应用二十份 migration，验证认证器重建后会话有效、数据库不含原始凭据、注销立即失效和过期记录清理；一次性数据库与远端临时文件随后删除并回读为零。
- Deployment：升级前备份 `/var/backups/larkflow-postgres/larkflow_target_dev-20260807T195221+0800.dump` 成功，大小 227704 bytes。wheel SHA-256 为 `a3b680c0a76545ab25a6c62ad500c9a2db0e24b2aac890eb4a1b708bc5fea729`，保存在 `/srv/larkflow/target/releases/20260807_195154_console_sessions_a6f5bab/`。长期库应用 `0020_console_sessions` 后回读二十份 migration、三个会话表索引和正确所有者；本次只重启 Console。
- Acceptance：真实成员重新授权后，PostgreSQL 回读一条有效且符合 64 位摘要约束的会话。Console 重启后同一记录仍有效，公网与 loopback `/console/` 均返回 200；用户直接刷新当前工作台仍保持登录。Console 与 Caddy 均为 `active / running / NRestarts=0`，验收窗口无 warning。
- Boundary：该结果关闭开发环境的单进程重启丢失登录态，不等于正式员工或生产发布。正式域名、生产限流、完整安全响应头、管理员后台、管理员级集中撤销、跨区域容灾与生产容量仍未完成。设计理由见 ADR-095。

## v0.58.0-draft · 2026-08-07 · 公网 IP 员工工作台与多成员隔离验收

- Deployment：内容提交 `fdbead1`、`ad13711` 与 `3916e24` 建立公网 IP TLS 入口并兼容无 SNI 客户端；`3fe8cd5` 使用飞书官方 OAuth v2 token 端点完成回调，`bc961b6` 在保持 Console loopback 监听的同时允许最小 OAuth 出站。Caddy 与 Console 均为 `active / NRestarts=0`，公网 `/console/` 返回 200，8780 继续只监听 `127.0.0.1`。
- Acceptance：飞书应用已同时发布网页应用与机器人能力，网页应用成为工作台默认入口。至少两名真实成员完成授权登录、本人 Owner 数据读取和跨 Owner 隔离验证；消息中的机器人命令入口继续可用。
- Security：飞书应用凭证已由官方令牌端点在服务器内验证；修复过程不输出 App Secret、用户 token、授权 code 或 cookie。Console env 保持 `0640 root:lf_target_dev`，公网反向代理不直接暴露应用监听端口。
- Boundary：该结果只关闭开发环境员工身份、网页入口和 Owner 可见性门槛。当前无正式域名，会话仍存于 Console 进程内存，服务重启后需要重新登录；管理员后台、跨副本会话、生产限流与生产发布均未完成。

## v0.57.0-draft · 2026-08-07 · 飞书应用内员工工作台登录

- Added：Owner Console 新增 `feishu` 鉴权模式，使用 OAuth v3 authorization code、PKCE S256、浏览器绑定 state、飞书 tenant 显式映射和不透明服务端会话。飞书 `open_id` 映射为当前 person；用户 access token 只在服务端读取一次用户信息，随后丢弃，不进入浏览器、日志或流程数据，也不依赖 `lark-cli` 用户登录。
- Security：OAuth state 单次且五分钟有效，回调只允许配置的 HTTPS origin。会话 cookie 使用 `Secure + HttpOnly + SameSite=Lax + __Host-`，服务端仅保存随机 token 的 SHA-256 摘要。用户 OAuth 不申请业务 scope，不保存 refresh token；其他飞书 tenant 拒绝映射。现有静态 Bearer 模式继续保留为 loopback 开发兼容路径。
- UX：页面改为“我的工作台”。`feishu` 模式下未登录用户自动进入授权链路，登录失败可重试，注销只结束当前 Console 会话；`static` 模式继续显示开发 token 入口。领域数据和可写边界不变。
- Verified：内容提交 `c2e9db99f4b463a895450371dde9b176d6c31ef1`。OAuth、会话、HTTP、CLI 与静态资源聚焦套件为 `31 passed`，JavaScript 语法与 Python 编译检查通过。移除本机代理后完整离线套件除沙箱禁止 `ps` 的单项外为 `952 passed, 18 skipped`，该进程树用例在沙箱外单独通过，因此完整等价结果为 `953 passed, 18 skipped`。候选 wheel SHA-256 为 `a0ce523fff41bd60004cb21c8f33689e7f979a45df2509c10c565c3cb8677669`，已确认包含新鉴权模块、CLI、HTTP 和三份前端静态资源。
- Boundary：本提交尚未推送或部署，开发服务器仍运行静态模式。当前没有可用公网 HTTPS、飞书应用主页与回调配置，也没有真实应用内登录、多成员 Owner 隔离或注销验收。会话存储是单进程内存，Console 重启会要求重新登录；管理员后台尚未实现。

## v0.56.0-draft · 2026-08-07 · Owner 待处理中心 v0

- Added：Owner Console 列表响应新增 `attention` 读模型，从同一 PostgreSQL 聚合派生失败恢复、本人 Human 待办、暂停继续和草稿确认四类提示。失败优先；多个失败节点统一建议完整实例重启。
- Safety：仓储先按 tenant、Instance Owner 和最多 100 个最近实例限制候选集，再只连接失败节点和归当前 Owner 的 `waiting_human` 节点。DTO 不返回人员 ID、原始错误、claim、凭据或 Audit payload。待处理数据不单独落库，不形成第二套业务状态。
- UI：页面新增待处理中心，可打开只读流程详情，或复制现有 `/larkflow confirm / resume / restart / restart-all` 命令。复制与打开按钮点击后立即显示处理中，随后显示成功或失败。浏览器没有新增写 API，真实命令仍由飞书入口重新授权并走既有预览确认链路。
- Verified：内容提交 `30dc7ee` 与 PostgreSQL Owner 查询边界加固 `b6eda8c` 已形成。Console 聚焦套件为 `16 passed`，JavaScript 语法检查与 Python 编译检查通过。移除本机代理环境后，完整离线套件为 `943 passed, 18 skipped`；唯一剩余的进程树用例因沙箱禁止 `ps` 单独在沙箱外重跑并通过，因此完整等价结果为 `944 passed, 18 skipped`。Git whitespace 检查通过。
- Deployment：内容提交 `b6eda8caaa06d338de8c5aa0283c3d787a8affe7` 的 wheel 已保存到 `/srv/larkflow/target/releases/20260807_010810_attention_b6eda8c/` 并安装到 Target 虚拟环境，SHA-256 为 `14cdbcfc5f343dc16d4985f62752ef7ab302cb6f20e8e1410eae7f7420befa3c`。升级前备份 `larkflow_target_dev-20260807T010925+0800.dump` 成功；migration runner 无待应用版本，长期库保持十九份 migration。本次只重启 Console，十个 Python 服务均为 `active / running / NRestarts=0`，5432、8765 与 8780 继续只监听 loopback。
- Acceptance：真实认证 API 回读最近 30 个本人流程和 22 条待处理项，其中失败恢复 2 条、本人 Human 1 条、草稿确认 19 条；响应不含配置的人员 ID。PostgreSQL 仓储直接查询返回 30 条有界候选、两条失败节点和一条本人 Human 等待节点。服务器安装态的三份静态资源与本地源码哈希一致；同一前端发布内容以非敏感测试身份完成浏览器功能验收，“查看流程”进入明确的“已打开”终态，1280 像素视口无横向溢出或浏览器错误。
- Boundary：本轮没有新增 migration，也没有把真实开发 token 注入自动化浏览器，因此不把浏览器部分描述为真实数据真机验收。待处理中心已经完成开发部署和真实 PostgreSQL 验收，但 Owner 不依赖开发者解释的独立使用仍未完成，更不构成生产上线证明。

## v0.55.0-draft · 2026-08-06 · 暂停、继续与安全取消

- Added：新增 Owner 范围的 `/larkflow pause`、`/larkflow resume`、`/larkflow cancel` 与 `/larkflow cancel-confirm`，命令总数增至十五个。暂停使用 drain 语义，只停止新节点调度；已发出的 Human、Agent 与 Tool 继续使用原 Attempt 收口，继续操作也不创建新 Attempt。
- Safety：取消先返回 aggregate version 绑定的完整影响预览，确认时重新校验 Owner 与版本。事务内把未完成 Node 和当前非终态 Attempt 置为 canceled，清除自动 claim，保留完成或失败节点、旧 Attempt、结果和追加型审计。Instance 终态、Node version 与 claim 撤销共同拒绝迟到结果；已经发生的外部副作用不自动回滚。
- Projection：取消通过 outbox 关闭已有普通 Human Task，并把已有 Human 决定卡替换为无按钮、无表单的“复核已取消”终态。重复 pause、resume 与 cancel-confirm 不增加版本或审计。
- Verified：生命周期、运行时、投影、决定卡、IM 命令与投影常驻循环聚焦套件为 `178 passed`；移除本机代理环境后，完整离线套件为 `939 passed, 18 skipped`。Python 编译检查和 Git whitespace 检查通过。
- Deployment：内容提交 `770243a02b116e12583ceebdb8362fd40b7fe0a7` 已推送。wheel SHA-256 为 `04b76ac0b1cbe14c410c739be0279d74e60127b9cd3f68eeb4f8a07e0ba2b8af`，保存在 `/srv/larkflow/target/releases/20260806_221621_lifecycle_770243a/` 并安装到 Target 与 legacy 虚拟环境。升级前备份为 `larkflow_target_dev-20260806T221621+0800.dump`、222114 bytes、`0600`；migration runner 返回空列表，长期库保持十九份 migration。十个服务均回读 `active / NRestarts=0`，部署窗口 warning 为 0，5432、8765 与 8780 只监听 loopback。
- Acceptance：一次性 PostgreSQL 双连接竞争证明重复取消确认只有一次落地和一次幂等回放，aggregate version 只增加 1，取消审计恰好 1 条。暂停与 Human dispatch 同时竞争时只允许一路成功，本轮为 dispatch 成功、pause 明确冲突，最终保持 `running / waiting_human`。测试库与远端临时上传件随后删除并回读为 0，正式 release wheel 保留且哈希一致。
- Boundary：本轮没有新增 migration。真实飞书命令、普通 Human Task 和决定卡取消收口尚未执行，不构成生产上线证明。
- Acceptance addendum：真实飞书实例 `im_c1c472a12a8ea4a7c8d63480` 依次完成确认、暂停、继续、取消预览与版本绑定确认，Instance 最终为 `canceled / version 5`，三个 Node 与三个 Attempt 均为 `canceled` 且 claim 已释放。审计按版本记录 confirmed、paused、resumed 与 canceled。普通 Human Task `4a980670-8cdc-473b-acf4-5e7f54dbf06c` 从飞书服务端回读为 `done`，中央 Projection 为 `completed=true / node_status=canceled`。
- Acceptance addendum：决定卡实例 `im_516c59e4082e82ab74b8bd14` 的前置 Task 通过真实完成事件推进到 Human 决定节点。取消确认后，Instance 为 `canceled / version 5`，决定节点与 Attempt 为 `canceled`，Projection 为 `settled=true / node_status=canceled`；飞书同一条 Card 2.0 消息原位更新为无控件“复核已取消”，并明确不能再提交。十个服务均为 `active / NRestarts=0`，验收窗口 warning 为 0。
- Boundary addendum：此前“真实飞书验收尚未执行”只描述首次部署时点，现已由上述开发测试组织证据关闭。该结果仍不证明生产上线、生产容量、内容质量或业务价值。

## v0.54.0-draft · 2026-08-06 · Console 实例状态与返工摘要

- Added：内容提交 `efc1dff935d21918517d73c0d10fd15336516d9a` 在 Owner 实例详情增加最终状态、返工节点和关键重启三类只读摘要。服务端从当前聚合与最近审计提炼 DTO，影响节点重新限定到冻结 Snapshot，不向浏览器暴露原始 Audit payload。
- Verified：完整离线套件为 `923 passed, 18 skipped`，Console 聚焦套件为 `11 passed`，JavaScript 语法检查与 Git whitespace 检查通过。wheel SHA-256 为 `ffb36696ca3eac191edeccf1c9b48642a3d1c164611c00fdcb8df0c0654993b9`，服务器安装资源、wheel 与本地源码的 `app.js` SHA-256 均为 `af67724e7370d54d9e8c06e0c79b4772d588e4007e2b81fd90a859a2d9d698ea`。
- Deployment：升级前备份 `/var/backups/larkflow-postgres/larkflow_target_dev-20260806T200617+0800.dump` 成功，大小 222115 bytes。migration runner 返回 `versions=[]`，长期库保持十九份 migration；本次只重启 Console，十个 Python 服务均为 `active / running / NRestarts=0`，5432、8765、8780 只监听 loopback，未认证 API 返回 401，部署窗口 Console warning 为 0。
- Acceptance：真实 Chrome 在 `source_grounded_reject_20260806_001940` 直接回读 `done / 4/4 / version 16`、三个 Attempt 2 节点和 00:53:27 的三节点重启；无返工实例同时回读两类空状态。页面显式声明只读边界。
- Boundary：摘要减少理解返工历史所需的人工解释，但尚未证明 Owner 能在完全没有开发者解释的情况下稳定完成状态判断。流程画板级操作和可写前端继续保持低优先级。

## v0.53.0-draft · 2026-08-06 · Console 拖动缩放与节点点击组合验收

- Fixed：内容提交 `b153c5311771eaa5b98d964fe6ffd448b62cf49d` 为真实依赖图增加空白区域拖动平移、50% 到 160% 缩放、100% 重置、适配、键盘操作和点击后视口保持。内容提交 `c3e23fcbf3bf9e66eeb9cf97bf8bbbc1bb2eefc3` 把节点卡片排除在平移手势启动范围之外，避免鼠标细微位移吞掉节点点击。
- Verified：完整离线套件为 `922 passed, 18 skipped`，Console 聚焦套件为 `10 passed`，JavaScript 语法检查通过。最终 wheel SHA-256 为 `5b1009c95fa493b1f583ea9ee63ee4a61190840b700efb151802f283e7b67dec`；wheel、本地源码与服务器安装态 `app.js` SHA-256 均为 `399cb70a72d2291ede7532cc6421f0d31a95d8c018b8b57a8d6309b67f0cb482`。
- Deployment：升级前备份 `/var/backups/larkflow-postgres/larkflow_target_dev-20260806T193753+0800.dump` 成功，大小 222118 bytes，权限为 `0600 lf_target_dev:lf_target_dev`。migration runner 返回 `versions=[]`，长期库仍为十九份 migration。本次只重启 Console；十个 Python 服务均为 `active / running / NRestarts=0`，5432、8765、8780 只监听 loopback，未认证 API 返回 401，部署窗口 Console warning 为 0。
- Acceptance：真实 Chrome 在 `source_grounded_reject_20260806_001940` 上把画布从阶段 01 拖到阶段 02 至 04，再用鼠标选中 Tool 节点；蓝色选中态和右侧“检查来源归因契约”Attempt 面板立即切换，视口保持。缩小从 100% 变为 90%，适配结果为 57%。
- Boundary：Console 继续只读，文字回复发生在当前 Agent 对话。第一次 Owner 独立使用暴露的操作缺口已经修复，但修复后的 Owner 再次独立使用尚未完成，不能据此宣称控制台已经降低状态追踪成本。

## v0.52.0-draft · 2026-08-06 · Console 真实 DAG 页面验收

- Acceptance：真实 Chrome 标签页经 SSH 隧道刷新后加载当前静态资源，页面回读 4 条 SVG 依赖边和 4 条直接依赖标签。Agent 同时指向 Tool 与最终 Human，Tool 再汇入最终 Human；横向滚动可查看完整图，选中最终节点时两条关联边与节点同步高亮。
- Evidence：同一真实运行中实例在页面显示 `3/4`、最终 Human Attempt 2 等待人工、Attempt 1 退回结果和 16 条追加型审计。该结果与页面 DTO 一致，没有通过数据库代查替代浏览器验收。
- Boundary：本轮关闭新版 DAG 图形目视缺口，不证明 Owner 已独立依靠 Console 降低追踪成本。下一项门槛仍是用户不依赖聊天状态命令或开发者解释完成一次受控 Console 使用。
- Known Issue：部署前已经打开的标签页仍运行当时加载的旧 JavaScript，页面数据刷新不能替换脚本；部署验收必须显式刷新标签页后再检查 DOM 与截图。

## v0.51.0-draft · 2026-08-06 · Console 真实 DAG 依赖图

- Fixed：内容提交 `623b9b6228caa52b4680eb30ad2fee723e8921b6` 移除把节点数组画成线性链的旧连接器。浏览器现在依据 `deps` 计算拓扑层级，以 SVG 绘制真实依赖方向，节点展示直接依赖；选择节点时关联边同步高亮，窗口尺寸变化后重绘。
- Verified：完整离线套件为 `922 passed, 18 skipped`，Console 与部署相关聚焦套件为 `22 passed`，JavaScript 语法检查通过。候选 wheel SHA-256 为 `6b8faed6eb5a4f32d695e40fdc495480585e53d9058e28e7ca7d2ece32421f8d`；服务器安装资源、wheel 与本地源码的 `app.js` SHA-256 均为 `a17afe0badc483d009b2b6049eb94c77a82c2f3cb1cbccd8045e7d50a08c2cc1`。
- Deployment：升级前备份 `/var/backups/larkflow-postgres/larkflow_target_dev-20260806T183926+0800.dump` 成功，大小 222114 bytes，权限为 `0600 lf_target_dev:lf_target_dev`。migration runner 返回 `versions=[]`，长期库仍为十九份 migration。本次只重启 Console；十个 Python 服务均为 `active / NRestarts=0`，5432、8765、8780 只监听 loopback，未认证与认证 API 分别返回 401 和 200，部署窗口 Console warning 为 0。
- Boundary：自动浏览器受本地回环地址安全策略限制，新版 DAG 图形仍需一次 Owner 人工目视确认。当前证据只证明代码、打包、部署与 HTTP 边界一致，不证明控制台已降低状态追踪成本，也不增加任何写操作。

## v0.50.0-draft · 2026-08-06 · 首批真实内部试用小样本基线

- Acceptance：第三个真实项目样本 `pilot_console_value_20260806_164925` 以固定版本路线图为来源，Human、Agent、Tool、Human 四节点均在 Attempt 1 完成。Tool 覆盖 5/5 条来源事实与 3/3 个开放问题，Owner 直接接受首次结果，实例从确认到接受用时 12 分 41 秒。
- Evidence：接受命令进入 `processed / human_decision_accepted / sent / updated`，首个服务端反馈为 1235 ms。Task、两条自动结果消息、决定卡、完成文档和最终通知均为唯一外部绑定；没有重启、例外人工干预或已观察到的重复外部副作用。
- Outcome：首批三项小样本覆盖直接退回、带具体意见返工和直接接受。它证明流程可以保留结果可用性、返工和人工干预信号，不证明模型质量规模化、市场价值或生产容量。
- Boundary：本轮状态跟踪仍由开发操作者通过 PostgreSQL 和聊天更新完成，未形成用户独立使用中央控制台的证据。控制台降低状态追踪成本仍是待验证假设，下一轮先做受控 Console 使用，不新增可写前端。

## v0.49.0-draft · 2026-08-06 · Owner 中央只读控制台

- Added：新增独立 `larkflow-console`、Owner 范围读取服务、loopback HTTP 边界和静态页面。列表展示本人最近流程，详情展示 DAG、节点状态、历史 Attempt 结果与最近审计；首版不提供确认、重启、编辑或其他写操作。
- Security：开发 Bearer token 至少 32 字符，并只映射到服务端配置的 tenant 与 person。列表 SQL 同时限定 tenant 和 Owner，详情再次校验 Owner；不存在与非 Owner 统一 404。DTO 不返回人员 ID、claim token、完整错误正文或审计 payload，结果、审计和列表均有上限。systemd unit 强制 `127.0.0.1:8780` 与地址、文件系统、设备、能力和命名空间限制。
- Verified：内容提交 `ee2fa9439594d765cd08f2caa0f7ecb20d30d78b`；完整离线套件 `922 passed, 18 skipped`。非 editable wheel 已回读页面资源，SHA-256 为 `58b27648ccaf3f863cf4bb0ca820b3e2209523b58b0574af626aa303c0e4ff5c`。
- Deployment：升级前 PostgreSQL 备份成功，migration runner 回读十九份既有 migration 且无待应用版本。九个 Target 服务与一个 legacy 消费者统一重启后均为 `active / NRestarts=0`，5432、8765 与 8780 只监听 loopback，部署窗口 warning 为 0。
- Acceptance：真实 API 返回 30 条当前 Owner 流程、运行中实例 4 个节点与 16 条审计；另一 Owner 的真实实例统一返回 404。SSH 隧道浏览器验证运行中 DAG、Attempt 1/2、审计时间线、草稿 0/3、零浏览器 error / warning 和显式锁定。隧道、两端临时凭据和上传件均已删除。
- Boundary：当前静态 token 只适合开发试用。生产前仍需飞书登录态或企业 SSO、反向代理授权、会话与 CSRF、限流、可见性策略、分页筛选和跨轮次对比；本次不构成生产上线或业务价值证明。

## v0.48.0-draft · 2026-08-06 · 原生表单绑定与真实返工上下文闭环

- Fixed：内容提交 `f6125331aa541e824675e25f9cd2d756cd4c6b56` 把 Card 2.0 表单按钮改为原生 `form_action_type=submit`，并由服务端投影保存决定绑定。表单回调不携带 `action_value` 时按消息 ID 恢复绑定；若客户端仍携带动作值，则必须与服务端绑定交叉一致。
- Verified：完整离线套件为 `910 passed, 18 skipped`。候选 wheel SHA-256 为 `f7c909a4844fa69ef7c5387d20f0fa8fa3e43863c807392c3b26d37cb9e45c61`，已同时安装到 Target 与 legacy 虚拟环境；长期 PostgreSQL 保持十九份 migration，九个 Python 服务回读 `active / running / NRestarts=0`，验收窗口 warning 为 0。
- Recovery：真实实例 `im_5717aa5b9480d146239907d5` 的退回意见原文进入 Human Attempt、质量证据与 `node.human_decision_rejected` 审计。卡片动作为 `processed / human_decision_rejected / sent / updated`，首个服务端反馈为 1155 ms。Owner 确认三节点重启后，只有 Agent Attempt 2 收到 `rework_feedback`，来源确认仍为 Attempt 1，Tool、Human 新 Attempt 和冻结 Snapshot 都未获得该结构化字段。
- Outcome：Agent Attempt 2 根据意见补出 1 项问题和 3 项验收条件，确定性 Tool 从首轮失败变为 `pass`，3/3 条来源事实和 2/2 个开放问题均按类别引用。新 Human Attempt 2 决定卡已从飞书服务端读回，实例保持 `running` 等待最终人工复核。
- Boundary：本次关闭开发环境中的具体退回意见与目标 Agent 返工上下文门槛。它不证明模型内容质量规模化、市场价值、生产容量或生产上线。

## v0.47.0-draft · 2026-08-06 · 退回意见与目标 Attempt 返工上下文

- Added：Human 决定卡保留表单外一键接受，退回改为必填 `rejection_feedback`，最多 1000 字。桥接层与领域服务都重新校验，空白和超长输入 fail closed。
- Decision：退回意见写入 Human Attempt 结果、质量证据和追加型审计；接受路径忽略客户端附带的额外意见，不让无关字段改变接受语义。
- Recovery：Owner 确认 `reject_target` 节点重启时，意见只进入目标新 Attempt 的 `rework_feedback`，Runner 激活时继续保留。范围外上游、受影响下游占位 Attempt、冻结 Instance Snapshot、旧 Attempt 和完整实例重启均不隐式复制局部意见。
- Verified：内容提交 `0dc5359e990635c7b6aa16ec0bcd798eb8df39d0` 已推送；完整离线套件为 `908 passed, 18 skipped`。删除 Runner 保留逻辑的定向变异被新增回归捕获。实现复用既有 JSONB，不增加 migration。
- Compatibility：旧发布件创建的决定卡没有退回意见输入框。升级前必须查询仍在等待的旧决定卡，必要时先完成，或明确作废旧 Attempt 并在升级后通过受控节点重启生成新卡；不能把旧卡回调失败误判成用户未操作。
- Boundary：本条只记录代码与离线证据。开发服务器部署、PostgreSQL 真实读回和真实飞书“填写意见、退回、重启、Agent 收到意见”仍待执行，不构成生产上线证明。

## v0.46.0-draft · 2026-08-06 · 真实退回、节点重启与 Attempt 2 恢复

- Acceptance：第二个公开材料实例 `source_grounded_reject_20260806_001940` 首轮完成 Human-Agent-Tool-Human，Tool 回读 6/6 条事实、3/3 个开放问题、零违规。Owner 明确退回后，决定命令进入 `processed / human_decision_rejected / sent / updated`，卡片反馈写入耗时 1041 ms，Instance 进入 `failed / version 9`。
- Recovery：真实节点重启预览只影响 Agent、Tool 与最终 Human 三个节点，保留来源确认 Attempt 1。确认命令只执行一次并创建三个 Attempt 2；第二轮 Agent 与 Tool 均完成，Tool 仍为 6/6、3/3、零违规。Owner 在新决定卡明确接受后，Instance 恢复为 `done / version 16 / graph_revision 1`。
- Evidence：两轮 Agent 与 Tool 结果、Human Attempt 1 的退回结果与 `quality=fail`、Human Attempt 2 的接受结果、两张独立决定卡、四条独立自动结果消息、完成文档、最终通知和原 Human Task 均保留外部绑定。审计包含一次退回、一次接受、一次节点重启和一次 Instance 完成；九个 Python 服务在验收窗口内无 warning。
- Changed：开发真栈已经覆盖直接接受与退回返工两条材料复核路径。根据 ADR-089，后续不再堆叠纯合成点击验收，转向 larkflow 项目真实工作的受控内部试用，记录完成、结果可用性、返工、人工干预、明确决定耗时和重复副作用。
- Boundary：本次证据来自开发测试组织和公开材料，只证明状态机、投影与历史保护可用。它不证明外部事实真伪、模型质量规模化、市场收益、付费意愿、生产容量或生产上线。

## v0.45.0-draft · 2026-08-06 · 来源约束型材料复核真实接受闭环

- Deployment：内容提交 `b7e589ba4af0398573ec995254dd61e9b1a4508c` 的 wheel SHA-256 为 `0dcccb7f674135dde8b44ab08d437ba397b92397b8456ede8a064f66f1eb2af1`，安装于 `/srv/larkflow/target/releases/20260805_233701_source_review_b7e589b/`，并同时更新 Target 与 legacy 虚拟环境。升级前 PostgreSQL 备份为 169991 bytes、`0600`，可由 `pg_restore --list` 读取 125 个 TOC 条目；长期库保持十九份 migration。
- Readback：八个 Target 服务与一个 legacy 消费者均为 `active / running / NRestarts=0`，部署窗口 warning 为 0；`source_grounded_review:1` 已导入并启用。
- Acceptance：真实飞书实例 `source_grounded_20260805_234517` 使用公开软件需求材料完成 Human-Agent-Tool-Human 4/4。Agent 输出为 `source_claims.v1`，确定性 Tool 回读 4/4 条事实、3/3 个开放问题、零违规和 `quality=pass`。Owner 明确点击“接受”后，决定命令进入 `processed / human_decision_accepted / sent / updated`，卡片反馈写入耗时 1098 ms，Instance 最终为 `done / version 9 / graph_revision 1`。
- Evidence：四个节点均为 Attempt 1 完成；Task、Agent 消息、Tool 消息、决定卡、完成文档和最终通知均有外部投影绑定。追加型审计包含四次节点激活、两次自动完成、一次 Human 提交、一次明确接受和一次 Instance 完成。
- Boundary：本次只验证开发测试组织中的单份公开材料与接受路径。确定性 Tool 不验证外部事实真伪；证据不证明模型质量规模化、市场价值、生产容量或生产上线。真实退回后从 Agent 节点重启形成新 Attempt，以及第二份材料复测，仍是下一门槛。

## v0.44.0-draft · 2026-08-05 · 来源约束型材料复核与人类明确决定

- Added：新增 `source_grounded_review` 模板。输入以 `source_url`、稳定 `F` 事实和 `Q` 开放问题形成来源登记；Agent 的 `source_claims.v1` 结果区分来源事实、推断和开放问题，并携带引用。
- Validation：新增确定性 `source_claims.check`，只校验结构、声明类型、引用覆盖和来源 URL 一致性，不访问网页，也不把契约检查描述为事实核验。
- Decision：Human 节点可声明 `accept_reject` 决定。该节点投影版本绑定 Card 2.0，接受正常完成，退回使 Human Attempt 与 Instance 失败并保留历史；普通 Task 完成、非 Owner、旧版本卡片和重复动作不能绕过决定。
- Security：决定回调复用耐久 IM 命令队列和即时“处理中”反馈，操作人只取飞书顶层认证字段；凭据侧重验活跃成员，领域侧重验 Owner、Instance、Node 与 Attempt 版本。
- Verified：内容提交 `b7e589ba4af0398573ec995254dd61e9b1a4508c` 已推送；完整离线套件为 `898 passed, 18 skipped`，候选 wheel 已确认包含新模板与实现。
- Boundary：本条记录本地实现与远端代码事实。开发服务器部署、真实 PostgreSQL migration 回读、模板启用和真实飞书业务材料验收尚未执行，不构成生产上线或业务价值证明。

## v0.43.0-draft · 2026-08-05 · 自然语言流程执行闭环

- Acceptance：自然语言实例 `im_74e775110afbd80aa598d3ae` 在最终图预览后由真实用户独立确认启动。Agent Attempt 1 经真实模型调用完成，随后创建飞书 Human Task；任务完成状态经周期读回写入耐久 Inbox，并由领域侧重新授权后提交 Human Attempt 1。
- Verified：PostgreSQL 终态为 `done / template_version_id IS NULL / graph_revision 1 / 2 nodes done`，审计链包含草稿创建、实例确认、两个节点激活、Agent 完成、Human 提交和实例完成。Agent 消息、Human Task、完成 Docx 和最终通知四类外部投影均已落库，后两项又从飞书服务端读取正文和消息确认真实存在。九个 Python 服务保持 `active / running / NRestarts=0`。
- Reliability：Task 完成事件仍未由 bot 长连接接收，周期状态读回在飞书任务完成后写入 `feishu_task_poll / larkflow.task.completion_reconciled_v1` 耐久信号，Inbox 终态为 `processed / submitted:human_node`。本次推进没有绕过凭据侧资源读取、当前 Projection、Attempt、Owner 与完成者重授权。
- Documentation：README 事实修正提交为 `3b3ce7ab11856b1bccecb477e6ab3ecf1f4a68d6`；本条只记录既有发布件上的真实验收，不替换服务器 wheel 或修改 migration。
- Boundary：本次目标和背景没有真实业务材料，Agent 正文明确报告缺少可汇总数据。该证据关闭开发环境中从自然语言候选草稿到中央执行、Human Task、完成 Docx 与最终通知的技术链路，不证明模型内容质量、业务价值、并发容量或生产上线。

## v0.42.0-draft · 2026-08-05 · 多阶段卡片稳定收口

- Fixed：自然语言草稿卡片不再把回调延时更新 token 用于首次反馈、生成进度和最终结果三次写入。首次无按钮反馈保留 token 路径，后续阶段与终态改按原消息 ID 更新，避免第三次调用返回 `300040` 后卡片停在“正在生成”。
- Reliability：消息 ID 更新要求 Card 2.0 在更新前后保持 `config.update_multi=true`；进度与回复 Worker 只读取动作中服务端保存的原消息 ID，不信任卡片动作 payload 提供的新目标。
- Verified：内容提交 `2ed644e640f3c3834f82c464e05fe0b4c3a241cc` 的完整离线套件为 `886 passed, 18 skipped`。两个隔离破坏测试分别恢复旧 token 路径和移除 `update_multi` 门禁，均被新增回归捕获。
- Deployment：wheel SHA-256 为 `56787f1f3e8298a831c80dc65d60e3a116d34f0c1aef9324c50448e4d15ee4b7`，安装于 `releases/20260805_214200_cardsettle_2ed644e/`。升级前 PostgreSQL 备份为 155532 bytes、`0600`；九个 Python 服务为 `active / running / NRestarts=0`，十九份 migration 与七条监听连接保持成立。
- Acceptance：旧实例卡片已按消息 ID 修复。新实例 `im_74e775110afbd80aa598d3ae` 从输入表单进入生成进度并收口为最终图预览，动作状态为 `processed / draft_created / reply sent`；飞书服务端读回同一卡片 `updated=true` 且没有按钮或输入框，回归窗口 warning 为 0。
- Boundary：本条只关闭开发环境中的 Card 2.0 多阶段更新缺陷，不代表生产上线、并发容量或客户端渲染时延得到验证。验收草稿未执行 `/larkflow confirm`。

## v0.41.0-draft · 2026-08-05 · 独立草稿生成与阶段进度

- Added：新增无飞书凭据的 Draft Generation Worker、`generate-drafts-once / generate-drafts` CLI、systemd unit 与独立 env 模板。migration `0019_draft_generation_progress` 为自然语言草稿动作保存生成 claim、`generating / repairing` 进度 revision、独立进度 claim 和最终回复栅栏。
- Security：模型进程不加载 lark-cli profile；普通人员分工 Worker 显式排除 `draft_wizard` 动作。身份验证、目录再验证、卡片更新和最终回复仍只在凭据侧执行，模型结果继续经过同一确定性 Snapshot 校验。
- Reliability：生成租约覆盖两次完整 LLM 路由预算加安全余量；首次候选被拒绝时，在第二次调用前持久化修复阶段。最终回复等待当前进度 revision 结算，迟到的旧进度不能覆盖成功或拒绝终态。通知只唤醒 Worker，耐久队列和轮询仍是可靠性基础。
- Verified：内容提交 `1a80b4035d0a5ad5c634af7be957f4b7d1ee37d7` 已推送；完整离线套件为 `884 passed, 18 skipped`。四个隔离变异均被测试捕获，候选 wheel 已确认包含 `draft_generation_daemon.py` 与 `0019_draft_generation_progress.sql`。
- Boundary：本条文档提交时，开发服务器仍是八服务、十八份 migration 和六条监听连接。第九服务、migration 19、真实 PostgreSQL 竞争与飞书卡片阶段变化验收尚未执行，不构成开发部署或生产上线证据。

## v0.40.0-draft · 2026-08-05 · 自然语言流程草稿引导

- Added：裸 `/larkflow draft` 现在创建 Card 2.0 引导，收集必填目标、可选背景和一名协作者。回调通过中央 Agent 生成最多八个 Human / Agent 节点，只创建无模板 Snapshot 草稿；同一原卡片最终显示节点、Owner、依赖和独立 `/larkflow confirm` 命令。带 JSON 的结构化高级入口保持兼容。
- Security：只接受原发起人、原消息和原卡片的认证回调，协作者必须来自冻结的活跃候选快照并在处理前重新验证。模型输出按严格 JSON 解析，Owner 角色限制为 `requester / collaborator`；服务端覆盖 `schema_version` 与用户原始输入，并拒绝 Tool、模型服务配置和 Personal Edge capability。
- Reliability：复用人员分工卡的耐久动作、canonical 去重、即时“处理中”、最终卡片更新和文本回复链路。重复回调只回读首次创建的草稿，不再次调用模型；可选背景字段在飞书省略时按空值处理。中央 Agent 的首个候选未通过确定性校验时最多重生成一次，第二次失败仍拒绝，旧候选、错误和安全边界不会被当成授权事实。
- Fixed：Card JSON 2.0 提交按钮改为官方 `action_type=form_submit`；开发部署同时更新 Target Runtime 与 legacy 飞书事件桥接虚拟环境，避免旧桥接包静默忽略新增动作名。
- Verified：内容提交依次为 `244fb0c25b67c789ed42f23a290438b86e1a7e18`、`6ff0af211280cbeeb8b35cca04308a88c2c67184` 和 `282ea515aeb463896133b4b3a60d9d42733d555c`。当前完整离线套件为 `877 passed, 17 skipped`；三组既有隔离变异和新增两项有界重生成回归均通过。真实点击在 1056 ms 内把原卡片更新为“处理中”；首个非法依赖候选被服务端拒绝并有界重生成，实例 `im_69af9ebdf241017341e5fee4` 最终为 `draft / template_version_id IS NULL / 3 nodes / 0 NodeInstance / 0 Attempt`。同卡只有一个 canonical 动作，状态为 `processed / draft_created / sent`；应用 bot 从飞书服务端回读原卡片为无操作控件的“流程草稿已生成”。
- Deployment：最终 wheel SHA-256 为 `e8b82659cb03a42892480164ef0541ed512b4dde4dbf259b0892e32d02e8d78e`，保存在 `releases/20260805_191842_draftretry_282ea51/` 并安装到 Target 与 legacy 两个虚拟环境。升级前备份 `larkflow_target_dev-20260805T191903+0800.dump` 为 150907 bytes；长期库仍为十八份 migration，八服务均回读 `active / running / NRestarts=0`，部署窗口 warning 级日志为 0。
- Boundary：本条只证明开发环境与测试组织中的草稿生成链路，不代表生产上线、模型输出质量稳定、客户端渲染延迟或并发容量。验收草稿未执行 `/larkflow confirm`。

## v0.39.0-draft · 2026-08-05 · 结构化无模板飞书草稿入口

- Added：新增 `/larkflow draft <JSON定义> [role=@成员 ...]`。命令不查找模板版本，直接生成 `template_version_id=NULL`、`locked=false` 的 Instance Snapshot 草稿；草稿仍需独立 `/larkflow confirm` 才启动，并进入既有 Human、Agent、Tool、Attempt、Projection 与审计运行时。
- Security：命令继续只信服务端验证的发送者和本条消息 mention。单角色可默认绑定发送者，多角色没有显式 mention 时拒绝。严格 JSON decoder 拒绝重复键、`NaN` 和 `Infinity`；定义最多 100 个节点，不能携带模型 provider、base URL 或密钥等服务配置，也不能请求 `personal.readonly` Edge capability。
- Verified：内容提交为 `5113a59aacc8b0a97481411e581b9d52f6462073`。完整离线套件为 `858 passed, 17 skipped`。四组变异分别证明严格 JSON、Edge capability 拒绝、mention Owner 验证和多角色显式绑定门禁不是假绿。
- Deployment：候选 wheel `larkflow-0.0.2-py3-none-any.whl` SHA-256 为 `d5b0964ce3bcb817a6ec22c3346bb4ff47aaae64bffdd0b0cd67027ee3dd4d2a`，已部署到 `releases/20260805_162906_inline_5113a59/`。即时 PostgreSQL 备份为 143443 bytes，migration 仍为十八份；八服务均回读 `active / running / NRestarts=0`，部署窗口 warning 为 0。
- Acceptance：真实飞书实例 `im_a9a43d1d4db354b31b798bb1` 从无模板草稿创建、确认启动到 Human-Agent-Tool-Human 4/4 全部完成。最终飞书状态和 PostgreSQL 终态一致；数据库额外确认 `template_version_id IS NULL`、四个节点全部 `done`。
- Boundary：本条关闭开发环境中的无模板用户入口缺口，不提供任意模型 provider、Personal Agent Edge capability、图形化编辑器或生产装配，也不构成市场验证和生产上线证据。

## v0.38.0-draft · 2026-08-05 · macOS Edge 哈希锁定离线 bundle 与安全评审

- Added：新增 `deploy/build-larkflow-edge-bundle.py`。发布方为明确的 macOS 架构与 Python 次版本下载 binary-only wheelhouse，manifest 记录 source commit、目标、主 artifact、manager、全部文件 SHA-256、大小和 wheel 包名版本清单。
- Security：manager 新增 `install --bundle --manifest-sha256`，在创建安装前验证 manifest 摘要、精确文件集、符号链接、目标、wheel metadata 与重复包；离线子进程清除 pip 配置、Python 注入变量和代理，强制 `--no-index --only-binary=:all:`。离线 release 使用 manifest 摘要而不是只用主 wheel 摘要，避免不同依赖集合误用旧环境。稳定 manager 在 current 切换前安装，避免 release 已激活而 manager 更新失败。
- Fixed：`pip-audit 2.10.1` 在第一版隔离候选中发现 pip 26.1 命中 `CVE-2026-8643`。bundle 现在额外携带哈希锁定的 pip 26.1.2 或更高且低于 27，manager 在安装应用 wheel 前先离线升级并验证版本。使用 pip 26.2.1 的隔离 venv 复扫为无已知漏洞；私有 `larkflow 0.0.2` 不在 PyPI 审计范围。
- Verified：内容提交为 `81bd43983598ff319150344e779223cd03731eba`。manager、builder、Edge 客户端与打包聚焦测试为 `61 passed`，真实 macOS 进程权限上下文中的完整离线套件为 `840 passed, 17 skipped`。测试 bundle 为 macOS arm64、CPython 3.12，共 45 个 wheel；在故意注入无效索引与代理时仍只从本地 wheelhouse 安装，`pip check` 无 broken requirements，安装态 CLI 可启动。
- Review：新增 `research/edge-distribution-security-review.md`，正式员工分发结论为 No-Go。P0 包括最小 Edge 独立分发、固定 lock 与构建证明、Developer ID 签名与公证、可信摘要渠道、目录级读取和数据外发治理、全新员工 Mac 验收。
- Boundary：本轮测试候选来自未提交工作树，manifest 中的既有 HEAD 只是调用者声明，不能作为正式来源证明。当前本机 Developer ID Application 与 Installer 身份均为 0，没有执行签名、公证或正式分发。

## v0.37.0-draft · 2026-08-05 · macOS Edge 最小安装升级体验

- Added：新增独立 `deploy/larkflow-edge-manager.py`。macOS 当前用户可以用同一条 `install --wheel --sha256` 命令完成首装或升级；manager 自动寻找 Python 3.10 或更高版本，在版本化 release 中建立独立 venv，并提供非敏感 `status` 与单步 `rollback`。
- Added：`larkflow-edge doctor` 离线校验本机凭据、Codex 命令和传输形状，不连接中央节点、不领取工作，也不输出 server URL、device ID 或 secret。包版本从 `0.0.1` 提升为 `0.0.2`，项目元数据与运行时版本由回归保持一致。
- Safety：wheel 必须是非符号链接普通文件，包名、版本和完整 SHA-256 必须匹配。新 release 只有在最终路径完成安装、`pip check` 和 CLI 启动验证后才切换；manager 拒绝 root、符号链接目录和已有的无关同名命令，不修改系统 Python、Keychain 或 launchd。
- Fixed：最初把已创建的 venv 从临时目录移动到 release 后，console script 的 shebang 仍指向旧目录，稳定命令无法执行。实现改为先读取 wheel metadata，再直接在最终 release 路径创建 venv；新增回归固定该约束。另修复已安装 manager 由 macOS 系统 Python 3.9 启动时误用自身解释器创建 3.10+ venv的问题。
- Verified：内容提交为 `5b0c79b4d946441063d92970e8f0e9cac31b2ab3`。manager、打包、Edge CLI 与客户端聚焦套件为 `49 passed`，完整离线套件为 `828 passed, 17 skipped`；禁止旧文案扫描和 `git diff --check` 均通过。候选 wheel SHA-256 为 `f513a61c18a6fdd0c60d34c57dcb2f0121d814870ec8f2bcea8218986bd054d2`。
- Acceptance：员工 Mac 已真实执行 `0.0.1 -> 0.0.2 -> rollback -> 0.0.2`。最终 current 为 `0.0.2-f513a61c18a6`，previous 为 `0.0.1-d33241ba7328`，两个稳定命令可解析；manager 源码与托管副本 SHA-256 均为 `2020ee85660f8623ca9f0b68caf7dde8f96555d9dd67c7c8894ffe7100bd548d`。真实用户上下文中的 `doctor` 为 ready，临时 SSH 隧道中的 `run-once` 返回 `no_work`，服务器设备保持 active 且认证时间推进；隧道已关闭。
- Boundary：本轮没有修改中央 Gateway 协议、数据库 migration 或服务器服务，也没有替换开发服务器 wheel。manager 与候选 wheel 尚未签名或公证，依赖仍从包索引解析，没有离线 wheelhouse、自动更新或正式分发通道；验收不等于生产安全或员工规模采用。

## v0.36.0-draft · 2026-08-05 · macOS Keychain 设备凭据

- Added：macOS 上 `larkflow-edge --credential-store auto` 默认把设备密钥保存到当前用户登录 Keychain。`0600` 元数据文件只保留 server URL、device ID 和 Keychain store 标识，并继续作为单设备锁定位。
- Migration：新增 `credential-migrate [--delete-source]`。迁移先写入并回读校验 Keychain；`--delete-source` 仅在一致后把原明文文件原子替换为非敏感元数据。校验或替换失败会删除本次新建的 Keychain 项，保留旧文件。
- Security：密钥不进入 `/usr/bin/security` 的 argv、环境变量、结构化日志或磁盘元数据。Keychain 写入通过无回显伪终端完成两次系统提示；读取后重新校验密钥内 device ID 与元数据一致。配对同时创建 Keychain 项和元数据，元数据失败会回滚新项。
- Compatibility：非 macOS 默认继续使用当前用户所有的 `0600` 文件；显式 `--credential-store file` 保留开发兼容路径。已有明文文件在不删除源时仍可作为非敏感字段来源，但运行时优先使用已存在的 Keychain 密钥。
- Verified：Edge 客户端与 CLI 聚焦套件 `34 passed`，完整离线套件 `816 passed, 17 skipped`。wheel SHA-256 为 `7be7c47a7b076585e0ed2133ae034dc5d3f58bf59d801de09a8fd56d2287164a`，安装态能解析迁移命令。隔离合成 Keychain 项已真实创建、完整回读并删除。员工 Mac 随后通过临时 SSH 隧道，以真实流程 Owner 身份完成默认槽位配对；元数据为 `0600` 且不含 secret，Keychain 完整回读一致，`run-once` 返回 `no_work`，服务器设备 active、凭据 hash、唯一配对审计和认证后时间戳均已回读。
- Boundary：实现内容提交为 `4d9cef0836859bb0a6772eb08640b9e6b29030c8`，验收前文档基线为 `398658a6a5886aba701842276e0bab4d7d8acec6`。客户端变更不要求替换现有 Gateway。持久 Keychain 凭据与非敏感元数据继续保留，临时隧道已关闭；正式员工安装、升级、安全评审、可持续公网连接和生产上线仍未完成。

## v0.35.0-draft · 2026-08-05 · 前台 Personal Agent Edge serve

- Added：新增 `larkflow-edge serve --workspace <path>`，在用户主动启动且保持可见的会话中持续领取 `personal.readonly` 节点。默认使用 20 秒长轮询、带抖动的有界指数退避、60 秒应用心跳和结构化任务摘要。
- Safety：同一设备凭据通过 POSIX 非阻塞文件锁限制为一个 `serve` 或 `run-once`。文件系统根目录、用户主目录和包含设备凭据的工作区不能作为前台会话范围。SIGINT、SIGTERM 或续租失败都会取消在途 Codex 进程组，不回传可能失去租约的结果。
- Reliability：网络请求异常统一为稳定 transport error；撤销或无效设备凭据立即停止，瞬时网络、执行器、陈旧租约和续租故障按上限退避。应用续租可观测，但日志不包含设备 secret、人员 ID、Instance ID 或 Node ID。本机执行器基础设施异常仍不调用领域失败命令。
- Verified：内容提交为 `fd6933a186bf115fe83adc5ac7d3a3b6153b0436`；Edge 聚焦测试为 `39 passed`，完整离线套件为 `807 passed, 17 skipped`。两项定向变异均捕获对应错误实现。wheel 共 103 个条目并包含新模块，安装态 `serve` CLI 与模块导入已通过。
- Deployment：内容提交 `fd6933a` 构建的 wheel SHA-256 为 `e4c0a60588969202ffacf57b660f39e7811d2cdb564016756584ddc0ecc2ea13`，发布件与 `5312f6c` 回滚件保存在 `releases/20260805_003605_edge_serve_fd6933a/`。升级前即时 PostgreSQL 备份为 139942 bytes、权限 `0600`；migration runner 返回空版本集，长期库保持十八份 migration。八服务均回读 `active / running / NRestarts=0`，部署窗口 warning 级日志为 0；Gateway 与 PostgreSQL 继续只监听 loopback，Caddy 保持 `disabled / inactive`。
- Acceptance：同一候选 wheel 以临时安装态在员工 Mac 上通过 SSH 隧道运行前台 `serve`。设备先产生 37 次无任务心跳，再领取合成单节点实例 `edge_serve_acceptance_20260805_0043`；真实 Codex 执行写入 18 条 `node.claim_renewed` 后把 Instance、Node 与 Attempt 完成为 `done`，结果适配器为 `codex.readonly`。同凭据第二个 Worker 以退出码 1 被锁拒绝；空闲 Worker 收到 SIGTERM 后记录停止请求并以退出码 0 收口；设备撤销后再次启动收到 403 `device_revoked` 并以 fatal 状态退出。一次性配对码文件、设备凭据、SSH 隧道与服务器临时输出均已删除。
- Boundary：本条证明开发服务器部署与受控员工 Mac 前台真机链路，不代表正式员工安装包、后台服务、系统凭据存储、安全评审、持续采用或生产上线。公网 Edge 仍受 ICP 接入备案阻断，本轮没有恢复 Caddy，也没有绕过公网限制。
- Product：交互延迟继续由既有耐久指标在真实功能验收中自然采集，不再把反复人工点击计时作为独立发布门槛；既有小样本仍不得外推为生产容量。

## v0.34.0-draft · 2026-08-04 · 独立 Interactive 双副本

- Added：新增 `interact-once / interact` 凭据侧 Worker，顺序访问 IM 命令验证与回复、人员分工卡创建、回调验证和回复五条车道。每条车道一次只领取一项，配置拒绝任何不等于 1 的 claim limit。
- Changed：Projection 不再认领凭据侧交互工作。开发 systemd 拓扑新增两个模板实例，使用稳定的主机名与副本号 Worker ID；统一重启脚本覆盖七个 Target 服务和一个 legacy 服务。
- Reliability：车道异常不会阻塞其他车道，日志异常不能终止耐久处理。任何车道领取到工作后立即继续扫描；无工作时仍由 PostgreSQL 通知唤醒并以 1 秒有界轮询兜底。
- Verified：内容提交为 `5312f6c026453ac6d9e2e62679b755f271c114f3`；完整离线套件为 `796 passed, 17 skipped`。三组变异测试均捕获对应错误实现；一次性真实 PostgreSQL 竞争验证两个副本各领取一条不同记录；服务器实际 systemd 版本通过新模板校验。
- Deployment：wheel SHA-256 为 `4f0ac761284da5e82ff52118da3b4ba5e273c4c8081b3f0170ccc65993d04ba2`，发布件与 `a506e7d` 回滚件保存在 `releases/20260804_205741_interactive_5312f6c/`。migration runner 返回空版本集，长期库保持十八份 migration。八服务均为 `active / NRestarts=0`，六条监听连接存在，安装文件哈希与本地提交一致。
- Operations：Target 虚拟环境由 root 管理，服务账号强制重装在卸载旧包后因元数据权限失败；随后按既有 root 管理方式恢复安装并通过 `pip check`，三个 pip 临时目录已移动到发布目录备份。Projection 在切换前停止，没有旧 Projection 与新 Interactive 重叠消费。
- Acceptance：三次真实飞书突发点击均只产生一个 canonical 动作和一个草稿，全部进入 `processed / draft_created / sent`。首反馈、凭据验证、领域处理和最终回复的 P50 / P95 分别为 1.015 / 1.196 秒、2.373 / 2.425 秒、2.586 / 2.677 秒和 4.793 / 5.498 秒。两个副本分别承担 2 / 1 条验证和 1 / 2 条回复，所有车道 `error_count=0`；应用 bot 从飞书服务端回读三张原卡片均为已更新、已冻结、不可再次提交的确认终态。
- Boundary：当前完成的是离线测试、真实 PostgreSQL 竞争、开发部署回读和三次真实飞书突发验收。样本不包含客户端渲染，隔离与更高强度限流回归尚未完成，不据此声明生产容量。

## v0.33.1-draft · 2026-08-04 · 批次完成时间逐项结算

- Fixed：人员分工与 IM 命令 Worker 不再把批次开始时间写入所有工作的验证、领域处理和回复完成字段；每条工作实际完成后独立读取时钟。PostgreSQL 条件更新显式转换可空文本参数类型，兼容 psycopg 3.3.4 的类型推断。
- Verified：内容提交为 `a506e7df078cbb6d8fa5f359272505ba62cac241`；完整离线套件为 `788 passed, 16 skipped`。定向变异证明旧批次开始行为会让新增回归失败；一次性真实 PostgreSQL 验证同批两条记录分别持久化 `+1000 ms` 和 `+2000 ms` 完成时间，测试库与临时文件随后删除。
- Deployment：wheel SHA-256 为 `385e9b2a272246b683d49b7232dcbbdd788aa1ee2efa051ee7cdff95bca46b6b`，发布件保存在 `releases/20260804_185244_item_completion_a506e7d/`。升级前备份为 136056 bytes、`0600 lf_target_dev:lf_target_dev`；长期库保持十八份 migration，六服务均为 `active / running / NRestarts=0`，四条监听连接存在，部署窗口 warning 级日志数为 0。
- Acceptance：五次真实人员选择卡全部进入 `processed / draft_created / reply sent`，每张卡只产生一个 canonical 动作和一个草稿；应用 bot 读回五张原卡片均为已确认终态且没有操作控件。首反馈、凭据验证、领域处理和最终回复的 P50 / P95 分别为 0.991 / 1.274 秒、4.757 / 12.358 秒、4.941 / 12.582 秒和 12.670 / 19.298 秒。前四次在 7.548 秒内到达，最终回复范围为 8.368 到 19.569 秒；第五次约 19 分钟后隔离点击，全链路为 4.044 秒。
- Correction：既有记录中的首反馈值来自独立单调计时，继续有效。提交 `a506e7d` 之前列出的身份校验、领域处理和最终回复精确耗时使用了批次开始时间，本条明确废止其逐项延迟解释。
- Boundary：五次样本由四次突发和一次隔离点击组成，不能视为同一并发负载。该验收只覆盖开发服务器与测试组织中的服务端区间，不包含客户端渲染，不代表生产容量。

## v0.33.0-draft · 2026-08-04 · PostgreSQL Worker 通知唤醒

- Added：migration `0018_worker_wakeups` 为 Outbox、Inbox、IM 命令和人员分工动作的可认领状态增加事务后 `pg_notify` 触发器。四类 Target 常驻服务在首次扫描前分别建立专用 `LISTEN larkflow_work_available` 连接。
- Reliability：通知 payload 为空，Worker 收到通知后仍从 PostgreSQL 队列表 claim 业务状态。连接、监听或等待失败时只退回当前有界轮询区间的剩余时间；默认离线和 SQLite 测试路径保持原有等待契约。
- Verified：内容提交为 `72d2e286c4a44b7893896939acf93aa97662db83`；完整离线套件为 `786 passed, 15 skipped`。一次性 PostgreSQL 14 验证未提交事务通知数为 0、提交后为 1，监听关闭后普通轮询仍领取同一类耐久工作；测试库与临时文件随后删除。
- Deployment：wheel SHA-256 为 `f2fe2072f98ef1ba371d97b18e4ef2d070fa1bfa21b657f9c12876d314aa89bb`，发布件保存在 `releases/20260804_171500_worker_wakeup_72d2e28/`。升级前备份为 129748 bytes、`0600 lf_target_dev:lf_target_dev`；长期库应用第十八份 migration 后，六服务均为 `active / running / NRestarts=0`，四个 Target Worker 各有一条真实监听连接，部署窗口 warning 级日志数为 0。
- Acceptance：真实人员选择卡只产生一个 canonical 动作和一个草稿。首个服务端反馈、身份校验、领域处理与最终回复分别为 0.868 秒、0.959 秒、1.912 秒和 2.200 秒；应用 bot 从飞书服务端读回原卡片为无按钮、无提交动作的已确认终态。
- Boundary：通知只降低耐久阶段之间的空闲等待，不承载业务状态，也不替代轮询、claim、授权或幂等。卡片数据只代表开发服务器和测试组织中的单次服务端测量，不包含客户端渲染，不代表生产上线或生产容量。

## v0.32.1-draft · 2026-08-04 · 卡片首个服务端反馈耐久观测

- Added：migration `0017_card_feedback_metrics` 在 `workflow_im_commands` 与 `workflow_role_binding_actions` 增加 `feedback_status`、`feedback_elapsed_ms` 和 `feedback_completed_at`，并用完整性约束拒绝部分指标写入。
- Changed：两类回调桥接器用单调时钟覆盖有效回调被接受、动作插入与直接卡片更新，在释放动作时原子保存成功或失败；结构化日志只包含动作类型、结果与耗时，不包含人员、消息或卡片标识。
- Verified：内容提交为 `c1d8fe510805cbe209a6275c4e4b3d8311b6692c`；完整离线套件为 `780 passed, 14 skipped`。干净 wheel SHA-256 为 `779990ca33771e0eb2ece2fa30bc8c1d4d2062625e4ded0f08e90d951d403204`，包含第十七份 migration。
- Deployment：发布件保存在 `releases/20260804_162012_card_metrics_c1d8fe5/`。升级前备份成功且为 124574 bytes、`0600 lf_target_dev:lf_target_dev`；长期库应用第十七份 migration 后，六个 Python 服务均回读 `active / running / NRestarts=0`，部署窗口 warning 级日志数为 0。
- Acceptance：真实人员选择卡的首个服务端反馈为 1.264 秒，领域处理与最终回复分别在入站后 4.484 秒和 5.030 秒完成；真实失败恢复卡的对应三项耗时为 0.990 秒、2.844 秒和 3.213 秒。两张原卡片均从飞书服务端读回终态且不含操作控件。
- Boundary：首个反馈指标从服务端接受有效回调计到飞书直接更新调用返回，不包含物理点击到服务端的网络时间，也不包含客户端渲染。以上仅代表开发服务器和测试组织，不代表生产上线。

## v0.32.0-draft · 2026-08-04 · 可操作卡片即时视觉反馈

- Added：人员选择卡与失败恢复卡在动作耐久落库后，立即尝试把原卡片替换为蓝色无按钮“处理中”，最终再收口为无按钮的成功或拒绝。legacy 卡片沿用既有两阶段同步更新。
- Ordering：直接卡片更新最长等待 3 秒；动作先延后 10 秒防止后台 Worker 抢先写入最终状态，更新结束后立即释放。桥接进程若在释放前崩溃，延后时间保证动作仍可恢复；视觉更新失败不回滚耐久动作。
- Database：新增 migration `0016_role_card_single_action`。同一人员选择卡只有一个 canonical 动作；真实长期库原有一组五条历史回调，迁移保留 1 条 canonical 与 4 条非 canonical 审计，canonical 重复组为零，没有删除历史。
- Verified：最终内容提交为 `dc77faad92e5d45f0271e45747bcbede3dd2ac02`；完整离线套件为 `779 passed, 14 skipped`。干净 wheel 安装确认包含即时反馈模块和第十六份 migration，SHA-256 为 `f4a830387ab058af19bc465789f050daca6d6f0d25ab139b427de9ebc04babbb`。
- Deployment：发布件保存在 `releases/20260804_150033_card_feedback_dc77faa/`。升级前备份成功且为 122949 bytes、`0600 lf_target_dev:lf_target_dev`；长期库应用第十六份 migration 后，六个 Python 服务均回读 `active / running / NRestarts=0`。
- Acceptance：测试组织中的新人员选择卡只接受 1 个 canonical 动作并创建 1 个草稿，领域处理在入站后 3.393 秒完成，最终回复在 5.793 秒完成。飞书服务端读回原消息为 `interactive / updated`，包含“人员分工已确认”，不含“处理中”或提交按钮标签。
- Boundary：用户忘记记录瞬态蓝色状态的视觉耗时，因此本轮只证明即时反馈代码已部署、动作与最终卡片真栈闭环，不宣称已测得客户端即时变化时间。以上仅代表开发服务器和测试组织，不代表生产上线。

## v0.31.1-draft · 2026-08-04 · Agent 失败恢复开发真栈闭环

- Fixed：Card 2.0 的两个操作使用唯一按钮名称；桥接层归一化 lark-cli 字符串化 `action_value`、可缺失 `action_name` 和微秒时间戳。若动作名称存在则必须与服务端动作值交叉一致，身份与授权仍完全由服务端事实决定。
- Shared：新增统一事件时间解析边界，恢复卡与人员分工卡共同接受秒、毫秒和微秒时间戳，避免两个回调桥接器独立演化。
- Verified：最终实现内容提交为 `50d3d7136160f4208421ff194f6929200103f141`；完整离线套件为 `776 passed, 13 skipped`，定向变异覆盖动作名称和微秒时间戳旧缺陷。
- Deployment：wheel SHA-256 为 `5000b1ebdc42524cc7f709ebc7f2fc723d2fd969e9d234540b132001f958c56b`，长期开发库已应用十五份 migration。六个 Python 服务统一重启后均为 active、`NRestarts=0`。
- Acceptance：真实合成实例的两个不同失败卡片分别创建 Attempt 2 与 3，人工接管创建 Attempt 4 与飞书 Task；Task 完成后 Instance 与 Attempt 4 进入 `done`，Attempts 1 至 3、错误、审计和投影全部保留，完成文档和最终通知已投影。
- Boundary：以上仅证明开发服务器与测试组织中的真栈闭环，不代表生产上线、生产容量或生产高可用。

## v0.31.0-draft · 2026-08-04 · Agent 失败恢复与人工接管

- Added：自动 Agent / Tool 节点失败后向节点 Owner 发送 Card 2.0，提供“重新执行”和“人工接管”。卡片回调进入耐久 IM 命令，操作成功后更新原卡片并发送文本回执。
- Domain：重试为目标节点及可达下游创建新 Attempt；人工接管为失败节点创建新 `waiting_human` Attempt 和 Human Task。原失败 Attempt、结果、错误代码和审计保留，人工接管 Task 也会在后续重启时被受控关闭。
- Security：操作人只从飞书顶层认证字段取值；凭据侧重新验证企业成员，领域侧精确校验节点 Owner、Instance version、Node version 和 Attempt 编号。卡片只显示稳定 `error_code`，不投影原始异常文本。
- Database：新增 migration `0015_recovery_cards`，为耐久命令增加卡片更新 token。wheel 回读已确认包含恢复模块与第十五份 migration。
- Verified：实现内容提交为 `fc48b4f8a295c19ba02f08e5b87e006988eccf44`；完整离线套件为 `769 passed, 13 skipped`；Owner 授权定向变异会让回归测试失败。
- Boundary：本记录反映已提交的代码与离线证据。文档提交时，长期 PostgreSQL 库 migration、开发服务部署和真实飞书恢复卡回调仍待验收，不得描述为开发真栈或生产上线。

## v0.30.0-draft · 2026-08-04 · 单聊人员选择卡与交互延迟收敛

- Added：多角色模板在单聊中缺少显式绑定时返回 Card 2.0 人员选择表单。候选快照、卡片发送、回调、目录再验证、领域处理、卡片更新和文本回复都使用 PostgreSQL 耐久状态；成功回调只创建一个冻结草稿，并把原卡片更新为绿色已确认状态。
- Security：回调只接受原命令发送者；被选人员必须来自冻结候选快照并再次通过当前企业活跃成员校验。卡片中的身份、显示名称和手填 open_id 不参与授权，领域侧仍不读取 lark-cli profile。
- Fixed：接受飞书回调中的秒、毫秒和微秒时间戳；补齐开发凭据身份对 `workflow_role_binding_actions` 的最小 ACL；启用 `config.update_multi`；卡片更新失败进入结构化计数与日志；已禁用的已确认选择器不再携带 `required=true`。
- Performance：Runtime 与 Projection 的开发空闲轮询上限由 5 秒收紧到 1 秒。真实人员分工卡片回调的服务端总耗时从 8.881 秒降至 3.272 秒，用户观察约 4 秒。长期仍需评估数据库通知唤醒，当前数据不能外推到生产负载。
- Deployment：代码发布件对应内容提交 `19ea7be`，wheel SHA-256 为 `ab18ddf5a2cf42084129893a9e2e16640ed1b769fb4bc089aa361128588688e0`；长期开发库已应用十四份 migration，六个 Python 服务 active 且 `NRestarts=0`。开发延迟配置与内容提交 `409167d` 对齐，原 5 秒 env 已保留可恢复备份。
- Validation：群聊 mention 和单聊 Card 2.0 两条跨人员正向入口均在测试组织通过。完整离线套件为 `758 passed, 13 skipped`。该证据仅代表开发环境和测试组织，不代表生产上线。

## v0.29.0-draft · 2026-08-03 · 飞书 mention 跨人员角色绑定

- Added：`/larkflow start <template_id> [JSON对象] [role=@成员 ...]` 支持按逻辑角色绑定本条消息中真实 @到的成员；未显式绑定的角色仍归发起人，Instance Owner 不变。新增 `collaborative_agent_review` 双角色 Human-Agent-Human 模板。
- Security：桥接层只保存飞书 mention 的 key 与 open_id，不保存显示名称。凭据侧在领域命令前验证发送者和被引用人员均为当前 tenant 活跃成员；领域侧只接受本条耐久消息中的 mention key，不接受手填 open_id、名称或缺失元数据的 token。群聊只允许认证 mention token 位于 `/larkflow` 前。
- Database：新增 migration `0013_im_command_mentions`，以受数组约束的 JSONB 保存最小化 mention 元数据，保证凭据侧校验和领域侧角色冻结读取同一条命令记录。增加可选真实 PostgreSQL 往返测试。
- Compatibility：公共 `parse_im_command` 返回值保持不变；旧的 `start` 命令继续把全部角色绑定给发送者。重复角色、未知角色、非法大小写、非 mention 值、缺失 mention 和非活跃成员均被拒绝。
- Verified：完整离线套件 `740 passed, 13 skipped`；本次聚焦套件 `73 passed`。默认套件不访问网络、真实飞书或 PostgreSQL。
- Boundary：内容提交 `289fdc0` 已推送；尚未部署或升级长期开发库，也未在真实群聊中完成跨人员正向创建、确认和 Task 投影验收，不能标记为开发真栈通过。

## v0.28.2-draft · 2026-08-03 · 跨人员非 Owner 真实飞书回归

- Directory：开发应用发布所需通讯录数据范围后，中央应用从根部门目录读取到五名活跃成员，并能解析选定测试成员在本应用下的身份。
- Acceptance：以该测试成员为 Owner 创建三节点合成实例并生成真实 Human Task 投影；测试成员无需完成待办。当前登录用户从真实飞书会话发送 `/larkflow edit`，命令被耐久处理为 `rejected:command`，合并拒绝回复成功发送。
- Integrity：实例保持 `running / graph_revision 1`，目标节点标题不变；GraphEditPreview 为 0，`instance.graph_edited` 审计为 0，拒绝路径没有污染领域状态。
- Operations：五个 Target 服务与 legacy 事件消费者均为 active 且 `NRestarts=0`；本次验收窗口没有 warning 级日志。
- Boundary：测试实例由正式中央 CLI 创建并确认，Owner 身份已由同一中央应用独立实时验证；本结论证明开发测试组织中的跨人员非 Owner 编辑拒绝，不代表生产装配或完整权限清单已经验收。

## v0.28.1-draft · 2026-08-03 · 运行中未来区域真实飞书验收

- Acceptance：Owner 从真实飞书创建并确认三节点实例，在首个 Human 节点等待时预览并确认最终 Human 节点改名；重复确认返回 no-op。后续 Agent、更新标题的 Human Task、完成 Docx 与最终通知均真实投影并从飞书服务端回读，Instance 最终为 `done / version 8 / graph_revision 2`，三个当前 Attempt 均为 `done`。
- Rejection：独立实例真实拒绝对 `waiting_human` 节点的冻结线修改和会形成环的依赖修改。另一个有效预览创建后先推进 Human 节点，使 aggregate version 漂移，再确认时收到陈旧预览拒绝；预览保持未消费，Instance 最终为 `done / version 7 / graph_revision 1`，图编辑审计为 0。
- Persistence：正向实例只有一条 `instance.graph_edited` 审计，预览记录为已消费并保存应用版本；两条 Human Task、完成文档、自动节点消息与最终通知全部绑定。负向实例两条 Human Task、完成文档与消息也已绑定，拒绝路径没有污染图 revision 或审计。
- Operations：五个 Target 服务与 legacy 事件消费者均为 active 且 `NRestarts=0`；验收窗口没有 warning 级日志。两个验收实例都已完成，没有遗留运行中节点。
- Tester：组织中新增激活成员可由当前用户搜索到，已通过用户身份为其中一人成功创建并分配明确标注“无需操作”的合成 Task，任务不要求完成。
- Boundary：开发应用的通讯录数据范围仍只返回当前 Owner，读取根部门返回 `40004`。中央应用尚不能取得新增成员在本应用下的 open_id，因此没有伪造跨应用身份，也没有把真实跨人员非 Owner 命令标记通过。下一次回归只需把一名测试成员加入最小通讯录数据范围，不需要其完成 Task。

## v0.28.0-draft · 2026-08-03 · 运行中未来区域安全编辑

- Added：新增 `/larkflow edit <instance_id> <JSON操作数组>` 与 `/larkflow edit-confirm <preview_id>`。首版支持有界的 `add_node / update_node / remove_node`，只修改 `running` 且未锁定 Instance 中没有执行痕迹的 `pending / ready` 节点；Template 和已执行历史保持不变。
- Security：GraphEditPreview 默认有效 15 分钟，绑定 tenant、Instance、创建 actor、规范化操作、增删改集合、aggregate version、当前与目标 `graph_revision` 及候选 Snapshot SHA-256。确认重新授权当前 Owner、重放操作并比较完整语义摘要；客户端身份、revision、影响集合与候选图都不可信。
- Database：新增 migration `0012_graph_edit_previews`，保存耐久预览、操作摘要、候选图哈希、消费时间与应用版本。确认事务原子保存 aggregate、消费预览、增加一次 `graph_revision`、追加一条审计及必要 outbox；重复确认只回读已应用结果。
- Projection：未来节点被删除后，先前排队的节点创建事件按 no-op 收口，不会重建已删除节点的外部 Task。若编辑后剩余节点均已完成，实例可以进入完成态并请求完成投影。
- Verified：完整离线套件 `726 passed, 12 skipped`。一次性 PostgreSQL 14 应用十二份 migration，两个真实连接并发确认同一编辑预览时恰好一路执行、一路幂等回放，aggregate version 和 `graph_revision` 都只增加 1，节点、依赖和单条审计正确；测试库与临时文件随后删除并回读为不存在。
- Deployment：内容提交 `6645d9d` 构建的 wheel SHA-256 为 `7ef30780e53df895a4c93d3c4eeb1783007cf2ed5f5c26015120f722423169d1`，保存在 `releases/20260803_190102_graph_edit_6645d9d/`。升级前备份回读成功，长期库已应用十二份 migration；六个 Python 服务统一启动后 active、`NRestarts=0`，部署窗口无 warning 级日志。
- Acceptance：真实飞书 `edit / edit-confirm` 命令验收将在本次发布流程的文档提交后执行，不在本提交中提前标记通过。
- Boundary：当前只提供窄 JSON 命令和服务端文本预览，没有图形化 diff、批量编排体验或生产装配。验证仅代表开发环境，不代表生产上线。

## v0.27.0-draft · 2026-08-03 · 完整实例安全重启闭环

- Added：新增 `/larkflow restart-all <instance_id>`，并让共享的 `/larkflow restart-confirm <preview_id>` 按显式 `node / instance` scope 执行。完整实例预览列出拓扑排序后的全部节点，确认后为全图创建新 Attempt，从所有根节点重新调度。
- Security：instance scope 不使用特殊节点值模拟，预览节点键必须为空；确认继续绑定 tenant、Instance、创建 actor、aggregate version、`graph_revision`、稳定影响集合和有效期。scope 不匹配、过期或任何状态漂移都拒绝执行。
- Database：新增 migration `0011_restart_scope`，为 RestartPreview 增加 scope、可空节点键和数据库约束。完整重启仍在同一事务内更新 aggregate、消费预览、取消活动旧 Attempt、清除 claim、写一条审计与投影 outbox；重复确认只回读已应用结果。
- Projection：完成文档和最终通知从第二轮开始按当前终端 Attempt 分代，首次完成继续沿用历史幂等键。实例重启后再次完成会产生新的文档与最终通知，旧轮次 Projection、外部资源和结果保持可查。
- Verified：完整离线套件 `715 passed, 11 skipped`。一次性 PostgreSQL 14 使用十一份 migration，分别对节点和完整实例 scope 进行双连接竞争；两者都恰好一路执行、一路幂等回放，aggregate version 只增加 1、旧 Attempt 结果保留、审计只有 1 条。测试库与临时文件随后删除并回读为不存在。
- Deployment：内容提交 `e66f6ab` 构建的 wheel SHA-256 为 `c1aa5b65eaba977e53175889d64332114a822b64c80349da4160abea01747751`，保存在 `releases/20260803_174919_full_restart/`。升级前备份回读成功，长期库已应用十一份 migration；六个 Python 服务统一启动后 active、`NRestarts=0`，验收窗口无错误级日志。
- Acceptance：测试组织实例 `im_64450d61fa02de36f86bcedd` 完成三节点全图预览与确认，新 Attempt 分别为 2、2、3。根节点重新调度后，两个 Human Task 由 Owner 完成，Agent 再次生成结果，Instance 最终为 `done / version 16 / graph_revision 1`。重复确认没有新增版本、Attempt、Task 或审计；旧 Attempt、Task、结果、完成文档和最终通知均保留。新完成文档与最终通知使用不同外部 ID，新文档已从飞书服务端回读三节点结果。
- Boundary：完整实例重启已完成开发部署与测试组织验收；运行中未来区域编辑、跨轮次浏览产品体验和生产装配仍未实现。该证据不代表生产上线。

## v0.26.0-draft · 2026-08-03 · 节点安全重启闭环

- Added：新增 `/larkflow restart <instance_id> <node_key>` 与 `/larkflow restart-confirm <preview_id>`。预览列出目标及全部可达下游，确认后为影响集合创建新 Attempt，旧 Attempt、结果和质量记录保持可查。
- Security：预览绑定 tenant、Instance、创建 actor、aggregate version、`graph_revision`、稳定影响集合和 15 分钟有效期；确认时重新授权当前 Instance Owner。过期、版本漂移、图漂移、影响集合变化、不完整依赖和未覆盖失败节点都会拒绝执行。
- Database：新增 migration `0010_restart_previews`。确认事务同时更新 aggregate、消费预览、取消活动旧 Attempt、清除 claim、写入一条重启审计和旧 Human Task 收口 outbox；重复确认只返回已应用结果。
- Projection：旧 Human Attempt 的 Task 按历史 Attempt 状态关闭，新 Attempt 使用不同稳定幂等键创建新 Task；旧 Task 的迟到完成不能推进当前 Attempt。
- Verified：完整离线套件 `709 passed, 10 skipped`。移除 Owner 检查、版本检查、可达下游计算、外部失败节点保护或历史 Task 状态映射时，对应变异测试都会失败。一次性 PostgreSQL 14 双连接同时确认同一预览，恰好一路执行、一路幂等回放，aggregate version 只增加 1，审计只有 1 条；测试库与上传件随后删除。
- Deployment：内容提交 `b319494` 构建的 wheel SHA-256 为 `093c20fb9d1936f3060fbc3153e8805cee278acaef88eaefbdf2eac40740f358`，保存在 `releases/20260803_162724_node_restart/`。升级前备份成功，长期库已应用十份 migration；六个 Python 服务统一启动后 active、`NRestarts=0`，验收窗口无错误级日志。
- Acceptance：测试组织实例 `im_64450d61fa02de36f86bcedd` 在最终 Human 节点等待时完成预览与确认。旧 Attempt 1 保留为 canceled，旧 Task 服务端为 done；新 Attempt 2 使用不同 Task，重复确认不增加版本、Attempt、Task 或审计。人工完成新 Task 后 Instance 回到 done，两条完成 Inbox 为 processed，11 条 Outbox 为 published，重启审计恰好 1 条，完成文档与最终通知均已投影。
- Boundary：当前只实现节点重启，完整实例重启与运行中未来区域编辑仍未实现。验收只代表开发环境和测试组织，不代表生产装配。

## v0.25.0-draft · 2026-08-03 · Owner 最近实例列表闭环

- Added：新增 `/larkflow list`，通过领域层 `list_for_owner` 和独立摘要 DTO 返回当前发送者拥有的最近实例；帮助文本同步扩展为五个窄命令。
- Security：查询同时限定 tenant 与 Instance Owner，只返回 Instance ID、目标摘要、状态和完成进度，不读取节点结果正文或人员 ID；最多展示十条，并以第十一条只判断是否需要截断提示。
- Database：新增 migration `0009_owner_instance_list`，为 `(tenant_id, owner_person_id, created_at DESC, id DESC)` 增加复合索引。一次性 PostgreSQL 14 已验证 migration 重入、Owner 与 tenant 隔离、稳定倒序、进度汇总和索引存在性。
- Verified：完整离线套件 `698 passed, 9 skipped`，删除 Owner 或 tenant 过滤的定向变异均使对应隔离测试失败。wheel SHA-256 为 `ced55f224cce312aec779fd1dd246403bffa757901d19968c36c355d06b152da`，六个 Python 服务统一重启后 active、`NRestarts=0`。
- Acceptance：Owner 在测试组织发送 `/larkflow list`，耐久命令记录为 `processed / instances_listed`，回复为 `sent`；飞书服务端按消息 ID 回读到十条本人实例，包含完成与进行中进度及详情提示，不包含人员 ID。
- Boundary：仅完成开发环境和测试组织验收，不代表生产装配；真实非 Owner 和跨 tenant 尝试未在测试组织逐项演练，隔离由离线变异测试与一次性 PostgreSQL 验证覆盖。

## v0.24.0-draft · 2026-08-03 · Owner 专属状态查询闭环

- Added：新增 `/larkflow status <instance_id>`，通过领域层 `get_for_owner` 返回流程状态、进度和节点摘要，命令保持只读。
- Security：仅 Instance Owner 可查询；实例不存在与非 Owner 使用相同错误；回复不包含节点结果正文或人员 ID，最多列出 20 个节点，每个可变字段最多 120 个字符。
- Verified：完整离线套件 `694 passed, 8 skipped`。wheel SHA-256 为 `b81103d0edd7a38922b3a0298c27f97b54dd9a11ae229b6da9676cbb068c6c2c`，六个 Python 服务统一重启后 active、`NRestarts=0`。
- Acceptance：Owner 在测试组织查询一个已完成的四节点实例，耐久命令记录为 `processed / status_shown`，回复为 `sent`；飞书服务端按消息 ID 回读到唯一文本消息，包含完成状态、`4/4` 与相对责任人，不包含 open_id。
- Boundary：仅完成开发环境和测试组织验收，不代表生产装配；非 Owner 查询和长流程截断已由离线测试覆盖，尚未在真实组织中逐项演练。

## v0.23.0-draft · 2026-08-03 · 飞书 IM 入口与完成投影闭环

- Added：耐久 `im.message.receive_v1` 命令入口，支持 `/larkflow help / start / confirm`；发送者先经当前企业活跃成员校验，命令、验证、领域执行与回复分别去重和持久化。
- Compatibility：桥接层同时接受飞书原始 V2 信封和 lark-cli 拍平事件，分别处理 JSON 字符串与普通文本形态的 `content`。
- Added：Agent / Tool 结果消息、Instance 完成 Docx、最终 Owner 通知，以及只修复一个已完成实例的幂等 `reconcile-instance-completion`。
- Operations：新增 `deploy/restart-development-services`，统一覆盖五个 Target 服务与 legacy，避免部分部署后旧进程继续运行旧代码。
- Verified：完整离线套件 `688 passed, 8 skipped`。wheel SHA-256 为 `2925e23856ea9107cdce16e0af1387f971c9357d52bc121d756b3f05a47c4162`，六个 Python 服务统一重启后 active、`NRestarts=0`。
- Acceptance：测试组织中的真实飞书消息完成草稿创建、确认、Human-Agent-Tool-Human、完成文档和最终通知；文档与消息都通过服务端回读，重复修复为 no-op。
- Boundary：仅完成开发环境和测试组织验收。Task 事件路径本轮仍为零事件，周期状态轮询是可靠完成入口；更多命令、更多业务 Tool、编辑 / 重启产品入口和生产装配仍未完成。

## v0.22.0-draft · 2026-08-02 · 企业目录 Owner 校验（ADR-067）

- Added：可选 `PersonDirectory` Port、lark-cli bot adapter 和草稿写入前的 Instance / Node Owner 去重校验。
- Security：目录缺字段、返回 ID 不匹配、未激活、冻结、离职、退出或未入职均 fail closed；默认关闭，不静默扩大应用权限。
- Verified：完整离线套件 `670 passed, 8 skipped`。wheel SHA-256 为 `537b6d8f4106c3f66f180309a58530b11c675d9e27f3edfa08c6439a7ccc161c`；升级前备份成功，五个服务重启后 active、`NRestarts=0`。
- Blocked：当前开发应用调用目录明确返回缺少通讯录只读 scope。未获权限扩展确认前保持 `LARKFLOW_TARGET_VALIDATE_DIRECTORY=false`，因此尚无目录真栈通过证据。

## v0.21.0-draft · 2026-08-02 · 确定性 Tool 与真实混合流程（ADR-066）

- Added：`ToolExecutorRouter` 按 `work.tool.kind` 路由内部 adapter；首个 `content.check` 对直接依赖正文执行长度与必需词检查，返回稳定证据和 `pass / fail` verdict。
- Added：`target_checked_agent_review.yaml` 四节点模板、显式 Tool 开关、输入长度上限和离线回归覆盖；wheel 已确认包含新模块与模板。
- Verified：完整离线套件 `663 passed, 8 skipped`。开发实例 `mixed_tool_acceptance_20260802_200044` 以合成输入完成 Human-Agent-Tool-Human，最终 Task 同时展示 Agent 正文和 `content.check` 证据，Tool verdict 为 `pass`。
- Deployment：wheel SHA-256 为 `0a51863069bf94f67a3fc2c9755d57b1442e8f2c7bdf6121734d60574836af15`，保存在 `releases/content-check-0a51863069bf/`；两个 Human Task、四个 Node 与 Attempt、Instance 均为 `done`，两条 Inbox 为 `processed`，10 条 Outbox 为 `published`。
- Resilience：五个 Target 服务整体重启后保持 active、`NRestarts=0`，实例和 Projection 状态不变。Caddy 继续 disabled / inactive。
- Boundary：`content.check` 只验证确定性文本契约，不验证事实、语义质量或业务正确性。通用飞书入口、企业目录、IM / Doc 投影、更多业务 Tool 和生产装配仍未完成。

## v0.20.0-draft · 2026-08-02 · Personal Agent Edge Proof v0（ADR-065）

- Added：一次性配对、哈希设备凭据、设备列出与撤销、追加型 Edge 审计，以及 PostgreSQL migration `0007_edge_devices`。
- Added：`/edge/v1` 私有 JSON 边界，覆盖 pair、claim、renew、complete 与 fail；Gateway 强制 loopback 监听，客户端对远程地址强制 HTTPS、拒绝重定向且默认不继承系统代理。
- Added：`larkflow-edge-gateway` 运维入口、`larkflow-edge pair / run-once` 用户入口、`target_personal_edge_review.yaml`，以及 Codex `read-only + ephemeral + ignore-user-config + skip-git-repo-check` 本机适配器。
- Security：设备只能领取本人拥有且 kind 为 `personal.readonly` 的 Agent 节点；Human gate、中央 `llm.generate` 和其他人员节点不可领取。Edge、Target 与飞书凭据从 Codex 子进程环境移除，超时按进程组终止，撤销设备与迟到结果均 fail closed。
- Resilience：长执行在原 Attempt 上续租；Worker、token、版本与租期仍由中央校验。本机适配器异常不直接把业务流程判为失败，等待当前租约过期后由合法设备恢复。
- Fixed：非 Git 工作区显式允许只读执行。本机需要 Clash 等环境代理时，默认最小环境仍不传代理；用户必须显式启用 loopback proxy 继承，且只接受无用户名和密码的 loopback HTTP / HTTPS / SOCKS URL，远程或带凭据代理丢弃。首次无代理合成测试在 120 秒超时，Edge 终止整个 Codex 进程组并保留中央节点等待租约恢复，没有把业务流程判失败。
- Verified：完整离线套件 `653 passed, 8 skipped`。最终 wheel 共 79 个条目并包含全部 Edge 模块、migration 与模板，SHA-256 为 `39a363e03aded26ddf5ab326024a9515ba97acf44e11e7a115af224048c623dd`。同一 wheel 在一次性 PostgreSQL 14 数据库应用七份 migration，第二次应用为空；同一配对码两路竞争恰好一条成功、一条 `PairingCodeUsedError`。领取、续租、完成、撤销、原始 secret 不落库和 Edge 审计不可改写均通过。测试库、脚本与 wheel 随后删除，既有四个 Target 服务回读 active、`NRestarts=0`。
- Verified：合成临时工作区通过真实 loopback HTTP 完成配对、领取、续租与结果提交，本机 Codex 在显式无凭据 loopback 代理下 20.6 秒返回 58 字摘要，命中 Project Aurora；中央 Instance 为 `done`，临时工作区随后删除。
- Deployment：内容提交 `b1d6165` 构建的 wheel SHA-256 为 `7728894b1338f89b465553a1064bd720e44302c2dafa238286e14d1f084e5c74`。升级前备份成功，长期开发库已应用 `0007_edge_devices`；`larkflow-target-edge.service` 以 `lf_target_dev` 常驻，只监听 `127.0.0.1:8765`，运行态拒绝非 loopback 网络。Runtime、Projection、两类 Inbound、Edge Gateway 与 legacy 均为 active，`NRestarts=0`。
- Verified：临时 SSH 隧道把本机 `127.0.0.1:18765` 映射到中央 loopback Gateway。两个合成单节点实例均由本机 Codex 只读执行并回传为 `done`；第二条 22.6 秒执行在同一 Attempt 上产生 10 条 `node.claim_renewed` 审计。测试设备随后撤销，旧凭据再次领取返回 `device has been revoked`；本机凭据、隧道和两端临时上传件均已删除。
- Boundary：尚未部署公网 HTTPS。服务器没有反向代理，公网监听仍只有 SSH；凭据仍是当前用户 `0600` 文件，不是系统 Keychain。目录级读取隔离、安全政策、持续采用和市场价值均未验证。
- Update：开发服务器已安装 Caddy 2.11.4，专用 DNS-only 子域名取得 Let’s Encrypt 证书，源站反向代理、可信链、正确 SAN、安全响应头与未认证 401 均已验证；仓库新增脱敏 Caddyfile 模板。
- Blocked：公网设备验收未通过。员工电脑后续 TLS ClientHello 被连接重置，服务器抓包确认请求未到达 ECS，而源站 loopback 与公网 hairpin 始终正常。证据与阿里云中国内地 ICP 接入备案阻断一致；合成实例保持未认领，没有签发配对码、设备凭据或运行 Codex。
- Boundary：必须先完成 ICP 接入备案，或迁移到合规的非中国内地环境，再重跑公网配对、领取、续租、回传和撤销。证书存在不代表公网 Edge 已可用，也不改变安全评审、系统凭据存储和产品化仍未完成的结论。
- Safety：确认阻断后已停止并禁用 Caddy 开机启动，服务器公网监听恢复为只有 SSH；Caddy 配置、证书和回滚备份保留，loopback Gateway 与其他 Target 服务保持 active。

## v0.19.0-draft · 2026-08-02 · Target Task 完成状态轮询（ADR-064）

- Added：Projection 服务周期扫描当前 `waiting_human` 节点绑定的 Task，观察到完成后以稳定信号 ID 写入 PostgreSQL Inbox；新增 `reconcile-completions` 运维命令、轮询周期与批量配置。
- Security：轮询不会直接提交节点。凭据侧仍重新读取 Task，领域侧仍校验当前 Attempt、Projection 绑定、应用来源、唯一 Owner 和完成人，轮询结果不作为 actor 证明。
- Resilience：飞书 Task 事件降为低延迟优化，不再是可靠性前提；完成信号可重复生成且由 Inbox 去重，单节点读取失败不会阻塞其他实例，常驻循环暴露每轮结构化计数。
- Verified：完整离线套件 `622 passed, 7 skipped`，wheel 已确认包含新模块。开发服务器首次扫描读取 3 个当前 Human Task，新增 2 条完成信号；凭据侧验证和领域侧提交各 2 条，两个滞留实例、Node 与 Projection 全部完成。显式重跑只看到 1 个待办 Task 且新增信号为 0。五个服务均为 active、`NRestarts=0`。
- Deployment：开发 wheel SHA-256 为 `e725f5ba39eedf264c675998082d1a21689b6e01632aba6de31086b14b72f8d7`，保存在 `releases/poll-e725f5ba39ee/`，对应内容提交 `e81aedf`。
- Permissions：开发应用已移除临时管理 scope `application:application:patch` 与 `application:application:self_manage`，权限页回读只剩 `task:task:read + task:task:writeonly`，在线版本仍为已发布的 `1.0.7`。收口后真实 Task 创建、完成和轮询读取均通过，五个服务保持 active、`NRestarts=0`。
- Boundary：当前按活跃 Human Task 线性产生只读 API 调用，只适用于单企业开发阶段。规模扩大前需要速率预算、抖动、游标和告警，不代表生产装配完成。

## v0.18.0-draft · 2026-08-01 · Target Task 启动对账与缺失重建（ADR-063）

- Added：Projection 常驻循环在消费 Outbox 前按 Instance ID 分页对账；新增 `reconcile-projections` 运维命令与 `LARKFLOW_TARGET_PROJECTION_RECONCILE_BATCH_SIZE`。
- Recovery：当前 `waiting_human` 节点没有 Projection 时使用原稳定幂等键补建；已绑定 Task 只在飞书明确返回 `1470404` 时使用带 repair generation 的新稳定键重建，并以旧绑定为并发条件换绑。
- Safety：权限、网络、限流或服务端错误不会被误判为 Task 删除；单个实例失败不阻塞其他实例；已终止节点不补发历史 Task。
- Verified：完整离线套件 `617 passed, 7 skipped`；回归测试覆盖缺失记录、外部删除、丢失响应、逐实例容错、终态边界、启动顺序和错误码分流。一次性 PostgreSQL 14 数据库应用六份 migration，回读补建 1、重建 1、重入 1，随后删除数据库与临时上传件。
- Deployment：内容提交 `99af528` 构建 wheel，SHA-256 为 `ed5d597db3d593322a549e02700543f32ef317b2e0dfdab4a2605f7f9fb119e4`。部署前备份成功，四个 Target 服务与 legacy 消费者均回读 active、`NRestarts=0`；启动与手动对账均为 1 个实例、2 个节点、2 条绑定不变且 0 失败。
- Boundary：开发环境尚未对真实飞书 Task 执行删除后重建，也不代表生产发布。
- Acceptance：随后以专用单 Human 实例完成开发环境真栈验收。删除前对账 3 条绑定全部不变；删除后 Task 读取返回 `1470404`，下一次对账只重建 1 条并原子换绑，`repair_generation=1`；再对账 3 条全部不变。2026-08-02 人工完成新 Task 后，凭据侧验证 1 条、领域侧提交 1 条且均无失败，Instance、Node、Attempt 和 Projection 均进入完成态，Inbox 为 `processed`。该证据关闭上一行的开发环境删除验收缺口，不改变非生产边界。

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
