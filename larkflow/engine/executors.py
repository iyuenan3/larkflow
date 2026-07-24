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
from .deliverables import ensure_container, materialize, read_upstream

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
    ):
        self.io = io                      # LarkIO（Mock 或 Cli）：任务 / 卡 / 通知
        self.resolver = resolver          # assignee_role -> assignee(open_id)
        self.llm = llm                    # LLMClient（stub 或真实多角色路由）
        self.deliverables = deliverables  # DeliverableIO（Fake 或 Cli）：交付物读写
        self.tool_handlers = tool_handlers or {}
        self.llm_handlers = llm_handlers or {}

    # ---------- 对外：三个 executor ----------
    def run_tool(self, node: dict, state: dict) -> dict:
        h = self.tool_handlers.get(node["id"])
        if h is None:
            raise ExecutorError(
                f"tool 节点 {node['id']} 未注册 handler（确定性程序无通用体，须注入）"
            )
        return self._collect(node, state, h(node, state, self))

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
        text = self.llm.complete(prompt=build_prompt(node, state, self._upstream(state, node)),
                                 model_role=node["model_role"])
        return self._collect(node, state, {"ok": True, "content": text})

    def prepare_human(self, node: dict, state: dict) -> dict:
        """human 节点挂起前的准备：produce 先备好交付物容器（人去飞书里写）。

        **只创建不覆盖**：interrupt 之前的代码在 resume 时会重跑，覆盖会抹掉人写的内容。
        """
        if not is_produce(node):
            return {}
        return ensure_container(
            self.deliverables, node, state,
            placeholder=self._placeholder(node),
        )

    def validate_coverage(self, dag: list[dict]) -> None:
        """装配期自检：每个 tool 节点都得有 handler（否则跑到一半才炸）。"""
        missing = [n["id"] for n in dag
                   if n["executor"] == "tool" and n["id"] not in self.tool_handlers]
        if missing:
            raise ExecutorError(f"tool 节点缺 handler: {missing}")

    # ---------- 内部 ----------
    def _upstream(self, state: dict, node: dict) -> dict[str, str]:
        if self.deliverables is None:
            return {}
        return read_upstream(self.deliverables, state, node)

    def _collect(self, node: dict, state: dict, result: dict) -> dict:
        """统一收尾：produce 的 content 落成交付物；gate 的产出须自带 passed。"""
        result = dict(result or {})
        if is_gate(node):
            if "passed" not in result:
                raise ExecutorError(f"gate 节点 {node['id']} 的产出必须含 passed")
            return result
        content = result.pop("content", None)
        if content is None or not is_produce(node):
            return result
        return {**result, **materialize(self.deliverables, node, state, content=content)}

    def _placeholder(self, node: dict) -> str:
        who = node.get("assignee_role") or "负责人"
        return (f"# {node.get('label', node['id'])}\n\n"
                f"> 待 {who} 填写。写完后按约定信号（{node.get('signal')}）发出定稿信号，"
                f"引擎不会因为「文档不动了」判定稿。\n")


def build_prompt(node: dict, state: dict, upstream: dict[str, str]) -> str:
    """节点 prompt + 项目要素 + 上游交付物正文，拼成一次调用的输入。"""
    parts = [node["prompt"]]
    inputs = (state.get("meta") or {}).get("inputs")
    if inputs:
        parts.append("## 项目要素\n" + json.dumps(inputs, ensure_ascii=False, indent=2))
    for dep, text in upstream.items():
        parts.append(f"## 上游交付物 · {dep}\n{text}")
    return "\n\n".join(parts)
