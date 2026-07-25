"""LLM 超时按角色可配（实测逼出来的：默认 60s 装不下真实起草）。

2026-07-26 用真配置量了一次「起草合同商务条款」：**109.7s / 2570 字**，而当时默认
`timeout=60`。真跑时 `biz_draft` 会被 httpx 掐断，且那一刻飞书文档已建、任务已派 ——
属于最难排查的一类失败（人看到的是「AI 那步失败了」，日志里是个 ReadTimeout）。

一个数字盖不住所有角色：起草要几分钟，机检 / 分诊几秒就够。所以按角色可配。
"""
from __future__ import annotations

from larkflow.config import load_llm_roles
from larkflow.llm import OpenAICompatLLM

BASE = {"LLM_BASE_URL": "https://ark", "LLM_API_KEY": "k0", "LLM_MODEL": "m0"}


def test_a_role_can_carry_its_own_timeout():
    roles = load_llm_roles({**BASE, "LLM_WRITER_BASE_URL": "https://ark",
                            "LLM_WRITER_API_KEY": "k1", "LLM_WRITER_MODEL": "m1",
                            "LLM_WRITER_TIMEOUT": "240"})
    assert roles["writer"]["timeout"] == 240.0
    assert "timeout" not in roles["default"], "没配的角色不该凭空多一个键"


def test_the_global_timeout_covers_the_default_role():
    roles = load_llm_roles({**BASE, "LLM_TIMEOUT": "180"})
    assert roles["default"]["timeout"] == 180.0


def test_a_backup_inherits_the_timeout():
    """备用线路慢不慢与主线路无关，但没人会想为它单独再写一遍。"""
    roles = load_llm_roles({**BASE, "LLM_TIMEOUT": "180", "LLM_BACKUP_API_KEY": "k0b"})
    assert roles["default"]["fallbacks"][0]["timeout"] == 180.0


def test_a_backup_may_override_the_timeout():
    roles = load_llm_roles({**BASE, "LLM_TIMEOUT": "180",
                            "LLM_BACKUP_API_KEY": "k0b", "LLM_BACKUP_TIMEOUT": "30"})
    assert roles["default"]["timeout"] == 180.0
    assert roles["default"]["fallbacks"][0]["timeout"] == 30.0


def test_a_junk_timeout_is_ignored_rather_than_crashing_assembly():
    """env 里写错一个字不该让整个服务起不来，而是退回默认值。"""
    for junk in ("", "abc", "-5", "0"):
        roles = load_llm_roles({**BASE, "LLM_TIMEOUT": junk})
        assert "timeout" not in roles["default"], junk


def test_the_default_is_wide_enough_for_a_real_draft():
    """实测 109.7s。默认值必须**明显**大于它，别卡在边界上。"""
    assert OpenAICompatLLM.DEFAULT_TIMEOUT >= 240


def test_the_per_role_timeout_reaches_the_client():
    seen = []

    def factory(cfg):
        seen.append(cfg.get("timeout"))
        raise RuntimeError("到这儿就够了，不用真建客户端")

    roles = {"writer": {"base_url": "https://ark", "api_key": "k1", "model": "m1",
                        "timeout": 240}}
    llm = OpenAICompatLLM(roles, client_factory=factory)
    try:
        llm.complete(prompt="p", model_role="writer")
    except Exception:
        pass
    assert seen == [240], "角色配的超时没传到客户端，等于没配"
