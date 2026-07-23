# CONVENTIONS · larkflow

## 命名
- 项目 slug `larkflow`（全小写，worklog 惯例）；中文名「飞流」（团队 / 飞书应用对外名）。
- 节点契约恒为数据：`{id, label, type: tool|llm|human, role, gate, deps}`（见 SPEC）。

## 偏好模式
- 用 LangGraph 地道能力：`interrupt()` 挂起人在环、`Send` 并行扇出、`Command` 状态 + 路由、SQLite checkpointer 持久续跑。
- spec 驱动执行器：一张固定编排器图解释领域 DAG 数据。
- 飞书 I/O 全走 lark-cli：入口 `event consume`（NDJSON 子进程）、出口读写任务 / 卡 / 文档 / 表格。

## 模板生成护栏（few-shot，ADR-010）
- 召回最近 2-3 张种子模板当 few-shot 范例，照同一节点 schema 产图、不从零发明。
- ① 每张含 tool/llm/human 三型且各有落点；② 每道门禁必须配一条显式回边（杜绝只有前向边的假流程）；③ 责任 / 放行 / 风险裁决节点强制 human，绝不让 LLM 自动放行。
- 生成图仍过人审门。

## 禁用模式
- 不 per-instance 现编译新 LangGraph 图（用固定编排器 + 数据驱动派发）。
- 不在 LangGraph state 里存业务真相源（真相源 = checkpointer，飞书 = 投影）。
- LLM 不直连厂商，走 newapi。入口不接飞书 SDK，只用 lark-cli event consume。

## 写作
- 中文产出不使用破折号，用逗号 / 句号 / 冒号 / 括号代替（继承全局规范）。
