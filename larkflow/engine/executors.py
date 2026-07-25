"""节点执行体：按 `executor × role + 配置` 跑，不再 per-node-id（ADR-015）。

  (llm, produce)   读 node.prompt + model_role → 多角色路由生成 → 末步物化交付物
  (tool, *)        确定性程序，注入 per-id handler（逃生舱：确定性代码天生是自定义的）；
                   produce handler 返回 {"content": …} 则由引擎统一物化
  (human, produce) 引擎先备好交付物容器（人在飞书里写），只创建不覆盖
  (human, gate)    纯挂起（interrupt 在 orchestrator，驱动层建卡）
  (llm, gate)      禁止：LLM 绝不自动放行（护栏③，这里再兜一道）

引擎保持通用（不认识具体模板）：模板专属的 tool 逻辑当 handlers 注入。
处理器签名: handler(node: dict, state: dict, ex: Executors) -> dict
返回 dict 即节点产出（写入 outputs[node_id]）；gate 节点须含 passed。
"""
from __future__ import annotations

import json
from typing import Callable

from ..model.node import is_gate, is_produce
from .deliverables import (
    PLACEHOLDER_MARK,
    ensure_container,
    materialize,
    prior_handle,
    read_upstream,
)
from .gates import reopen_feedback
from .support import V1_POLICIES, UnsupportedInV1
from .tools import TOOL_KINDS

Handler = Callable[[dict, dict, "Executors"], dict]


class ExecutorError(RuntimeError):
    """执行体配置 / 契约错误（宁可炸，不静默产出坏数据）。"""


class Executors:
    def __init__(
        self,
        io,
        resolver,
        llm=None,
        deliverables=None,
        tool_handlers: dict[str, Handler] | None = None,
        llm_handlers: dict[str, Handler] | None = None,
        tool_kinds: dict | None = None,
    ):
        self.io = io                      # LarkIO（Mock 或 Cli）：任务 / 卡 / 通知
        self.resolver = resolver          # assignee_role -> assignee(open_id)
        self.llm = llm                    # LLMClient（stub 或真实多角色路由）
        self.deliverables = deliverables  # DeliverableIO（Fake 或 Cli）：交付物读写
        self.tool_kinds = TOOL_KINDS if tool_kinds is None else tool_kinds  # 内置能力库（配置选取）
        self.tool_handlers = tool_handlers or {}   # 逃生舱：按 node id 的一次性代码
        self.llm_handlers = llm_handlers or {}

    # ---------- 对外：三个 executor ----------
    def run_tool(self, node: dict, state: dict) -> dict:
        h = self.tool_handlers.get(node["id"])
        if h is not None:                                    # 逃生舱优先
            return self._collect(node, state, h(node, state, self))
        kind = (node.get("tool") or {}).get("kind")
        fn = self.tool_kinds.get(kind)
        if fn is None:
            raise ExecutorError(
                f"tool 节点 {node['id']} 无可执行体：tool.kind={kind!r} 不在能力库 "
                f"{sorted(self.tool_kinds)}，也没注册 per-id handler"
            )
        return self._collect(node, state, fn(node, state, self, (node.get("tool") or {}).get("args") or {}))

    def run_llm(self, node: dict, state: dict) -> dict:
        if is_gate(node):
            raise ExecutorError(
                f"{node['id']}：llm 节点不得当 gate（LLM 绝不自动放行）；"
                "AI 评审须落成 (llm, produce) 出意见 + human gate 拍板"
            )
        h = self.llm_handlers.get(node["id"])
        if h is not None:
            return self._collect(node, state, h(node, state, self))
        if self.llm is None:
            raise ExecutorError(f"llm 节点 {node['id']} 无可用 LLMClient")
        prompt = build_prompt(node, state, self._upstream(state, node),
                              feedback=reopen_feedback(state.get("dag") or [],
                                                       state.get("outputs") or {}, node["id"]),
                              previous=self._previous_draft(state, node))
        text = self.llm.complete(prompt=prompt, model_role=node["model_role"])
        return self._collect(node, state, {"ok": True, "content": text})

    def prepare_human(self, node: dict, state: dict) -> dict:
        """human 节点挂起前的准备：produce 先备好交付物容器（人去飞书里写）。

        **只创建不覆盖**：interrupt 之前的代码在 resume 时会重跑，覆盖会抹掉人写的内容。
        """
        if is_gate(node):
            # auto 门不该走到人（护栏③已保证 auto=tool）；会签 / 阈值 runtime 落 v1.3
            assert_gate_policy_supported(node)
            return {}
        if not is_produce(node) or node.get("deliverable") is None:
            return {}   # 纯动作的人工节点（线下动作确认等）不需要文档容器
        return ensure_container(
            self.deliverables, node, state,
            placeholder=self._placeholder(node),
        )

    def validate_coverage(self, dag: list[dict]) -> None:
        """装配期自检：每个 tool 节点都得有可执行体（内置 kind 或 per-id 逃生舱）。"""
        missing = [
            n["id"] for n in dag
            if n["executor"] == "tool"
            and n["id"] not in self.tool_handlers
            and (n.get("tool") or {}).get("kind") not in self.tool_kinds
        ]
        if missing:
            raise ExecutorError(
                f"tool 节点无可执行体: {missing}；声明 tool.kind ∈ {sorted(self.tool_kinds)} 即可，"
                "无需写 Python"
            )

    # ---------- 内部 ----------
    def _upstream(self, state: dict, node: dict) -> dict[str, str]:
        if self.deliverables is None:
            return {}
        return read_upstream(self.deliverables, state, node)

    def _previous_draft(self, state: dict, node: dict) -> str | None:
        """被打回重算时把自己的上一稿回喂：让 AI「照意见改」，而不是从零重写一遍。"""
        if self.deliverables is None:
            return None
        handle = prior_handle(state.get("outputs") or {}, node["id"])
        return self.deliverables.fetch(handle) if handle is not None else None

    def _collect(self, node: dict, state: dict, result: dict) -> dict:
        """统一收尾：produce 的 content 落成交付物；gate 的产出须自带 passed。"""
        result = dict(result or {})
        if is_gate(node):
            if "passed" not in result:
                raise ExecutorError(f"gate 节点 {node['id']} 的产出必须含 passed")
            return result
        content = result.pop("content", None)
        if not is_produce(node):
            return result
        declared = node.get("deliverable") is not None
        if content is None:
            # 声明了落点却什么都没产出 = 静默的空洞：下游会经「透传」悄悄读到祖父节点的正文，
            # 全程无声。gate 缺 passed 会炸，produce 缺产出没理由不炸。
            if declared and prior_handle(state.get("outputs") or {}, node["id"]) is None:
                raise ExecutorError(
                    f"produce 节点 {node['id']} 声明了 deliverable 却没产出任何 content"
                )
            return result          # 纯动作节点（无 deliverable）：不产文档是正常的
        if not declared:
            raise ExecutorError(
                f"{node['id']} 产出了正文却没声明 deliverable（交付物没有落点，会被丢掉）"
            )
        return {**result, **materialize(self.deliverables, node, state, content=content)}

    def _placeholder(self, node: dict) -> str:
        who = node.get("assignee_role") or "负责人"
        # PLACEHOLDER_MARK 是引擎与 auto 机检门共用的常量：机检据它判「人还没真写」
        return (f"# {node.get('label', node['id'])}\n\n"
                f"> {PLACEHOLDER_MARK} 由 {who} 填写。写完后发出约定的完成信号"
                f"（{node.get('signal')}），引擎不会因为「文档不动了」判定稿。\n")


