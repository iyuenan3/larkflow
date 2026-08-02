from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import subprocess
from typing import Any

import pytest

import larkflow.workflow.edge_client as edge_client
from larkflow.workflow.edge_client import (
    CodexReadonlyExecutor,
    EdgeLeasePayload,
    EdgeTransportError,
    EdgeWorker,
    HttpEdgeTransport,
    StoredEdgeCredential,
    load_edge_credential,
    save_edge_credential,
    validate_edge_server_url,
)


NOW = datetime(2026, 8, 2, 8, tzinfo=timezone.utc)


def lease_payload(*, expires_at: datetime | None = None) -> EdgeLeasePayload:
    return EdgeLeasePayload(
        device_id="device_1",
        tenant_id="tenant_1",
        instance_id="instance_1",
        node_key="local_review",
        attempt_id="attempt_1",
        attempt_no=1,
        owner_person_id="person_1",
        executor="agent",
        work={
            "objective": "Review the current workspace",
            "acceptance": ["Return a concise summary"],
            "agent": {
                "kind": "personal.readonly",
                "instructions": "Read only relevant files",
            },
        },
        input_snapshot={"submitted": {"topic": "Edge"}},
        expected_node_version=2,
        claim_token="claim_1",
        claim_expires_at=expires_at or NOW + timedelta(minutes=10),
        idempotency_key="tenant_1:attempt_1",
    )


def test_server_url_requires_https_except_loopback():
    assert validate_edge_server_url("https://edge.example.com/") == (
        "https://edge.example.com"
    )
    assert validate_edge_server_url("http://127.0.0.1:8765") == (
        "http://127.0.0.1:8765"
    )

    for value in (
        "http://edge.example.com",
        "https://user:secret@edge.example.com",
        "https://edge.example.com/private",
        "https://edge.example.com?token=secret",
    ):
        with pytest.raises(ValueError):
            validate_edge_server_url(value)


def test_credential_file_is_created_once_with_private_permissions(tmp_path: Path):
    target = tmp_path / "device.json"
    credential = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )

    save_edge_credential(target, credential)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert load_edge_credential(target) == credential
    with pytest.raises(FileExistsError):
        save_edge_credential(target, credential)


