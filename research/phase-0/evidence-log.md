# Phase 0 证据台账

> 状态：Deferred 空白基线。当前没有条件采集访谈、历史材料或对照实验；本台账不构成当前 Phase 0 工程门。

## 数据卫生

- 企业、人员和流程使用匿名编号。
- 不粘贴飞书 `open_id`、群 ID、文档 token、密钥、合同正文或其他敏感内容。
- 原始材料由提供方保管。本仓库只记录脱敏计数、结构和证据类型。
- 口述估计标记为 `estimate`，系统或材料计数标记为 `observed`。
- 没有证据时写 `not collected`，不按 0 处理。

## 招募与访谈

| Enterprise | Participant | Role | Qualified | Interview date | Historical artifact | Follow-up commitment |
|---|---|---|:--:|---|---|---|
| E01 | P01 | not collected |  |  |  |  |
| E02 | P02 | not collected |  |  |  |  |
| E03 | P03 | not collected |  |  |  |  |
| E04 | P04 | not collected |  |  |  |  |
| E05 | P05 | not collected |  |  |  |  |

## 候选流程池

每个流程必须来自一个已发生实例。`Frequency source` 记录数字来自系统计数、材料计数或口述估计。

| Process | Enterprise | Last instance | Monthly frequency | Frequency source | Human owners | Acceptance point | Follow-ups | Sync minutes | Baseline eligible |
|---|---|---|---:|---|---:|---|---:|---:|:--:|
| F01 |  |  |  | not collected |  |  |  |  |  |
| F02 |  |  |  | not collected |  |  |  |  |  |
| F03 |  |  |  | not collected |  |  |  |  |  |
| F04 |  |  |  | not collected |  |  |  |  |  |
| F05 |  |  |  | not collected |  |  |  |  |  |
| F06 |  |  |  | not collected |  |  |  |  |  |
| F07 |  |  |  | not collected |  |  |  |  |  |
| F08 |  |  |  | not collected |  |  |  |  |  |
| F09 |  |  |  | not collected |  |  |  |  |  |
| F10 |  |  |  | not collected |  |  |  |  |  |

## 三级映射

层级表示责任边界，不表示工具步骤。`Needs L4` 只有在第四层存在独立责任人、权限边界和验收契约时才为 yes。

| Process | L1 responsibility | L2 responsibility | L3 responsibility | Needs L4 | Parent summary sufficient | Mapping note |
|---|---|---|---|:--:|:--:|---|
| F01 |  |  |  |  |  |  |
| F02 |  |  |  |  |  |  |
| F03 |  |  |  |  |  |  |
| F04 |  |  |  |  |  |  |
| F05 |  |  |  |  |  |  |
| F06 |  |  |  |  |  |  |
| F07 |  |  |  |  |  |  |
| F08 |  |  |  |  |  |  |
| F09 |  |  |  |  |  |  |
| F10 |  |  |  |  |  |  |

## Beachhead 评分

每项 0 至 2 分，总分至少 9 才可进入对照实验。任何硬性淘汰项都优先于总分。

| 维度 | 0 | 1 | 2 |
|---|---|---|---|
| 月频 | 少于 4 | 4 至 9 | 至少 10 |
| 责任边界 | 单人或同一角色 | 两人但边界弱 | 至少两名独立责任人 |
| 验收 | 无明确完成判据 | 有口头判据 | 有明确验收人和证据 |
| 协调成本 | 无可见成本 | 有痛点但不可量化 | 催办或同步可量化 |
| 飞书原生不足 | 原生方案已足够 | 需大量人工配置 | 多层契约或返工明显缺口 |
| Owner 投入 | 无后续动作 | 只约访谈 | 提供材料并排期共创 |

硬性淘汰：涉及不可脱敏的高敏数据、没有流程 Owner、没有下一次真实运行时间，或单层飞书审批已经低成本解决。

| Process | Frequency | Boundaries | Acceptance | Coordination | Native gap | Owner | Total | Hard reject | Selected |
|---|---:|---:|---:|---:|---:|---:|---:|:--:|:--:|
|  |  |  |  |  |  |  |  |  |  |

## 对照实验汇总

详细事件数据按 [feishu-baseline-protocol.md](feishu-baseline-protocol.md) 记录。

| Process | Primary metric | Baseline median | Concierge median | Change | Guardrails pass | H2 result |
|---|---|---:|---:|---:|:--:|---|
|  |  |  |  |  |  | not collected |

## 模板复用

| Template | Owner | First assisted run | Independent second run | Days elapsed | Material change needed | H3 result |
|---|---|---|---|---:|---|---|
|  |  |  |  |  |  | not collected |

## 假设判定

| Hypothesis | Required evidence | Current evidence | Status | Decision date |
|---|---|---|---|---|
| H1 高频流程存在 | 3 of 5 qualified enterprises | not collected | open |  |
| H2 协调收益至少 30% | 3 baseline and 3 concierge runs | not collected | open |  |
| H3 Owner 独立复用 | 2 independent second runs | not collected | open |  |
| H4 三级覆盖至少 80% | 8 of 10 real process mappings | not collected | open |  |
| H5 个人 Agent 选择至少 30% | eligible work package choices | deferred | deferred |  |

状态只能使用 `open`、`passed`、`failed`、`inconclusive`、`deferred`。每次改变状态必须在下方追加记录。

## 决策记录，append-only

| Date | Evidence added | Hypothesis impact | Decision | Owner |
|---|---|---|---|---|
| 2026-08-01 | 验证协议建档，无外部数据 | none | 保持 Phase 0，不进入 Phase 1 | Maxwell |
| 2026-08-01 | 用户确认当前无访谈条件，仍无外部数据 | H1 至 H5 保持未验证 | 研究协议转为 Deferred；工程改走既有设计简化与一致性核验 | Maxwell |