def assert_gate_policy_supported(node: dict) -> None:
    if node.get("approval_policy") not in V1_POLICIES:
        raise UnsupportedInV1(
            f"{node['id']} approval_policy={node.get('approval_policy')}：会签 / 投票阈值落 v1.3"
        )


def build_prompt(node: dict, state: dict, upstream: dict[str, str],
                 feedback: list[dict] | None = None, previous: str | None = None) -> str:
    """节点 prompt + 项目要素 + 上游交付物正文 + **上一轮打回意见与自己的上一稿**。

    扇入（merge）就是多 deps 的这一条路径：每个上游按 label 标注来源，引擎无需
    「merge 节点类型」。

    打回意见必须进这里，否则重算就是空转：同一份 prompt 重跑，真 LLM（temperature=0）
    会一字不差地再生成同一份稿，人点的「打回」等于没点。
    """
    labels = {n["id"]: n.get("label", n["id"]) for n in (state.get("dag") or [])}
    parts = [node["prompt"]]
    inputs = (state.get("meta") or {}).get("inputs")
    if inputs:
        parts.append("## 项目要素\n" + json.dumps(inputs, ensure_ascii=False, indent=2))
    for dep, text in upstream.items():
        parts.append(f"## 上游交付物 · {labels.get(dep, dep)}（{dep}）\n{text}")
    if feedback:
        parts.append("## 上一轮打回意见（必须逐条处理）\n" + render_feedback(feedback))
        if previous:
            parts.append(f"## 你的上一稿（在它基础上按意见修改，不要推倒重写）\n{previous}")
    return "\n\n".join(parts)


def render_feedback(feedback: list[dict]) -> str:
    return "\n".join(f"- {f.get('label') or f.get('from')}：{f.get('comment') or '（未留言）'}"
                     for f in feedback)
