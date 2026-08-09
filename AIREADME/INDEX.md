# larkflow · 飞流 · AIREADME

> 飞书原生的企业协作 DAG：把多人流程拆成有依赖、有唯一责任人、可验收和可追溯的节点。
>
> 文档状态：2026-08-10 Phase 1 首次成功路径与可用性收口。内容提交 `86189216a0b67cf258daa4027d368257eee7491a` 将流程详情固定为“描述目标、核对流程、确认启动、完成或判断、查看结果”五步主线；当前主动作置顶，本人负责的 Human Task 或决定直接在顶部处理；提交、判断与转交后刷新当前详情；流程结果进入主页面，并优先展示 Agent 或 Tool 的实质产出；画板、每次执行和审计默认收进高级视图。聚焦套件为 `96 passed`，完整离线套件为 `1037 passed, 24 skipped`；本地深浅色、桌面和移动页面均无页面级横向溢出。wheel SHA-256 为 `5851a5bc7d296d0a40e74552aeae631175c50953b59adb7f82c3431063ec802f`，已部署到 `/srv/larkflow/target/releases/20260810_0233_workflow_mainline_8618921/`；备份、23 份 migration、十个 Python 服务、Caddy、公网资源哈希与安全边界均已回读。真实飞书 OAuth 连续路径仍未验收，因此当前产品仍不适合邀请测试或推荐使用。飞书继续承担通知、待办和备用决定卡，PostgreSQL 继续是唯一业务真相。多人实时协同、通用自由白板与 Personal Agent Edge 扩张均不属于当前优先级。正式域名、生产容量和异机容灾仍缺。现有 Personal Agent Edge 只保留已验证的开发 Proof，正式分发仍为 No-Go。`Target` 表示目标产品契约，`As-built` 表示当前代码事实，两者不得混写。
>
> 内容提交 `4eb417cc9d020b2165b0fa8f857e3cdc8806d126` 来自草稿画板真实登录验收中的可用性发现。部署前旧标签页仍加载旧 JavaScript，显式刷新后草稿编辑按钮正确启用；随后发现 9 像素连接端点在自动适配缩放后只剩约 3 到 4 像素。修复把端点扩大到 24 像素，并增加高亮、十字光标和悬停反馈。完整离线套件为 `1034 passed, 24 skipped`。wheel SHA-256 为 `339c7ec280b4d3151dd49c33e9f5473433e26e9fe33009e0954e32ca1ed95686`，已部署到 `/srv/larkflow/target/releases/20260809_232154_canvas_handles_4eb417c/`；安装态、loopback 和公网 CSS 哈希一致，Console 与 Caddy 为 active，`NRestarts=0`，部署窗口无 warning。随后在公网真实登录工作台完成拖拽连接、Graph r1 到 r2 预览确认、选中新增连线、断开和 Graph r2 到 r3 预览确认。最终可见画板只保留原有两条边；PostgreSQL 回读草稿仍未启动，NodeInstance、Attempt 与 Projection 均为 0，两条预览和两条图编辑审计均只出现一次。
>
> 内容提交 `f320fd5b9b200fae24cefeb6a853c684a38e7565` 让草稿与运行中未来区域共用 GraphEditPreview，并为 React Flow 画板增加依赖连接与断开操作。草稿确认只更新 Snapshot 和 graph revision，启动仍是独立动作。完整离线套件为 `1034 passed, 24 skipped`。wheel SHA-256 为 `fd164c85fe0d3f0076d9ed37a4d2212e149cc4cd394fe136ec21f13ce132c1d9`，已部署到 `/srv/larkflow/target/releases/20260809_223113_draft_canvas_f320fd5/`。升级前 custom-format 备份可读，migration runner 返回空集，ledger 保持 23 份；十个 Python 服务与 Caddy 为 `active / NRestarts=0`，Console 公网与 loopback 200，未认证图编辑 401，安装与公网静态资源哈希一致，部署窗口 warning 为 0。一次性真实 PostgreSQL 数据库完成草稿连接、断开和独立启动，编辑期间保持 `0 NodeInstance / 0 Attempt`，启动后为 3 个节点和 3 个 Attempt，测试库随后删除。真实登录页面刷新保持登录，但本轮浏览器控制器超时，未完成可见拖拽手势复验。
>
> 内容提交 `e6106e5f218f9c520928bef0293899ace7a2395f` 增加 `edge-data-v0.1`、显式 `synthetic / public` 分类、结果策略摘要和精确前缀安全卸载。完整离线套件为 `1032 passed, 23 skipped`。clean commit 的主 wheel、最小员工 artifact 与 manifest SHA-256 分别为 `e163cb8a118c26f74d2543b2102564a841e356caa64692a3e4cbf1a489932b26`、`b535408b879e2767f9fbe995f3fb30def7a6b4016c23c6a6d09b078fc03372ea` 和 `86b61dfbf3dd0aa2e6bd57aa7d967164f77534dab9f1f010e8659c7ed516dd60`。干净员工 Mac 上的纯合成实例 `edge_clean_mac_e6106e5_20260809_215429` 完成 3/3 Attempt 1，撤销后的旧凭据被中央拒绝，安装、临时 Keychain、元数据、工作区和隧道均按精确范围清理。主 wheel 已部署到 `/srv/larkflow/target/releases/20260809_2203_edge_policy_e6106e5/`；备份、23 份 migration、十个 Python 服务、Caddy、Console 200、未认证 Edge 401、loopback 监听与零 warning 均已读回。
>
> 内容提交 `ab3ad5e8f00dded71763c70e6437ff0782050e8b` 把 Personal Agent Edge 从普通只读 sandbox 收紧为 `larkflow_edge_readonly` permission Profile。根路径默认拒绝，最小系统路径只读，仅所选工作区可读，并排除 Agent 配置、环境文件、证书和常见私钥名；网络命令与其他本机工具禁用。`doctor --workspace` 和每次执行器构造都运行工作区可读、外部哨兵不可读两项真实探针，`run-once` 与 `serve` 在加载凭据或领取工作前要求当前前台会话显式确认模型外发。完整离线套件为 `1029 passed, 23 skipped`，真实 Codex 0.147 还验证 `.env` 被拒绝。最小 bundle 为 9 个 wheel，artifact SHA-256 为 `b59e918a2b823cd1c3c76349211b74615bcdda995c1c9645bfbcaa746cf64634`，manifest SHA-256 为 `a6074ab95cc9347b954cf611681a5244511cd02d48d7b02e303e04569440f771`。主 wheel SHA-256 为 `1330bd7c418a87241583b0b9fedccd766eea64951160ec855356d9fd18390042`，已部署到 `/srv/larkflow/target/releases/20260809_2122_edge_permissions_ab3ad5e/`；备份、23 份 migration、十个 Python 服务、Caddy、公网 Console 200、Edge 401 与零 warning 均已读回。该证据不证明无模型数据外发或正式员工分发安全。
>
> 内容提交 `b60cbbd8beb98742cc80082df78ac185274e3a8a` 增加基于 React Flow 12.11.2 与 ELK.js 0.12.0 的受控运行画板。节点拖动只保存当前浏览器布局；增加、修改和删除节点使用既有未来区域 GraphEditPreview，节点返工使用既有 RestartPreview，旧 Attempt、结果与审计继续保留。完整离线套件为 `1019 passed, 23 skipped`，wheel SHA-256 为 `e558f75a2e495d7d1e79e52a1b36458fb55c76ed6eca608e365dc94d43f97221`，已部署到 `/srv/larkflow/target/releases/20260809_175848_console_canvas_b60cbbd/`。migration runner 返回 `versions=[]`，长期 ledger 保持 `23 / 0023_console_draft_requests`；公网工作台返回 200，未登录图编辑接口返回 401，安装与公网静态资源哈希一致，十个 Python 服务和 Caddy 均为 `active / NRestarts=0`，部署窗口 warning 为 0。本地真实浏览器已完成拖动持久化、节点增加、节点修改、删除预览取消和四节点返工预览确认；本轮没有在服务器真实业务实例上执行图变更。
>
> 验收补充：真实登录 Owner 已在公网纯合成实例 `console_draft_2cd1ec1ad73e84abf9292ae0835c4fcc` 完成未来节点修改、三节点返工和末尾 Human 节点新增，Graph 从 r1 进入 r3，实例最终为 `running / version 6 / 4 nodes`。旧 Attempt 保留为 canceled，新返工节点进入 Attempt 2，新增节点为 `pending / Attempt 1`。故意为未来 Agent 节点选择下游节点作为上游依赖时，页面明确拒绝循环依赖；PostgreSQL 没有新增 preview、审计或 revision。页面审计显示两次合法图更新和一次节点重启，五个相关服务均为 `active / NRestarts=0`，验收窗口无 warning。
>
> 内容提交 `482c280cf9007951fb117b835086a4b19eb1f932` 为飞书 Task 描述增加 3000 字符和 10000 字节双重上限，截断时保留流程定位尾注，完整上下文仍以 PostgreSQL 为准。完整离线套件为 `1016 passed, 23 skipped`，wheel SHA-256 为 `9d1cbf7cd1a0880632cdabb2dc31a757e29230d63bf89afc24ccfa3e5e2f08af`，已部署到 `/srv/larkflow/target/releases/20260809_164706_task_desc_482c280/`。真实公开材料实例的失败 outbox 以原幂等键恢复，最终为 `done / version 7 / 3 Attempt 1`；Owner 退回内容并要求补齐官方逐条来源和真实运行约束。该样本关闭投影长度缺陷和首个公开材料真流程门槛，不证明草稿可用于生产决策。下一步同时推进来源约束增强与受控 DAG 画板，不建设通用自由白板。
>
> 内容提交 `432fea77c210e7a2cfa5344054eb30d01706bf87` 增加工作台“发起流程”、耐久 `DraftRequest`、独立生成租约和三条草稿 API。重复 request ID 保持幂等，已生成候选在 Worker 接管时保持冻结，模型或基础设施失败最多尝试五次后进入保留历史的终态；只有生成成功后的独立确认才调用既有领域启动。完整离线套件为 `1015 passed, 23 skipped`。wheel SHA-256 为 `6b320b22804c02eaa2840d9a101bcf1b4ffe75287509816486727588ccdc0198`，已部署到 `/srv/larkflow/target/releases/20260809_0357_console_drafts_432fea7/`，长期库应用 `0023_console_draft_requests`。真实 PostgreSQL 双连接只产生一个 claim，全部服务与 Caddy 为 `active / NRestarts=0`，公网 200、未登录草稿 API 401、安全响应头、安装资源哈希和零 warning 均已读回。同一真实成员主体的五分钟临时会话先以纯合成文本通过同一 API 和真实中央模型创建三节点草稿，数据库保持 `draft / 0 Attempt / 0 Projection`，会话随即注销。随后真实 Chrome 会话从公网工作台点击生成，按钮立即进入“生成中”，页面自动打开草稿 `console_draft_a11c6bb9d2ae071d78b10f802f567119`；独立数据库回读为 `draft / version 0 / 3 nodes / 0 NodeInstance / 0 Attempt / 0 Projection`，请求为 `ready / attempt 1`。本轮没有确认启动或创建外部待办，真实浏览器点击验收已经关闭。
>
> 内容提交 `3fd42df8740825482eb3bbebd5cf69715f37df5b` 把转交后的中央事务状态和飞书异步投影状态分开显示。按钮立即进入“中央已转交，飞书同步中”，接口返回 `projection.status=queued`，失败继续由管理员异常队列承接。完整离线套件为 `1005 passed, 22 skipped`。真实浏览器已分别完成一次普通 Human 提交和一次跨成员转交；所需 Task 权限开通后，既有 outbox 在第 8 次尝试发布。飞书 Task 回读为 `todo / mode=1 / 单一负责人`，负责人和中央 NodeInstance、Projection 一致，`sync_version=2`。wheel SHA-256 为 `79ac572f4feb160db835d8a26b25d77b84e57916542367c347f0df0b65426ee1`，已部署到 `/srv/larkflow/target/releases/20260809_0259_transfer_sync_3fd42df/`；升级前备份、migration、服务、静态资源、公网边界和零 warning 均已回读。
>
> 内容提交 `ed118e7b3a9eeb5b5daed52e3d7b0296896f12f1` 为 Projection outbox 增加默认 24 次的有界重试终态。达到上限的事件进入 `exhausted` 后不再领取，同时保留 payload、累计次数、最后错误和终止时间；migration `0022_outbox_exhaustion` 不删除或覆写历史。完整离线套件为 `1005 passed, 22 skipped`，一次性真实 PostgreSQL 双连接竞争得到一路领取、一路跳过，终止后未来领取为 0。wheel SHA-256 为 `a9f68581294ac65e71b2eae5f97940618289194eedd77c5943c40f539e4f6245`，已部署到 `/srv/larkflow/target/releases/20260809_0201_outbox_exhaustion_ed118e7/`。长期库应用第二十二份 migration 后，两条历史永久无效投影从 1171 次失败进入 `exhausted / 1172`，日志为 `claimed=2 / failed=2 / exhausted=2`；全部服务和 Caddy 为 `active / NRestarts=0`。
>
> 内容提交 `3d438bb476ad9b9f98cd4c2873802a2894718fe4` 为工作台增加普通 Human Task 的有界详情、结果输入框和转交操作。当前负责人可提交结果，或把任务转交给同租户内、应用可见且活跃的成员；服务端同时校验 Attempt 与节点版本，旧负责人立即失权。转交只修改运行时 NodeInstance Owner，冻结 Snapshot 不变，并追加审计与 outbox、更新既有飞书 Task 负责人。决定节点不进入普通任务 API。完整离线套件为 `1003 passed, 22 skipped`；真实 PostgreSQL 双连接竞争、部署 wheel、升级前备份、二十一份 migration、十个 Python 服务、Caddy、公网静态资源、安全响应头、登录态任务与成员目录 API 和临时会话撤销均已读回。后续真实浏览器提交与真实飞书 Task 转交已通过。
>
> 内容提交 `da94891f5e6d01ecee6082a98bab6148abba12ee` 为 Owner 工作台增加受控流程操作。确认草稿、暂停和继续直接复用既有领域服务；取消使用 aggregate version 预览确认，节点与完整实例重启复用耐久 RestartPreview。所有操作继续由服务端重新校验飞书会话、tenant、Instance Owner 与当前状态，跨 Owner 和不存在实例统一返回 404；`feishu` 写请求要求精确同源 `Origin`、专用动作头、空 query 与空 body。按钮会在请求发出前立即显示处理中，高风险操作在同页展示影响节点后再确认。完整离线套件为 `995 passed, 21 skipped`，wheel SHA-256 为 `fca2eee16d3af57dcfb4bb78409a0b6f9e23b7d3d29aa7d7435cc1f26dd3063a`，已部署到 `/srv/larkflow/target/releases/20260808_235309_console_actions_da94891/`。升级前备份、二十一份 migration、`pip check`、公网静态资源哈希、401 边界、安全响应头、全部服务 `active / NRestarts=0` 与零 warning 均已读回。真实登录 Owner 随后在公网工作台直接确认并启动 `internal_trial_20260808_155244`，三个节点均在 Attempt 1 完成，实例终态为 `done / version 7`。两个飞书 Task、Agent 结果消息、完成文档与最终通知均已外部绑定。该证据只关闭首个真实登录 Owner 写操作门槛，暂停、继续、取消和重启仍未逐项完成页面验收。
>
> 内容提交 `e3bd98d155a446a66bdb2c947e124f7ba7fc9c31` 把 Owner 中央工作台重构为本人待处理、全部流程和三页签详情的信息架构，增大正文与操作字号，并提供跟随系统、可本地记忆的浅色和深色主题。领域接口、飞书身份、Owner 隔离和只读边界不变。完整离线等价结果为 `988 passed, 21 skipped`，wheel SHA-256 为 `7b7b5318c4f94b096210629f5c94db1d2369ee2cb94f5e66cfc146ab8a2a5178`，已部署到 `/srv/larkflow/target/releases/20260808_231502_console_ui_e3bd98d/`。本次只重启 Console；九个 Target 服务、legacy 消费者与 Caddy 均为 active，公网与 loopback 页面返回 200，未认证 API 返回 401。该版本尚待真实登录浏览器刷新后完成新版视觉复验。
>
> 内容提交 `ee2fa9439594d765cd08f2caa0f7ecb20d30d78b` 新增 Owner 范围的中央只读控制台。浏览器只能读取服务端映射身份本人发起的最近流程、DAG、历史 Attempt 和追加型审计，不提供确认、重启、编辑或其他写操作。开发鉴权使用至少 32 字符的随机 Bearer token，服务强制监听 loopback；非 Owner 与不存在实例统一返回 404。完整离线套件为 `922 passed, 18 skipped`。wheel SHA-256 为 `58b27648ccaf3f863cf4bb0ca820b3e2209523b58b0574af626aa303c0e4ff5c`，长期库 migration runner 回读 `19 / 0019_draft_generation_progress` 且无待应用版本。控制台及其余九个 Python 服务统一重启后均为 `active / NRestarts=0`，部署窗口 warning 为 0。真实 Owner 浏览器回读 30 条流程，并验证运行中、草稿、DAG、Attempt、审计和锁定状态；其他 Owner 的真实实例返回 404。该入口只供开发试用，生产前仍需飞书登录态或企业 SSO、反向代理授权和更完整的可见性策略。

