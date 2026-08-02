"""Explicitly scoped executor adapters for the Target runtime."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import time
from typing import Protocol

from .model import ExecutorKind, QualityResult, QualityVerdict
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


class ToolExecutorRouter:
    """Route a Tool request to exactly one explicitly accepting adapter."""

    def __init__(self, adapters: Sequence[object]) -> None:
        if not adapters:
            raise ValueError("at least one Tool adapter is required")
        for adapter in adapters:
            if not callable(getattr(adapter, "accepts", None)):
                raise TypeError("every Tool adapter must define accepts()")
            if not callable(getattr(adapter, "execute", None)):
                raise TypeError("every Tool adapter must define execute()")
        self.adapters = tuple(adapters)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        matches = self._matches(executor=request.executor, work=request.work)
        if not matches:
            tool = request.work.get("tool")
            raise ValueError(f"unsupported Tool contract: {tool!r}")
        if len(matches) > 1:
            tool = request.work.get("tool")
            raise ValueError(f"ambiguous Tool contract: {tool!r}")
        return matches[0].execute(request)

    def accepts(self, *, executor: ExecutorKind, work: Mapping[str, object]) -> bool:
        return bool(self._matches(executor=executor, work=work))

    def _matches(
        self,
        *,
        executor: ExecutorKind,
        work: Mapping[str, object],
    ) -> tuple[object, ...]:
        return tuple(
            adapter
            for adapter in self.adapters
            if adapter.accepts(executor=executor, work=work)
        )


class ContentCheckToolExecutor:
    """Evaluate a bounded text contract without external side effects."""

    KIND = "content.check"

    def __init__(self, *, max_source_chars: int = 50_000) -> None:
        if max_source_chars < 1:
            raise ValueError("max_source_chars must be positive")
        self.max_source_chars = max_source_chars

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        tool = request.work.get("tool")
        if not self.accepts(executor=request.executor, work=request.work):
            raise ValueError(f"unsupported content check contract: {tool!r}")
        assert isinstance(tool, Mapping)
        args = tool.get("args") or {}
        if not isinstance(args, Mapping):
            raise ValueError("content.check args must be an object")

        source = args.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("content.check source is required")
        source = source.strip()
        found, content = self._lookup(request.input_snapshot, source)
        if not found:
            raise ValueError(f"content.check source was not found: {source}")
        if not isinstance(content, str):
            raise ValueError("content.check source must resolve to text")
        if len(content) > self.max_source_chars:
            raise ValueError(
                f"content.check source exceeds {self.max_source_chars} characters"
            )

        required_terms = self._required_terms(args.get("required_terms", ()))
        minimum = self._bounded_int(
            args.get("min_chars", 1),
            "min_chars",
            minimum=0,
            maximum=self.max_source_chars,
        )
        maximum = self._bounded_int(
            args.get("max_chars", self.max_source_chars),
            "max_chars",
            minimum=1,
            maximum=self.max_source_chars,
        )
        if minimum > maximum:
            raise ValueError("content.check min_chars cannot exceed max_chars")
        case_sensitive = args.get("case_sensitive", True)
        if not isinstance(case_sensitive, bool):
            raise ValueError("content.check case_sensitive must be a boolean")

        candidate = content if case_sensitive else content.casefold()
        missing = tuple(
            term
            for term in required_terms
            if (term if case_sensitive else term.casefold()) not in candidate
        )
        length_ok = minimum <= len(content) <= maximum
        passed = length_ok and not missing
        evidence_parts = []
        if missing:
            evidence_parts.append("缺少必需内容：" + "、".join(missing))
        if not length_ok:
            evidence_parts.append(
                f"正文长度 {len(content)}，要求 {minimum} 到 {maximum} 个字符"
            )
        if not evidence_parts:
            evidence_parts.append(
                f"正文长度 {len(content)}，且包含全部 {len(required_terms)} 项必需内容"
            )
        evidence = "；".join(evidence_parts)
        suggestion = "" if passed else "补齐缺失内容或调整正文长度后交由节点 Owner 复核。"
        verdict = QualityVerdict.PASS if passed else QualityVerdict.FAIL
        return ExecutionResult(
            result={
                "verdict": verdict.value,
                "evidence": evidence,
                "suggestion": suggestion,
                "source": source,
                "char_count": len(content),
                "missing_terms": list(missing),
                "request_id": request.idempotency_key,
            },
            quality_result=QualityResult(
                verdict=verdict,
                evidence=evidence,
                suggestion=suggestion,
            ),
        )

    def accepts(self, *, executor: ExecutorKind, work: Mapping[str, object]) -> bool:
        tool = work.get("tool")
        return (
            executor == ExecutorKind.TOOL
            and isinstance(tool, Mapping)
            and tool.get("kind") == self.KIND
        )

    @staticmethod
    def _lookup(root: Mapping[str, object], path: str) -> tuple[bool, object]:
        current: object = root
        for part in path.split("."):
            if not part or not isinstance(current, Mapping) or part not in current:
                return False, None
            current = current[part]
        return True, current

    @staticmethod
    def _required_terms(value: object) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError("content.check required_terms must be a sequence")
        if len(value) > 20:
            raise ValueError("content.check accepts at most 20 required_terms")
        terms = []
        for term in value:
            if not isinstance(term, str) or not term.strip():
                raise ValueError("content.check required_terms must contain text")
            normalized = term.strip()
            if len(normalized) > 100:
                raise ValueError("content.check required term exceeds 100 characters")
            terms.append(normalized)
        return tuple(terms)

    @staticmethod
    def _bounded_int(
        value: object,
        field_name: str,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"content.check {field_name} must be an integer")
        if value < minimum or value > maximum:
            raise ValueError(
                f"content.check {field_name} must be between {minimum} and {maximum}"
            )
        return value


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
