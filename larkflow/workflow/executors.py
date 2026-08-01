"""Explicitly scoped executor adapters for the Target runtime."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import time
from typing import Protocol

from .model import ExecutorKind
from .runtime import ExecutionRequest, ExecutionResult
from .serde import to_json_value


class AgentCompletionClient(Protocol):
    """Minimal completion port required by the Target Agent adapter."""

    def complete(self, *, prompt: str, model_role: str) -> str:
        ...


class LLMAgentExecutor:
    """Run the explicit ``llm.generate`` Agent contract."""

    KIND = "llm.generate"

    def __init__(
        self,
        client: AgentCompletionClient,
        *,
        max_prompt_chars: int = 20_000,
        max_result_chars: int = 50_000,
    ) -> None:
        if max_prompt_chars < 1:
            raise ValueError("max_prompt_chars must be positive")
        if max_result_chars < 1:
            raise ValueError("max_result_chars must be positive")
        self.client = client
        self.max_prompt_chars = max_prompt_chars
        self.max_result_chars = max_result_chars

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        agent = request.work.get("agent")
        if not self.accepts(executor=request.executor, work=request.work):
            raise ValueError(f"unsupported Agent kind: {agent!r}")
        assert isinstance(agent, Mapping)
        model_role = agent.get("model_role", "default")
        if not isinstance(model_role, str) or not model_role.strip():
            raise ValueError("Agent model_role must be a non-empty string")
        instructions = agent.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("Agent instructions are required")

        prompt = self._prompt(
            request,
            instructions=instructions.strip(),
        )
        if len(prompt) > self.max_prompt_chars:
            raise ValueError(
                f"Agent prompt exceeds {self.max_prompt_chars} characters"
            )
        content = self.client.complete(
            prompt=prompt,
            model_role=model_role.strip(),
        )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Agent returned an empty result")
        content = self._plain_text(content)
        if not content:
            raise ValueError("Agent returned an empty result")
        if len(content) > self.max_result_chars:
            raise ValueError(
                f"Agent result exceeds {self.max_result_chars} characters"
            )
        return ExecutionResult(
            result={
                "content": content,
                "agent_kind": self.KIND,
                "model_role": model_role.strip(),
                "request_id": request.idempotency_key,
            }
        )

    def accepts(self, *, executor: ExecutorKind, work: Mapping[str, object]) -> bool:
        agent = work.get("agent")
        return (
            executor == ExecutorKind.AGENT
            and isinstance(agent, Mapping)
            and agent.get("kind") == self.KIND
        )

    @staticmethod
    def _prompt(
        request: ExecutionRequest,
        *,
        instructions: str,
    ) -> str:
        context = json.dumps(
            to_json_value(request.input_snapshot),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        outputs = json.dumps(
            to_json_value(request.work.get("outputs", ())),
            ensure_ascii=False,
            indent=2,
        )
        acceptance = "\n".join(
            f"- {item}" for item in request.work.get("acceptance", ())
        )
        return (
            "你是企业协作工作流中的 Agent 节点。只完成当前节点，不执行外部操作，"
            "不虚构未提供的事实。\n\n"
            f"节点目标：{request.work.get('objective', '')}\n"
            f"节点指令：{instructions}\n\n"
            f"预期输出：\n{outputs}\n\n"
            f"验收条件：\n{acceptance}\n\n"
            f"已提交的输入与上游结果：\n{context}\n\n"
            "请直接给出可供下一人工节点审阅的正文，不要返回 JSON、代码块或字段包装。"
        )

    @staticmethod
    def _plain_text(content: str) -> str:
        """Extract text from common structured completion wrappers."""
        candidate = content.strip()
        if candidate.startswith("```") and candidate.endswith("```"):
            lines = candidate.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                candidate = "\n".join(lines[1:-1]).strip()

        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            return content.strip()

        if isinstance(value, Mapping):
            for key in ("content", "text"):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    return text.strip()
        if isinstance(value, list):
            parts = []
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                for key in ("text", "content"):
                    text = item.get(key)
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
                        break
            if parts:
                return "\n\n".join(parts)
        return content.strip()


class DevelopmentToolExecutor:
    """Deterministic dev-only Tool adapter used for persistence rehearsals."""

    KIND = "development.echo"

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_delay_seconds: float = 60.0,
    ) -> None:
        if max_delay_seconds <= 0:
            raise ValueError("max_delay_seconds must be positive")
        self.sleep = sleep
        self.max_delay_seconds = max_delay_seconds

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        tool = request.work.get("tool")
        if not self.accepts(executor=request.executor, work=request.work):
            raise ValueError(f"unsupported development tool kind: {tool!r}")
        args = tool.get("args") or {}
        if not isinstance(args, Mapping):
            raise ValueError("development tool args must be an object")

        delay = args.get("delay_seconds", 0)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)):
            raise ValueError("delay_seconds must be a number")
        if delay < 0 or delay > self.max_delay_seconds:
            raise ValueError(
                f"delay_seconds must be between 0 and {self.max_delay_seconds:g}"
            )
        if delay:
            self.sleep(float(delay))

        result = args.get("result", {"value": args.get("value")})
        if not isinstance(result, Mapping):
            raise ValueError("development tool result must be an object")
        return ExecutionResult(result=to_json_value(result))

    def accepts(self, *, executor: ExecutorKind, work: Mapping[str, object]) -> bool:
        tool = work.get("tool")
        return (
            executor == ExecutorKind.TOOL
            and isinstance(tool, Mapping)
            and tool.get("kind") == self.KIND
        )