> 内容提交 `c2e9db99f4b463a895450371dde9b176d6c31ef1` 在不改变领域只读边界的前提下增加飞书应用内员工工作台登录基础。`feishu` 模式使用 OAuth v3、PKCE S256、浏览器绑定 state、显式飞书 tenant 到 Target tenant 映射和不透明 HttpOnly 会话；用户 access token 只在服务端读取一次用户信息后丢弃，不复用 `lark-cli` 用户登录。聚焦套件为 `31 passed`，完整等价结果为 `953 passed, 18 skipped`，候选 wheel SHA-256 为 `a0ce523fff41bd60004cb21c8f33689e7f979a45df2509c10c565c3cb8677669`。该实现提交时尚未推送或部署，待完成公网 HTTPS、飞书应用配置、多成员 Owner 隔离和进程内会话替换；这些开发门槛现已由后续提交关闭，管理员后台仍为后置范围。

> 后续内容提交 `fdbead1`、`ad13711` 与 `3916e24` 建立公网 IP HTTPS 入口，`3fe8cd5` 修正 OAuth 回调换取令牌，`bc961b6` 允许 Console 访问飞书 OAuth 端点。飞书应用已同时发布网页应用与机器人能力，网页应用作为工作台默认入口。至少两名真实成员完成授权登录、本人 Owner 数据读取和跨 Owner 隔离验证；Caddy 与 Console 均为 `active / NRestarts=0`，公网 `/console/` 返回 200，Console 继续只监听 `127.0.0.1:8780`。该证据关闭开发环境身份与可见性门槛；该部署时点的进程内会话缺口已由后续 PostgreSQL 会话提交关闭，公网 IP 入口仍不构成正式域名或生产发布。
>
> 内容提交 `a6f5babb07623590e9be2a2b8c523857cce56ff7` 增加 migration `0020_console_sessions`，只在 PostgreSQL 保存不透明凭据的 SHA-256 摘要、服务端主体与有效期。完整离线套件等价结果为 `955 passed, 19 skipped`；一次性真实 PostgreSQL 验证认证器重建、原始凭据不落库、注销和过期清理。wheel SHA-256 为 `a3b680c0a76545ab25a6c62ad500c9a2db0e24b2aac890eb4a1b708bc5fea729`，已部署到开发服务器。真实成员重新授权后，Console 重启前后的 PostgreSQL 会话记录保持有效，公网与 loopback 均返回 200，浏览器直接刷新仍保持登录；Console 与 Caddy 均为 `active / NRestarts=0`，验收窗口无 warning。
>
> 内容提交 `e15f47942fcc01bc85ecbbfa822acd00558c06f0` 增加当前企业的管理员只读概览。管理员资格只由服务端 `tenant + person` allowlist 计算；未命中成员和不存在路由统一返回 404。响应只含流程、会话、migration 与七条耐久队列的有界聚合，不含人员 ID、原始错误、payload 或 claim。完整离线套件为 `960 passed, 20 skipped`，wheel SHA-256 为 `fbdd2e325d57fb595362c4aac8c32b10ae734843014c4bbef2da71480bbe418b`，已部署到 `/srv/larkflow/target/releases/20260807_204031_admin_e15f479/`。真实 HTTP 验收返回管理员 200、普通成员 404、七条队列、55 个流程与二十份已对齐 migration；短期验收会话已撤销，原有真实登录会话仍为一条。十个 Python 服务与 Caddy 保持 `active / NRestarts=0`，验收窗口无 warning。真实登录浏览器随后完成“管理概览”视觉验收。

