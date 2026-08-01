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
- 跨模板 node id 撞名静默跑错业务：把那个 tool 节点改名叫 `close`，用合同装配的 service 起，**照样放行**，一张视频脚本图挂上了合同的收口逻辑，不报错不告警。
- 打回意见结构性进不了重算：写入落 `outputs[gate]["comment"]`，全仓无读取方。实测两次 writer prompt **逐字节相同**，真 LLM temperature=0 会一字不差再生成同一份稿。**打回是空转。**

**并行缺陷（合同 e2e 因为点击顺序刚好合适而全测不出来）**
- 财务先打回、法务还没点 → `status={finance_gate: failed}`、起草次数不变、卡片不变，**商务方零通知**，直到那个不相干的人碰巧响应。
- 上述窗口内做一次合法改图 → `outputs['finance_gate']` 变 `None`、`failed` 消失，**人的裁决连同意见被静默吞掉**，旧卡还被 remap 成有效。根因：`update_state` 落新 checkpoint 时，在飞 super-step 里已完成任务的写入尚未提交。
- 更根本：**super-step 是屏障**。构造 `A(human) 独立 / B→C→D→E` 后实测「B done，C/D/E 一个都没跑」，全卡在毫不相干的签字上。
- 修法与理由见 ADR-028（保值写回 + `as_node=<worker>` 借位让 dispatch 真跑）、ADR-029（打回预算 + `blocked`，auto 门反复不过会一路撞 recursion limit，实测炸过）。

**避免**：给一个通用引擎写 e2e 时，别只按「顺手的顺序」点。**并行分支的应答顺序本身就是测试维度**；顺序一换就坏的东西，在真实多方协作里天天发生。

## 2026-07-25 · 第二轮对抗验证：上一轮修复自己带进来的 4 条回归（全部实测复现）
拿 5 路 agent **攻击刚落地的修复**（不是再审老代码），25 条攻击性发现证伪后剩 18 条，主干 4 条我逐条复现并修掉。

- **审计记录被伪造（critical）**：两道门共享同一上游时，A 打回会把还在等的 B 一起卷进新一轮。`_remap_interrupts` 只按 node_id 对号入座，把 B 的新中断误判成「改图迁移」→ B 收不到新卡，而 B 手里上一轮的旧卡被重绑到新一轮，一点就把「对 v1 的裁决」写成「放行 v2」。实测输出：法务从未见过 v2，`status['g2']='done'`。修：`before` 快照只收「已答复之外、且这一拍未被打回重置」的中断。
- **一次非法 reopen 永久砖化实例（critical）**：`if reopen and node_id` 依赖**回传的** node_id，而卡片封套是前端可自由构造的，少一个字段就绕过合法域校验；非法值落进权威 state 后，此后每次 resume / reconcile / edit_graph 都在 `reopen_resets` 同一处抛，`pending()` 还返回 `[]`（驾驶舱显示无人等待）。修：gate 身份改从**中断本身**取；`reopen_resets` 不再抛，剔除非法项、全非法标 blocked。
- **重复派单无上限（high）**：幂等键含 interrupt id，而中断 id 每推进一拍就换。实测法务从头到尾没被叫过第二次却拿到两张卡；`reconcile()` 号称幂等，第一次就重发。修：新增 `attempts` 累加 channel，幂等键改用**轮次**。
- **预算与递归上界互不感知（high）**：`reopen_budget` 配到 6 以上就退回 ADR-029 要消灭的 GraphRecursionError；有屏障时则静默停在 failed 半截态、零通知。修：两个上界都算进打回预算，耗尽时记错误 + 通知。

**教训（比 bug 本身值钱）**
- 「修完一轮就拿 agent 攻击这一轮的修复」值得固定下来：这 4 条没有一条是原 review 找出来的，全是**新代码自己带进来的**。
- 变异测试是判断「测试有没有用」的唯一硬办法。第一次做的时候我用 `git checkout --` 还原变异，把当轮未提交的修复一起冲了（已补进 pitfalls）。**先提交再变异**。
- 覆盖不到的交互会让变异存活：`reopen_counts` 那条一开始没被抓住，因为测试图里没有人工节点、压根不会触发推进拍。补了「旁支挂一个永不应答的人」后才真正覆盖。

## 2026-07-30 · 产品意图与实现偏移审计

用 pm-skills 的 product-strategy、create-prd、intended-vs-implemented 三套框架，重新核对讨论结论、AIREADME 与代码。

**产品结论**

- larkflow 不替代飞书，复用其 IM、Task、Docs、Drive 和 Directory。
- 护城河是可治理模板、跨人/部门 DAG、三级父子工作契约和个人 Agent 协作，不是 Agent 创建待办。
- 待办属于真实人员；电脑离线不影响责任和流程状态。
- 企业入驻必须渐进授权、候选发现、人工校准，不能承诺一次性学习全部知识。
- 产品 DAG 与 LangGraph 解耦；中央业务数据库是目标真相源。

**实现证据**

