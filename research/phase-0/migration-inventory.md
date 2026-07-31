# Legacy 原型迁移清单

> 状态：2026-08-01 按简化范围重新收敛。本文只说明 legacy 资产如何进入目标架构，不把旧行为升级为目标契约。

目标是区分可以迁移的机制、必须隔离的框架假设和需要按新领域模型重写的能力。目标架构以 `AIREADME/ARCHITECTURE.md` 为准。

## 分类

- Preserve：概念和测试价值可以保留，必要时更换存储或接口。
- Extract：从混合模块提炼纯逻辑或适配器，不能整体搬迁。
- Replace：按 PostgreSQL 领域模型重写。
- Isolate：冻结为 legacy 回归路径，不继续承载新产品语义。

## 模块清单

| Area | 当前价值 | 处理 | Phase 1 动作 | 红线 |
|---|---|---|---|---|
| `io/cli.py` | lark-cli 调用、错误和超时边界 | Preserve | 保留为中央 Feishu Adapter | 不引入个人 Agent Edge 身份 |
| `io/lark_io.py` | Task、消息、卡片投影与 Mock 接口 | Extract | 提炼 Projection Port | 飞书对象不决定业务状态或权限 |
| `io/deliverable.py` | 文档 handle、幂等创建和测试替身 | Preserve | 纳入 Projection 与 Attempt | 不把文档 token 当业务真相 |
| `io/correlations.py` | 外部对象关联和幂等键 | Extract | 迁入 PostgreSQL Projection、Inbox、Outbox | SQLite 关联表不能成为第二真相源 |
| `io/events.py` | 事件消费、异常隔离和进程清理 | Preserve | 作为投影事件适配器 | 事件必须重做 actor、状态和 revision 校验 |
| `serve.py` | 启动对账、事件泵和优雅退出 | Extract | 替换实例枚举和恢复来源 | 不再从 LangGraph checkpointer 枚举业务实例 |
| `engine/permissions.py` | 客户端身份不可信的纯函数经验 | Extract | 改为 Person、Owner、Instance 权限 | 不沿用旧字符串角色作为目标授权模型 |
| `engine/gates.py` | ready、验收、Attempt 和重开算法 | Extract | 迁入领域服务 | 重做产生新 Attempt，图保持无环 |
| `engine/livegraph.py` | 只改未开始区域和编辑审计经验 | Extract | 重写为预览、确认、revision 协议 | 不允许单步直接改权威图 |
| `model/template.py` | DAG 校验和祖先算法 | Replace | 建 TemplateVersion 与 Instance Snapshot；复用纯图算法 | legacy YAML 不得静默标记为 v0.2 |
| `model/node.py` | executor、role 和遍历助手 | Extract | 替换为 Owner 与 executor 分离模型 | Agent 和 Tool 不能成为 Owner |
| `engine/orchestrator.py` | LangGraph 全图机制经验 | Isolate | Phase 1 使用独立 Scheduler | LangGraph 只用于单个复杂 Agent NodeRun |
| `engine/state.py` | checkpointer channel | Replace | PostgreSQL Instance、Node、Attempt、Audit | 不建立新的全局 Agent state 真相源 |
| `service.py` | 状态、投影、权限、恢复混合驱动 | Extract | 拆出领域、投影、授权和审计模块 | 不整体移植或继续加入新领域语义 |
| `store.py` | SQLite、WAL、flock 和单机锁 | Isolate | SQLite 只留 legacy 与测试 | 目标业务真相使用 PostgreSQL |
| `app.py` | 当前真实服务装配 | Replace | 新建模块化单体装配，旧 factory 独立保留 | 测试不得构造真实飞书服务 |
| `config.py` | 明确失败和 LLM 配置经验 | Extract | 保留配置边界，责任改为 Person 绑定 | Owner 歧义必须阻止启动 |
| `llm/client.py` | 路由、超时、故障切换和可观测性 | Preserve | 放到 Agent Node Runner 之后 | SDK 与业务层不能叠加重试 |
| `engine/executors.py`、`engine/tools.py` | Human、LLM、Tool 适配经验 | Extract | 统一为 Human、Agent、Tool Node Runner | 执行结果不能声明权限或改图 |
| `templates/*.yaml` | 三个 legacy compact 示例 | Isolate | 编写显式 importer 和不兼容报告 | 不作为 v0.2 合规模板 |

## 优先迁移的测试资产

- 飞书 payload 解析、卡片落定、Task 轮询和 stale task 关闭。
- 外部写幂等、事件重复、事件乱序和投影失败恢复。
- 权限纯函数、越权拒绝、重启影响范围和审计记录。
- LLM 超时、故障切换、代理隔离和可观测性。
- 优雅退出、事件泵重启和进程树清理。

这些测试应该改为针对新 Port 或领域服务，不依赖 LangGraph checkpointer。

## 仅作 legacy 回归

- `StateGraph` super-step、`Send`、interrupt/resume 和 reducer 行为。
- SQLite 多进程锁、checkpointer 实例枚举和全局 state 合并。
- 旧模板字段、旧 CLI 实例状态和运行中直接修改 legacy DAG。

它们保证原型不回退，但不能作为目标产品验收。

## Phase 1 最小切片

1. PostgreSQL 建立 Template、TemplateVersion、Instance、NodeInstance、Attempt、Projection、Audit 和 Outbox。
2. 从启用模板或结构化无模板定义创建 `draft`，不产生外部副作用。
3. 确认启动前把所有节点解析到唯一人类 Owner，失败则保持草稿。
4. Scheduler 解锁节点；Human 等待 Owner，Agent 与 Tool 走中央 Node Runner。
5. Projection Worker 为每个节点创建飞书责任入口，并记录稳定幂等键。
6. 服务重启后从 PostgreSQL 恢复，缺失投影可以对账重建。

明确不进入该切片：子 DAG、个人 Agent Edge、Capability Lease、RAG、复杂 ACL、五维评分、Kafka 和高级图形化编辑。

## 启动实现前的门

- 简化 MVP 的每项能力都有明确产品理由和可判定验收。
- CORE、PRD、DAG Contract、ARCHITECTURE、ROADMAP、README 和 pyproject.toml 口径一致。
- 已选择一个不依赖外部访谈也能离线验收的技术纵切。
- legacy 与 Target 测试套件有清晰命名和运行边界。
- 文档明确声明市场、频率、收益和付费意愿仍未验证。
