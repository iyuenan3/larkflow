"""LLM 节点（triage_ai）：只走 newapi 网关（红线），不直连厂商。

StubLLM   本地 e2e 用，固定返回，零网络。
NewApiLLM 真调用：OpenAI 兼容，base_url=newapi /v1，key 走 env（不入库），
          自签 TLS 靠 CA bundle 绝对路径（绝不 verify=False）。
"""
from __future__ import annotations

import json


class LLMClient:
    def triage(self, bug: dict) -> dict:
        """定级 / 定类 / 建议负责人。返回 {severity, type, proposed_owner}。"""
        raise NotImplementedError


class StubLLM(LLMClient):
    def __init__(self, fixed: dict | None = None):
        self.fixed = fixed or {"severity": "P1", "type": "bug", "proposed_owner": "开发"}

    def triage(self, bug: dict) -> dict:
        return dict(self.fixed)


class NewApiLLM(LLMClient):
    """真飞书阶段接通。本地测试不构造它（避免引入网络 / 证书依赖）。"""

    SYSTEM = (
        "你是缺陷分诊助手。给定 bug 描述，只输出 JSON，键："
        "severity(P0|P1|P2|P3)、type(bug|regression|feature|question|infra)、proposed_owner。不要 JSON 以外的文字。"
    )

    def __init__(self, *, base_url: str, api_key: str, ca_bundle: str, model: str = "auto-llm"):
        import httpx
        from openai import OpenAI

        self.model = model
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=httpx.Client(verify=ca_bundle, timeout=60),
        )

    def triage(self, bug: dict) -> dict:
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": json.dumps(bug, ensure_ascii=False)},
            ],
        )
        return json.loads(resp.choices[0].message.content)
