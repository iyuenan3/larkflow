# Personal Agent Edge macOS 分发安全评审

> 评审日期：2026-08-05
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

## 威胁与结论

| 风险 | 当前控制 | 剩余缺口 | 门禁 |
|---|---|---|---|
| 交付物被替换 | manifest SHA-256、逐文件哈希和精确文件集 | 首次运行的 manager 本身尚未由系统信任链认证，同渠道交付 bundle 与摘要会失去独立性 | P0 |
| 构建依赖漂移 | 安装候选件由 manifest 完整锁定 | 构建阶段仍从版本范围解析依赖，没有可复现 lock 和构建证明 | P0 |
| 依赖供应链与攻击面 | 只接受 wheel、离线安装、包清单、已知漏洞扫描 | 当前完整 `larkflow` wheel 把中央栈带到员工端，测试 bundle 共 45 个 wheel，其中 44 个是应用及运行依赖 | P0 |
| 已知依赖漏洞 | `pip-audit 2.10.1` 审计隔离安装 | 首次候选的 pip 26.1 命中 `CVE-2026-8643`；实现已先升级至 26.2.1，复扫无已知漏洞，但私有 `larkflow` 包不在 PyPI 审计范围 | 已缓解，发布时重扫 |
| macOS Gatekeeper 信任 | 无 | 本机没有 Developer ID Application 或 Developer ID Installer 身份，也没有公证凭据；当前 bundle 未签名、未公证、未 stapling | P0 |
| 本机代码执行范围 | Codex 使用 read-only sandbox、最小环境和固定工作区 | 只读不等于目录级读取隔离，也不等于模型调用无数据外发；恶意任务输入仍可能诱导读取当前用户可读内容 | P0 |
| 凭据泄露 | Keychain、非敏感元数据、密钥不进 argv 和环境 | 同一登录用户下的恶意本机进程仍不在当前防护能力内 | 接受为首版环境前提，需员工告知 |
| 升级失败 | 独立 release、原子 current/previous、rollback | 旧 release 清理和磁盘上限尚未定义 | P1 |
| 来源追溯 | manifest 记录完整 commit | source commit 仍由构建命令调用者声明，未与 clean tree、CI 身份或签名证明绑定 | P0 |

`pip-audit` 首次输出把同一个 `PYSEC-2026-196` 公告列出两次，唯一对应公告为 `CVE-2026-8643 / GHSA-wf93-45jw-7689`。修复后的隔离 venv 使用 pip 26.2.1，当前扫描结果为 `No known vulnerabilities found`。该结论只代表 2026-08-05 的公开漏洞数据库快照。

## 正式分发放行条件

以下条件必须全部满足，才能把结论改为 Go：

1. 拆出最小 Edge 分发包，只包含设备协议、Keychain、HTTP 客户端、Codex 适配器与 CLI，不携带 LangGraph、OpenAI SDK、PostgreSQL 驱动或中央运行时。
2. 从 clean Git commit 和固定 lock 构建，记录构建器版本、Python 目标、依赖来源、CycloneDX SBOM、完整哈希与漏洞审计结果。
3. 取得 Apple Developer Program 下的 Developer ID 身份与公证凭据，凭据只进入构建机 Keychain 或受控 CI secret store。
4. 对所有提交给 Apple 的可执行代码与原生库使用安全时间戳；适用的 Mach-O 启用 hardened runtime。使用 `notarytool` 提交，完成 stapling，并由 `spctl`、`codesign`、`pkgutil` 和 `stapler validate` 回读。
5. manifest 摘要经独立可信渠道发布，最终交付件不可只依赖同目录文本摘要建立信任。
6. 在一台没有项目源码、没有既有 Edge 安装的全新员工 Mac 上完成首装、配对、`doctor`、前台执行、升级失败、回滚、撤销和卸载边界验收。
7. 明确数据外发政策、允许工作区、敏感目录排除、用户告知与事件响应流程。目录级读取隔离未实现前，不向含敏感材料的工作区开放。

Apple 当前要求 Developer ID 分发软件使用 Developer ID 签名并通过公证流程；`altool` 已不再接受公证上传，应使用 `notarytool`。相关依据：

- [Notarizing macOS software before distribution](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
- [Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)
- [Resolving common notarization issues](https://developer.apple.com/documentation/security/resolving-common-notarization-issues)
- [Configuring the hardened runtime](https://developer.apple.com/documentation/xcode/configuring-the-hardened-runtime/)

## 本轮验证证据

- 本机为 macOS arm64，Command Line Tools 已安装，`notarytool 1.1.2`、`pkgbuild`、`productbuild`、`productsign` 与 `stapler` 可用。
- 当前真实用户 Keychain 中 Developer ID Application 和 Developer ID Installer 身份均为 0，因此没有执行虚假签名或公证。
- 测试 bundle 为 macOS arm64、CPython 3.12，共 45 个 wheel，manifest 固定全部文件和 wheel 元数据。
- 在故意注入无效 pip index 与 HTTP、HTTPS、SOCKS 代理的环境下，安装仍只从 bundle wheelhouse 完成，pip 26.2.1 先于项目依赖安装，`pip check` 无 broken requirements。
- `pip-audit 2.10.1` 对最终隔离 site-packages 复扫为无已知漏洞；私有 `larkflow 0.0.2` 因不在 PyPI 被明确跳过。
- 本轮候选由未提交工作树构建，只用于验证机制。manifest 中的既有 HEAD 不能证明候选来源，禁止作为正式发布件。
