"""LLM 调用的重试归属与可观测性（真跑第一条 e2e 时被一个 30 分钟的静默停摆逼出来的）。

现场：`merge` 节点点了通过之后 **18 分钟**没有任何动静 —— 没有新 checkpoint、没有报错、
没有日志、CPU 近零。挖到最后是两件事叠在一起：

1. **openai SDK 默认 `max_retries=2`，而它坐在我们的故障切换里面。** 实测：超时设 2s，
   一次失败的调用实际耗 7.5s。按当时配置换算就是 `300s × 3 = 15 分钟`一条线路，
   主备两条 = 最坏 **30 分钟**才轮到那个节点失败。
2. **整条链路对「正在等 LLM」零可观测。** 一次 110 秒的正常起草和一次 30 分钟的静默停摆，
   在日志里长得一模一样（都是什么都没有），运维无从判断该不该动手。
"""
from __future__ import annotations

from larkflow.llm import OpenAICompatLLM

ROLES = {"writer": {"base_url": "https://x", "api_key": "k", "model": "m"}}


class _Rec:
    """记下 OpenAI(...) 收到的关键参数。"""

    def __init__(self):
        self.kw = {}

    def __call__(self, **kw):
        self.kw = kw
        return self


def build(**extra):
    rec = _Rec()
    llm = OpenAICompatLLM(ROLES, openai_factory=rec, http_factory=lambda **kw: kw, **extra)
    llm._build_client(ROLES["writer"])
    return rec


def test_the_sdk_does_not_retry_behind_our_back():
    """重试策略只能有一处，就是我们的故障切换链（ADR-036）。

    SDK 自己再重试 N 次，效果是把每条线路的最坏耗时乘以 N+1，而且**乘在超时上**：
    人配 300s，实际最坏 900s，配置与行为对不上，日志里也看不见那两次重试。
    """
    assert build().kw.get("max_retries") == 0


def test_a_slow_call_is_announced_before_and_after():
    """一次 110 秒的正常起草与一次 30 分钟的静默停摆，日志里必须长得不一样。

    没有这两行，运维手上唯一的判据是「等了很久」，而正常起草本来就要等很久。
    """
    seen = []
    llm = OpenAICompatLLM(ROLES, on_call=seen.append,
                          client_factory=lambda cfg: _FakeOK())
    llm.complete(prompt="p", model_role="writer")
    assert [r["event"] for r in seen] == ["start", "end"]
    assert seen[0]["model_role"] == "writer" and seen[0]["model"] == "m"
    assert seen[1]["ok"] is True and isinstance(seen[1]["seconds"], float)


def test_a_failed_call_is_announced_too_with_the_reason():
    seen = []
    llm = OpenAICompatLLM(ROLES, on_call=seen.append,
                          client_factory=lambda cfg: _FakeBoom())
    try:
        llm.complete(prompt="p", model_role="writer")
    except Exception:
        pass
    assert [r["event"] for r in seen] == ["start", "end"]
    assert seen[1]["ok"] is False and "Boom" in seen[1]["error"]


def test_the_reporter_never_takes_the_call_down_with_it():
    """观测钩子自己抛异常，不许把一次已经成功的生成搞砸。"""
    def boom(_rec):
        raise RuntimeError("日志系统炸了")

    llm = OpenAICompatLLM(ROLES, on_call=boom, client_factory=lambda cfg: _FakeOK())
    assert llm.complete(prompt="p", model_role="writer") == "ok"


class _FakeOK:
    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kw):
        return type("R", (), {"choices": [type("C", (), {
            "message": type("M", (), {"content": "ok"})()})()]})()


class _FakeBoom:
    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kw):
        raise RuntimeError("Boom")
