# RELATIONS · larkflow

## 出向依赖（我用谁）
| 依赖 | 用途 | 指向 |
|---|---|---|
| 飞书开放平台 (Lark Open Platform) | 应用载体：IM 机器人 / 消息卡片 / 任务 / 多维表格 / 云文档 / 画板 / 事件订阅 | 外部平台；dev 阶段用独立测试租户（ADR-008）|
| lark-cli (@larksuite/cli，飞书官方) | **入口 + 出口全包**：入口 = `event consume` 收 NDJSON 事件；出口 = 读写文档 / 表格 / 任务 / 发卡；+ 节点内 AI 工具 + 运维 | 外部 CLI；配方见 worklog memory reference-lark-cli-doc-deep-read / reference-feishu-lark-cli-access |
| newapi-proxy | LLM 网关（多账号聚合，规划 / 评分 / 内容生成走它）| ../newapi-proxy/ |
| LangGraph (Python) | 有环 Pregel 编排引擎（interrupt / Send / Command / SQLite checkpointer）| 外部库 |

## 入向（谁用我）
无（新项目，dev 阶段独立租户测试；后续是否落团队租户 = 部署决定，ADR-008）。

## 共享底座
- **引擎宿主 alicloud-sh**（<engine-host>，Ubuntu）：本项目独占用作引擎服务器，配置写本项目 DEPLOYMENT（ADR-007）。原 FRP 枢纽已于 7/01 退役，现闲置。