- `app.py` 仍构造 SQLite `SqliteSaver` 和中央 lark-cli/LLM。
- `service.py` 明确把 checkpointer 当权威。
- `model/template.py` 只加载 `nodes`，没有 Template v0.1 顶层元数据、版本和权限。
- `config.py` 只从静态 env 把角色映射到 `open_id`。
- 代码中没有 tenant、child instance、设备注册、Capability/Skill/MCP 领域模型。

**避免**

以后每份文档必须标 Target 或 As-built。不能因为某个机制原型真栈跑通，就把它写成目标产品架构；也不能因为目标 PRD 已定，就在 SPEC 里暗示已经实现。

## 2026-08-01 · 应用角色直接 pg_restore 不会可靠恢复 public schema ACL

- 现象（已复现）：以 `lf_target_dev` 把 custom-format 备份恢复到新库时，业务表、migration 和触发器都成功，但 pg_restore 只对 `public` schema ACL 输出 warning 并以 0 退出；回读发现 PUBLIC 仍有 CREATE 权限。只看退出码会把权限回退当成恢复成功。
- 根因（已复现）：目标库的 `public` schema 归管理员所有，应用角色不能撤销其默认 ACL。dump 含 ACL 不等于应用角色有权在新库重放 ACL。
- 结论：恢复前由 postgres 管理员创建数据库并预置 schema ACL，再以应用角色执行 `pg_restore --no-acl`。恢复验收必须回读 migration、表所有者、PUBLIC CREATE=false 和应用角色 CREATE=true，不能只看 pg_restore 退出码或表数量。

## 2026-08-01 · 故障注入必须证明 kill 发生在 claim 尚未完成时

- 现象（已复现）：第一次 recovery 演练在状态回读时看到节点 running，但下一条 SSH 命令真正送达 SIGKILL 前，20 秒内的执行已经完成。systemd 确实换了 PID，但日志是 `recovered=0`；若只看自动拉起，会把空闲进程重启误报成 Attempt 恢复成功。
- 根因（已复现）：状态采样、工具往返和 kill 之间存在时间窗；“曾经 running”不等于“kill 时仍 running”。
- 结论：恢复验收必须同时保留 kill 前当前 claim、被杀 PID、重启后不同 Worker、相同 Attempt ID、递增节点版本、`node.claim_recovered` 审计和最终 `recovered=1`。缺任一项都只能证明部分链路，不能声称崩溃恢复通过。

## 2026-08-01 · 共享 outbox 的消费者必须在认领时过滤事件类型

- 现象（设计复核）：Projection Worker 若调用无类型过滤的通用 `claim_outbox`，当前只有投影事件时看不出问题；未来加入通知或 webhook 事件后，它会先认领不属于自己的事件，再永久失败重试。
- 根因（已确认）：消费者边界只写在 `_project()` 的处理分支，数据库认领没有表达所属事件集合。处理后才拒绝已经太晚，租约和失败状态已经被错误消费者改写。
- 结论：事件所有权必须进入 `FOR UPDATE SKIP LOCKED` 的选取条件。Projection 只认领两类节点投影事件，未知事件保持原状态，留给所属消费者。

## 2026-08-01 · Linux lark-cli 凭据不能通过复制 master key 来共享

- 现象（已确认）：为新服务用户复制 `config.json`、加密 app secret 与 `master.key` 虽然能快速复用测试应用，但等于把可解密凭据完整复制给另一个身份，扩大访问边界。
- 根因（已确认）：Linux 的 lark-cli keychain 实际是本地密文文件加同目录主密钥，不具备 OS 钥匙串的不可导出隔离。
- 结论：当前开发 Projection 以已有凭据所有者 `lf-dev` 运行，只给该身份最小数据库权限，并通过单用户 ACL 复用 Target venv。生产部署应使用独立中央 adapter 身份和独立凭据生命周期，不能把该过渡拓扑当成目标形态。

## 2026-08-01 · Task V2 事件不能直接证明完成人

- 现象（已确认）：`task.task.update_user_access_v2` 的完成事件只给 event ID、Task GUID 与 event types，没有可作为 Target actor 的完成人。普通 `mode=2` 任务的详情也不给出可稳定校验的单个 assignee 完成关系。
- 根因（已确认）：事件信封表示「某任务发生了完成变化」，不是领域授权证明；`mode=2` 是任一人完成的任务语义，不匹配「唯一人类 Owner」不变量。
- 结论：Target Human Task 固定创建为 `mode=1` 且只有唯一 Owner assignee。入站必须先按 GUID 在服务端读取 Task 详情，再同时校验 source、绑定、assignee 与 completed assignee。事件 payload 只是触发信号，不直接作为 actor。

## 2026-08-01 · LLM 路由总预算必须小于节点 claim 租期

