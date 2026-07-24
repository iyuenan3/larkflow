# CONVENTIONS · larkflow

## 命名
- 项目 slug `larkflow`（全小写，worklog 惯例）；中文名「飞流」（团队 / 飞书应用对外名）。
- 节点契约恒为数据：`executor(tool|llm|human) × role(produce|gate) + 配置`（见 SPEC）。

## 偏好模式
- 用 LangGraph 地道能力：`interrupt()` 挂起人在环、`Send` 并行扇出、`Command` 状态 + 路由、SQLite checkpointer 持久续跑。
- 数据驱动执行器：一张固定编排器图解释领域图数据（`dag` 是可写 channel，改它 = 运行时改图）。
- 交付物统一飞书文档 handle：produce 节点末步物化到飞书；下游消费 fetch 正文；版本靠飞书原生（ADR-016）。
- 飞书 I/O 全走 lark-cli：入口 `event consume`（NDJSON 子进程）、出口 `markdown`/`docs`/`drive`/`task`/`im`。真写前先 `lark-cli skills read lark-doc/lark-markdown/lark-drive`，不靠 `--help` 猜 flag。

## 完成信号约定（ADR-015）
- `human-produce`（人手写文档）完成 = 显式定稿信号（发飞书消息 / 完成飞书任务），引擎绝不因「文档不动了」判定稿。
- `human-gate`（人工审核）完成 = 点「通过 / 打回」卡片；打回时当场手选一组节点（选择性重算）。

## 模板生成护栏（few-shot，ADR-010）
- 召回最近 2-3 张种子模板当 few-shot，照同一节点 schema 产图、不从零发明。
- ① 每张含 tool/llm/human 三型且各有落点；② 每道 gate 必配回边（打回目标，杜绝只有前向边的假流程）；③ 责任 / 放行 / 风险裁决节点强制 human，绝不让 LLM 自动放行。
- 生成图仍过人审门。

## 禁用模式
- 不 per-instance 现编译新 LangGraph 图（用固定编排器 + 数据驱动派发）。
- 不在 LangGraph state 里存业务真相源（真相源 = checkpointer，飞书 = 投影）。
- 不改历史：受控活图只改 pending 子图，不原地改写已完成节点产出。
- LLM 不直连厂商专有 SDK，走 OpenAI 兼容多角色路由。入口不接飞书 SDK，只用 lark-cli event consume。

## 写作
- 中文产出不使用破折号，用逗号 / 句号 / 冒号 / 括号代替（继承全局规范）。
