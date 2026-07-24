# ROADMAP · larkflow

## Now（v1 · 证交付物流转 + 打回省算）
- 建 dev 飞书租户企业自建应用（ADR-008）+ 订阅事件（卡片 action / 任务完成 / 定稿消息 EventKey）。
- 引擎起在 alicloud-sh：Python + LangGraph + SQLite checkpointer（ADR-007）。
- **独立 doc 拓扑**跑通：produce/gate 节点 + 选择性重算打回 + `auto`/`single` 审核 + 交付物 = 飞书 markdown handle + merge 节点（ADR-016 / ADR-018）。
- 接 LLM 多角色路由（OpenAI 兼容，ADR-017）。
- 达成第一个 win（见 PRD）：一个真实交付物流转项目（合同类）端到端跑通、打回选择性重算、运行中改图、auto-approve。

## Next（v2 · 共享协同 + 会签 + 子项目）
- **共享协同拓扑**：docx `region=section` + 预划 section + 子项目产出回填父 doc 段（先验 docx block_id 跨 update 稳定性）。
- 会签（`approval_policy` = `any`/`all`）、AI-gate（AI 审核节点）。
- 子项目 spawn（子 DAG，组员自起项目）。
- 投影到多维表格（项目看板）+ 进度卡。

## Later
- 崩溃后对账重建（seg-1 推迟的 finding D，真常驻服务上线前必做，见 MEMORY）。
- reopen 预算 / blocked 终态（finding C，自动化门禁回填时）。
- 五维 AI 评分（可选增强，非 win 核心，ADR-015）。
- 多模板库（种子库）+ few-shot 生成（ADR-010）。
- 小程序 / H5 全局面板（采用证明后，ADR-011）；是否落团队租户（ADR-008）。

## 搁置（+ 原因）
- 通用 iPaaS 集成：偏离飞书原生定位。
- MVP 自建前端面板：不服务「先证采用 + 流转」。
- 对 C 收费 / 多租户 SaaS：团队内部工具优先，过早。