> 内容提交 `8ba0ab9d93554b7958a650492e0282ad40db0d2e` 增加管理员会话治理 v0。有效会话列表只返回安全 ID、`you / member` 关系、创建与过期时间；当前会话只能注销，其他会话必须先创建五分钟耐久预览，再显式确认撤销。确认在同一事务中删除目标、消费预览并追加不可变审计，竞争确认只有一路执行，重复回放保持幂等。完整离线套件为 `965 passed, 21 skipped`，wheel SHA-256 为 `b2cff677a419f7151f6ceb6dc8986fcd061999406cbd8212ac2cdde7504fecc8`，已部署到 `/srv/larkflow/target/releases/20260807_212230_session_gov_8ba0ab9/`；长期库应用 `0021_console_session_governance`。真实 HTTP 验收覆盖列表 200、当前会话拒绝 409、预览 201、确认 200、重复确认幂等、被撤销会话 401 和普通成员 404。原有真实登录会话仍保留，十个 Python 服务与 Caddy 保持 `active / NRestarts=0`，验收窗口无 warning。用户随后在真实登录浏览器中完成新面板视觉验收。

> 内容提交 `66b2c12d3ea27a61e5a1cdc21332ed03adb516ac` 完成公网 Console 边界加固 v0。Caddy 覆盖 `X-Larkflow-Client-IP`，loopback Console 只把该值用于限流公平性，不用于身份或授权；应用用带进程随机密钥的 BLAKE2s 摘要区分来源，不保存原始 IP。默认预算为每个来源每分钟 300 次读取、30 次认证、30 次管理员写入，以及每分钟 3000 次全局请求。完整离线套件为 `972 passed, 21 skipped`，wheel SHA-256 为 `3ff1d97317bf4c72e4040622e747bc16d7ca98709ecf2525371f894b9fa1b9df`，已部署到 `/srv/larkflow/target/releases/20260808_004500_console_public_66b2c12/`。真实公网并发验收用 31 个不同伪造来源值仍只得到 30 次 200 和 1 次 429，429 携带 `Retry-After`；公网安全响应头、Caddy 运行时超时、loopback 200、未认证管理员 401、十个 Python 服务与 Caddy `active / NRestarts=0`、零 warning 和一条原有真实登录会话均已回读。该开发证据不等于生产容量、分布式限流、正式域名或生产发布。

