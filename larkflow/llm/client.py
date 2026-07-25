"""LLM 节点：OpenAI 兼容接口，按任务角色路由（ADR-017），不直连厂商专有 SDK。

节点配 `model_role` 选角色；每角色一组 `(base_url, api_key, model)`，可分别指向
火山方舟 / 中转站 / 直连供应商，各角色独立 key（key 只从 env 读，绝不入库）。

StubLLM         本地 e2e 用，固定返回 + 记录调用，零网络。
OpenAICompatLLM 真调用，step 9 接。
"""
from __future__ import annotations

import hashlib
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


class LLMUnavailable(RuntimeError):
    """一个角色的**整条线路**（主 + 全部备用）都打不通。"""


# 400 / 422 = 我们自己的请求有问题（报文不合法、超长、内容被判违规）。换条线路只会
# 原样再错一次，还多烧一次钱。其余一律可切：连不上 / 超时 / 429 / 5xx 是「掉线」的
# 主要形态，401 / 404 也算（key 过期、这家没有这个模型，换一家正好可能有）。
NO_FAILOVER_STATUS = (400, 422)


def _status_of(exc: Exception):
    """从异常里挖 HTTP 状态码。鸭子类型取，**不 import openai**：那个包只在真跑时才装，
    模块级 import 会把本地测试也拖下水。"""
    for probe in (exc, getattr(exc, "response", None)):
        code = getattr(probe, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _can_fail_over(exc: Exception) -> bool:
    return _status_of(exc) not in NO_FAILOVER_STATUS


class OpenAICompatLLM(LLMClient):
    """真 LLM 阶段接通。本地测试不构造它（避免引入网络 / 证书依赖）。

    roles: {model_role: {"base_url":…, "api_key":…, "model":…, "fallbacks": [同形 …]}}，
    从 env 按角色前缀装配（见 config.load_llm_roles）。未命中的角色回退 default 角色，
    **连同它的备用线路一起**。

    每个角色是一条**有序链**：主线路打不通就顺着备用往下试。LLM 供应商掉线 / 限流 /
    key 过期是常态不是异常，而一个 llm 节点跑挂 = 那一支的产出没了，上游可能已经花掉
    真人的时间。切换会**留痕**（`self.failovers` + `on_failover` 回调）：静默切走的话，
    主线路可以死一个月都没人知道。
    """

    DEFAULT_ROLE = "default"

    def __init__(self, roles: dict[str, dict], *, ca_bundle: str | None = None,
                 timeout: int = 60, client_factory=None, on_failover=None):
        if not roles:
            raise RuntimeError("LLM 角色路由表为空（见 .env.example 的 LLM_* 三元组）")
        self.roles = roles
        self.ca_bundle = ca_bundle
        self.timeout = timeout
        self.client_factory = client_factory or self._build_client
        self.on_failover = on_failover
        self.failovers: list[dict] = []
        self._clients: dict[str, object] = {}

    def _build_client(self, cfg: dict):
        import httpx
        from openai import OpenAI

        return OpenAI(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            # 自签 TLS 时传 ca_bundle；绝不 verify=False
            http_client=httpx.Client(verify=self.ca_bundle or True, timeout=self.timeout),
        )

    def _chain(self, model_role: str) -> list[dict]:
        cfg = self.roles.get(model_role) or self.roles.get(self.DEFAULT_ROLE)
        if cfg is None:
            raise RuntimeError(f"未配置 LLM 角色 {model_role}，且无 default 角色兜底")
        return [cfg, *(cfg.get("fallbacks") or ())]

    def _client(self, cfg: dict):
        # 缓存键必须含 **api_key**：同端点同模型换把 key 是备用的主用法，只按
        # (base_url, model) 做键会命中主线路那个客户端，于是「换把 key 重试」变成
        # 「拿同一把挂掉的 key 再试一次」，备用形同虚设。key 只进哈希，不进键面。
        digest = hashlib.sha256(cfg["api_key"].encode("utf-8")).hexdigest()[:12]
        key = f"{cfg['base_url']}|{cfg['model']}|{digest}"
        if key not in self._clients:
            self._clients[key] = self.client_factory(cfg)
        return self._clients[key]

    def complete(self, *, prompt: str, model_role: str) -> str:
        chain = self._chain(model_role)
        tried: list[str] = []
        for i, cfg in enumerate(chain):
            try:
                resp = self._client(cfg).chat.completions.create(
                    model=cfg["model"],
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                )
            except Exception as exc:
                tried.append(f"{cfg['base_url']}/{cfg['model']}: {type(exc).__name__}: {exc}")
                if not _can_fail_over(exc):
                    raise            # 请求本身有问题，换线路无益
                if i == len(chain) - 1:
                    raise LLMUnavailable(
                        f"角色 {model_role} 的 {len(chain)} 条线路全部打不通："
                        + "｜".join(tried)) from exc
                continue
            if i:
                self._note_failover(model_role, i, len(chain), tried)
            return resp.choices[0].message.content or ""
        raise LLMUnavailable(f"角色 {model_role} 没有任何可用线路")   # 链为空（装配期已挡）

    def _note_failover(self, model_role: str, used: int, total: int, tried: list[str]) -> None:
        record = {"model_role": model_role, "used": used, "total": total, "errors": list(tried)}
        self.failovers.append(record)
        if self.on_failover is not None:
            try:
                self.on_failover(record)
            except Exception:      # 报警路上再报警，不许把这次已经成功的生成带走
                pass

    def triage(self, bug: dict) -> dict:
        text = self.complete(
            prompt=("你是缺陷分诊助手。给定 bug 描述，只输出 JSON，键："
                    "severity(P0|P1|P2|P3)、type(bug|regression|feature|question|infra)、"
                    "proposed_owner。不要 JSON 以外的文字。\n\n"
                    + json.dumps(bug, ensure_ascii=False)),
            model_role="triage",
        )
        return json.loads(text)
