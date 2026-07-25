"""测试用替身。"""
from __future__ import annotations

from larkflow.llm import LLMClient


class CountingLLM(LLMClient):
    """每个 model_role 独立计数，产出自带版本号，便于断言「谁重算了、谁没动」。"""

    def __init__(self, texts: dict[str, str] | None = None):
        self.texts = texts or {}
        self.counts: dict[str, int] = {}
        self.calls: list[dict] = []

    def complete(self, *, prompt: str, model_role: str) -> str:
        self.counts[model_role] = self.counts.get(model_role, 0) + 1
        self.calls.append({"prompt": prompt, "model_role": model_role})
        body = self.texts.get(model_role, f"{model_role} 正文")
        return f"{body} v{self.counts[model_role]}"

    def prompt_of(self, model_role: str, nth: int) -> str:
        return [c["prompt"] for c in self.calls if c["model_role"] == model_role][nth]
