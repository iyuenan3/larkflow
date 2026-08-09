# larkflow · 飞流

> 飞书原生的企业协作 DAG 系统。它把多人流程拆成有依赖、有唯一责任人、可验收、可返工和可追溯的节点。

## 项目状态

当前处于开发试用阶段，不是生产就绪版本。

- Target 中央工作流已具备 Human、Agent、Tool 节点编排，PostgreSQL 持久化，飞书投影和追加型审计。
- 员工工作台支持本人流程、普通 Human 待办、草稿确认、受控流程操作和 DAG 画板。
- Personal Agent Edge 仍是受限 Proof，只允许已批准的合成或公开材料，不适合正式员工分发。
- 早期 LangGraph、SQLite 与 lark-cli 原型继续保留，用于回归已经验证的适配器和事件处理机制。

当前能力、验收证据和剩余门槛以 [AIREADME](AIREADME/INDEX.md) 为准，README 不记录部署流水、提交清单或单次验收明细。

## 核心能力

- 草稿先行：模板、结构化定义或自然语言请求都先生成草稿，只有人类明确确认后才启动。
- 唯一责任人：每个 Human 节点绑定唯一 Owner，Agent 只能执行，不能成为组织责任主体。
- 依赖调度：中央 Scheduler 按 DAG 依赖解锁 Human、Agent 和 Tool 节点。
- 安全变化：节点返工、完整实例重启和未来区域编辑都先生成影响预览，再由服务端重新校验并确认。
- 飞书协作：Human Task、决定卡、Agent 或 Tool 结果、完成文档和通知通过飞书投影，并可从中央状态对账重建。
- 历史保护：Attempt、结果、质量判断、责任转交和图变更保留追加型历史，不用覆盖旧记录换取“重来”。
- 员工工作台：Owner 查看本人流程和审计，参与者处理分配给自己的任务，管理员只获得显式允许的聚合和会话治理能力。

## 工作方式

1. 用户创建流程草稿并核对节点、依赖、责任人和验收条件。
2. 用户确认启动，中央节点冻结本次 Instance Snapshot。
3. Scheduler 按依赖推进节点，飞书承担人类责任入口，Agent 和 Tool 由受控执行器处理。
4. 失败、退回或需求变化通过新 Attempt、重启或未来区域编辑处理，旧历史继续保留。
5. PostgreSQL 保存业务权威状态、revision、投影记录和审计，外部对象可以幂等修复。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

pytest -q
python -m larkflow.demo --auto
```

运行单个测试模块：

```bash
pytest -q tests/test_workflow_kernel.py
```

离线测试使用 Mock Lark I/O、Stub LLM 和内存 SQLite，不访问真实飞书。PostgreSQL 集成测试只能指向可销毁数据库，并通过 `LARKFLOW_TEST_POSTGRES_DSN` 显式启用。

Target、Console、飞书和 Edge 的真实运行需要受控开发配置，操作前阅读 [部署文档](AIREADME/DEPLOYMENT.md)，不要直接复用示例凭据或面向生产环境运行。

## 仓库结构

```text
larkflow/
├── engine/                  # legacy 编排与执行机制
├── io/                      # 飞书适配器
├── llm/                     # 模型调用与路由
├── model/                   # legacy 数据模型
├── templates/               # YAML 业务流程模板
└── workflow/                # Target 领域内核、服务、Worker 与 Console

frontend/console-canvas/     # DAG 画板前端
tests/                       # 离线测试与显式 PostgreSQL 集成测试
deploy/                      # 开发部署脚本与 systemd/Caddy 示例
research/                    # 研究协议与安全评审
AIREADME/                    # 产品、架构、契约、部署、决策和验收真相源
```

新增业务流程优先写入 `larkflow/templates/*.yaml`，不要为每种业务创建新的 Python executor 类型。

## 设计原则

- 人类负责：Agent 可以执行和建议，但不能伪装成组织责任人或权限来源。
- 草稿不执行：生成、核对和启动是相互独立的动作。
- 服务端裁决：身份、权限、DAG 合法性、revision 和影响范围都由中央节点计算。
- 历史不覆盖：返工与重启创建新 Attempt，旧结果和审计继续可查。
- 投影可修复：飞书对象是责任入口和交付界面，不是工作流业务状态的唯一真相源。
- 证据有边界：开发环境闭环不等于生产容量、商业价值或正式上线。

## 文档

- [AIREADME 索引](AIREADME/INDEX.md)：项目真相源与按任务阅读入口
- [产品定位](AIREADME/CORE.md)：使命、边界与硬约束
- [产品需求](AIREADME/PRD.md)：MVP 功能和体验契约
- [目标架构](AIREADME/ARCHITECTURE.md)：Target 组件、数据流与禁改项
- [接口契约](AIREADME/SPEC.md)：CLI、HTTP、身份和兼容边界
- [部署运维](AIREADME/DEPLOYMENT.md)：环境、服务、备份与回滚
- [路线图](AIREADME/ROADMAP.md)：Now、Next、Later
- [变更记录](AIREADME/CHANGELOG.md)：实现和验收里程碑
- [决策历史](AIREADME/DECISIONS.md)：关键取舍及其理由

## 安全

- 不提交凭证、token、真实人员 ID 或生产数据库。
- 真实飞书运行会创建任务、卡片、消息和文档，只能使用明确配置的开发环境。
- 测试不得构造 `build_real_service`，不得访问网络或真实飞书资源。
- Personal Agent Edge 的分发、安全与数据边界见 [安全评审](research/edge-distribution-security-review.md) 和 [数据策略](research/edge-data-policy-v0.md)。

## 致谢

感谢 [AI自动推广系统](http://bizbot.zvo.cn/)（BizBot）对本项目的赞助与支持。
