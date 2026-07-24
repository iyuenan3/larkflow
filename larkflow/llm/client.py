"""LLM 节点：OpenAI 兼容接口，按任务角色路由（ADR-017），不直连厂商专有 SDK。

节点配 `model_role` 选角色；每角色一组 `(base_url, api_key, model)`，可分别指向
火山方舟 / 中转站 / 直连供应商，各角色独立 key（key 只从 env 读，绝不入库）。

StubLLM         本地 e2e 用，固定返回 + 记录调用，零网络。
OpenAICompatLLM 真调用，step 9 接。
"""
from __future__ import annotations

import json


class LLMClient:
    def complete(self, *, prompt: str, model_role: str) -> str:
        """(llm, produce) 的通用生成：一段正文，随后由引擎物化成交付物。"""
        raise NotImplementedError


class StubLLM(LLMClient):
    def __init__(self, fixed: dict | None = None, completion: str = "（stub 生成的正文）"):
        self.fixed = fixed or {"severity": "P1", "type": "bug", "proposed_owner": "开发"}
        self.completion = completion
        self.calls: list[dict] = []

    def complete(self, *, prompt: str, model_role: str) -> str:
        self.calls.append({"prompt": prompt, "model_role": model_role})
        return self.completion

    def triage(self, bug: dict) -> dict:
        """缺陷流 per-id handler 用（seg-1 遗留结构化产出）。"""
        return dict(self.fixed)


class OpenAICompatLLM(LLMClient):
    """真 LLM 阶段接通。本地测试不构造它（避免引入网络 / 证书依赖）。

    roles: {model_role: {"base_url":…, "api_key":…, "model":…}}，从 env 按角色前缀装配
    （见 config.load_llm_roles）。未命中的角色回退 default 角色。
    """

    DEFAULT_ROLE = "default"

    def __init__(self, roles: dict[str, dict], *, ca_bundle: str | None = None, timeout: int = 60):
        if not roles:
            raise RuntimeError("LLM 角色路由表为空（见 .env.example 的 LLM_* 三元组）")
        self.roles = roles
        self.ca_bundle = ca_bundle
        self.timeout = timeout
        self._clients: dict[str, object] = {}

    def _client(self, model_role: str):
        cfg = self.roles.get(model_role) or self.roles.get(self.DEFAULT_ROLE)
        if cfg is None:
            raise RuntimeError(f"未配置 LLM 角色 {model_role}，且无 default 角色兜底")
        key = cfg["base_url"] + "|" + cfg["model"]
        if key not in self._clients:
            import httpx
            from openai import OpenAI

            self._clients[key] = OpenAI(
                base_url=cfg["base_url"],
                api_key=cfg["api_key"],
                # 自签 TLS 时传 ca_bundle；绝不 verify=False
                http_client=httpx.Client(verify=self.ca_bundle or True, timeout=self.timeout),
            )
        return self._clients[key], cfg["model"]

    def complete(self, *, prompt: str, model_role: str) -> str:
        client, model = self._client(model_role)
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""

    def triage(self, bug: dict) -> dict:
        text = self.complete(
            prompt=("你是缺陷分诊助手。给定 bug 描述，只输出 JSON，键："
                    "severity(P0|P1|P2|P3)、type(bug|regression|feature|question|infra)、"
                    "proposed_owner。不要 JSON 以外的文字。\n\n"
                    + json.dumps(bug, ensure_ascii=False)),
            model_role="triage",
        )
        return json.loads(text)
