# MEMORY · larkflow（append-only）

⚑ 尚无运行事故（立项 pre-code）。

踩坑随开发追加，每条 = 现象（事实）+ 根因（标把握度：已复现 / 仅推测）+ 结论 / 避免。预期高发区：飞书事件订阅长连接稳定性、LangGraph interrupt / resume 与飞书卡片回调的时序、checkpointer 与飞书投影的一致性。

## 2026-07-23 · 第一段引擎对抗性审查（9 项确认，去重 6 根因）
本地骨架跑通后做了一轮对抗性代码审查（多 agent 找 + 逐条复核）。**关键结论：全部不影响已交付的 seg-1 线性人工门禁流程**（e2e 真绿），都是「通用引擎」预埋隐患，只在并行 / 自动化门禁 / AI 生成图 / 崩溃时触发。

**已修（本轮）**：
- A 并行回边竞争（HIGH，静默数据损坏，已复现）：worker 写全下游 status + last-write-wins reducer，在同一 super-step 里回边 pending 被兄弟 done 覆盖。根因修：worker 只写自己键（门禁失败标 `failed`），回边 `reopen_resets` 由 `dispatch` 单点做。回归测试 `test_engine_parallel.py`。
- B on_fail 非祖先未校验（校验缺口）：加护栏②b（on_fail 须是门禁节点 deps 祖先），否则门禁节点不在重置集、dispatch 无限重选。
- E resume 无 per-thread 串行化：EventPump 每 EventKey 一线程，同实例并发 resume 会丢更新。加 `_thread_lock`。
- recursion_limit 未设：运行 config 加 `2*len(dag)+25`。
- F 陈旧守卫并行下不可靠（LOW，无损坏）：`get_state().interrupts` 在同批兄弟未 resolve 时会滞留已 resume 的 id。守卫加节点 `status==done` 交叉核对。

**推迟（记录，修复时机）**：
- **D 崩溃后无对账重建（MEDIUM，仅推测·未复现）**：`_provision` 先 `create_task` 后 `corr.put` 非原子；崩在中间 → 任务在、关联缺 → 任务完成事件（只带 task_guid）无法路由 → 卡死。触发：真服务进程重启。修：启动时 `reconcile()` 重跑 `_handle`（幂等 re-provision 自愈，依赖 interrupt.id 跨 rehydrate 稳定，已核实 1.2.9 成立）。**时机 = 真飞书常驻服务上线前必做**。
- **C reopen 预算 / blocked 终态（MEDIUM）**：自动化（tool/llm）门禁若持续失败，回边在单次 invoke 内累积 super-step 撞 recursion_limit（单设上限只是推迟）。修：每门禁 reopen 计数，超预算降级 `blocked` 终态。**时机 = seg-2 回填真 ci_test（自动化门禁）时**。
