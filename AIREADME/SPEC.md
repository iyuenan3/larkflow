# SPEC · larkflow

⚑ 部分定型（节点契约已定，飞书事件 / 卡片 schema 待 dev app 建好后填）。

## 模板节点契约（已定，路线 1 与生成共用）
一张模板 = 节点数组，节点：
```
{
  id:    string          # 节点唯一 id
  label: string          # 展示名
  type:  "tool"|"llm"|"human"   # 机械动作 / AI 备料 / 人担责
  role:  string          # human 节点的 assignee 角色（如 开发/评审人/QA/负责人）；tool/llm 填 "-"
  gate:  string          # 门禁：达标条件；无则 "-"
  deps:  string[]        # 前置节点 id（依赖解锁）
}
```
- 边由 `deps` 表达；门禁不达标 → 回边（环）到指定上游（环的出口 = 门禁达标）。
- 生成新模板走 few-shot，须过三护栏（三型齐全 / 每门禁配回边 / 放行节点强制 human），见 DECISIONS ADR-010。
- 首个实例化模板 = 缺陷生命周期（11 节点），见 ARCHITECTURE / ADR-009。

## 待填（dev app 建好后）
- **飞书事件订阅 EventKey 清单**：@bot / 卡片 action / 任务完成对应的 key（`lark-cli event list` 需 app 上下文）→ 引擎动作映射。
- **卡片 action schema**：派单卡 / 门禁卡（通过·打回）/ 确认卡的按钮 action 契约。
