# DEPLOYMENT · larkflow

⚑ **仍未部署**（一次真部署都没做过，宿主上没起过进程）。变化的是：**部署形态已经落码**（`larkflow serve` + CLI，ADR-031），不再是「立项 pre-code」的纸面设想。差的是 dev 飞书应用与真栈验证，见文末〈还缺什么才能真跑〉。

## 目标形态
- **宿主**：alicloud-sh（Ubuntu 22.04 / 2 核 / 1.6G 内存 / 40G 盘；内网地址走 keychain 不入库，当前只开 22 端口、闲置）。
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
| 关任务（目前只定义未调用） | `task +complete --task-id --as bot` | `task:task:write` | ✅ schema |
| 收「任务完成」事件 | `event consume task.task.update_user_access_v2 --as bot` | `task:task:read` + **控制台事件** `task.task.update_user_access_v2` | ✅ 事件 schema |
| 发门禁卡片 | `im +messages-send --user-id\|--chat-id --msg-type interactive` | 发消息权限（`im:message` 一族） | ⚠️ 推断 |
| 发通知（打回回执 / 卡死告警） | `im +messages-send … --msg-type text` | 同上 | ⚠️ 推断 |
| 收卡片按钮点击 | `event consume card.action.trigger --as bot` | `im:message:readonly` + **控制台回调** `card.action.trigger` | ✅ 事件 schema |
| 建交付物 | `markdown +create --name --content -` | Drive 文件写入（`drive:drive` / `drive:file:upload` 一族） | ⚠️ 推断 |
| 覆盖交付物 | `markdown +overwrite --file-token --content -` | 同上 | ⚠️ 推断 |
| 读交付物正文 | `markdown +fetch --file-token` | Drive 文件读取 | ⚠️ 推断 |

**「事件」与「回调」是两个东西，别在同一个页签里找**：开发者后台「事件与回调」下分两栏，`task.task.update_user_access_v2` 在**事件**里，`card.action.trigger`（卡片回传交互）在**回调**里。只订了事件、没订回调时，`lark-cli event consume card.action.trigger` 会以 `failed_precondition` 直接拒绝（文案用词是 callbacks 不是 events），并给出一键订阅链接，扫它最快。

**事件订阅方式必须选长连接**，不要 webhook：`lark-cli event consume` 走长连接，这正是 ADR-007「引擎无需任何入站端口」的来源；选了 webhook 整条入站链路不通。

**身份**：卡片回调只有 **bot** 收得到，故常驻服务 `LARKFLOW_IDENTITY=bot`。

**代理**：本机 shell 有 Clash 全局代理，lark-cli 会警告凭证经由代理传输。飞书是境内服务，建议 `LARK_CLI_NO_PROXY=1` 绕开。注意这条警告与 `failed_precondition` 类报错**无关**，后者是发请求之前的本地前置校验，别把它误诊成网络问题。

## 启动 / 退出行为
`larkflow serve` 的一生（顺序是硬的，理由见 ADR-031）：装 SIGINT/SIGTERM → **启动全实例对账** → 起泵 → block 到收到信号 → 停订阅 → 等在飞的那条事件跑完 → 关 DB。

**启动对账**做什么：按 checkpointer 里的实例逐个 `reconcile`（重建崩溃时丢掉的卡 / 待办投影，把被 super-step 屏障挡住的分支推到位）。三条要知道的性质：
- 实例枚举的真相源就是 checkpointer，**没有第二张实例表**；换掉 checkpointer 时优雅降级（报告里带 `degraded`），服务照起。
- **逐实例容错**：一个坏实例不阻塞启动，失败进报告与 stderr 日志。
- **跳过已跑完的实例**（没有投影要重建，重推只会重发通知）。
- 实例多了启动会变慢（无并发、无分批），这段时间入站通道还没起。**对账期间收到 SIGTERM 会当场中止**，没轮到的实例进报告与日志（不会把剩下几百个跑完，也不会再白起一次泵）。
- `larkflow reconcile`（不带实例）与它走**同一条代码路径**。

**退出**：停订阅 → 等在飞的那条事件跑完 → 关 DB。**没排空就不关 DB**（那条事件可能正握着实例锁写 checkpointer，关连接等于把桌子从它手底下抽走），改为记一笔 `drain` 故障、把半截写留给下次启动对账兜，并让进程**退出码 1**。所以 systemd 里 `Restart=always` 配合 `TimeoutStopSec` 要给足（一条事件里可能在跑 LLM）；日志里出现 `drain` 就说明上次没收干净，下次启动会补。

