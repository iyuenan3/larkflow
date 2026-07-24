# RELATIONS · larkflow

## 出向依赖（我用谁）
| 依赖 | 用途 | 指向 |
|---|---|---|
| 飞书开放平台 (Lark Open Platform) | 应用载体 + 交付物承载：IM 机器人 / 消息卡片 / 任务 / 云文档 / 云盘 / 多维表格 / 事件订阅 / 原生版本史 | 外部平台；dev 走独立测试租户（ADR-008）|
| lark-cli (@larksuite/cli，飞书官方) | **入口 + 出口全包**：入口 = `event consume` 收 NDJSON 事件；出口 = 交付物读写 + 建任务 / 发卡 + 节点内 AI 工具 + 运维（命令级契约见 SPEC）| 外部 CLI；dev 走独立测试组织（ADR-008）|
| 妙搭 (Miaoda，飞书官方 app 平台) | 前端宿主：本地开发 + 飞书托管（`aiforce.cloud`），挂工作台；larkflow 前端 = 引擎的投影 + 客户端（ADR-019）| 外部平台；dev 走测试组织 |
| LLM 供应商（OpenAI 兼容） | LLM 网关：按任务角色路由到不同模型（火山方舟 / 中转站 / 直连），各角色独立 key（ADR-017）| 外部 API；配置见 `.env.example` |
| LangGraph (Python) | 有环 Pregel 编排引擎（interrupt / Send / Command / SQLite checkpointer）| 外部库 |

## 入向（谁用我）
无（新项目，dev 阶段独立租户测试；后续是否落团队租户 = 部署决定，ADR-008）。

## 共享底座
- **引擎宿主 alicloud-sh**（Ubuntu）：本项目独占用作引擎服务器，配置 / 内网地址走 keychain 不入库，写本项目 DEPLOYMENT（ADR-007）。
