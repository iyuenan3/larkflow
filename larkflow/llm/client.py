"""LLM 节点：OpenAI 兼容接口，按任务角色路由（ADR-017），不直连厂商专有 SDK。

节点配 `model_role` 选角色；每角色一组 `(base_url, api_key, model)`，可分别指向
火山方舟 / 中转站 / 直连供应商，各角色独立 key（key 只从 env 读，绝不入库）。

StubLLM         本地 e2e 用，固定返回 + 记录调用，零网络。
OpenAICompatLLM 真调用，step 9 接。
"""
from __future__ import annotations

import hashlib
import json
import time


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


def _env_off(key: str) -> bool:
    """`<KEY>=1/true/yes/on` 表示「关掉」，返回 False（即不 trust_env）。"""
    import os

    return str(os.environ.get(key, "")).strip().lower() not in ("1", "true", "yes", "on")


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
    # 实测（2026-07-26，方舟 doubao-seed-2.1-turbo）：起草一份合同商务条款 **109.7s /
    # 2570 字**。原来的 60s 会把 `biz_draft` 掐断，而那一刻飞书文档已建、任务已派：
    # 人看到的是「AI 那步失败了」，日志里只有一个 ReadTimeout。默认值要明显大于实测值，
    # 别卡在边界上；单个角色嫌慢就用 `LLM_<ROLE>_TIMEOUT` 单独收紧。
    DEFAULT_TIMEOUT = 300

    def __init__(self, roles: dict[str, dict], *, ca_bundle: str | None = None,
                 timeout: float | None = None, client_factory=None, on_failover=None,
                 trust_env: bool | None = None, http_factory=None,
                 openai_factory=None, on_call=None):
        if not roles:
            raise RuntimeError("LLM 角色路由表为空（见 .env.example 的 LLM_* 三元组）")
        self.roles = roles
        self.ca_bundle = ca_bundle
        # 是否吃环境里的 http(s)_proxy / all_proxy。默认吃（有人的 LLM 在墙外，确实要走代理）。
        # `LLM_NO_PROXY=1` 关掉：本机 Clash 会把 all_proxy 设成 socks5://…，而 httpx 在
        # **建客户端那一刻**就急切构造 socks 传输，缺 socksio 直接 ImportError；实测
        # `no_proxy` 救不了。境内 LLM 端点本来也不该绕一趟代理（顺带少一处凭证经手方）。
        self.trust_env = (_env_off("LLM_NO_PROXY") if trust_env is None else trust_env)
        self.http_factory = http_factory
        self.openai_factory = openai_factory
        # 「正在等 LLM」的唯一可见信号。没有它，一次 110 秒的正常起草和一次 30 分钟的
        # 静默停摆在日志里长得一模一样（都是什么都没有），运维无从判断该不该动手。
        self.on_call = on_call
        self.timeout = self.DEFAULT_TIMEOUT if timeout is None else timeout
        self.client_factory = client_factory or self._build_client
        self.on_failover = on_failover
        self.failovers: list[dict] = []
        self._clients: dict[str, object] = {}

    def _build_client(self, cfg: dict):
        make_openai = self.openai_factory
        if make_openai is None:
            from openai import OpenAI

            make_openai = OpenAI
        make = self.http_factory
        if make is None:
            import httpx
            make = httpx.Client
        http = make(
            # 自签 TLS 时传 ca_bundle；绝不 verify=False
            verify=self.ca_bundle or True,
            # 超时按**角色**取（起草要几分钟，机检 / 分诊几秒就够，一个数字盖不住）
            timeout=cfg.get("timeout") or self.timeout,
            trust_env=self.trust_env,
        )
        return make_openai(
            base_url=cfg["base_url"], api_key=cfg["api_key"], http_client=http,
            # **重试策略只能有一处**：我们的故障切换链（ADR-036）。SDK 默认 max_retries=2
            # 坐在它里面，效果是把每条线路的最坏耗时乘 3，**而且乘在超时上**：人配 300s，
            # 实际最坏 900s，主备两条就是 30 分钟，全程零日志（真跑第一条 e2e 时实测踩到）。
            max_retries=0,
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
            started = time.monotonic()
            self._note_call({"event": "start", "model_role": model_role, "link": i,
                             "model": cfg["model"], "base_url": cfg["base_url"],
                             "timeout": cfg.get("timeout") or self.timeout})
            try:
                resp = self._client(cfg).chat.completions.create(
                    model=cfg["model"],
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                )
            except Exception as exc:
                self._note_call({"event": "end", "model_role": model_role, "link": i,
                                 "ok": False, "seconds": round(time.monotonic() - started, 1),
                                 "error": f"{type(exc).__name__}: {exc}"})
                tried.append(f"{cfg['base_url']}/{cfg['model']}: {type(exc).__name__}: {exc}")
                if not _can_fail_over(exc):
                    raise            # 请求本身有问题，换线路无益
                if i == len(chain) - 1:
                    raise LLMUnavailable(
                        f"角色 {model_role} 的 {len(chain)} 条线路全部打不通："
                        + "｜".join(tried)) from exc
                continue
            self._note_call({"event": "end", "model_role": model_role, "link": i,
                             "ok": True, "seconds": round(time.monotonic() - started, 1)})
            if i:
                self._note_failover(model_role, i, len(chain), tried)
            return resp.choices[0].message.content or ""
        raise LLMUnavailable(f"角色 {model_role} 没有任何可用线路")   # 链为空（装配期已挡）

    def _note_call(self, record: dict) -> None:
        if self.on_call is None:
            return
        try:
            self.on_call(record)
        except Exception:      # 观测钩子炸了，不许把一次已经成功的生成带走
            pass

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
