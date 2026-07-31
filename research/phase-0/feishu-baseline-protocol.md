# 飞书原生基线与 concierge 对照协议

> 目的：用同一个真实流程比较当前飞书原生做法和 larkflow 协作模型。该实验验证增量价值，不验证现有 LangGraph 原型。

## 进入条件

候选流程必须同时满足：

- 最近 30 天实际发生过，预估月频至少 10 次。
- 至少两个独立人类责任人和一个明确验收点。
- 流程 Owner 能提供最近 3 次实例的脱敏事件或可靠计数。
- 下一次真实实例已有预计发生时间。
- 不需要把敏感原文、人员 ID 或凭证写入仓库。

## 比较单位

- Baseline：同一流程最近 3 次使用现有飞书 Task、审批、文档、群聊或 Base 的实例。
- Contender：后续 3 次真实实例，由研究者人工扮演尚未实现的 larkflow 控制面。
- 流程范围、责任角色、验收标准和主要交付物在实验开始前冻结。
- 若出现重大范围变化，该实例单列，不混入主比较。

样本很小，因此只用于决定是否值得进入 Phase 1，不用于宣称统计显著性或普遍市场效果。

## Contender 操作模型

不写新产品代码。研究者使用现有飞书原语人工执行以下逻辑：

1. 从一份经 Owner 确认的流程模板创建工作包。
2. 每个工作包绑定唯一真实责任人、输入、输出、截止时间和验收人。
3. 责任人只看到与自己相关的入口和材料。
4. 子流程只向父层汇报 Contract Summary、阻塞、交付物和验收状态。
5. 打回创建新的 Attempt 记录，不删除历史交付和验收。
6. 研究者负责提醒、聚合和审计记录，但不得代替责任人作答。

这是一项 concierge 实验。人工服务时间必须单独记录，避免把不可扩展的人力隐藏成产品效率。

## 预注册指标

每个试点在看结果前选择一个主指标：

- `follow_up_count`：为获取状态或推动下一步而发生的人工催办次数。
- `coordination_minutes`：不直接产出交付物的状态同步、找人、催办和拼接信息的人分钟。

不能在实验结束后从两个指标中挑表现更好的一个。另一个作为次要指标保留。

共同护栏：

- `cycle_time_hours`：从正式发起到最终验收的小时数。
- `rework_attempts`：被拒收后产生的新 Attempt 数。
- `orphaned_packages`：没有唯一责任人或无人可处理的工作包数。
- `unauthorized_actions`：未经服务端或实验主持人按预设规则授权的状态变更数。
- `acceptance_quality`：由同一验收标准判定的通过、拒收和缺陷数。
- `concierge_minutes`：研究者每个实例投入的人分钟。

## 事件记录

| Field | Definition |
|---|---|
| process_id | 匿名流程编号 |
| run_id | B01 至 B03 或 C01 至 C03 |
| event_at | Asia/Shanghai 时间戳 |
| event_type | created、assigned、acknowledged、blocked、follow_up、submitted、accepted、rejected、reassigned |
| actor_role | 脱敏角色，不记录人员 ID |
| work_package | 脱敏工作包编号 |
| attempt | 从 1 开始的轮次 |
| minutes | 只对人工协调事件记录 |
| source | observed、artifact 或 estimate |
| note | 不含敏感正文的事实说明 |

建议在单独的受控工作表记录事件，仓库只保存聚合后的脱敏数字。

## 计算

```text
coordination_minutes(run) = sum(minutes where event is coordination)
follow_up_count(run) = count(event_type = follow_up)
cycle_time_hours(run) = accepted_at - created_at
relative_change = (contender_median - baseline_median) / baseline_median
improvement = -relative_change
```

基线主指标为 0 时，该流程不能使用相对下降比例。改用预先约定的绝对门槛，或选择另一个有非零基线的流程。

## 通过门槛

H2 通过必须同时满足：

1. 主指标的中位数改善至少 30%。
2. 周期时间中位数退化不超过 10%，除非质量护栏有预先说明的显著改善。
3. `orphaned_packages = 0`。
4. `unauthorized_actions = 0`。
5. 验收标准没有被放宽。
6. Owner 明确确认降低的是实际协调负担，并提供第二次独立启动的具体时间。

`concierge_minutes` 不计入用户协调收益，但必须报告。如果每次运行仍需要大量研究者操作，结论只能是需求存在，不能直接证明产品可扩展。

## 执行步骤

1. 与 Owner 锁定流程边界和验收标准。
2. 收集最近 3 次 baseline，标记 observed 与 estimate。
3. 选择并冻结主指标。
4. 将真实流程映射为最多三级的工作包和 Work Contract。
5. 用飞书原语人工运行 3 次 contender。
6. 每次结束后 24 小时内补齐事件和 concierge 时间。
7. 计算中位数与护栏，不删除异常实例。
8. 在 [evidence-log.md](evidence-log.md) 追加 H2 判定和原因。

## 需要控制的偏差

- 学习效应：后续实例天然可能更快，记录流程参与人的熟悉程度。
- 实例难度：记录输入规模和异常事件，不把明显更简单的实例混为同质样本。
- 研究者效应：研究者催得更勤可能缩短周期，却增加协调成本，必须计入 concierge 时间。
- 指标挑选：主指标必须预注册。
- 质量变化：不能通过少做审核或降低验收标准制造效率。
