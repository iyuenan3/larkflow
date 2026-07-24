# DEPLOYMENT · larkflow

⚑ 未部署（立项 pre-code）。宿主与形态已定（见 DECISIONS ADR-007），落地后补细节。

## 目标形态
- **宿主**：alicloud-sh（Ubuntu 22.04 / 2 核 / 1.6G 内存 / 40G 盘；内网地址走 keychain 不入库，当前只开 22 端口、闲置）。
- **持久化**：LangGraph checkpointer 用 **SQLite**（省内存，单租户 MVP 够）。
- **事件入口**：引擎 spawn `lark-cli event consume <EventKey>` 子进程收 NDJSON，出站长连接。原设计「无需任何入站端口」（ADR-007）。
- **前端↔引擎传输（命门，ADR-019）**：前端要引擎的读 / 命令 API = 入站。但妙搭云托管（`aiforce.cloud`）能否 egress 到本机、且本机能否公网可达（公网 IP / 域名 / 证书 / 反代 / 隧道）**未确认**。若不能，**退「命令走飞书原生轨」**：app 写多维表格 / 发消息 / 触发自动化 → 引擎经 `event consume` 消费（保 ADR-007「无入站」）。**列为妙搭原型第一必验项，排在画布之前。**
- **LLM**：OpenAI 兼容多角色路由（火山方舟 / 中转站 / 直连，见 RELATIONS / DECISIONS ADR-017）。
- **飞书应用**：独立 dev 飞书租户（ADR-008）的企业自建应用，挂工作台 = 妙搭前端（本地开发，飞书托管 `aiforce.cloud`，ADR-019）+ bot + 卡片。凭证（app_id / secret / lark-cli token）走 env / keychain，绝不入库。

## 待落地后填
- 进程守护（systemd / supervisor）+ `event consume` 子进程拉起与断线重启。
- 环境变量清单（只列 key 名，不写值）。
- 升级 / 回滚 / 备份（SQLite 文件备份）。
- 内存吃紧的观测阈值与升配 / 迁 Postgres 触发条件。
- 妙搭前端部署（本地开发 → 发布 `aiforce.cloud`）+ 引擎读 / 命令 API 的暴露方式（ADR-019）。
- 意图路由层（v1.1，ADR-021）= 引擎外独立进程：可与引擎共宿 alicloud-sh、headless 不依赖飞书运行时；落地时纳入进程守护，明确其与 event consume / 引擎的进程拓扑与 env（复用已列 LLM 多角色路由，无新增外部依赖）。
