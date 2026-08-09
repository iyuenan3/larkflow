# Personal Agent Edge macOS 分发安全评审

> 初评日期：2026-08-05；目录隔离与数据外发复评：2026-08-09
>
> 结论：正式员工分发 No-Go，受控开发试用可继续。

## 评审范围

本评审只覆盖 macOS Personal Agent Edge 的构建、交付、安装、升级、回滚、本机凭据与执行边界。中央 PostgreSQL、飞书应用权限和公网接入另有既有证据，不因本评审自动获得生产结论。

当前实现已经具备以下控制：

- 设备密钥默认进入当前用户登录 Keychain，磁盘只留 `0600` 非敏感引用。
- manager 在最终 release 路径创建独立 venv，完成 `pip check` 和安装态 CLI 启动校验后才原子切换。
- 离线 bundle 固定 macOS 架构、Python 实现和次版本，记录完整 source commit、主 wheel、manager、全部 wheel 的 SHA-256、大小、包名和版本。
- 安装前要求通过独立可信渠道取得 manifest SHA-256，并验证目录不存在额外文件、符号链接、缺失文件或元数据漂移。
- 离线安装清除 pip 配置、Python 注入变量和代理变量，强制 `--no-index --only-binary=:all:`。
- bundle 携带哈希锁定的修复版 pip，先离线升级 bootstrap pip，再安装项目 wheel。
- 安装、升级和回滚不读取或修改 Keychain，不注册 launchd，不自动联网更新。
- macOS Codex 使用 fail-closed permission Profile：根路径默认拒绝，最小系统路径只读，仅所选工作区可读，并继续排除工作区内的 Agent 配置、环境文件、证书和常见私钥文件。
- `doctor --workspace` 在不联系中央节点或模型服务商的情况下，真实验证所选工作区可读且工作区外临时哨兵不可读；执行会话在领取工作前再次运行同一探针。
- `run-once` 与 `serve` 要求用户对当前前台会话显式确认模型数据外发，执行结果记录工作区、敏感路径、命令网络和模型外发策略摘要。

## 威胁与结论

| 风险 | 当前控制 | 剩余缺口 | 门禁 |
|---|---|---|---|
| 交付物被替换 | manifest SHA-256、逐文件哈希和精确文件集 | 首次运行的 manager 本身尚未由系统信任链认证，同渠道交付 bundle 与摘要会失去独立性 | P0 |
| 构建依赖漂移 | 安装候选件由 manifest、精确 lock 与 build proof 完整锁定 | 构建阶段仍从版本范围解析依赖，source commit 仍由调用者声明，尚未绑定受信 CI 身份或签名 | P0 |
| 依赖供应链与攻击面 | 最小 `larkflow-personal-edge` artifact 只含四个 Edge 模块，当前 bundle 为 9 个 wheel | 私有包不在公开漏洞库，发布时仍需重新扫描并审计构建来源 | 发布时重扫，来源绑定仍为 P0 |
| 已知依赖漏洞 | `pip-audit 2.10.1` 审计隔离安装 | 首次候选的 pip 26.1 命中 `CVE-2026-8643`；实现已先升级至 26.2.1，复扫无已知漏洞，但私有 `larkflow` 包不在 PyPI 审计范围 | 已缓解，发布时重扫 |
| macOS Gatekeeper 信任 | 无 | 本机没有 Developer ID Application 或 Developer ID Installer 身份，也没有公证凭据；当前 bundle 未签名、未公证、未 stapling | P0 |
| 本机代码执行范围 | Codex permission Profile、真实正反探针、固定工作区、敏感路径 deny、最小环境、命令网络与本机工具禁用 | 上游 Profile 仍是 beta 且证据只覆盖当前 macOS 与 Codex 0.147；完成任务所需的工作区内容仍会发送给模型服务商 | 机制已缓解，版本与组织政策仍为 P0 |
| 凭据泄露 | Keychain、非敏感元数据、密钥不进 argv 和环境 | 同一登录用户下的恶意本机进程仍不在当前防护能力内 | 接受为首版环境前提，需员工告知 |
| 升级失败 | 独立 release、原子 current/previous、rollback | 旧 release 清理和磁盘上限尚未定义 | P1 |
| 来源追溯 | manifest 记录完整 commit | source commit 仍由构建命令调用者声明，未与 clean tree、CI 身份或签名证明绑定 | P0 |