- 现象（设计复核）：Node claim 默认 300 秒，LLM 单线路默认 timeout 也曾是 300 秒；若再配置备用线路，最坏等待时间是各线路 timeout 之和。即使生成正常结束，结果提交时也可能已经超过租期，被服务端按迟到结果拒绝。
- 根因（已确认）：claim 约束的是完整外部执行窗口，不是某一条 HTTP 请求。主备线路由应用层顺序调用，SDK 内层重试若再开启还会继续放大预算。
- 结论：Target Agent 启动时计算每个角色完整路由链的 timeout 总和，取最大值并加安全余量，必须严格小于 claim TTL。SDK 重试保持为零，业务重试只能在 Attempt 语义中单独设计。

## 2026-08-01 · 服务器管理权限不自动包含跨服务复制 API key

- 现象（已确认）：Target Runtime 与 legacy 服务使用不同 OS 身份。把 legacy 环境里的 `LLM_API_KEY` 复制到 Target env，会让 `lf_target_dev` 新增读取和使用该凭证的能力，即使两个服务在同一台完全受控服务器上也是权限扩大。
- 根因（已确认）：对机器和部署的授权描述了可操作范围，不等于凭证 Owner 已针对新的服务身份授权复用具体 secret。
- 结论：优先为 Target 创建专属 key。确需复用时，先说明新增可读身份、用途和回滚方式并取得明确授权；未授权前可以准备 wheel 与回滚备份，但不能复制 secret 或启用真实调用。

## 2026-08-01 · Draft 中的依赖定义不能早于 NodeInstance 物化

- 现象（已复现）：带依赖的多节点 Snapshot 在 `create_draft` 时触发 PostgreSQL Dependency 外键错误。单节点真实集成测试全部通过，直到第一条真实 Human-Agent-Human 草稿才暴露；事务完整回滚，没有半成品实例。
- 根因（已确认）：Draft 只保存不可变 Snapshot，还没有 NodeInstance；仓储仍遍历 Snapshot 并尝试写 Dependency。Dependency 外键要求当前节点和依赖节点都已物化。
- 结论：Draft 持久化阶段只写 Instance Snapshot。确认事务先物化全部 NodeInstance 与 Attempt，再写 Dependency；真实 PostgreSQL 集成测试必须至少包含一条有依赖的多节点草稿，不能只测单节点往返。

## 2026-08-01 · Human 完成的业务结果不能复制外部任务元数据

- 现象（真实链路）：首条 Human 完成后，Agent prompt 收到了 Task GUID 与完成时间。模型把这些技术字段当成业务依据写进摘要，下游任务因此泄露无关实现细节。
- 根因（已确认）：入站 Worker 把用于验证和审计的外部任务元数据同时当成 Human 节点结果。Projection、Inbox 和关联记录本已保存这些事实，下游业务依赖不需要再携带一份。
- 结论：当前 Task 完成语义只提交 `{confirmed: true}`。外部标识、时间与事件状态留在边界记录；以后新增有内容的 Human 提交时，应定义独立业务 schema，不能复用验证报文。

## 2026-08-01 · 文本 Agent 边界必须归一化常见结构包装

- 现象（真实模型）：节点要求纯正文，模型仍返回包含 `id / type / text` 的 JSON 数组。状态正确，但下游 Human Task 会直接显示 JSON 外壳。
- 根因（已确认）：prompt 中的输出 schema 会诱导部分兼容模型结构化回答，仅靠自然语言要求不能形成稳定适配契约。
- 结论：文本 Agent adapter 在结果边界提取常见对象、数组和整段 JSON 代码块中的 `content / text`，无法识别的 JSON 保留原文而不猜测；归一化后再执行非空与长度校验。

## 2026-08-01 · Task 完成变化事件不能替代当前状态读回

- 现象（真实链路）：一条 `task_completed_update` 进入 Inbox 后，Task 详情仍连续返回 `todo` 且没有已完成 assignee。凭据侧重试 9 次均拒绝；后续状态可验证的新事件被正常处理，实例只推进一次。
- 根因（边界确认）：事件说明完成关系发生过变化，不证明读取时仍满足完成、唯一 Owner 与当前 Attempt 条件。
- 结论：事件只负责唤醒，不能直接提交 Human 节点。失败事件保留并有界退避；只有服务端详情同时满足绑定、`mode=1`、唯一 assignee、完成状态和完成人时，才写入 verified payload。

## 2026-08-01 · wheel 发布件用目录版本化，不能改 wheel 基名

- 现象（已复现）：把 `larkflow-0.0.1-py3-none-any.whl` 重命名为带短哈希的 `larkflow-0.0.1-7d262a55.whl` 后，pip 在读取内容前直接拒绝，报文件名不是合法 wheel。
- 根因（已确认）：wheel 基名必须符合标准的 distribution、version、build tag、Python tag、ABI tag 和 platform tag 结构，任意插入短哈希会被解析器当成非法标签。
- 结论：发布件按 `releases/<短哈希>/larkflow-0.0.1-py3-none-any.whl` 保存，目录表达构建身份，wheel 基名保持标准格式。部署前后都回读完整 SHA-256；回滚件遵循同一规则。
