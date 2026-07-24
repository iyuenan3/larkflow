# DEPLOYMENT · larkflow

⚑ 未部署（立项 pre-code）。宿主与形态已定（见 DECISIONS ADR-007），落地后补细节。

## 目标形态
- **宿主**：alicloud-sh（Ubuntu 22.04 / 2 核 / 1.6G 内存 / 40G 盘；内网地址走 keychain 不入库，当前只开 22 端口、闲置）。
- **持久化**：LangGraph checkpointer 用 **SQLite**（省内存，单租户 MVP 够）。
- **事件入口**：引擎 spawn `lark-cli event consume <EventKey>` 子进程收 NDJSON。**出站长连接，无需开任何入站端口 / 域名 / 证书**，正合该机锁死状态。
- **LLM**：OpenAI 兼容多角色路由（火山方舟 / 中转站 / 直连，见 RELATIONS / DECISIONS ADR-017）。
- **飞书应用**：独立 dev 飞书租户（ADR-008）的企业自建应用，MVP 挂工作台 = app + bot + 卡片（cards-only，ADR-011）。凭证（app_id / secret / lark-cli token）走 env / keychain，绝不入库。

## 待落地后填
- 进程守护（systemd / supervisor）+ `event consume` 子进程拉起与断线重启。
- 环境变量清单（只列 key 名，不写值）。
- 升级 / 回滚 / 备份（SQLite 文件备份）。
- 内存吃紧的观测阈值与升配 / 迁 Postgres 触发条件。
