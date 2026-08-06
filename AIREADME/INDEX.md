# larkflow · 飞流 · AIREADME

> 飞书原生的企业协作 DAG：把多人流程拆成有依赖、有唯一责任人、可验收和可追溯的节点。
>
> 文档状态：2026-08-06 Phase 1 中央工作流基础实现。飞书 IM 窄命令、Human-Agent-Tool-Human、完成投影、Owner 查询、两类重启、未来区域编辑、失败恢复、跨人员分工、自然语言草稿和来源约束型材料复核均已在开发环境闭环。首批三项真实项目小样本已覆盖直接退回、带意见返工和直接接受，但尚未证明稳定内容质量或市场价值。Owner 只读中央控制台 v0 已通过真实 PostgreSQL、SSH 隧道和 Chrome 页面验收，页面按真实 `deps` 绘制拓扑层级与依赖箭头，提供拖动、缩放、适配和点击后视口保持，并在详情顶部直接汇总最终状态、返工节点与最近重启。九个 Target 服务与一个 legacy 事件消费者组成十个 Python 服务；十九份 migration 和七条 PostgreSQL 通知连接已回读。自然语言草稿生成已隔离到无飞书 profile 的 Draft Generation Worker，回调首次反馈使用延时 token，后续阶段与终态按原消息 ID 更新。Personal Agent Edge 已完成员工 Mac 前台、Keychain 与离线安装机制验证，但正式签名分发、全新员工 Mac 和可持续公网链路仍未完成。公网设备链路受 ICP 接入备案阻断，Caddy 已停止。`Target` 表示目标产品契约，`As-built` 表示当前代码事实，两者不得混写。
>
> 内容提交 `ee2fa9439594d765cd08f2caa0f7ecb20d30d78b` 新增 Owner 范围的中央只读控制台。浏览器只能读取服务端映射身份本人发起的最近流程、DAG、历史 Attempt 和追加型审计，不提供确认、重启、编辑或其他写操作。开发鉴权使用至少 32 字符的随机 Bearer token，服务强制监听 loopback；非 Owner 与不存在实例统一返回 404。完整离线套件为 `922 passed, 18 skipped`。wheel SHA-256 为 `58b27648ccaf3f863cf4bb0ca820b3e2209523b58b0574af626aa303c0e4ff5c`，长期库 migration runner 回读 `19 / 0019_draft_generation_progress` 且无待应用版本。控制台及其余九个 Python 服务统一重启后均为 `active / NRestarts=0`，部署窗口 warning 为 0。真实 Owner 浏览器回读 30 条流程，并验证运行中、草稿、DAG、Attempt、审计和锁定状态；其他 Owner 的真实实例返回 404。该入口只供开发试用，生产前仍需飞书登录态或企业 SSO、反向代理授权和更完整的可见性策略。

