"""Doubao Custom Search adapter contracts without real network access."""
from __future__ import annotations

import pytest

from larkflow.search import (
    DOUBAO_SEARCH_ENDPOINT,
    DoubaoSearchConfig,
    DoubaoSearchProvider,
    SearchEvidenceMissingError,
    SearchProtocolError,
    SearchQuotaExhaustedError,
    SearchRateLimitedError,
    SearchTransportError,
    SearchTimeoutError,
)


def response(*, results=None):
    return {
        "ResponseMetadata": {"RequestId": "request-safe"},
        "Result": {
            "ResultCount": 2,
            "TimeCost": 37,
            "WebResults": results
            if results is not None
            else [
                {
                    "Title": "苏州博物馆参观须知",
                    "Summary": "开放与预约信息。",
                    "Url": "https://www.szmuseum.com/guide",
                    "PublishTime": "2026-08-01",
                },
                {
                    "Title": "交通信息",
                    "Snippet": "公共交通线路说明。",
                    "Url": "https://jtj.suzhou.gov.cn/transit",
                },
            ],
        },
    }


def test_preflight_requires_only_the_custom_api_key():
    absent = DoubaoSearchProvider.preflight({})
    partial = DoubaoSearchProvider.preflight(
        {"LARKFLOW_DOUBAO_SEARCH_API_ID": "resource-id"}
    )
    ready = DoubaoSearchProvider.preflight(
        {"LARKFLOW_DOUBAO_SEARCH_API_KEY": "secret-key"}
    )

    assert (absent.configured, absent.available, absent.reason) == (
        False,
        False,
        "api_key_missing",
    )
    assert (partial.configured, partial.available, partial.reason) == (
        True,
        False,
        "api_key_missing",
    )
    assert (ready.configured, ready.available, ready.reason) == (
        True,
        True,
        "configured",
    )


def test_search_normalizes_source_evidence_usage_and_unknown_publish_time():
    calls = []

    def transport(url, **kwargs):
        calls.append((url, kwargs))
        return response()

    provider = DoubaoSearchProvider(
        DoubaoSearchConfig(api_key="secret-key"),
        transport=transport,
    )
    result = provider.search(query="苏州景点预约规则")

    assert result.provider == "doubao_search_custom"
    assert result.query == "苏州景点预约规则"
    assert result.error is None
    assert result.sources[0].published_at == "2026-08-01"
    assert result.sources[0].published_at_status == "known"
    assert result.sources[1].published_at is None
    assert result.sources[1].published_at_status == "unknown"
    assert result.sources[0].health == "unknown"
    assert result.sources[0].authority == "unknown"
    assert result.sources[0].support == "unknown"
    assert result.usage.as_dict() == {
        "result_count": 2,
        "time_cost_ms": 37,
        "provider_request_id": "request-safe",
    }
    url, request = calls[0]
    assert url == DOUBAO_SEARCH_ENDPOINT
    assert request["json"] == {
        "Query": "苏州景点预约规则",
        "SearchType": "web",
        "Count": 10,
        "NeedSummary": True,
    }


def test_search_deduplicates_urls_and_fails_closed_without_verifiable_sources():
    duplicate = {
        "Title": "重复来源",
        "Summary": "重复内容",
        "Url": "https://example.com/source",
    }
    provider = DoubaoSearchProvider(
        DoubaoSearchConfig(api_key="secret-key"),
        transport=lambda *_args, **_kwargs: response(
            results=[duplicate, duplicate, {"Url": "file:///etc/passwd"}]
        ),
    )

    assert len(provider.search(query="核对来源").sources) == 1

    normalized_duplicate = DoubaoSearchProvider(
        DoubaoSearchConfig(api_key="secret-key"),
        transport=lambda *_args, **_kwargs: response(
            results=[
                {**duplicate, "Url": "HTTPS://EXAMPLE.COM:443/source#one"},
                {**duplicate, "Url": "https://example.com/source"},
            ]
        ),
    )
    assert len(normalized_duplicate.search(query="核对来源").sources) == 1

    empty = DoubaoSearchProvider(
        DoubaoSearchConfig(api_key="secret-key"),
        transport=lambda *_args, **_kwargs: response(
            results=[{"Title": "无 URL"}, {"Url": "javascript:alert(1)"}]
        ),
    )
    with pytest.raises(SearchEvidenceMissingError, match="source URLs"):
        empty.search(query="核对来源")


def test_provider_errors_are_classified_and_never_include_the_api_key():
    key = "never-leak-this-key"
    assert key not in repr(DoubaoSearchConfig(api_key=key))

    rejected = DoubaoSearchProvider(
        DoubaoSearchConfig(api_key=key),
        transport=lambda *_args, **_kwargs: {
            "ResponseMetadata": {"Error": {"Code": "10403", "Message": key}}
        },
    )
    with pytest.raises(SearchProtocolError) as rejected_error:
        rejected.search(query="核对来源")
    assert rejected_error.value.error_code == "search_protocol_error"
    assert key not in str(rejected_error.value)

    def unavailable(*_args, **_kwargs):
        raise PermissionError(key)

    failed = DoubaoSearchProvider(
        DoubaoSearchConfig(api_key=key),
        transport=unavailable,
    )
    with pytest.raises(SearchTransportError) as transport_error:
        failed.search(query="核对来源")
    assert transport_error.value.error_code == "search_transport_error"
    assert key not in str(transport_error.value)


@pytest.mark.parametrize(
    ("response_code", "error_type", "error_code"),
    [
        ("QuotaExhausted", SearchQuotaExhaustedError, "search_quota_exhausted"),
        ("TooManyRequests429", SearchRateLimitedError, "search_rate_limited"),
    ],
)
def test_provider_rejections_have_stable_actionable_error_codes(
    response_code,
    error_type,
    error_code,
):
    provider = DoubaoSearchProvider(
        DoubaoSearchConfig(api_key="secret-key"),
        transport=lambda *_args, **_kwargs: {
            "ResponseMetadata": {"Error": {"Code": response_code}}
        },
    )

    with pytest.raises(error_type) as error:
        provider.search(query="核对来源")
    assert error.value.error_code == error_code


def test_provider_timeout_has_a_stable_error_code():
    provider = DoubaoSearchProvider(
        DoubaoSearchConfig(api_key="secret-key"),
        transport=lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    with pytest.raises(SearchTimeoutError) as error:
        provider.search(query="核对来源")
    assert error.value.error_code == "search_timeout"


@pytest.mark.parametrize("query", ["", "x" * 101])
def test_query_bounds_fail_before_transport(query):
    calls = []
    provider = DoubaoSearchProvider(
        DoubaoSearchConfig(api_key="secret-key"),
        transport=lambda *_args, **_kwargs: calls.append(True),
    )

    with pytest.raises(ValueError):
        provider.search(query=query)

    assert calls == []


def test_search_target_modules_do_not_import_langgraph():
    from pathlib import Path

    root = Path(__file__).parents[1] / "larkflow" / "search"
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py"))
    )
    assert "langgraph" not in combined
