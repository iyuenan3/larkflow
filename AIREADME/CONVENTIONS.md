# CONVENTIONS · larkflow

## 命名
- 项目 slug `larkflow`（全小写，worklog 惯例）；中文名「飞流」（团队 / 飞书应用对外名）。
- 节点契约恒为数据：`executor(tool|llm|human) × role(produce|gate) + 配置`（见 SPEC）。

## 偏好模式
- 用 LangGraph 地道能力：`interrupt()` 挂起人在环、`Send` 并行扇出、`Command` 状态 + 路由、SQLite checkpointer 持久续跑。
- 数据驱动执行器：一张固定编排器图解释领域图数据（`dag` 是可写 channel，改它 = 运行时改图）。
- 新语义皆下沉配置、执行器不为业务改：多人投票（`vote`，主负责人由 ADR-023）/ 条件分支（`when` 守卫 + `skipped`）/ 子项目（节点由子实例回填）都是节点数据（ADR-024 / ADR-025）。
- 交付物统一飞书文档 handle：produce 节点末步物化到飞书；下游消费 fetch 正文；版本靠飞书原生（ADR-016）；handle 权威登记 = `state.outputs[node_id]`，`deliverable.container` 为活图声明位 / 回填指针（ADR-020）。
- 飞书 I/O 全走 lark-cli：入口 `event consume`（NDJSON 子进程）、出口 `markdown`/`docs`/`drive`/`task`/`im`。真写前先 `lark-cli skills read lark-doc/lark-markdown/lark-drive`，不靠 `--help` 猜 flag。
- 引擎 headless 可测：Mock/Stub 飞书 I/O + SQLite `:memory:` checkpointer 做单测，引擎核心不依赖飞书运行时（seg-1 15 测试即此法）。

## 完成信号约定（ADR-015）
- `human-produce`（人手写文档）完成 = 显式定稿信号，**优先结构化信号**（完成飞书任务 / 点卡片，无歧义；发消息变体推迟，ADR-021），引擎绝不因「文档不动了」判定稿。
- `human-gate`（人工审核）完成 = 点「通过 / 打回」卡片；打回时当场手选一组节点（选择性重算）。

## 模板生成护栏（few-shot，ADR-010）
- 召回最近 2-3 张种子模板当 few-shot，照同一节点 schema 产图、不从零发明。
- ① 每张含 tool/llm/human 三型且各有落点；② 每道 gate 须有可回退的传递祖先（打回时运行时手选一组 `reopen`、每个目标须 ⊆ gate 传递祖先，不再模板预声明单一 `on_fail`；refine ADR-010 ②，见 ADR-014）；③ 责任 / 放行 / 风险裁决节点强制 human，绝不让 LLM 自动放行；④ human 节点 ≥1 负责人、多人节点须 1 主负责人（ADR-023）；⑤ 条件分支决策取值域被分支守卫全覆盖或留默认支（ADR-025）。
- 生成图仍过人审门。

## 禁用模式
- 架构级不变式（固定编排器 / state 不存真相源 / 只改未来不改历史 / 入口不接飞书 SDK）见 ARCHITECTURE 禁改项，不在此复述。
- 编码层：LLM 不直连厂商专有 SDK，走 OpenAI 兼容多角色路由（从 env 按角色读 `(base_url, api_key, model)`）。

## 写作
- 中文产出不使用破折号，用逗号 / 句号 / 冒号 / 括号代替（继承全局规范）。
