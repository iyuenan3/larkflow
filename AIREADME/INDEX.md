# larkflow · 飞流 · AIREADME

> 飞书原生的企业协作 DAG：把多人流程拆成有依赖、有唯一责任人、可验收和可追溯的节点。
>
> 文档状态：2026-08-05 Phase 1 中央工作流基础实现。飞书 IM 窄命令、Human-Agent-Tool-Human、完成投影、Owner 查询、两类重启、未来区域编辑、失败恢复、跨人员分工和自然语言草稿均已在开发环境闭环。八个 Target 服务与一个 legacy 事件消费者组成九个 Python 服务；十九份 migration 和七条 PostgreSQL 通知连接已回读。自然语言草稿生成已隔离到无飞书 profile 的 Draft Generation Worker，回调首次反馈使用延时 token，后续阶段与终态按原消息 ID 更新。Personal Agent Edge 已完成员工 Mac 前台、Keychain 与离线安装机制验证，但正式签名分发、全新员工 Mac 和可持续公网链路仍未完成。公网设备链路受 ICP 接入备案阻断，Caddy 已停止。`Target` 表示目标产品契约，`As-built` 表示当前代码事实，两者不得混写。
>
> 内容提交 `5113a59aacc8b0a97481411e581b9d52f6462073` 已增加 `/larkflow draft <JSON定义>`，结构化无模板定义现在直接生成 `template_version_id=NULL` 的未锁定 Snapshot 草稿，并沿用发送者验证、mention 角色绑定、独立确认和中央运行时。严格 JSON、100 节点上限、模型服务配置拒绝和 `personal.readonly` Edge capability 拒绝共同限制该入口。真实实例 `im_a9a43d1d4db354b31b798bb1` 已在测试组织完成 Human-Agent-Tool-Human 4/4，PostgreSQL 终态回读为 `done` 且四个节点全部完成。
>
> 内容提交 `244fb0c25b67c789ed42f23a290438b86e1a7e18`、`6ff0af211280cbeeb8b35cca04308a88c2c67184` 与 `282ea515aeb463896133b4b3a60d9d42733d555c` 依次实现裸 `/larkflow draft` 自然语言引导、Card 2.0 正确表单提交和非法候选的一次有界重生成。最终 wheel 已同时部署到 Target Runtime 与 legacy 飞书事件桥接虚拟环境。真实点击首反馈为 1056 ms；首个非法依赖候选被拒绝，第二个候选创建三节点无模板草稿 `im_69af9ebdf241017341e5fee4`。该实例保持 `draft / 0 Attempt`，同卡唯一 canonical 动作为 `processed / draft_created / sent`，飞书服务端原卡片已冻结为无操作控件的图预览。本轮没有确认或运行草稿。
>
> 内容提交 `1a80b4035d0a5ad5c634af7be957f4b7d1ee37d7` 已部署独立 Draft Generation Worker、migration `0019_draft_generation_progress` 和阶段 revision 栅栏。内容提交 `2ed644e640f3c3834f82c464e05fe0b4c3a241cc` 又把生成进度与最终结果从受限的回调 token 更新改为按原消息 ID 更新。完整离线套件为 `886 passed, 18 skipped`；两项新增隔离破坏测试被捕获。旧卡片修复和新实例 `im_74e775110afbd80aa598d3ae` 均已从飞书服务端回读为无按钮、无输入框的最终图预览。
>
> Personal Agent Edge 的 macOS 默认凭据后端已切换为登录 Keychain，磁盘只留 `0600` 非敏感引用；旧明文文件支持回读校验后迁移。合成 Keychain 项的真实创建、回读和删除已通过。默认槽位现绑定员工 Mac 的真实 Owner 设备，`run-once` 认证返回 `no_work`，服务器回读 active、配对审计和认证后时间戳均成立。凭据与元数据继续保留，当前隧道已关闭。该证据不包含正式员工分发或可持续公网连接。
>
> 内容提交 `5b0c79b4d946441063d92970e8f0e9cac31b2ab3` 已完成 macOS 开发试用的最小安装升级体验：独立 manager 验证 wheel SHA-256，在最终版本目录创建 venv 并完成 `pip check` 与 CLI 启动校验后，原子切换 `current / previous`；`doctor` 只做本机离线诊断，不连接中央节点。员工 Mac 已真实完成 `0.0.1 -> 0.0.2 -> rollback -> 0.0.2`，现有 Keychain 凭据未迁移或覆盖；真实用户上下文中的 `doctor` 为 ready，经临时 SSH 隧道执行 `run-once` 返回 `no_work`，服务器设备保持 active 且认证时间推进。隧道已关闭。
>
> 内容提交 `81bd43983598ff319150344e779223cd03731eba` 新增哈希锁定离线 bundle、精确 wheel 清单、目标 Mac 与 Python 绑定、修复版 bootstrap pip 和安装时强制断网。故意注入无效索引与代理后，45-wheel 测试 bundle 仍完成安装与 `pip check`；pip 26.1 的 `CVE-2026-8643` 已通过先离线升级至 26.2.1 缓解，复扫无已知漏洞。正式分发安全评审结论仍为 No-Go：员工端依赖面尚未最小化，本机没有 Developer ID 身份或公证凭据，构建来源证明与目录级读取隔离也未完成。代码签名、公证和全新员工 Mac 验收尚未执行。
>
> last-synced: 2ed644e640f3c3834f82c464e05fe0b4c3a241cc · 2026-08-05

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
| PRD | ✅ | Target 单层 DAG MVP 与 Edge Proof 功能契约 |
| DAG_TEMPLATE_SPEC | ✅ | v0.2 模板、mention 角色绑定、草稿预览、未来区域编辑和两类重启已实现 |
| ARCHITECTURE | ✅ | Target 模块化单体、独立凭据侧 Interactive 双副本、无凭据 Draft Generation Worker、PostgreSQL 通知唤醒与轮询兜底、飞书投影、失败恢复、Agent / Tool adapter、Edge Proof、macOS 版本化安装与剩余差距 |
| RELATIONS | ✅ | Target 飞书、mention 与人员选择卡身份边界、中央 lark-cli、Edge HTTPS、Node Runner 与 LangGraph 边界 |
| ROADMAP | ✅ | Phase 1 已完成员工 Mac 前台 Edge、Keychain、真实设备配对、最小安装升级和离线 bundle 验证，正式分发仍受最小依赖、签名、公证与数据隔离门禁阻断 |
| SPEC | ✅ | legacy 契约、Target CLI、独立 interact 与 draft generation Worker、数据库通知唤醒、十一个飞书窄命令、模板与无模板草稿、阶段进度、人员选择与失败恢复卡、Task 入站、受控变化、完成投影与私有 Edge v1 HTTP、前台客户端、doctor 及 macOS manager |
| DEPLOYMENT | ✅ | Legacy ECS 与当前 Target 八服务、十九份 migration、七条监听连接、双 Interactive 副本、独立草稿生成、Edge serve、macOS 版本化安装、PostgreSQL、备份与回滚实录 |
| CONVENTIONS | ✅ | Target 与 As-built 的命名、状态、安全和文档约定 |
| DECISIONS | ✅ | Append-only ADR 历史，最新记录自然语言草稿候选、授权与有界修复边界 |
| CHANGELOG | ✅ | Append-only 已实现变更，最新为自然语言飞书草稿引导及真实闭环 |
| MEMORY | ⚑ | Append-only 经验，仍含语义占位，已记录回调漂移、通知边界、批次计时、虚拟环境、Keychain 上下文、bootstrap pip 与构建模块遮蔽风险 |

## 按任务读取

- 改产品范围：CORE + PRODUCT_STRATEGY + PRD + DECISIONS
- 核对既有设计的简化范围：`../research/design-simplification.md` + PRD + DECISIONS
- 改模板：DAG_TEMPLATE_SPEC + CONVENTIONS + PRD
- 改运行时或数据模型：ARCHITECTURE + SPEC + DECISIONS
- 改飞书集成：RELATIONS + ARCHITECTURE + DEPLOYMENT
- 判断“设计了还是做了”：先看 ARCHITECTURE 的差距表，再看 SPEC 和代码
- 未来恢复外部验证：`../research/phase-0/README.md`
