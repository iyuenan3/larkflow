# larkflow · 飞流 · AIREADME

> 飞书原生的企业协作与 Agent 工作流控制平面，把项目目标变成可确认、可执行、可返工和可追溯的 DAG。生命周期：active development。

当前产品处于开发试用阶段，不是生产就绪版本。Target 中央工作流、飞书责任投影和员工工作台已经形成可运行闭环；`PlannerRuntime` 与 `AgentRuntime` 基线端口已落码并部署到开发环境。项目级 UTF-8 txt/md 上传参与 DAG 规划已完成真实 PostgreSQL、Caddy、migration、服务端部署和 Owner 作用域 HTTPS API 验收；完整附件加明确 no-web 的新疆 8 日流程已经走完 Human、Agent、Human 决定闭环。No-web 旅游候选图现在必须把包含完整来源交付物的 Human 根节点直接连到被复核 Agent，并以可解析的正向出发地、日期、人数、预算、景点和交通证据做 fail-closed 校验，旁路根节点或“待定、未确认、没有”类否定文本不能放行；日期、人数和总预算还必须绑定到对应业务字段，无关的资料更新时间、酒店限住人数或酒店单项预算不能替代未确认的出行参数。日期标签还必须经过明确赋值边界，复合字段名和说明文字中的资料更新时间不能借用“出行日期”前缀放行。Agent 完成性同时校验供应商结束原因、结构化完成标记、验收证据锚点和服务端长度上限，任一条件失败都以 `agent_result_incomplete` 结束 Automated Attempt。薄 Python 豆包 `SearchProvider`、静态 capability preflight 和规范化来源证据已部署，开发环境真实公开查询回读 10 条带 URL 的来源与 provider request ID。来源质量内容提交 `fba57583af164d5a39077d7979b751e604cc3382` 已部署，增加 URL 结构、health、freshness、authority 与 support 分层状态，并用 `source_evidence.check` 把 claim 绑定到当前搜索 Attempt 的规范 URL 与 provider 原文片段；生产 `SafeOutboundFetcher` 保持 unavailable，不会直接抓取任意返回 URL，安装态 synthetic 探针确认 health 为 unknown、伪造片段被拒绝且语义真实性仍未独立验证。新建文本交付物已统一为飞书原生 Docx，并完成真实创建、同一 document_id 覆盖、正文和原生标题、列表、表格回读。Phase 2B 的项目附件 Agent Attempt 上下文也已部署：只有显式声明服务器拥有输入的 Agent 节点，才会在 tenant、Instance、Node、Attempt 和外发策略复验后获得有界正文与短时只读能力信封，Attempt 只持久化安全 manifest、fingerprint 和运行证据，不保存正文、object key、claim 或凭据；一次性真实 PostgreSQL 合同与安装态 synthetic Runtime 探针已通过。企业共享正文、ContextBundle 和 Console 显式资料选择均已部署：发起人只在 collecting 草稿中选择 source，服务端于生成边界冻结精确版本，重试不漂移，撤销继续阻断新的 Planner 与 Agent Context。真实管理员浏览器发布、显式选择页面和真实内部资料验收仍待手工完成。Tool Gateway、生产对象存储、PDF/DOCX/OCR、语义检索和向量检索继续后置，Personal Agent Edge 已暂停。最新实现与验收历史只记录在 [CHANGELOG.md](CHANGELOG.md)，当前推进边界见 [ROADMAP.md](ROADMAP.md)。

企业共享资料正文能力已经部署到阿里云开发环境：独立 create-once BlobStore 保存不可变 UTF-8 txt/md，管理员发布必须提交固定的 tenant 全员授权声明，服务端生成绑定 tenant、来源、版本、正文哈希、管理员身份和时间的追加型授权证明。Knowledge Context Service 在规划和每个 Agent Attempt 前重新验证 published 状态、撤销、哈希、大小、分级、外发、预算和 TTL，再按确定性顺序合并项目附件与企业资料；Runtime 只接收有界正文和安全引用。撤销不改写旧 Attempt 的 manifest 与审计，但阻断新 ContextBundle。代码提交 `17aba5ae35261bc915d2dd6d6f4c272273d2da89` 又把正文读取后的当前授权复验设为明确发行线性化点，关闭撤销与 Blob I/O 交错时仍返回新 bundle 的竞态；该修复已经部署，真实一次性 PostgreSQL 并发合同和安装态 planning、Agent synthetic 竞态探针均通过。内容提交 `e663edfa2c54230b3b58bfc9e26ad046f95a3b51` 已把自动全选收紧为 Console collecting 草稿的显式 source 选择，并在生成边界冻结精确版本；`0027` 已完成空库迁移、重入、强制失败回滚、真实 PostgreSQL 合同、开发部署和安装态 synthetic 探针。真实管理员浏览器发布、显式选择页面和真实内部资料验收仍待手工完成，不能描述为生产企业知识库。

