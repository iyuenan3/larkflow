"""LLM 出站是否吃环境里的代理（本机 Clash 的 socks all_proxy 会让 httpx 直接建不起来）。"""
from larkflow.llm import OpenAICompatLLM

ROLES = {"default": {"base_url": "https://x", "api_key": "k", "model": "m"}}


def test_by_default_the_client_still_honours_the_environment():
    """默认不改变标准行为：有人的 LLM 在墙外、确实要走代理。"""
    seen = {}
    OpenAICompatLLM(ROLES, http_factory=lambda **kw: seen.update(kw))._build_client(
        ROLES["default"])
    assert seen.get("trust_env") is True


def test_no_proxy_makes_the_client_ignore_the_environment():
    """本机 Clash 把 all_proxy 设成 socks5://…，httpx **建客户端那一刻**就会因为缺
    socksio 抛 ImportError。实测 no_proxy 救不了（socks 传输是急切构造的），
    只有 trust_env=False 能。而方舟是境内服务，本来也不该绕一趟代理。
    """
    seen = {}
    OpenAICompatLLM(ROLES, trust_env=False,
                    http_factory=lambda **kw: seen.update(kw))._build_client(ROLES["default"])
    assert seen.get("trust_env") is False


def test_the_env_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("LLM_NO_PROXY", "1")
    assert OpenAICompatLLM(ROLES).trust_env is False
    monkeypatch.setenv("LLM_NO_PROXY", "0")
    assert OpenAICompatLLM(ROLES).trust_env is True