def test_credential_loader_rejects_loose_permissions_and_symlinks(tmp_path: Path):
    target = tmp_path / "device.json"
    target.write_text(
        json.dumps(
            {
                "server_url": "https://edge.example.com",
                "device_id": "device_1",
                "credential": "device_1.secret",
            }
        ),
        encoding="utf-8",
    )
    target.chmod(0o644)
    with pytest.raises(PermissionError):
        load_edge_credential(target)

    target.chmod(0o600)
    link = tmp_path / "device-link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_edge_credential(link)

    target.write_text(
        json.dumps(
            {
                "server_url": "https://edge.example.com",
                "device_id": "device_other",
                "credential": "device_1.secret",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="stored device id"):
        load_edge_credential(target)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeHttpClient:
    def __init__(self, recorder: dict[str, Any], response: FakeResponse, **kwargs: Any):
        self.recorder = recorder
        self.response = response
        recorder["client"] = kwargs

    def __enter__(self) -> FakeHttpClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def post(self, path: str, **kwargs: Any) -> FakeResponse:
        self.recorder["post"] = {"path": path, **kwargs}
        return self.response


def test_http_transport_does_not_follow_redirects_or_inherit_proxy():
    recorder: dict[str, Any] = {}
    response = FakeResponse(204)
    transport = HttpEdgeTransport(
        "https://edge.example.com",
        credential="device_1.secret",
        client_factory=lambda **kwargs: FakeHttpClient(
            recorder,
            response,
            **kwargs,
        ),
    )

    assert transport.claim(wait_seconds=3) is None
    assert recorder["client"]["follow_redirects"] is False
    assert recorder["client"]["trust_env"] is False
    assert recorder["post"]["headers"]["Authorization"] == (
        "Bearer device_1.secret"
    )
    with pytest.raises(ValueError, match="between 0 and 25"):
        transport.claim(wait_seconds=26)


def test_http_transport_surfaces_stable_server_error():
    response = FakeResponse(
        409,
        {"error": {"code": "stale_lease", "message": "no longer current"}},
    )
    transport = HttpEdgeTransport(
        "https://edge.example.com",
        credential="device_1.secret",
        client_factory=lambda **kwargs: FakeHttpClient({}, response, **kwargs),
    )

    with pytest.raises(EdgeTransportError) as error:
        transport.complete(lease_payload(), {"content": "late"})
    assert error.value.status == 409
    assert error.value.code == "stale_lease"


def test_http_transport_rejects_redirect_responses_even_with_json():
    response = FakeResponse(
        307,
        {"claim_expires_at": (NOW + timedelta(minutes=10)).isoformat()},
    )
    transport = HttpEdgeTransport(
        "https://edge.example.com",
        credential="device_1.secret",
        client_factory=lambda **kwargs: FakeHttpClient({}, response, **kwargs),
    )

    with pytest.raises(EdgeTransportError) as error:
        transport.renew(lease_payload())
    assert error.value.status == 307


class FakeProcess:
    def __init__(self, stdout: str = "reviewed") -> None:
        self.stdout = stdout
        self.returncode = 0
        self.pid = None
        self.input: str | None = None
        self.timeout: float | None = None

    def communicate(self, value: str | None = None, timeout: float | None = None):
        self.input = value
        self.timeout = timeout
        return self.stdout, ""


def test_codex_adapter_uses_ephemeral_readonly_sandbox_and_scrubs_edge_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    process = FakeProcess()
    invocation: dict[str, Any] = {}

    def popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        invocation["argv"] = argv
        invocation["kwargs"] = kwargs
        return process

    monkeypatch.setenv("LARKFLOW_EDGE_CREDENTIAL", "must-not-leak")
    monkeypatch.setenv("LARKFLOW_TARGET_DSN", "must-not-leak")
    monkeypatch.setenv("LARK_APP_SECRET", "must-not-leak")
    monkeypatch.setenv("UNRELATED_API_KEY", "must-not-leak")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("PATH", "/safe/path")
    executor = CodexReadonlyExecutor(
        tmp_path,
        codex_binary="/fake/codex",
        clock=lambda: NOW,
        popen_factory=popen,
    )

    result = executor.execute(lease_payload())

    argv = invocation["argv"]
    assert argv[:2] == ["/fake/codex", "exec"]
    assert ["--sandbox", "read-only"] == argv[2:4]
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv
    assert "--skip-git-repo-check" in argv
    assert not any("dangerously" in item for item in argv)
    assert invocation["kwargs"]["start_new_session"] is True
    assert invocation["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert invocation["kwargs"]["env"]["PATH"] == "/safe/path"
    assert "UNRELATED_API_KEY" not in invocation["kwargs"]["env"]
    assert "LARKFLOW_EDGE_CREDENTIAL" not in invocation["kwargs"]["env"]
    assert "LARKFLOW_TARGET_DSN" not in invocation["kwargs"]["env"]
    assert "LARK_APP_SECRET" not in invocation["kwargs"]["env"]
    assert "HTTPS_PROXY" not in invocation["kwargs"]["env"]
    assert result["content"] == "reviewed"
    assert "只读" in (process.input or "")


def test_codex_adapter_can_explicitly_inherit_only_credential_free_loopback_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    invocation: dict[str, Any] = {}

    def popen(_argv: list[str], **kwargs: Any) -> FakeProcess:
        invocation["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@127.0.0.1:7897")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.example.com:1080")
    executor = CodexReadonlyExecutor(
        tmp_path,
        codex_binary="/fake/codex",
        inherit_loopback_proxy=True,
        clock=lambda: NOW,
        popen_factory=popen,
    )

    executor.execute(lease_payload())

    assert invocation["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert "HTTP_PROXY" not in invocation["env"]
    assert "ALL_PROXY" not in invocation["env"]


def test_codex_timeout_terminates_the_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    process = FakeProcess()

    def communicate(_value: str | None = None, timeout: float | None = None):
        raise subprocess.TimeoutExpired("codex", timeout)

    process.communicate = communicate  # type: ignore[method-assign]
    terminated: list[Any] = []
    monkeypatch.setattr(
        edge_client,
        "_terminate_process_group",
        lambda item: terminated.append(item),
    )
    executor = CodexReadonlyExecutor(
        tmp_path,
        codex_binary="/fake/codex",
        clock=lambda: NOW,
        popen_factory=lambda *_args, **_kwargs: process,
    )

    with pytest.raises(TimeoutError):
        executor.execute(lease_payload())
    assert terminated == [process]


def test_codex_communication_error_also_terminates_the_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    process = FakeProcess()

    def communicate(_value: str | None = None, timeout: float | None = None):
        raise OSError("broken output pipe")

    process.communicate = communicate  # type: ignore[method-assign]
    terminated: list[Any] = []
    monkeypatch.setattr(
        edge_client,
        "_terminate_process_group",
        lambda item: terminated.append(item),
    )
    executor = CodexReadonlyExecutor(
        tmp_path,
        codex_binary="/fake/codex",
        clock=lambda: NOW,
        popen_factory=lambda *_args, **_kwargs: process,
    )

    with pytest.raises(OSError, match="broken output"):
        executor.execute(lease_payload())
    assert terminated == [process]


class FakeTransport:
    def __init__(self, lease: EdgeLeasePayload | None) -> None:
        self.lease = lease
        self.completed: list[Any] = []
        self.failed: list[Any] = []

    def claim(self, *, wait_seconds: float = 0) -> EdgeLeasePayload | None:
        return self.lease

    def renew(self, lease: EdgeLeasePayload) -> datetime:
        return lease.claim_expires_at + timedelta(minutes=1)

    def complete(self, lease: EdgeLeasePayload, result: Any) -> None:
        self.completed.append((lease, result))

    def fail(self, lease: EdgeLeasePayload, **kwargs: Any) -> None:
        self.failed.append((lease, kwargs))


def test_local_executor_error_does_not_fail_the_business_workflow():
    transport = FakeTransport(lease_payload())

    class BrokenExecutor:
        def execute(self, _lease: EdgeLeasePayload):
            raise OSError("local setup is unavailable")

    report = EdgeWorker(transport, BrokenExecutor()).run_once()

    assert report.status == "executor_error"
    assert report.error == "OSError"
    assert transport.completed == []
    assert transport.failed == []
