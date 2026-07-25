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

## 2026-07-24 · 交付物产出协议实测（lark-cli 写能力 = 统一飞书文档方案的命门）
「交付物统一成飞书文档 handle」整套设计压在 lark-cli 能不能写飞书。在测试组织实测 markdown 闭环：
- `+create` → 稳定 file_token + 可打开 URL；`+fetch` 正文完整回来（下游 llm 可消费）；`+overwrite` 重跑后 **file_token 不变、内容与 version 更新**；`drive +version-history` 自动留两版（tag 1/2 + editor）。
- **关键结论**：handle 跨 overwrite 稳定 = 选择性重算「旁支复用旧产出」的实证基础（没重跑的节点 handle 不变、读到旧内容；重跑的 overwrite 后 handle 不变、读到新内容）。版本靠飞书原生、引擎不自建。
- **避免**：真写 produce/consume executor 前必须先 `lark-cli skills read lark-doc/lark-markdown/lark-drive`，不靠 `--help` 猜 flag（CLI 明确要求，版本匹配的内嵌 skill 讲 block/selector/格式）。
- 待验：`drive +upload`（二进制 / 视频终态交付物）；`docs` docx 协同 + block_id 跨 update 稳定性（v2 共享协同拓扑）；`im` 卡片 + `event` 定稿信号（human 节点闭环）。

## 2026-07-24 · 受控活图实测：update_state 会让挂起中断换 id（已复现）
写 `edit_graph` 前先量了一把（现象太关键，不敢按直觉写）。**事实**：线程挂在 interrupt 时调 `graph.update_state(...)`，下一次 `invoke(None)` 后 human 节点重新 interrupt，**中断 id 必变**；实测 `as_node=None` / `as_node="dispatch"` / 值没变的空更新 / `{}` 四种情形**一律换 id**（根因：update_state 落新 checkpoint → 任务 id 重算，非我们用法不对）。
- **影响**：卡片 `action_value` 里嵌的是旧 interrupt id，改一次图就把所有在等的人手里的卡点废（点了没反应，被判 stale）。这恰好砸在 v1.0 win 要演的「运行中改图」上。
- **修**：`edit_graph` 前后按 `node_id` 对齐挂起中断，把 old→new 记进 `interrupt_remap` 表（`correlations.py`），`resume` 先顺迁移链重绑；同时**跳过重复派单**（卡 / 任务还在人手里）。只记「改图导致的迁移」，**打回产生的新中断不进表**（打回本就该出新单、旧卡该失效，这是 seg-1 的幂等设计）。
- **另一条**：status 里从没有 `running`（worker 不写），故「不删在跑节点」不能只看 status。`edit_graph` 把「有挂起中断的节点」并进冻结线当 running（tool/llm 在单个 super-step 内跑完，且与 invoke 同锁，不会长时间在飞）。
- **避免**：任何「改 state 后旧外部对象还要能回调」的地方，别假设 LangGraph 的 interrupt id 稳定；先量。

## 2026-07-25 · 通用性对抗 review：三条实测出来的「合同焊死」与两条并行缺陷
6 维度 62 agent 对抗 review 后逐条自己复现。**全部结论都来自我亲手跑出来的输出，不是推理。**

**焊死处（换业务场景就跑不了）**
- 护栏①「三型齐全」被实现成硬校验：招聘接力（全 human）报 `缺少 executor {'llm','tool'}`、视频脚本（llm+human）报 `缺少 executor {'tool'}`，一行代码都跑不到。为凑三型补一个 tool 节点，又必然报 `tool 节点缺 handler`。**「不存在只加 yaml 就能跑的业务图」**，ADR-022 的生成主路径在 as-built 下不可能成立。
- 跨模板 node id 撞名静默跑错业务：把那个 tool 节点改名叫 `close`，用合同装配的 service 起 —— **放行**，一张视频脚本图挂上了合同的收口逻辑，不报错不告警。
- 打回意见结构性进不了重算：写入落 `outputs[gate]["comment"]`，全仓无读取方。实测两次 writer prompt **逐字节相同**，真 LLM temperature=0 会一字不差再生成同一份稿。**打回是空转。**

**并行缺陷（合同 e2e 因为点击顺序刚好合适而全测不出来）**
- 财务先打回、法务还没点 → `status={finance_gate: failed}`、起草次数不变、卡片不变，**商务方零通知**，直到那个不相干的人碰巧响应。
- 上述窗口内做一次合法改图 → `outputs['finance_gate']` 变 `None`、`failed` 消失，**人的裁决连同意见被静默吞掉**，旧卡还被 remap 成有效。根因：`update_state` 落新 checkpoint 时，在飞 super-step 里已完成任务的写入尚未提交。
- 更根本：**super-step 是屏障**。构造 `A(human) 独立 / B→C→D→E` 后实测「B done，C/D/E 一个都没跑」，全卡在毫不相干的签字上。
- 修法与理由见 ADR-028（保值写回 + `as_node=<worker>` 借位让 dispatch 真跑）、ADR-029（打回预算 + `blocked`，auto 门反复不过会一路撞 recursion limit，实测炸过）。

**避免**：给一个通用引擎写 e2e 时，别只按「顺手的顺序」点。**并行分支的应答顺序本身就是测试维度**；顺序一换就坏的东西，在真实多方协作里天天发生。