> 内容提交 `623b9b6228caa52b4680eb30ad2fee723e8921b6` 修正 Console 把 DAG 误画为线性链的问题。页面现在依据 `deps` 计算拓扑层级，以 SVG 绘制真实依赖边并标注每个节点的直接依赖；选中节点时同步突出关联边，窗口变化后重绘。完整离线套件为 `922 passed, 18 skipped`，Console 与部署相关聚焦套件为 `22 passed`，JavaScript 语法检查通过。候选 wheel SHA-256 为 `6b8faed6eb5a4f32d695e40fdc495480585e53d9058e28e7ca7d2ece32421f8d`，安装后静态资源 SHA-256 与源码、wheel 均一致。升级前备份成功，migration runner 返回 `versions=[]`；本次只重启 Console，十个 Python 服务仍为 `active / NRestarts=0`，loopback、401/200 与部署窗口 warning 边界均已回读。真实 Chrome 标签页刷新后回读 4 条依赖边和 4 条依赖标签，分叉、汇合、关联高亮与横向滚动均完成目视确认。
>
> 内容提交 `b153c5311771eaa5b98d964fe6ffd448b62cf49d` 和 `c3e23fcbf3bf9e66eeb9cf97bf8bbbc1bb2eefc3` 关闭第一次 Owner 独立 Console 试用暴露的图形操作缺口。流程图支持空白区域拖动平移、50% 到 160% 缩放、100% 重置、适配和键盘操作；点击节点只更新选中态、依赖边和 Attempt，不重建画布或丢失视口。第二个提交排除节点卡片上的平移手势启动，避免鼠标点击被细微位移吞掉。最终真实 Chrome 在 `source_grounded_reject_20260806_001940` 上完成拖动后鼠标选中 Tool 节点、Attempt 切换、100% 到 90% 缩小和 57% 适配。当前发布件已部署；Owner 再次独立使用仍是产品价值门槛。
>
> 内容提交 `efc1dff935d21918517d73c0d10fd15336516d9a` 增加服务端提炼的实例摘要。详情页直接展示最终状态与完成进度、所有 `current_attempt_no > 1` 的节点，以及最近一次节点或实例重启的时间、操作者、目标和实际影响节点；原始 Audit payload 仍不进入浏览器 DTO。真实 Chrome 已在返工实例回读 `done / 4/4 / version 16`、三个 Attempt 2 节点与三节点重启，并在无返工实例回读两类空状态。Console 继续只读，页面明确把回复和操作留在 Agent 对话或飞书入口。
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
> 内容提交 `b7e589ba4af0398573ec995254dd61e9b1a4508c` 新增来源约束型材料复核：`source_claims.v1` 区分来源事实、推断和开放问题，`source_claims.check` 只校验确定性来源契约，最终 Human Owner 通过版本绑定 Card 2.0 明确接受或退回。完整离线套件为 `898 passed, 18 skipped`。该提交现已部署；候选 wheel SHA-256 为 `0dcccb7f674135dde8b44ab08d437ba397b92397b8456ede8a064f66f1eb2af1`，长期库保持十九份 migration，九个 Python 服务回读 `active / running / NRestarts=0`。两个公开材料实例已分别完成直接接受，以及退回后从 Agent 节点重启、Attempt 2 重新执行和最终接受恢复；证据仅覆盖开发测试组织，下一阶段转向受控内部试用。
>
> 内容提交 `0dc5359e990635c7b6aa16ec0bcd798eb8df39d0` 已把具体退回意见纳入 Human 决定与节点返工契约，内容提交 `f6125331aa541e824675e25f9cd2d756cd4c6b56` 修正原生 Card 2.0 表单提交的服务端绑定。退回表单必填且最多 1000 字，服务端把规范化意见保存到 Attempt、质量证据和审计，并在 `reject_target` 节点重启时只注入该目标的新 Attempt 输入快照。接受路径忽略额外意见，冻结 Instance Snapshot、范围外上游与旧 Attempt 不变。完整离线套件为 `910 passed, 18 skipped`，无需新增 migration。开发服务器已部署该候选；真实实例 `im_5717aa5b9480d146239907d5` 已完成具体意见退回、三节点重启、Agent Attempt 2 接收意见、Tool 从失败转为通过和新决定卡投影，当前停在最终人工复核。
>
> 内容提交 `770243a02b116e12583ceebdb8362fd40b7fe0a7` 增加暂停、继续和版本绑定取消，真实飞书验收现已关闭最后的外部投影缺口。实例 `im_c1c472a12a8ea4a7c8d63480` 依次完成确认、暂停、继续、取消预览和确认，普通 Human Task 从飞书服务端回读为 `done`；实例 `im_516c59e4082e82ab74b8bd14` 进入决定节点后取消，原 Card 2.0 被原位更新为无控件“复核已取消”。两个实例的 PostgreSQL Instance、Node、Attempt、Projection 与追加型审计均和飞书终态一致。十个服务保持 `active / NRestarts=0`，验收窗口 warning 为 0。该证据只关闭开发测试组织中的生命周期链路，不构成生产上线或业务价值证明。
>
> last-synced: b6eda8caaa06d338de8c5aa0283c3d787a8affe7 · 2026-08-07

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
| PRD | ✅ | Target 单层 DAG MVP、来源约束型结果、带具体意见的人类决定与 Edge Proof 功能契约 |
| DAG_TEMPLATE_SPEC | ✅ | v0.2 模板、mention 角色绑定、草稿预览、未来区域编辑和两类重启已实现 |
| ARCHITECTURE | ✅ | Target 模块化单体、可拖动缩放且按真实依赖分层的 Owner 只读中央控制台、独立凭据侧 Interactive 双副本、无凭据 Draft Generation Worker、来源契约检查、带返工上下文的人类决定卡、PostgreSQL 通知唤醒与轮询兜底、飞书投影、失败恢复、Edge Proof、macOS 版本化安装与剩余差距 |
| RELATIONS | ✅ | Target 飞书、mention 与人员选择卡身份边界、中央 lark-cli、Edge HTTPS、Node Runner 与 LangGraph 边界 |
| ROADMAP | ✅ | 首批三项真实工作小样本已建立，生命周期真实飞书验收已完成，下一门槛为 Console 复用、运维告警、生产鉴权边界及 Edge 正式分发门禁 |
| SPEC | ✅ | legacy 契约、Target CLI、Owner 只读 Console HTTP、独立 interact 与 draft generation Worker、数据库通知唤醒、来源声明与确定性检查、必填退回意见的人类决定卡、十五个飞书窄命令、模板与无模板草稿、暂停继续取消、Task 入站、受控变化、完成投影与私有 Edge v1 HTTP、前台客户端、doctor 及 macOS manager |
| DEPLOYMENT | ✅ | Legacy ECS 与当前 Target 九服务、十九份 migration、七条监听连接、暂停继续取消的真实飞书收口、可拖动缩放的真实依赖 Console、独立草稿生成、Edge serve、macOS 安装、PostgreSQL、备份与回滚实录 |
| CONVENTIONS | ✅ | Target 与 As-built 的命名、状态、安全和文档约定 |
| DECISIONS | ✅ | Append-only ADR 历史，最新暂停 drain 语义与版本绑定取消已完成真实飞书验收 |
| CHANGELOG | ✅ | Append-only 已实现变更，最新为暂停、继续和取消的开发真栈闭环 |
| MEMORY | ⚑ | Append-only 经验，仍含语义占位，已记录流程图操作可发现性、手势竞争、静态页面旧标签页、回调漂移、通知边界、批次计时、虚拟环境、Keychain 上下文、bootstrap pip 与构建模块遮蔽风险 |

## 按任务读取

- 改产品范围：CORE + PRODUCT_STRATEGY + PRD + DECISIONS
- 核对既有设计的简化范围：`../research/design-simplification.md` + PRD + DECISIONS
- 改模板：DAG_TEMPLATE_SPEC + CONVENTIONS + PRD
- 改运行时或数据模型：ARCHITECTURE + SPEC + DECISIONS
- 改飞书集成：RELATIONS + ARCHITECTURE + DEPLOYMENT
- 判断“设计了还是做了”：先看 ARCHITECTURE 的差距表，再看 SPEC 和代码
- 未来恢复外部验证：`../research/phase-0/README.md`