> 内容提交 `c1340ca21f13ed3f543df8f1411b94e46d9e6b7e`、`abc4f5e7ad8c3617cef641efc01523055e9b695e` 与 `00b3c8f920e6b856d11d9d4a91678959de3da6a5` 增加 root 侧管理员 allowlist 运维工具。活跃 Console 会话解析、十分钟预览、env 指纹栅栏、最后一名管理员保护、原子更新、健康回读、失败自动恢复、显式回滚和不含人员 ID 的追加型审计均已实现；完整离线套件为 `982 passed, 21 skipped`。真实服务器只执行当前管理员的无变化确认和唯一管理员移除拒绝，没有给其他成员提权。新 custom-format 备份已恢复到隔离库，21 份 migration、22 张表、55 个流程实例、1 条有效会话和既有撤销审计与源库一致，对象所有者、ACL、UTC 与 timeout 均通过。暴露前清空会话后审计、流程和 migration 保留，隔离库已删除；备份仍只在同一系统盘，不构成生产容灾。

> 内容提交 `623b9b6228caa52b4680eb30ad2fee723e8921b6` 修正 Console 把 DAG 误画为线性链的问题。页面现在依据 `deps` 计算拓扑层级，以 SVG 绘制真实依赖边并标注每个节点的直接依赖；选中节点时同步突出关联边，窗口变化后重绘。完整离线套件为 `922 passed, 18 skipped`，Console 与部署相关聚焦套件为 `22 passed`，JavaScript 语法检查通过。候选 wheel SHA-256 为 `6b8faed6eb5a4f32d695e40fdc495480585e53d9058e28e7ca7d2ece32421f8d`，安装后静态资源 SHA-256 与源码、wheel 均一致。升级前备份成功，migration runner 返回 `versions=[]`；本次只重启 Console，十个 Python 服务仍为 `active / NRestarts=0`，loopback、401/200 与部署窗口 warning 边界均已回读。真实 Chrome 标签页刷新后回读 4 条依赖边和 4 条依赖标签，分叉、汇合、关联高亮与横向滚动均完成目视确认。
>
> 内容提交 `b153c5311771eaa5b98d964fe6ffd448b62cf49d` 和 `c3e23fcbf3bf9e66eeb9cf97bf8bbbc1bb2eefc3` 关闭第一次 Owner 独立 Console 试用暴露的图形操作缺口。流程图支持空白区域拖动平移、50% 到 160% 缩放、100% 重置、适配和键盘操作；点击节点只更新选中态、依赖边和 Attempt，不重建画布或丢失视口。第二个提交排除节点卡片上的平移手势启动，避免鼠标点击被细微位移吞掉。最终真实 Chrome 在 `source_grounded_reject_20260806_001940` 上完成拖动后鼠标选中 Tool 节点、Attempt 切换、100% 到 90% 缩小和 57% 适配。当前发布件已部署；Owner 再次独立使用仍是产品价值门槛。
>
> 内容提交 `efc1dff935d21918517d73c0d10fd15336516d9a` 增加服务端提炼的实例摘要。详情页直接展示最终状态与完成进度、所有 `current_attempt_no > 1` 的节点，以及最近一次节点或实例重启的时间、操作者、目标和实际影响节点；原始 Audit payload 仍不进入浏览器 DTO。真实 Chrome 已在返工实例回读 `done / 4/4 / version 16`、三个 Attempt 2 节点与三节点重启，并在无返工实例回读两类空状态。Console 继续只读，页面明确把回复和操作留在 Agent 对话或飞书入口。
>
> 内容提交 `30dc7ee` 与查询边界加固 `b6eda8caaa06d338de8c5aa0283c3d787a8affe7` 增加 Owner 待处理中心。开发发布件已保存到 `/srv/larkflow/target/releases/20260807_010810_attention_b6eda8c/`，真实认证 API 回读最近 30 个本人流程和 22 条待处理项，PostgreSQL 直接查询与人员 ID 不外泄检查通过。十个 Python 服务保持 `active / running / NRestarts=0`，5432、8765 与 8780 只监听 loopback。同发布内容浏览器功能验收确认“查看流程”进入“已打开”、无横向溢出或浏览器错误；真实开发 token 未进入自动化浏览器，下一证据仍是 Owner 不依赖开发者解释的独立使用。
>
> 内容提交 `5113a59aacc8b0a97481411e581b9d52f6462073` 已增加 `/larkflow draft <JSON定义>`，结构化无模板定义现在直接生成 `template_version_id=NULL` 的未锁定 Snapshot 草稿，并沿用发送者验证、mention 角色绑定、独立确认和中央运行时。严格 JSON、100 节点上限、模型服务配置拒绝和 `personal.readonly` Edge capability 拒绝共同限制该入口。真实实例 `im_a9a43d1d4db354b31b798bb1` 已在测试组织完成 Human-Agent-Tool-Human 4/4，PostgreSQL 终态回读为 `done` 且四个节点全部完成。
>
> 内容提交 `244fb0c25b67c789ed42f23a290438b86e1a7e18`、`6ff0af211280cbeeb8b35cca04308a88c2c67184` 与 `282ea515aeb463896133b4b3a60d9d42733d555c` 依次实现裸 `/larkflow draft` 自然语言引导、Card 2.0 正确表单提交和非法候选的一次有界重生成。最终 wheel 已同时部署到 Target Runtime 与 legacy 飞书事件桥接虚拟环境。真实点击首反馈为 1056 ms；首个非法依赖候选被拒绝，第二个候选创建三节点无模板草稿 `im_69af9ebdf241017341e5fee4`。该实例保持 `draft / 0 Attempt`，同卡唯一 canonical 动作为 `processed / draft_created / sent`，飞书服务端原卡片已冻结为无操作控件的图预览。本轮没有确认或运行草稿。
>
> 内容提交 `1a80b4035d0a5ad5c634af7be957f4b7d1ee37d7` 已部署独立 Draft Generation Worker、migration `0019_draft_generation_progress` 和阶段 revision 栅栏。内容提交 `2ed644e640f3c3834f82c464e05fe0b4c3a241cc` 又把生成进度与最终结果从受限的回调 token 更新改为按原消息 ID 更新。完整离线套件为 `886 passed, 18 skipped`；两项新增隔离破坏测试被捕获。旧卡片修复和新实例 `im_74e775110afbd80aa598d3ae` 均已从飞书服务端回读为无按钮、无输入框的最终图预览。该新实例随后由真实用户确认启动，Agent 与 Human Attempt 1 全部完成，Task 完成状态经周期读回进入耐久 Inbox；PostgreSQL 终态为 `done / template_version_id IS NULL / 2 nodes done`，完成 Docx 和最终通知均已从飞书服务端读回。本次输入没有真实业务数据，因此只关闭开发环境技术链路，不证明内容质量或业务价值。
>
> Personal Agent Edge 的 macOS 默认凭据后端已切换为登录 Keychain，磁盘只留 `0600` 非敏感引用；旧明文文件支持回读校验后迁移。合成 Keychain 项的真实创建、回读和删除已通过。默认槽位现绑定员工 Mac 的真实 Owner 设备，`run-once` 认证返回 `no_work`，服务器回读 active、配对审计和认证后时间戳均成立。凭据与元数据继续保留，当前隧道已关闭。该证据不包含正式员工分发或可持续公网连接。
>
> 内容提交 `5b0c79b4d946441063d92970e8f0e9cac31b2ab3` 已完成 macOS 开发试用的最小安装升级体验：独立 manager 验证 wheel SHA-256，在最终版本目录创建 venv 并完成 `pip check` 与 CLI 启动校验后，原子切换 `current / previous`；`doctor` 只做本机离线诊断，不连接中央节点。员工 Mac 已真实完成 `0.0.1 -> 0.0.2 -> rollback -> 0.0.2`，现有 Keychain 凭据未迁移或覆盖；真实用户上下文中的 `doctor` 为 ready，经临时 SSH 隧道执行 `run-once` 返回 `no_work`，服务器设备保持 active 且认证时间推进。隧道已关闭。
>
> 内容提交 `81bd43983598ff319150344e779223cd03731eba` 新增哈希锁定离线 bundle、精确 wheel 清单、目标 Mac 与 Python 绑定、修复版 bootstrap pip 和安装时强制断网。故意注入无效索引与代理后，45-wheel 测试 bundle 仍完成安装与 `pip check`；pip 26.1 的 `CVE-2026-8643` 已通过先离线升级至 26.2.1 缓解，复扫无已知漏洞。正式分发安全评审结论仍为 No-Go：员工端依赖面尚未最小化，本机没有 Developer ID 身份或公证凭据，构建来源证明与目录级读取隔离也未完成。代码签名、公证和全新员工 Mac 验收尚未执行。
>
> 内容提交 `00067f717ca8e0258b234e81c56e1388226bc471` 已关闭上述依赖面最小化和单次构建证据缺口。员工 artifact 只包含四个 Edge 模块与最小 initializer，不包含中央控制面；真实 bundle 从 45 个 wheel 降到 9 个，主 artifact 为 18367 bytes。schema v2 manifest 绑定精确哈希 lock、SPDX SBOM 和 build proof，安装器逐项校验后使用 `--require-hashes` 断网安装。完整离线套件为 `1025 passed, 23 skipped`。真实 macOS bundle manifest SHA-256 为 `c09dd9abda0e71934e4365b3d828f32dc1050651fe677db6e3143bd68b3cde29`，隔离前缀安装、CLI、status 和中央模块未导入检查均通过。中央开发 wheel 已部署到 `/srv/larkflow/target/releases/20260809_2050_edge_minimal_00067f7/`，真实 migration 无新增，十个 Python 服务、Console、Edge 401 边界和零 warning 均已读回。该提交发布时目录隔离、数据治理和干净 Mac 验收尚缺，现已由顶部 `ab3ad5e8f00dded71763c70e6437ff0782050e8b` 与 `e6106e5f218f9c520928bef0293899ace7a2395f` 的后续证据关闭开发机制门槛；正式员工分发仍缺签名、公证、可信摘要发布、真实登录 Keychain 首次体验和合规公网入口。
>
> 内容提交 `b7e589ba4af0398573ec995254dd61e9b1a4508c` 新增来源约束型材料复核：`source_claims.v1` 区分来源事实、推断和开放问题，`source_claims.check` 只校验确定性来源契约，最终 Human Owner 通过版本绑定 Card 2.0 明确接受或退回。完整离线套件为 `898 passed, 18 skipped`。该提交现已部署；候选 wheel SHA-256 为 `0dcccb7f674135dde8b44ab08d437ba397b92397b8456ede8a064f66f1eb2af1`，长期库保持十九份 migration，九个 Python 服务回读 `active / running / NRestarts=0`。两个公开材料实例已分别完成直接接受，以及退回后从 Agent 节点重启、Attempt 2 重新执行和最终接受恢复；证据仅覆盖开发测试组织，下一阶段转向受控内部试用。
>
> 内容提交 `0dc5359e990635c7b6aa16ec0bcd798eb8df39d0` 已把具体退回意见纳入 Human 决定与节点返工契约，内容提交 `f6125331aa541e824675e25f9cd2d756cd4c6b56` 修正原生 Card 2.0 表单提交的服务端绑定。退回表单必填且最多 1000 字，服务端把规范化意见保存到 Attempt、质量证据和审计，并在 `reject_target` 节点重启时只注入该目标的新 Attempt 输入快照。接受路径忽略额外意见，冻结 Instance Snapshot、范围外上游与旧 Attempt 不变。完整离线套件为 `910 passed, 18 skipped`，无需新增 migration。开发服务器已部署该候选；真实实例 `im_5717aa5b9480d146239907d5` 已完成具体意见退回、三节点重启、Agent Attempt 2 接收意见、Tool 从失败转为通过和新决定卡投影，当前停在最终人工复核。
>
> 内容提交 `770243a02b116e12583ceebdb8362fd40b7fe0a7` 增加暂停、继续和版本绑定取消，真实飞书验收现已关闭最后的外部投影缺口。实例 `im_c1c472a12a8ea4a7c8d63480` 依次完成确认、暂停、继续、取消预览和确认，普通 Human Task 从飞书服务端回读为 `done`；实例 `im_516c59e4082e82ab74b8bd14` 进入决定节点后取消，原 Card 2.0 被原位更新为无控件“复核已取消”。两个实例的 PostgreSQL Instance、Node、Attempt、Projection 与追加型审计均和飞书终态一致。十个服务保持 `active / NRestarts=0`，验收窗口 warning 为 0。该证据只关闭开发测试组织中的生命周期链路，不构成生产上线或业务价值证明。
>
> 内容提交 `db7651228e26055eb1229ae9f451e3e87c31df38` 把来源约束型材料复核与决策生成拆成独立结果契约，新增 `source_decision.v1`、`source_decision.check` 与 `source_grounded_decision`，并用 JSON 代码块消除结构化卡片 URL 的 `%22` 污染。完整离线等价结果为 `988 passed, 21 skipped`，干净 wheel SHA-256 为 `54a4bbf4c96834d7d69a3434d01b083d2467f5df6dd129c9ac6e35876efb49ff`，已部署到 `/srv/larkflow/target/releases/20260808_040000_source_decision_db76512/`。真实实例 `source_decision_20260808_0405` 的四个 Attempt 1 均完成，Agent 回答 Q1、Q2、Q3，Tool 覆盖 6/6 个 F 和 3/3 个 Q、零违规且 `verdict=pass`。Owner 明确接受后实例为 `done / version 9`，终态决定卡已从飞书服务端回读为已接受、无按钮且无 `%22`。九个 Target 服务均为 `active / NRestarts=0`，legacy 消费者与 Caddy 仍 active，部署窗口 warning 为 0。该开发证据不等于业务建议正确、生产容量或生产发布。
>
> 内容提交 `d879a280d49e584d2d7e5927a498e7947544bb63` 已把自然语言 Agent 候选的明确决定出口升级为服务端结构不变量，并完成真实开发部署与飞书返工验收。真实合成实例先退回 Agent Attempt 1，再通过节点重启只创建 Agent 与最终 Human Attempt 2；具体意见进入 Agent 新输入，第二版补充回滚条件和监控窗口后明确接受。旧结果、两张决定卡、完成文档、最终通知和审计均保留。该证据只适用于开发环境，不证明内容质量、业务价值、生产容量或生产发布。
>
> last-synced: 86189216a0b67cf258daa4027d368257eee7491a · 2026-08-10

