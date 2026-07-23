# ARCHITECTURE · larkflow

## 核心结构：两层
- **领域 DAG（数据模型）**：一张任务依赖图 = 工作流「是什么」。节点 = 步骤（tool / llm / human），边 = 依赖（deps，前置完成解锁下游），关键节点带 gate（验收 / 门禁，不达标触发回边）。持久在 checkpointer + 投影到飞书；用户看到的是飞书任务 / 看板。
- **LangGraph 引擎（运行时）**：有环 Pregel 运行时 = 工作流「怎么推进」。一张固定的「编排器」图解释领域 DAG 数据，派发就绪节点、跑 tool / llm、human 节点 interrupt 持久挂起、门禁不达标环回，直到跑完。

> 关系：LangGraph 读领域 DAG → 推进 → 写回。DAG 是数据，LangGraph 是引擎。LangGraph 的「有环」正是打回 / iterate / 重启 / wait 所需，纯 DAG 引擎做不了。

## 组件
- **引擎服务**：Python + LangGraph + **SQLite checkpointer**（宿主 alicloud-sh，见 DEPLOYMENT / DECISIONS ADR-007）。含编排器图 + 模板注册表 + 节点执行器（tool / llm / human）。
- **飞书事件入口**：引擎 spawn `lark-cli event consume <EventKey>` 子进程，读其 stdout 的 **NDJSON 逐行事件**（有人 @bot / 点卡片按钮 / 任务完成）→ 起实例 / 唤醒挂起的图。**不接飞书 SDK**（ADR-005）。
- **飞书动作出口 + 工具手**：建任务 / 发卡 / 写文档 / 写多维表格，走 lark-cli。
- **LLM**：newapi 网关。

## 数据流（一次实例）
1. 触发：飞书事件（NDJSON）→ 起 LangGraph 实例（thread_id = 实例 id，选中模板）。
2. 规划 / 参数：AI 节点填参数 → 确认卡（human interrupt）。
3. 派发：编排器并行 super-step，各 human 节点 interrupt → 事件层建飞书任务 + 派单卡（含验收标准）→ 图 checkpoint 挂起。
4. 执行：AI 节点自动跑（newapi / lark-cli）；human 节点等交付。
5. 回收 + 门禁：执行人提交 → 飞书事件 → Command(resume=交付物) 唤醒该分支 → 门禁节点评分 → 达标解锁下游 / 不达标 needs_revision 环回。
6. 投影：图状态变 → 写多维表格（看板）+ 发进度卡。
7. 收口：全节点 done → 汇总卡 + 归档文档。

## 首个工作流：缺陷生命周期（见 DECISIONS ADR-009）
11 节点（5 门禁 5 环 + 1 旁路），三型齐全。**分两段建**：
- 第一段（证采用 + 门禁）：`intake(tool) → triage_ai(llm) → triage_review(human·★分诊) → reproduce(human·★可复现) → assign(tool·派飞书卡) → fix(human) → qa_verify(human·★验证·可 reopen 打回) → close(tool)`；`ci_test` / `code_review` 先用人工确认桩。
- 第二段（补全）：回填 `ci_test`（接真 CI·★CI 绿）/ `code_review`（human·★评审）/ `release_note`（llm 回执）。

## 关键选型（理由见 DECISIONS）
- 引擎 = LangGraph（有环），非纯 DAG 编排器（ADR-001）。
- 两层分离 + 单一事实源 = checkpointer + 飞书投影（ADR-002）。
- 路线 1 策展模板起步，节点契约按数据设计；生成走 few-shot（ADR-003 / ADR-010）。
- 宿主 alicloud-sh + SQLite（ADR-007）；入口 lark-cli event consume（ADR-005）。

## 禁改项
- LangGraph state 只放执行游标 + in-flight scratch；业务真相源 = checkpointer，飞书是投影，不反向写真相。
- 一张固定编排器图解释变化的 DAG 数据；**不按运行时数据 per-instance 现编译新 LangGraph 图**。
- 节点契约恒为数据 `{id, label, type, role, gate, deps}`，使生成（ADR-010）= 加 AI 作者 + 人审门、执行器不改。
- 入口只用 lark-cli event consume（NDJSON 子进程），不引入飞书 SDK。
