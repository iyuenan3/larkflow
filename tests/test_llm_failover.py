"""LLM 备用线路：主供应商掉线时自动切备用（ADR-017 的可用性补充）。

为什么值得做：一个 llm 节点跑挂 = 那一支的产出没了，而这条图上游可能已经花了真人的时间。
LLM 供应商掉线 / 限流 / key 过期是常态不是异常，不该让它变成「整个项目停在半截」。

env 约定（见 .env.example）：
    LLM_<ROLE>_BASE_URL / _API_KEY / _MODEL                 主
    LLM_<ROLE>_BACKUP_BASE_URL / _API_KEY / _MODEL          备（缺项继承主）
    LLM_<ROLE>_BACKUP2_* / BACKUP3_* …                      再备，按序号排队
"""
from __future__ import annotations

import pytest

from larkflow.config import load_llm_roles
from larkflow.llm import LLMUnavailable, OpenAICompatLLM

PRIMARY = {"LLM_WRITER_BASE_URL": "https://ark", "LLM_WRITER_API_KEY": "k1",
           "LLM_WRITER_MODEL": "m1"}


# ---------- 装配 ----------

def test_a_full_backup_triple_becomes_a_second_link():
    roles = load_llm_roles({**PRIMARY,
                            "LLM_WRITER_BACKUP_BASE_URL": "https://relay",
                            "LLM_WRITER_BACKUP_API_KEY": "k2",
                            "LLM_WRITER_BACKUP_MODEL": "m2"})
    assert roles["writer"]["base_url"] == "https://ark"
    assert roles["writer"]["fallbacks"] == [
        {"base_url": "https://relay", "api_key": "k2", "model": "m2"}]


def test_a_backup_key_alone_means_same_endpoint_different_key():
    """最常见的用法：同一个供应商配两把 key，一把被限流了换另一把。

    只写 BACKUP_API_KEY 时，base_url / model 继承主配置。要求人重复填三遍才能换把 key，
    等于逼他复制粘贴，改主配置时还会漏改备用的那份。
    """
    roles = load_llm_roles({**PRIMARY, "LLM_WRITER_BACKUP_API_KEY": "k2"})
    assert roles["writer"]["fallbacks"] == [
        {"base_url": "https://ark", "api_key": "k2", "model": "m1"}]


def test_backups_are_ordered_by_their_number():
    roles = load_llm_roles({**PRIMARY,
                            "LLM_WRITER_BACKUP3_API_KEY": "k4",
                            "LLM_WRITER_BACKUP_API_KEY": "k2",
                            "LLM_WRITER_BACKUP2_API_KEY": "k3"})
    assert [f["api_key"] for f in roles["writer"]["fallbacks"]] == ["k2", "k3", "k4"]


def test_the_default_role_can_have_backups_too():
    roles = load_llm_roles({"LLM_BASE_URL": "https://ark", "LLM_API_KEY": "k0",
                            "LLM_MODEL": "m0", "LLM_BACKUP_API_KEY": "k0b"})
    assert roles["default"]["fallbacks"] == [
        {"base_url": "https://ark", "api_key": "k0b", "model": "m0"}]


def test_a_backup_never_invents_a_role_of_its_own():
    """`LLM_WRITER_BACKUP_BASE_URL` 长得就像「角色 writer_backup 的主配置」。

    角色正则若不排除 BACKUP 段，会凭空多出一个 `writer_backup` 角色：模板里没人用它，
    于是备用线路静默失效，而配置看起来完全正常。
    """
    roles = load_llm_roles({**PRIMARY,
                            "LLM_WRITER_BACKUP_BASE_URL": "https://relay",
                            "LLM_WRITER_BACKUP_API_KEY": "k2"})
    assert set(roles) == {"writer"}


def test_a_backup_without_a_working_primary_is_dropped_with_the_role():
    """主配置三元组不全 → 整个角色跳过（既有语义），它的备用也一并丢掉，不许自己上位。"""
    roles = load_llm_roles({"LLM_WRITER_BASE_URL": "https://ark",   # 缺 key
                            "LLM_WRITER_BACKUP_BASE_URL": "https://relay",
                            "LLM_WRITER_BACKUP_API_KEY": "k2",
                            "LLM_WRITER_BACKUP_MODEL": "m2"})
    assert roles == {}


def test_no_backup_means_no_fallbacks_key():
    """没配备用时形状与从前逐字一致，别给所有既有配置凭空加一个空列表。"""
    assert load_llm_roles(PRIMARY)["writer"] == {
        "base_url": "https://ark", "api_key": "k1", "model": "m1"}


