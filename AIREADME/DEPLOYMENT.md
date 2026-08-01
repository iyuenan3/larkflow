# DEPLOYMENT · larkflow

> **As-built / Legacy Prototype + Target Runtime 开发部署。** 本文保存 legacy LangGraph + SQLite 服务与 Target PostgreSQL Runtime 在单台 ECS 上的真实部署记录。它不是目标 SaaS 拓扑：目标架构是 PostgreSQL 中央控制面的模块化单体，个人 Agent Edge 已移出近期范围，见 [ARCHITECTURE.md](ARCHITECTURE.md)。
>
> 除修正事实错误外，不再给这套部署增加新的产品领域能力。个人端不得复用下文的企业 bot 全局凭证；中央端和个人端必须使用不同身份、权限与生命周期。

## Target PostgreSQL 开发验证状态（2026-08-01）

- `alicloud-sh` 的 PostgreSQL 14.23 保持 active，`listen_addresses=localhost`，5432 只监听 `127.0.0.1`。宿主系统盘约有 33 GB 可用，内存约有 993 MB available。该数据库是自建 Target 开发环境，不是生产数据库，也不具备托管数据库的高可用能力。
- 一次性数据库与最小权限密码角色通过本机 SSH 隧道运行完整 `tests/test_workflow_postgres.py`，3 项全部通过：migration 重入、聚合与 outbox 往返、两个真实连接竞争同一节点、过期 claim 恢复。测试前先跑单 Worker 基线；完成后数据库与角色均已删除，并从系统目录回读为 0。
- 长期开发库为 `larkflow_target_dev`，所有者是无密码角色 `lf_target_dev`。同名 Unix 系统用户通过本机 Unix socket 的 peer authentication 连接；角色不能超级管理、建库、建角色、复制或绕过 RLS，`PUBLIC` 没有数据库连接权。数据库默认 `timezone=UTC`、`statement_timeout=30s`、`lock_timeout=5s`、`idle_in_transaction_session_timeout=60s`。
- 长期开发库仍应用 `0001_workflow` 到 `0005_template_lifecycle`。仓库新增的 `0006_inbox_verification_exhaustion` 已在一次性 PostgreSQL 14 数据库验证，但尚未部署到长期开发库。第六份 migration 允许 Inbox 进入不可再认领的 `exhausted` 终态；凭据侧默认验证上限为 24 次，结构化日志中的非零 `exhausted` 必须告警。
- Runtime 使用 `/srv/larkflow/target/venv` 中的 wheel，以 `lf_target_dev` 运行并通过 Unix socket peer authentication 连接。`/etc/larkflow-target.env` 为 `0640 root:lf_target_dev`，systemd unit 为 `0644 root:root`。
- 包含严格字段校验的 Template Service、正式模板 CLI 和 `llm.generate` Agent adapter wheel 已安装到 Target 独立虚拟环境，SHA-256 为 `8fb89a37e11fed5215a8b0177d262216ab3f13a89508929427ef1c8d6601dce3`，四个 Target 服务已重启并回读 active。`/etc/larkflow-target.env` 已在明确授权下启用开发用 OpenAI 兼容主路由，不配置备用线路；`LLM_TIMEOUT=240`、claim TTL 为 300 秒、安全余量为 30 秒，且关闭环境代理继承。env 保持 `0640 root:lf_target_dev`，路由真值不进入仓库或日志。
- 部署前备份已回读 `Result=success / ExecMainStatus=0`。最终发布件保存在 `releases/8fb89a37/`，前一测试件与功能前版本保存在 `releases/7d262a55/`、`releases/7ebd6582/`。文件均为 `0640 root:lf_target_dev`，可按相同停服、安装、启动步骤回滚。
- 真实开发实例已在测试飞书组织完成 `Human -> Agent -> Human`：首个 Human Task 完成后只提交 `{confirmed: true}`，真实模型生成 210 字正文，最终 Human Task 精确展示该正文；第二次人工完成后 Instance 与三个 Node / Attempt 全部为 `done`。验证不代表生产上线。
- 正式模板 CLI 已用合成输入依次完成模板创建、启用、从模板创建草稿、只读预览和确认。实例 `template_entry_20260801_213749` 保存 `target_agent_review:1` 完整快照，并已用真实飞书 Task 与真实模型完成 `Human -> Agent -> Human`：Instance 与三个 Attempt 均为 `done`，两条 Task Projection 均完成，该实例八条 Outbox 均为 `published`。该流程仍是开发验证，不含用户业务数据。
- Projection 使用同一 wheel 和独立 `larkflow-target-projection.service`，以持有测试飞书 profile 的 `lf-dev` 运行，不复制加密 app secret。PostgreSQL 同名角色只能 SELECT migration、Instance、Node、Attempt、Outbox 与 Projection，只能 UPDATE Outbox、INSERT / UPDATE Projection，不能更新 Instance 领域状态。`/srv/larkflow/target` 保持 `0750`，只通过 ACL 给 `lf-dev` 路径穿越权限；Projection env 为 `0640 root:lf-dev`，不含飞书密钥。
- 凭据侧入站校验使用 `larkflow-target-inbound-adapter.service`，以 `lf-dev` 运行并只读飞书 Task 详情。它可以 SELECT / UPDATE Inbox，不能更新 Instance、Node 或 Attempt。领域入站使用 `larkflow-target-inbound.service`，以 `lf_target_dev` 运行，不能读取 legacy 飞书 profile 与应用凭据。
- `larkflow-target.service`、`larkflow-target-projection.service`、`larkflow-target-inbound-adapter.service`、`larkflow-target-inbound.service` 与 legacy `larkflow@dev` 均 enabled / active。legacy 是 Task EventKey 的唯一消费者，只把原始信号写入 Inbox，不写 Target 领域表。
- 常驻验证覆盖普通 Tool 完成、SIGTERM 干净退出、SIGKILL 后 5 秒自动拉起，以及租约到期后由不同 Worker 恢复同一 Attempt。有效故障注入最终记录 `recovered=1`、`completed=1`、`stale_results=0` 和 `node.claim_recovered` 审计。
- `larkflow-target-backup.timer` 每天北京时间 03:20 后随机延迟不超过 15 分钟执行 custom-format `pg_dump`，本机保留约 7 天。备份目录权限为 `0700 lf_target_dev:lf_target_dev`，备份文件为 `0600`，backup service 使用 systemd 文件系统与权限沙箱。最近一次恢复演练在当时只有两份 migration 时完成，已回读表所有权和收紧的 schema 权限并删除恢复库。五份 migration 已进入备份范围，但新版恢复演练尚未重跑。
- 两阶段 Inbox 已在一次性真实 PostgreSQL 数据库中验证 migration 重入、event ID 去重、校验与领域两组双 Worker 竞争、无效 claim token 拒绝、阶段恢复和最终 `processed` 终态。一次性数据库已删除。
- Template Service 已在一次性真实 PostgreSQL 14 数据库中验证五份 migration 重入、两路同时启用时一条成功一条并发冲突、版本更新触发器拒绝修改、模板审计追加和冻结实例外键。一次性数据库与远端验证脚本均已删除。
- 备份目前只在同一块系统盘，能处理误操作和局部数据损坏，不能处理整机或云盘丢失，也没有 PITR。进入生产前必须增加异机或对象存储副本、恢复演练、容量告警和 PostgreSQL 升级流程。
- `larkflow@dev` 始终保持 active，仍运行 legacy SQLite 路径。Projection 开发服务只复用它的飞书 OS 身份和 profile，不读取 legacy SQLite；Target Runtime 与 legacy 领域状态没有混接。

