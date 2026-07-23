# ROADMAP · larkflow

## Now（第一段 · 证采用 + 门禁）
- 建 dev 飞书租户企业自建应用（ADR-008）+ 订阅事件，`event list` 定卡片 action / 任务完成的确切 EventKey。
- 引擎起在 alicloud-sh：Python + LangGraph + SQLite checkpointer（ADR-007）。
- 缺陷流「人主干 + G5 门禁」跑通：`intake → triage_ai → triage_review → assign(派飞书卡) → fix → qa_verify(★可 reopen 打回) → close`；`ci_test` / `code_review` 先用人工确认桩。
- 达成第一个 win（见 PRD）：真实 bug 走完、门禁打回一次、团队真处理、自动收口。

## Next（第二段 · 补全缺陷流）
- 回填 `ci_test`（接真 CI）/ `code_review` / `release_note`，补全 11 节点。
- 投影到多维表格（实例看板）+ 进度卡。

## Later
- 多模板库（种子库）+ **few-shot 生成**（召回最近 2-3 张种子 + 3 护栏 + 人审门，ADR-010）。
- 小程序 / H5 全局面板（采用证明后再加，ADR-011）。
- 运行中实例编辑；项目健康度信号；是否落团队租户（ADR-008 部署决定）。

## 搁置（+ 原因）
- 通用 iPaaS 集成：偏离飞书原生定位。
- MVP 自建前端面板：不服务「先证采用 + 门禁」。
- 对 C 收费 / 多租户 SaaS：团队内部工具优先，过早。
