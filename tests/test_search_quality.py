"""Provider-neutral source quality and claim support contracts."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from larkflow.search import (
    DisabledSafeOutboundFetcher,
    OutboundFetchResult,
    SourceQualityPolicy,
    normalize_source_records,
    normalize_source_url,
    source_freshness,
    validate_claim_support,
)


class StubFetcher:
    def __init__(self, *, health: str, final_url: str | None = None) -> None:
        self.health = health
        self.final_url = final_url
        self.calls: list[str] = []

    def capability(self) -> tuple[bool, str]:
        return True, "stub_enabled"

    def check(self, *, url: str) -> OutboundFetchResult:
        self.calls.append(url)
        return OutboundFetchResult(
            health=self.health,
            reason="stub_observation",
            final_url=self.final_url or url,
        )


def record(**overrides):
    value = {
        "title": "公开资料",
        "snippet": "苏州博物馆实行预约参观。",
        "source_url": "https://example.com/guide",
        "published_at": "2026-08-01",
        "published_at_status": "known",
    }
    value.update(overrides)
    return value


def test_url_normalization_is_structural_and_does_not_claim_reachability():
    assert (
        normalize_source_url("HTTPS://Example.COM:443/guide#section")
        == "https://example.com/guide"
    )
    assert normalize_source_url("https://user:secret@example.com/guide") is None
    assert normalize_source_url("file:///etc/passwd") is None

    disabled = DisabledSafeOutboundFetcher()
    assert disabled.capability() == (
        False,
        "safe_outbound_fetcher_unavailable",
    )
    observation = disabled.check(url="http://127.0.0.1/private")
    assert observation.health == "unknown"
    assert observation.reason == "safe_outbound_fetcher_unavailable"


def test_default_outbound_module_contains_no_network_client_implementation():
    source = (
        Path(__file__).parents[1] / "larkflow" / "search" / "outbound.py"
    ).read_text(encoding="utf-8")

    assert "import httpx" not in source
    assert "import requests" not in source
    assert "urllib.request" not in source
    assert "socket." not in source


def test_freshness_requires_an_explicit_policy_and_parseable_date():
    no_policy = SourceQualityPolicy(as_of=date(2026, 8, 19))
    bounded = SourceQualityPolicy(
        as_of=date(2026, 8, 19),
        freshness_max_age_days=30,
    )

    assert source_freshness("2020-01-01", policy=no_policy) == "unknown"
    assert source_freshness("2026-08-01", policy=bounded) == "current"
    assert source_freshness("2026-01-01", policy=bounded) == "stale"
    assert source_freshness("时间不明", policy=bounded) == "unknown"


def test_source_records_upgrade_legacy_fields_and_keep_health_unknown_by_default():
    normalized = normalize_source_records(
        [record()],
        ["HTTPS://EXAMPLE.COM:443/guide#fragment"],
        policy=SourceQualityPolicy(
            as_of=date(2026, 8, 19),
            freshness_max_age_days=30,
        ),
    )

    assert normalized == (
        {
            "title": "公开资料",
            "snippet": "苏州博物馆实行预约参观。",
            "source_url": "https://example.com/guide",
            "published_at": "2026-08-01",
            "published_at_status": "known",
            "url_status": "valid",
            "health": "unknown",
            "health_reason": "safe_outbound_fetcher_unavailable",
            "freshness": "current",
            "authority": "unknown",
            "authority_basis": "not_assessed",
            "support": "unknown",
        },
    )


def test_enabled_fetcher_observation_is_separate_from_authority_and_truth():
    fetcher = StubFetcher(health="reachable")
    normalized = normalize_source_records(
        [record()],
        ["https://example.com/guide"],
        policy=SourceQualityPolicy(as_of=date(2026, 8, 19)),
        fetcher=fetcher,
    )

    assert fetcher.calls == ["https://example.com/guide"]
    assert normalized[0]["health"] == "reachable"
    assert normalized[0]["authority"] == "unknown"
    assert normalized[0]["support"] == "unknown"

    with pytest.raises(ValueError, match="domain heuristics"):
        normalize_source_records(
            [record(authority="government", authority_basis="domain_heuristic")],
            ["https://example.com/guide"],
            policy=SourceQualityPolicy(as_of=date(2026, 8, 19)),
        )


def test_claim_support_requires_current_url_and_exact_provider_excerpt():
    records = [record()]
    claims = [
        {
            "claim_id": "C1",
            "text": "需要预约",
            "source_url": "HTTPS://EXAMPLE.COM:443/guide#detail",
            "supporting_excerpt": "实行预约参观",
        }
    ]

    supported, violations = validate_claim_support(claims, records)
    assert violations == ()
    assert supported[0] == {
        "claim_id": "C1",
        "source_url": "https://example.com/guide",
        "supporting_excerpt": "实行预约参观",
        "support": "supported",
    }

    forged = [{**claims[0], "supporting_excerpt": "免费且无需预约"}]
    assert validate_claim_support(forged, records)[1] == (
        "claims[0].supporting_excerpt 不是 provider 原文片段",
    )
    replaced = [{**claims[0], "source_url": "https://other.example/guide"}]
    assert "不属于当前搜索 Attempt" in validate_claim_support(
        replaced, records
    )[1][0]


def test_claim_support_rejects_duplicates_and_oversized_fields():
    claims = [
        {
            "claim_id": "C1",
            "text": "需要预约",
            "source_url": "https://example.com/guide",
            "supporting_excerpt": "实行预约参观",
        },
        {
            "claim_id": "C1",
            "text": "x" * 1_001,
            "source_url": "https://example.com/guide",
            "supporting_excerpt": "实行预约参观",
        },
    ]
    _, violations = validate_claim_support(claims, [record()])

    assert "claim_id 重复：C1" in violations