# ---------- 运行时切换 ----------

class _Boom(Exception):
    def __init__(self, status=None):
        super().__init__(f"boom {status}")
        self.status_code = status


class FakeClient:
    """最小的 OpenAI 兼容替身：要么按剧本抛，要么回一句带自己身份的正文。"""

    def __init__(self, cfg, script=None):
        self.cfg, self.script, self.calls = cfg, list(script or []), 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, *, model, temperature, messages):
        self.calls += 1
        if self.script:
            raise self.script.pop(0)
        text = f"{self.cfg['api_key']}@{self.cfg['base_url']}/{model}"
        return type("R", (), {"choices": [type("C", (), {
            "message": type("M", (), {"content": text})()})()]})()


def build(roles, scripts=None):
    scripts = scripts or {}
    made = {}

    def factory(cfg):
        c = FakeClient(cfg, scripts.get(cfg["api_key"]))
        made[cfg["api_key"]] = c
        return c

    seen = []
    llm = OpenAICompatLLM(roles, client_factory=factory,
                          on_failover=lambda rec: seen.append(rec))
    return llm, made, seen


CHAIN = {"writer": {"base_url": "https://ark", "api_key": "k1", "model": "m1",
                    "fallbacks": [{"base_url": "https://relay", "api_key": "k2", "model": "m2"}]}}


def test_a_healthy_primary_is_used_and_the_backup_is_never_touched():
    llm, made, seen = build(CHAIN)
    assert llm.complete(prompt="p", model_role="writer") == "k1@https://ark/m1"
    assert "k2" not in made and seen == []


def test_a_dead_primary_falls_through_to_the_backup():
    llm, made, seen = build(CHAIN, {"k1": [_Boom(503)]})
    assert llm.complete(prompt="p", model_role="writer") == "k2@https://relay/m2"
    assert len(seen) == 1 and seen[0]["model_role"] == "writer"
    assert seen[0]["used"] == 1, "记下用的是链上第几条，好让人看出主线路挂了"


def test_transport_errors_without_a_status_also_fail_over():
    """连不上 / 超时这类根本没有 HTTP 状态码的错误，正是「掉线」的主要形态。"""
    llm, _made, _seen = build(CHAIN, {"k1": [ConnectionError("connection reset")]})
    assert llm.complete(prompt="p", model_role="writer") == "k2@https://relay/m2"


def test_rate_limit_and_server_errors_fail_over():
    for status in (429, 500, 502, 503, 401):
        llm, _made, _seen = build(CHAIN, {"k1": [_Boom(status)]})
        assert llm.complete(prompt="p", model_role="writer").startswith("k2@"), status


def test_a_bad_request_does_not_fail_over():
    """400 / 422 是**我们自己的请求**有问题，换条线路只会原样再错一次，还多烧一次钱。"""
    for status in (400, 422):
        llm, made, _seen = build(CHAIN, {"k1": [_Boom(status)]})
        with pytest.raises(Exception):
            llm.complete(prompt="p", model_role="writer")
        assert "k2" not in made, f"{status} 不该切备用"


def test_when_every_link_is_down_the_error_says_so():
    llm, _made, _seen = build(CHAIN, {"k1": [_Boom(503)], "k2": [_Boom(503)]})
    with pytest.raises(LLMUnavailable) as e:
        llm.complete(prompt="p", model_role="writer")
    assert "writer" in str(e.value) and "2" in str(e.value)


def test_the_same_endpoint_with_two_keys_gets_two_clients():
    """客户端缓存若只按 (base_url, model) 做键，同端点换 key 会命中主线路那个缓存对象，
    于是「换把 key 重试」变成「拿同一把挂掉的 key 再试一次」，备用形同虚设。"""
    roles = {"writer": {"base_url": "https://ark", "api_key": "k1", "model": "m1",
                        "fallbacks": [{"base_url": "https://ark", "api_key": "k2",
                                       "model": "m1"}]}}
    llm, made, _seen = build(roles, {"k1": [_Boom(429)]})
    assert llm.complete(prompt="p", model_role="writer") == "k2@https://ark/m1"
    assert set(made) == {"k1", "k2"}


def test_an_unconfigured_role_borrows_the_default_chain_backups_included():
    roles = {"default": {"base_url": "https://ark", "api_key": "k1", "model": "m1",
                         "fallbacks": [{"base_url": "https://relay", "api_key": "k2",
                                        "model": "m2"}]}}
    llm, _made, _seen = build(roles, {"k1": [_Boom(503)]})
    assert llm.complete(prompt="p", model_role="没配过的角色") == "k2@https://relay/m2"
