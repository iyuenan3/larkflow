"""Public Console request limiting and proxy-source boundary tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from larkflow.workflow.console_cli import _build_rate_limiter
from larkflow.workflow.console_http import (
    _SECURITY_HEADERS,
    _rate_limit_response,
)
from larkflow.workflow.console_rate_limit import ConsoleRequestRateLimiter


class MutableClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _limiter(clock: MutableClock, **overrides) -> ConsoleRequestRateLimiter:
    values = {
        "window_seconds": 60,
        "requests_per_client": 2,
        "auth_requests_per_client": 1,
        "admin_writes_per_client": 1,
        "workflow_writes_per_client": 1,
        "global_requests": 20,
        "max_client_keys": 20,
        "clock": clock,
        "hash_key": b"rate-limit-test-key-material",
    }
    values.update(overrides)
    return ConsoleRequestRateLimiter(**values)


def test_rate_limiter_separates_route_budgets_and_resets_after_window():
    clock = MutableClock(10.0)
    limiter = _limiter(clock)

    assert limiter.check("GET", "/console/auth/login", "203.0.113.1").allowed
    denied_auth = limiter.check(
        "GET",
        "/console/api/v1/auth",
        "203.0.113.1",
    )
    assert denied_auth.allowed is False
    assert denied_auth.policy == "auth"
    assert denied_auth.retry_after_seconds == 60

    assert limiter.check("GET", "/console/", "203.0.113.1").allowed
    assert limiter.check("GET", "/console/app.js", "203.0.113.1").allowed
    assert not limiter.check("GET", "/console/styles.css", "203.0.113.1").allowed

    assert limiter.check(
        "POST",
        "/console/api/v1/admin/sessions/abc/revoke-preview",
        "203.0.113.1",
    ).allowed
    assert not limiter.check(
        "POST",
        "/console/api/v1/admin/sessions/abc/revoke-preview",
        "203.0.113.1",
    ).allowed

    assert limiter.check(
        "POST",
        "/console/api/v1/instances/instance_a/resume",
        "203.0.113.1",
    ).allowed
    denied_workflow = limiter.check(
        "POST",
        "/console/api/v1/instances/instance_a/resume",
        "203.0.113.1",
    )
    assert denied_workflow.allowed is False
    assert denied_workflow.policy == "workflow_write"

    clock.value = 70.0
    assert limiter.check("GET", "/console/auth/login", "203.0.113.1").allowed
    assert limiter.check("GET", "/console/", "203.0.113.1").allowed


def test_global_budget_survives_client_churn_and_client_keys_are_hashed():
    clock = MutableClock()
    limiter = _limiter(
        clock,
        requests_per_client=1,
        global_requests=2,
        max_client_keys=1,
    )

    assert limiter.check("GET", "/console/", "203.0.113.1").allowed
    assert limiter.check("GET", "/console/", "203.0.113.2").allowed
    denied = limiter.check("GET", "/console/", "203.0.113.3")

    assert denied.allowed is False
    assert denied.policy == "global"
    assert len(limiter._clients) == 1
    stored_key = next(iter(limiter._clients))[1]
    assert isinstance(stored_key, bytes)
    assert b"203.0.113" not in stored_key


def test_invalid_client_sources_share_one_unknown_budget():
    clock = MutableClock()
    limiter = _limiter(
        clock,
        requests_per_client=1,
        global_requests=20,
    )

    assert limiter.check("GET", "/console/", "not-an-ip").allowed
    assert not limiter.check("GET", "/console/", "also-not-an-ip").allowed


def test_concurrent_requests_cannot_overrun_one_client_budget():
    clock = MutableClock()
    limiter = _limiter(
        clock,
        requests_per_client=10,
        global_requests=100,
    )

    with ThreadPoolExecutor(max_workers=16) as executor:
        decisions = tuple(
            executor.map(
                lambda _item: limiter.check("GET", "/console/", "203.0.113.1"),
                range(100),
            )
        )

    assert sum(decision.allowed for decision in decisions) == 10
    assert {decision.policy for decision in decisions if not decision.allowed} == {
        "read"
    }


def test_proxy_source_header_is_trusted_only_from_a_loopback_peer():
    clock = MutableClock()
    loopback_limiter = _limiter(
        clock,
        requests_per_client=1,
        global_requests=20,
    )
    first = _rate_limit_response(
        loopback_limiter,
        method="GET",
        target="/console/",
        headers={"X-Larkflow-Client-IP": "203.0.113.1"},
        peer_address="127.0.0.1",
    )
    second = _rate_limit_response(
        loopback_limiter,
        method="GET",
        target="/console/",
        headers={"X-Larkflow-Client-IP": "203.0.113.2"},
        peer_address="127.0.0.1",
    )
    assert first is None
    assert second is None

    direct_limiter = _limiter(
        clock,
        requests_per_client=1,
        global_requests=20,
    )
    assert _rate_limit_response(
        direct_limiter,
        method="GET",
        target="/console/",
        headers={"X-Larkflow-Client-IP": "203.0.113.1"},
        peer_address="198.51.100.10",
    ) is None
    denied = _rate_limit_response(
        direct_limiter,
        method="GET",
        target="/console/",
        headers={"X-Larkflow-Client-IP": "203.0.113.2"},
        peer_address="198.51.100.10",
    )
    assert denied is not None
    assert denied.status == 429
    assert denied.headers == {"Retry-After": "60"}
    assert json.loads(denied.body) == {
        "error": {
            "code": "rate_limited",
            "message": "request rate limit exceeded",
        }
    }


def test_feishu_rate_limit_configuration_is_bounded(monkeypatch):
    for key in (
        "LARKFLOW_CONSOLE_RATE_LIMIT_WINDOW_SECONDS",
        "LARKFLOW_CONSOLE_RATE_LIMIT_REQUESTS_PER_CLIENT",
        "LARKFLOW_CONSOLE_RATE_LIMIT_AUTH_REQUESTS_PER_CLIENT",
        "LARKFLOW_CONSOLE_RATE_LIMIT_ADMIN_WRITES_PER_CLIENT",
        "LARKFLOW_CONSOLE_RATE_LIMIT_WORKFLOW_WRITES_PER_CLIENT",
        "LARKFLOW_CONSOLE_RATE_LIMIT_GLOBAL_REQUESTS",
    ):
        monkeypatch.delenv(key, raising=False)

    limiter = _build_rate_limiter()
    assert limiter.window_seconds == 60
    assert limiter.requests_per_client == 300
    assert limiter.auth_requests_per_client == 30
    assert limiter.admin_writes_per_client == 30
    assert limiter.workflow_writes_per_client == 60
    assert limiter.global_requests == 3_000

    monkeypatch.setenv("LARKFLOW_CONSOLE_RATE_LIMIT_WINDOW_SECONDS", "9")
    with pytest.raises(ValueError, match="between 10 and 3600"):
        _build_rate_limiter()


def test_security_headers_cover_browser_isolation_and_unused_capabilities():
    assert _SECURITY_HEADERS["Content-Security-Policy"].endswith(
        "frame-ancestors 'none'"
    )
    assert _SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert _SECURITY_HEADERS["Cross-Origin-Opener-Policy"] == "same-origin"
    assert _SECURITY_HEADERS["Cross-Origin-Resource-Policy"] == "same-origin"
    permissions = _SECURITY_HEADERS["Permissions-Policy"]
    for capability in ("camera=()", "geolocation=()", "microphone=()", "usb=()"):
        assert capability in permissions