### Target PostgreSQL 运维入口

- 应用身份：systemd 服务以 `lf_target_dev` 运行，通过 `postgresql:///larkflow_target_dev` 连接，不配置数据库密码，不改用 TCP。
- Target CLI：`/srv/larkflow/target/venv/bin/larkflow-target --env-file /etc/larkflow-target.env <command>`；模板控制面支持 template-create、template-add-version、template-enable、template-disable、template-delete、template-list、template-show、create-from-template 与 preview，并保留实例和四类 Worker 命令。
- Agent 开关：`LARKFLOW_TARGET_ENABLE_AGENT_EXECUTOR=true`。路由使用 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`，单线路可用 `LLM_TIMEOUT` 收紧。启动时会计算主线路与全部备用线路的超时总和，并要求该值加 `LARKFLOW_TARGET_AGENT_CLAIM_SAFETY_SECONDS` 后严格小于 `LARKFLOW_TARGET_CLAIM_TTL_SECONDS`；不满足时服务拒绝启动。
- Runtime 服务：`systemctl status larkflow-target.service`；日志看 `journalctl -u larkflow-target.service`。
- Projection 服务：`systemctl status larkflow-target-projection.service`；日志看 `journalctl -u larkflow-target-projection.service`。仓库 unit 与 env 模板为 `deploy/larkflow-target-projection.service`、`deploy/larkflow-target-projection.env.example`。
- 入站校验服务：`systemctl status larkflow-target-inbound-adapter.service`；日志看 `journalctl -u larkflow-target-inbound-adapter.service`。
- 领域入站服务：`systemctl status larkflow-target-inbound.service`；日志看 `journalctl -u larkflow-target-inbound.service`。
- 手工只读连接：`sudo -u lf_target_dev env --chdir=/ psql -X --dbname=larkflow_target_dev`。
- migration：由目标应用启动入口调用 package-data migration runner。长期库的五份 migration 已落地，后续不得手工改 schema 后跳过 migration ledger。
- 立即备份：`sudo systemctl start larkflow-target-backup.service`；结果看 `systemctl show larkflow-target-backup.service --property=Result,ExecMainStatus`。
- 定时器：`systemctl show larkflow-target-backup.timer --property=ActiveState,UnitFileState,NextElapseUSecRealtime`。
- 恢复：先由 postgres 管理员创建目标库，重建 UTC 与三项 timeout，撤销 `PUBLIC` 对 `public` schema 的 CREATE，并授予 `lf_target_dev` USAGE 与 CREATE；再以 `lf_target_dev` 执行 `pg_restore --exit-on-error --single-transaction --no-acl`。不能直接让应用角色恢复 ACL，`public` schema 不归它所有，pg_restore 只会 warning，目标库会保留默认 PUBLIC CREATE。最终验收同时回读 migration、表所有者、ACL、时区与 timeout。
- 仓库资产：`deploy/larkflow-target-backup`、`deploy/larkflow-target-backup.service`、`deploy/larkflow-target-backup.timer`。服务器安装位置分别是 `/usr/local/sbin/` 与 `/etc/systemd/system/`。

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
| 派人工任务 | `task +create --summary --description --assignee --idempotency-key --as bot` | `task:task:write` | ✅ schema |
| 关任务 | `task +complete --task-id --as bot` | `task:task:write` | ✅ Target 真栈 |
| 收「任务完成」事件 | `event consume task.task.update_user_access_v2 --as bot` | `task:task:read` + **控制台事件** `task.task.update_user_access_v2` | ✅ 事件 schema |
| 发门禁卡片 | `im +messages-send --user-id\|--chat-id --msg-type interactive` | 发消息权限（`im:message` 一族） | ⚠️ 推断 |
| 发通知（打回回执 / 卡死告警） | `im +messages-send … --msg-type text` | 同上 | ⚠️ 推断 |
| 收卡片按钮点击 | `event consume card.action.trigger --as bot` | `im:message:readonly` + **控制台回调** `card.action.trigger` | ✅ 事件 schema |
| 建交付物 | `markdown +create --name --content -` | Drive 文件写入（`drive:drive` / `drive:file:upload` 一族） | ⚠️ 推断 |
| 覆盖交付物 | `markdown +overwrite --file-token --content -` | 同上 | ⚠️ 推断 |
| 读交付物正文 | `markdown +fetch --file-token` | Drive 文件读取 | ⚠️ 推断 |

**已在真栈实测通过（测试组织）**：2026-07-26 的 `im +messages-send --msg-type interactive` 与 `event consume card.action.trigger --as bot`；2026-08-01 的 Target Human Task 原生创建、完成与详情读取。测试 app 当前权限仍宽于最小集合，所以「命令能跑」已确认，「最小 scope 是哪一个」仍需逐个关闭权限回归。Target Task 完成入站已接线，其他入站命令仍未实现。

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