## 阅读顺序

1. [CORE.md](CORE.md)：产品身份、边界和不变量。
2. [PRODUCT_STRATEGY.md](PRODUCT_STRATEGY.md)：当前证据边界、取舍和成功标准。
3. [PRD.md](PRD.md)：简化 MVP 的功能、体验和验收。
4. [DAG_TEMPLATE_SPEC.md](DAG_TEMPLATE_SPEC.md)：DAG Contract v0.2 目标契约。
5. [ARCHITECTURE.md](ARCHITECTURE.md)：目标架构、数据权威和原型迁移边界。

既有设计的范围取舍见 [`research/design-simplification.md`](../research/design-simplification.md)。

## 状态

| 文件 | 状态 | 摘要 |
|---|:--:|---|
| CORE | ✅ | Target 身份、简化边界、Edge Proof 和不变量 |
| PRODUCT_STRATEGY | ✅ | 范围收敛取舍、窄 Edge 实验，明确未做市场验证 |
| PRD | ✅ | Target 单层 DAG MVP、受控自然语言草稿、第一版受控 DAG 画板、工作台身份与耐久会话、Owner 受控流程操作、普通 Human Task 提交与转交、管理员聚合与会话治理、来源约束型结果、带具体意见的人类决定与 Edge Proof 功能契约 |
| DAG_TEMPLATE_SPEC | ✅ | v0.2 模板、mention 角色绑定、草稿预览、未来区域编辑和两类重启已实现 |
| ARCHITECTURE | ✅ | Target 模块化单体、飞书 OAuth 与 PostgreSQL 耐久会话工作台、受控 DAG 画板、耐久草稿请求与独立生成 Worker、Owner 受控流程操作、参与者任务面与运行时责任转交、管理员聚合与会话治理、投影有界终止、来源契约检查、失败恢复、Edge Proof 与剩余差距 |
| RELATIONS | ✅ | Target 飞书、mention 与人员选择卡身份边界、中央 lark-cli、Edge HTTPS、Node Runner 与 LangGraph 边界 |
| ROADMAP | ✅ | 网页受控流程输入、普通 Human 责任入口、跨成员飞书 Task 转交、Chrome 可见草稿生成与草稿依赖连线闭环均已落地，下一步是小范围真实业务试用 |
| SPEC | ✅ | legacy 契约、Target CLI、Owner Console 读取与写入、受控 DAG 画板和图编辑 API、耐久草稿请求 API、OAuth v3、PostgreSQL 耐久会话、服务端管理员 allowlist、聚合与受审计会话撤销、公网请求预算和 429 契约、独立 interact 与 draft generation Worker、数据库通知唤醒、来源声明与确定性检查、必填退回意见的人类决定卡、十五个飞书窄命令、模板与无模板草稿、暂停继续取消、Task 入站、受控变化、完成投影与私有 Edge v1 HTTP、前台客户端、doctor 及 macOS manager |
| DEPLOYMENT | ✅ | Legacy ECS 与 Target 十服务、二十三份 migration、耐久网页草稿、投影永久失败终止、公网 IP Console、Owner 工作台、真实飞书登录、管理员运维、Caddy 边界、Edge、备份与回滚实录 |
| CONVENTIONS | ✅ | Target 与 As-built 的命名、状态、安全和文档约定 |
| DECISIONS | ✅ | Append-only ADR 历史，最新为画板只提交受控领域命令并把布局留在浏览器 |
| CHANGELOG | ✅ | Append-only 已实现变更，最新为草稿依赖连线的公网可见验收闭环 |
| MEMORY | ⚑ | Append-only 经验，仍含语义占位，已记录永久投影重试、psql 参数化、恢复会话、流程图手势、回调漂移、通知、虚拟环境与安装风险 |

## 按任务读取

- 改产品范围：CORE + PRODUCT_STRATEGY + PRD + DECISIONS
- 核对既有设计的简化范围：`../research/design-simplification.md` + PRD + DECISIONS
- 改模板：DAG_TEMPLATE_SPEC + CONVENTIONS + PRD
- 改运行时或数据模型：ARCHITECTURE + SPEC + DECISIONS
- 改飞书集成：RELATIONS + ARCHITECTURE + DEPLOYMENT
- 判断“设计了还是做了”：先看 ARCHITECTURE 的差距表，再看 SPEC 和代码
- 未来恢复外部验证：`../research/phase-0/README.md`