`pip-audit` 首次输出把同一个 `PYSEC-2026-196` 公告列出两次，唯一对应公告为 `CVE-2026-8643 / GHSA-wf93-45jw-7689`。修复后的隔离 venv 使用 pip 26.2.1，当前扫描结果为 `No known vulnerabilities found`。该结论只代表 2026-08-05 的公开漏洞数据库快照。

## 正式分发放行条件

以下条件必须全部满足，才能把结论改为 Go：

1. 已完成开发机制：拆出最小 Edge 分发包，只包含设备协议、Keychain、HTTP 客户端、Codex 适配器与 CLI，不携带 LangGraph、OpenAI SDK、PostgreSQL 驱动或中央运行时。
2. 部分完成：从 clean Git commit 构建并记录 Python 目标、精确 lock、SPDX SBOM、完整哈希与 build proof；仍需把源码身份和构建阶段依赖解析绑定到受信 CI 或签名来源，并在正式候选上重跑漏洞审计。
3. 取得 Apple Developer Program 下的 Developer ID 身份与公证凭据，凭据只进入构建机 Keychain 或受控 CI secret store。
4. 对所有提交给 Apple 的可执行代码与原生库使用安全时间戳；适用的 Mach-O 启用 hardened runtime。使用 `notarytool` 提交，完成 stapling，并由 `spctl`、`codesign`、`pkgutil` 和 `stapler validate` 回读。
5. manifest 摘要经独立可信渠道发布，最终交付件不可只依赖同目录文本摘要建立信任。
6. 在一台没有项目源码、没有既有 Edge 安装的全新员工 Mac 上完成首装、配对、`doctor`、前台执行、升级失败、回滚、撤销和卸载边界验收。
7. 目录隔离和会话级告知机制已经实现；正式放行前仍需批准组织级数据外发政策、工作区分级、模型服务商条款、日志与保留边界和事件响应责任，并在全新员工 Mac 上验证相同 fail-closed 结果。当前仍不得向含敏感材料的工作区开放。

## 2026-08-09 目录隔离与数据外发复评

### 当前允许与禁止范围

- 允许：仅由用户在当前前台会话中显式选择、经过确认且不含敏感材料的开发工作区。模型可接收中央节点提示、节点输入以及完成任务所必需的工作区内容。
- 禁止：文件系统根、用户主目录、Edge 凭据所在目录，以及工作区内的 `.agents`、`.codex`、`.env`、`.env.*`、`*.pem`、`*.key`、`id_rsa*` 与 `id_ed25519*`。组织认定为机密、受监管或不允许交给模型服务商的材料不得进入当前 Edge。
- 命令网络：Codex 生成的本机命令不能联网；网页搜索、浏览器、Computer Use、应用和图片生成也被关闭。模型 API 本身仍需要网络，这条链路不属于命令网络。
- 用户告知：`run-once` 和 `serve` 缺少 `--allow-model-egress` 时，在读取凭据、领取中央工作或调用模型之前 fail closed。这个参数只确认当前可见前台会话，不形成永久授权。
- 审计：成功结果携带 `workspace_access=selected_workspace_readonly`、`sensitive_paths=denied`、`command_network=denied`、`model_egress=acknowledged` 和 permission Profile 名称；日志只记录确认布尔值，不记录工作区内容。

### 真实验证

