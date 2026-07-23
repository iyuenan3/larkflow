"""执行器注入点：tool / llm 节点的具体行为按 node id 查处理器。

引擎保持通用（不认识具体模板）；缺陷流的 tool/llm 逻辑放在
templates/defect_handlers.py，作为 handlers 注入。human 节点不在这里
（它只 interrupt 传数据、由驱动层做飞书 I/O，规避 resume 重跑副作用）。

处理器签名: handler(node: dict, state: dict, ex: Executors) -> dict
返回 dict 即节点产出（写入 outputs[node_id]）；门禁节点须含 passed。
"""
from __future__ import annotations

from typing import Callable

Handler = Callable[[dict, dict, "Executors"], dict]


class Executors:
    def __init__(
        self,
        io,
        resolver,
        llm=None,
        tool_handlers: dict[str, Handler] | None = None,
        llm_handlers: dict[str, Handler] | None = None,
    ):
        self.io = io                      # LarkIO（Mock 或 Cli）
        self.resolver = resolver          # role -> assignee(open_id)
        self.llm = llm                    # LLMClient（stub 或 newapi）
        self.tool_handlers = tool_handlers or {}
        self.llm_handlers = llm_handlers or {}

    def run_tool(self, node: dict, state: dict) -> dict:
        h = self.tool_handlers.get(node["id"])
        return h(node, state, self) if h else {"ok": True}

    def run_llm(self, node: dict, state: dict) -> dict:
        h = self.llm_handlers.get(node["id"])
        return h(node, state, self) if h else {"ok": True}