**DB 路径**：默认 `~/.larkflow/larkflow.sqlite`，`--db` / `LARKFLOW_DB` 给相对路径也会**落成绝对路径并回显**。默认值曾经是 cwd 相对的 `larkflow.sqlite`，于是 systemd 起的 daemon（`WorkingDirectory=/`）与你在 home 敲的救场命令各开各的库：两边都不报错、都「正常」，只是各看各的实例，而这个分叉没有任何症状。

**幂等**：派单与通知的幂等键记在本地幂等表里（ADR-033），所以重启 / 反复对账**不会**再给还在等的人发第二遍卡、建第二条待办。

## 多进程写同一个 SQLite
daemon 常驻握着 DB，而运维的一次性命令（尤其 `unblock`，那是 `blocked` 门的唯一出口）必须能同时执行。做法与边界：

- **保证**：走这套 API 的进程，对同一实例的状态变更严格串行（跨进程 flock，锁文件在 `<DB>.locks/`）；同一个 DB 只允许一个 daemon（`<DB>.serve.lock`）；SQLite 层不再有「database is locked」这类伪故障（WAL + busy_timeout）。
- **不保证**：flock 是**建议锁**，裸 sqlite3 或别的工具照样能进来写；NFS / SMB 上语义不可靠（故开不了 WAL 时 `open_db` 直接拒绝启动，不降级）；不是事务；不保证公平；对方握锁超过 `--lock-timeout`（默认 120s）时这边报错而不是硬闯。
- 拿不到实例锁时 daemon 会**丢掉那一条事件**（记一笔故障、继续下一条）。人手里的卡还在，再点一次即可，但那一次点击确实没被处理。
- Windows 跑不了（flock 依赖 fcntl，构造时直接抛，不静默降级成「没有锁」）。

## 环境变量（只列 key 名，真值走 `.env` / keychain，绝不入库；完整注释见仓库 `.env.example`）
- 飞书应用：`LARK_APP_ID`、`LARK_APP_SECRET`、`LARK_PROFILE`（lark-cli profile，认哪个应用）、`LARKFLOW_IDENTITY`（bot | user，卡片回调只有 bot 收得到）。
- 引擎：`LARKFLOW_DB`（SQLite 路径，**本地盘**）、`LARKFLOW_TEMPLATE`（默认模板名）、`LARKFLOW_DRIVE_FOLDER`（交付物落哪个云空间文件夹）。
- 角色映射：`LARKFLOW_ROLES`（JSON，`assignee_role → open_id`；中文角色名当环境变量名 export 不进去，故以 JSON 为主）、`LARKFLOW_ROLE_<ASCII 别名>`（辅，会合并）。真栈 strict：模板里出现的角色没配全会在**装配期直接抛**，绝不伪造 `ou_<角色名>` 发给飞书。
- LLM（ADR-017，按角色一组三元组）：`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`（默认角色兜底），以及 `LLM_<ROLE>_BASE_URL` / `_API_KEY` / `_MODEL`（如 writer / legal / editor / triage）。三元组缺项的角色会被跳过。

## 进程守护建议（尚未在真机上验证）
- systemd 单元：`ExecStart=/usr/local/bin/larkflow serve`、`Restart=always`、`RestartSec=5`，`EnvironmentFile=` 指到 `.env`，`WorkingDirectory` 与 `LARKFLOW_DB` 的目录读写权限对齐（锁文件与 WAL 会写在 DB 同目录）。停止用默认 SIGTERM 即可（daemon 会优雅收尾）；`TimeoutStopSec` 给到大于事件处理时间（一条事件里可能在跑 LLM）。
- `lark-cli event consume` 子进程的拉起与断线重启由 daemon 自己管（退避重启 + 上限，达上限会喊出来），systemd 只管 daemon 本身。
- 观测：目前只有 stderr 日志 + 进程内计数（`server.stats` / `server.errors`），**没有 HTTP 探针、不落盘指标**（ADR-007 无入站端口下有意为之）。运维靠 `journalctl` 与 `larkflow status <实例>`。
- 备份：直接备份 SQLite 文件（WAL 模式下连 `-wal` / `-shm` 一起，或先 `sqlite3 .backup`）。锁文件（`<DB>.locks/`、`<DB>.serve.lock`）不必备份，已进 `.gitignore`。

## 还缺什么才能真跑（按顺序，每条都没做）
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