- 真实 `doctor --workspace` 通过两项系统 sandbox 探针：所选仓库可读，工作区外临时哨兵返回拒绝。
- 真实 Codex 模型工具调用验证 `pyproject.toml` 可读而工作区内 `.env` 被拒绝，没有读取文件正文。
- 外层宿主直连 `https://example.com` 返回 200，同一命令进入 Edge permission Profile 后因 DNS 被拒绝返回 6，证明结果不是测试机断网制造的假通过。
- 未提供 `--allow-model-egress` 的 `run-once` 与 `serve` 在加载设备凭据和访问中央节点前失败；执行器构造也要求同一确认，避免旁路 CLI。
- 完整离线套件为 `1029 passed, 23 skipped`。真实 macOS arm64、CPython 3.12 最小 bundle 为 9 个 wheel，artifact SHA-256 为 `b59e918a2b823cd1c3c76349211b74615bcdda995c1c9645bfbcaa746cf64634`，manifest SHA-256 为 `a6074ab95cc9347b954cf611681a5244511cd02d48d7b02e303e04569440f771`；隔离安装、`pip check`、CLI、status 和模块白名单均通过。
- 开发服务器主 wheel SHA-256 为 `1330bd7c418a87241583b0b9fedccd766eea64951160ec855356d9fd18390042`，位于 `/srv/larkflow/target/releases/20260809_2122_edge_permissions_ab3ad5e/`。升级前备份已通过 `pg_restore --list`，migration ledger 保持 23 份，十个 Python 服务与 Caddy 均为 `active / NRestarts=0`，部署窗口无 warning。

### 事件响应底线

疑似越界读取或错误外发时，立即停止前台 Edge，中央撤销设备，保留 Edge 与 Attempt 追加型审计；若模型服务商凭据可能暴露，则同时撤销或轮换该凭据。随后固定涉事 workspace、Codex 版本、permission Profile 和任务输入，按供应商保留政策确认数据处置，再决定是否恢复。恢复前必须在相同版本上重新通过正反探针，不能只依赖配置静态检查。

复评结论仍为正式员工分发 No-Go。目录级只读和会话级外发告知从未实现缺口变为开发机制已验证，但上游 Profile 为 beta、证据只覆盖一台现有 Mac，且组织级数据政策、签名、公证、可信摘要渠道与全新员工 Mac 验收仍未关闭。

Apple 当前要求 Developer ID 分发软件使用 Developer ID 签名并通过公证流程；`altool` 已不再接受公证上传，应使用 `notarytool`。相关依据：

- [Notarizing macOS software before distribution](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
- [Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)
- [Resolving common notarization issues](https://developer.apple.com/documentation/security/resolving-common-notarization-issues)
- [Configuring the hardened runtime](https://developer.apple.com/documentation/xcode/configuring-the-hardened-runtime/)

Codex permission Profile 的语法、文件系统规则、macOS Seatbelt 后端与 beta 状态依据官方文档：[Codex permissions](https://learn.chatgpt.com/docs/permissions)。该文档是上游能力说明，不替代本项目在每个目标版本上的真实正反探针。

## 本轮验证证据

- 本机为 macOS arm64，Command Line Tools 已安装，`notarytool 1.1.2`、`pkgbuild`、`productbuild`、`productsign` 与 `stapler` 可用。
- 当前真实用户 Keychain 中 Developer ID Application 和 Developer ID Installer 身份均为 0，因此没有执行虚假签名或公证。
- 测试 bundle 为 macOS arm64、CPython 3.12，共 45 个 wheel，manifest 固定全部文件和 wheel 元数据。
- 在故意注入无效 pip index 与 HTTP、HTTPS、SOCKS 代理的环境下，安装仍只从 bundle wheelhouse 完成，pip 26.2.1 先于项目依赖安装，`pip check` 无 broken requirements。
- `pip-audit 2.10.1` 对最终隔离 site-packages 复扫为无已知漏洞；私有 `larkflow 0.0.2` 因不在 PyPI 被明确跳过。
- 本轮候选由未提交工作树构建，只用于验证机制。manifest 中的既有 HEAD 不能证明候选来源，禁止作为正式发布件。