来源 provenance 与撤销选择恢复也已部署到开发环境。`source_evidence.check` 现在只接受 WorkflowRunner 根据直接依赖 NodeSpec 与已提交 Attempt 冻结的真实 `tool / web.search` provenance，普通 Agent 的结果自报、嵌套复制和缺失 provenance 均 fail closed。撤销后的已选企业来源以安全墓碑继续可见，Owner 必须显式移除并保存，服务端不会自动清空。长期开发库已增量应用并重入 `0028_console_enterprise_source_uniqueness`，数据库层也拒绝重复 source ID；真实浏览器人工验收仍后置。

飞书 Console 的过期会话恢复已部署到开发环境。认证 401 与 OAuth 失败页面现在保留“重新授权飞书”入口，点击后先清理当前 cookie 会话，再进入新的 OAuth；清理请求失败也不会阻断授权。安装态、loopback 与公网静态资源哈希一致，Console 与安全边界回读正常；真实浏览器按钮点击仍待 Maxwell 人工验收。

联网旅行草稿现在先经过 larkflow 自有的 `deterministic_travel_v1` Planner。明确的出发地、起止日期、人数和总预算缺一时，Console 返回安全的结构化缺字段提示；字段完整且豆包搜索 capability 可用时，不调用规划模型，直接生成 `Human 确认 -> 景点 web.search + 交通 web.search -> Agent 综合 -> Human 复核` 的受控 DAG，再由统一 validator 复验。DeepSeek 与 qwen 对同一复杂冻结输入均超过 120 秒的诊断不再进入该交互主路径。开发环境 synthetic smoke 使用一份冻结企业资料，在 0.58 秒内生成 5 节点 ready 草稿，豆包公开查询另行回读 10 条带 URL 的来源。该证据不替代真实飞书浏览器端到端，也不证明搜索事实正确。

typed 豆包搜索现在从 NodeSpec 明确声明消费的已确认直接依赖编译实际 query，不再只发送通用静态指令。景点与交通字段按确定性优先级压缩到 100 字，未授权字段不进入查询；明显与目的地和关键实体完全无交集的来源以 `search_evidence_missing` 拒绝。实际 query 同时保存在 Tool result 和完成审计。该修复已完成真实 provider 安装态技术探针，旧新疆实例结果不改写，仍需新流程或正式 restart 完成真实飞书端到端复验。

last-synced: 7d79ce3b35bf09c4ad4478d71298ede2844971cb · 2026-08-20

## 阅读顺序

1. [CORE.md](CORE.md)：产品身份、边界和不变量。
2. [PRODUCT_STRATEGY.md](PRODUCT_STRATEGY.md)：当前证据边界、取舍和成功标准。
3. [PRD.md](PRD.md)：简化 MVP 的功能、体验和验收。
4. [DAG_TEMPLATE_SPEC.md](DAG_TEMPLATE_SPEC.md)：DAG Contract v0.2 目标契约。
5. [ARCHITECTURE.md](ARCHITECTURE.md)：目标架构、数据权威和原型迁移边界。

既有设计的范围取舍见 [`research/design-simplification.md`](../research/design-simplification.md)。

## 状态

| 文件 | 状态 | 摘要 |
|---|:--:|---|
| CORE | ✅ | Cloud-first Target 身份、边界、暂停 Edge Proof 和不变量 |
| PRODUCT_STRATEGY | ✅ | 范围取舍、证据边界和成功标准 |
| PRD | ✅ | MVP 功能、体验与验收契约 |
| DAG_TEMPLATE_SPEC | ✅ | DAG Contract v0.2 与模板边界 |
| ARCHITECTURE | ✅ | Target 组件、知识边界、可替换 Runtime、数据权威与实现差距 |
| RELATIONS | ✅ | 飞书、企业资料、Pi、DSH、暂停 Edge 与外部依赖 |
| ROADMAP | ✅ | Now、Next、Later 与明确搁置项 |
| SPEC | ✅ | Target 与 legacy 的 CLI、HTTP、事件和数据契约 |
| DEPLOYMENT | ✅ | 开发环境服务、迁移、备份、回滚和安全边界 |
| CONVENTIONS | ✅ | 命名、状态、安全、文档治理和提交约定 |
| DECISIONS | ✅ | Append-only ADR 历史 |
| CHANGELOG | ✅ | Append-only 实现、发布和验收里程碑 |
| MEMORY | ⚑ | Append-only 工程经验，仍含语义占位 |

## 按任务读取

- 改产品范围：CORE + PRODUCT_STRATEGY + PRD + DECISIONS
- 核对既有设计的简化范围：`../research/design-simplification.md` + PRD + DECISIONS
- 改模板：DAG_TEMPLATE_SPEC + CONVENTIONS + PRD
- 改运行时或数据模型：ARCHITECTURE + SPEC + DECISIONS
- 改飞书集成：RELATIONS + ARCHITECTURE + DEPLOYMENT
- 改部署或运维：DEPLOYMENT + MEMORY
- 查实现和验收历史：CHANGELOG；需要原因时再读 DECISIONS
- 判断“设计了还是做了”：先看 ARCHITECTURE 的差距表，再看 SPEC 和代码
- 未来恢复外部验证：`../research/phase-0/README.md`
