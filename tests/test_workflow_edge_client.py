from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import subprocess
from threading import Event, Thread
from typing import Any

import httpx
import pytest

import larkflow.workflow.edge_client as edge_client
from larkflow.workflow.edge_agent import EdgeAgentLoop
from larkflow.workflow.edge_client import (
    CodexReadonlyExecutor,
    EdgeCredentialNotKeychainReferenceError,
    EdgeDeviceLock,
    EdgeExecutionCancelled,
    EdgeKeychainCredentialNotFoundError,
    EdgeKeychainReference,
    EdgeLeasePayload,
    EdgeTransportError,
    EdgeWorker,
    EdgeWorkerReport,
    HttpEdgeTransport,
    StoredEdgeCredential,
    load_edge_keychain_credential,
    load_edge_keychain_reference,
    load_edge_credential,
    replace_edge_credential_with_keychain_reference,
    save_edge_keychain_credential,
    save_edge_keychain_reference,
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


def test_keychain_writer_keeps_secret_out_of_process_argv(monkeypatch):
    stored = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(edge_client, "_require_macos_keychain", lambda: None)
    monkeypatch.setattr(
        edge_client,
        "edge_keychain_credential_exists",
        lambda **_kwargs: False,
    )

    def write(argv: list[str], password: str) -> None:
        captured["argv"] = argv
        captured["password"] = password

    monkeypatch.setattr(edge_client, "_security_password_prompt", write)

    save_edge_keychain_credential(stored)

    argv = captured["argv"]
    assert argv[-1] == "-w"
    assert stored.credential not in argv
    assert captured["password"] == stored.credential

    oversized = StoredEdgeCredential(
        server_url=stored.server_url,
        device_id="device_1",
        credential=f"device_1.{('s' * 121)}",
    )
    with pytest.raises(ValueError, match="too large"):
        save_edge_keychain_credential(oversized)

    unsafe = StoredEdgeCredential(
        server_url=stored.server_url,
        device_id="device_1",
        credential="device_1.secret\twith-control",
    )
    with pytest.raises(ValueError, match="printable ASCII"):
        save_edge_keychain_credential(unsafe)


def test_keychain_loader_validates_payload_and_maps_missing_item(monkeypatch):
    stored = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )
    reference = EdgeKeychainReference(stored.server_url, stored.device_id)
    monkeypatch.setattr(edge_client, "_require_macos_keychain", lambda: None)
    calls: list[list[str]] = []

    def found(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{stored.credential}\n".encode(),
        )

    monkeypatch.setattr(edge_client.subprocess, "run", found)
    assert load_edge_keychain_credential(reference) == stored
    assert calls[-1][-1] == "-w"

    def missing(
        argv: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 44, stdout=b"")

    monkeypatch.setattr(edge_client.subprocess, "run", missing)
    with pytest.raises(EdgeKeychainCredentialNotFoundError):
        load_edge_keychain_credential(reference)


def test_keychain_metadata_never_contains_the_secret(tmp_path: Path):
    target = tmp_path / "device.json"
    stored = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )

    save_edge_keychain_reference(target, stored)

    contents = target.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stored.credential not in contents
    assert '"credential"' not in contents
    assert load_edge_keychain_reference(target) == EdgeKeychainReference(
        stored.server_url,
        stored.device_id,
    )

    legacy = tmp_path / "legacy.json"
    save_edge_credential(legacy, stored)
    with pytest.raises(EdgeCredentialNotKeychainReferenceError):
        load_edge_keychain_reference(legacy)

    hybrid = tmp_path / "hybrid.json"
    hybrid.write_text(
        json.dumps(
            {
                "credential_store": "keychain",
                "server_url": stored.server_url,
                "device_id": stored.device_id,
                "credential": stored.credential,
            }
        ),
        encoding="utf-8",
    )
    hybrid.chmod(0o600)
    with pytest.raises(ValueError, match="cannot contain a secret"):
        load_edge_keychain_reference(hybrid)


def test_verified_legacy_secret_can_be_replaced_with_keychain_metadata(
    tmp_path: Path,
):
    target = tmp_path / "device.json"
    stored = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )
    save_edge_credential(target, stored)

    replace_edge_credential_with_keychain_reference(target, stored)

    contents = target.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stored.credential not in contents
    assert load_edge_keychain_reference(target) == EdgeKeychainReference(
        stored.server_url,
        stored.device_id,
    )


def test_keychain_migration_rejects_a_hardlinked_plaintext_source(tmp_path: Path):
    target = tmp_path / "device.json"
    linked = tmp_path / "device-copy.json"
    stored = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )
    save_edge_credential(target, stored)
    linked.hardlink_to(target)

    with pytest.raises(ValueError, match="multiple hard links"):
        replace_edge_credential_with_keychain_reference(target, stored)

    assert load_edge_credential(target) == stored
    assert load_edge_credential(linked) == stored


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


def test_http_transport_normalizes_network_failures():
    class UnavailableHttpClient(FakeHttpClient):
        def post(self, path: str, **kwargs: Any) -> FakeResponse:
            raise httpx.ConnectError(
                "unreachable",
                request=httpx.Request("POST", f"https://edge.example.com{path}"),
            )

    transport = HttpEdgeTransport(
        "https://edge.example.com",
        credential="device_1.secret",
        client_factory=lambda **kwargs: UnavailableHttpClient(
            {},
            FakeResponse(500),
            **kwargs,
        ),
    )

    with pytest.raises(EdgeTransportError) as error:
        transport.claim()
    assert error.value.status == 0
    assert error.value.code == "transport_unavailable"


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


def test_codex_stop_event_terminates_the_process_group(tmp_path: Path):
    class BlockingProcess:
        def __init__(self) -> None:
            self.pid = None
            self.returncode: int | None = None
            self.started = Event()
            self.released = Event()

        def communicate(
            self,
            _value: str | None = None,
            timeout: float | None = None,
        ) -> tuple[str, str]:
            self.started.set()
            assert self.released.wait(timeout or 1)
            return "", ""

        def terminate(self) -> None:
            self.returncode = -15
            self.released.set()

    process = BlockingProcess()
    stop_event = Event()

    def request_stop() -> None:
        assert process.started.wait(1)
        stop_event.set()

    requester = Thread(target=request_stop)
    requester.start()
    executor = CodexReadonlyExecutor(
        tmp_path,
        codex_binary="/fake/codex",
        timeout_seconds=5,
        clock=lambda: NOW,
        popen_factory=lambda *_args, **_kwargs: process,
    )

    with pytest.raises(EdgeExecutionCancelled):
        executor.execute(lease_payload(), stop_event=stop_event)
    requester.join(timeout=1)
    assert process.returncode == -15


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
        def execute(
            self,
            _lease: EdgeLeasePayload,
            *,
            stop_event: Event | None = None,
        ):
            raise OSError("local setup is unavailable")

    report = EdgeWorker(transport, BrokenExecutor()).run_once()

    assert report.status == "executor_error"
    assert report.error == "OSError"
    assert transport.completed == []
    assert transport.failed == []


def test_renewal_loss_cancels_local_execution_and_never_completes():
    execution_stopped = Event()

    class RenewalFailureTransport(FakeTransport):
        def renew(self, lease: EdgeLeasePayload) -> datetime:
            raise OSError("renewal unavailable")

    class WaitingExecutor:
        def execute(
            self,
            _lease: EdgeLeasePayload,
            *,
            stop_event: Event | None = None,
        ) -> dict[str, str]:
            assert stop_event is not None
            assert stop_event.wait(1)
            execution_stopped.set()
            raise EdgeExecutionCancelled("lease lost")

    transport = RenewalFailureTransport(lease_payload())
    report = EdgeWorker(
        transport,
        WaitingExecutor(),
        renew_interval_seconds=0.01,
    ).run_once()

    assert report.status == "lease_lost"
    assert report.error == "OSError"
    assert execution_stopped.is_set()
    assert transport.completed == []


def test_successful_renewal_is_observable_during_execution():
    renewed = Event()
    observed: list[datetime] = []

    def observe(expires_at: datetime) -> None:
        observed.append(expires_at)
        renewed.set()

    class WaitingExecutor:
        def execute(
            self,
            _lease: EdgeLeasePayload,
            *,
            stop_event: Event | None = None,
        ) -> dict[str, str]:
            assert renewed.wait(1)
            return {"content": "done"}

    transport = FakeTransport(lease_payload())
    report = EdgeWorker(
        transport,
        WaitingExecutor(),
        renew_interval_seconds=0.01,
        renewal_observer=observe,
    ).run_once()

    assert report.status == "completed"
    assert observed == [lease_payload().claim_expires_at + timedelta(minutes=1)]


def test_external_stop_cancels_execution_without_completing():
    execution_started = Event()
    outer_stop = Event()

    class WaitingExecutor:
        def execute(
            self,
            _lease: EdgeLeasePayload,
            *,
            stop_event: Event | None = None,
        ) -> dict[str, str]:
            assert stop_event is not None
            execution_started.set()
            assert stop_event.wait(1)
            raise EdgeExecutionCancelled("service stopped")

    def request_stop() -> None:
        assert execution_started.wait(1)
        outer_stop.set()

    requester = Thread(target=request_stop)
    requester.start()
    transport = FakeTransport(lease_payload())
    report = EdgeWorker(transport, WaitingExecutor()).run_once(
        stop_event=outer_stop,
    )
    requester.join(timeout=1)

    assert report.status == "stopped"
    assert transport.completed == []


class FakeAgentWorker:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[float, Event | None]] = []

    def run_once(
        self,
        *,
        wait_seconds: float = 0,
        stop_event: Event | None = None,
    ) -> EdgeWorkerReport:
        self.calls.append((wait_seconds, stop_event))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_edge_agent_continues_until_bounded_task_limit():
    lease = lease_payload()
    worker = FakeAgentWorker(
        [
            EdgeWorkerReport(status="no_work"),
            EdgeWorkerReport(status="completed", lease=lease),
            EdgeWorkerReport(status="completed", lease=lease),
        ]
    )
    events: list[tuple[str, Any]] = []

    summary = EdgeAgentLoop(
        worker,  # type: ignore[arg-type]
        max_tasks=2,
        log=lambda event, fields: events.append((event, fields)),
    ).run(Event())

    assert summary.ticks == 3
    assert summary.tasks_claimed == 2
    assert summary.completed == 2
    assert summary.no_work == 1
    assert [call[0] for call in worker.calls] == [20, 20, 20]
    assert events[-1][0] == "edge_agent_stopped"


def test_edge_agent_emits_application_heartbeat_after_server_round_trip():
    lease = lease_payload()
    worker = FakeAgentWorker(
        [
            EdgeWorkerReport(status="no_work"),
            EdgeWorkerReport(status="completed", lease=lease),
        ]
    )
    monotonic_values = iter((0.0, 61.0, 62.0))
    events: list[tuple[str, Any]] = []

    EdgeAgentLoop(
        worker,  # type: ignore[arg-type]
        heartbeat_seconds=60,
        max_tasks=1,
        monotonic=lambda: next(monotonic_values),
        clock=lambda: NOW,
        log=lambda event, fields: events.append((event, fields)),
    ).run(Event())

    heartbeat = next(fields for event, fields in events if event == "edge_agent_heartbeat")
    assert heartbeat["last_server_ok_at"] == NOW.isoformat()
    assert heartbeat["no_work"] == 1


def test_edge_agent_retries_transient_transport_error_with_bounded_backoff():
    lease = lease_payload()
    worker = FakeAgentWorker(
        [
            EdgeTransportError(503, "transport_unavailable", "unavailable"),
            EdgeWorkerReport(status="completed", lease=lease),
        ]
    )
    waits: list[float] = []

    summary = EdgeAgentLoop(
        worker,  # type: ignore[arg-type]
        retry_base_seconds=2,
        retry_max_seconds=8,
        max_tasks=1,
        random_value=lambda: 0.5,
        wait_for_stop=lambda _event, delay: waits.append(delay) or False,
    ).run(Event())

    assert summary.transport_errors == 1
    assert summary.completed == 1
    assert waits == [2]


def test_edge_agent_stops_on_revoked_device_without_retrying():
    worker = FakeAgentWorker(
        [EdgeTransportError(403, "device_revoked", "revoked")]
    )
    waits: list[float] = []

    summary = EdgeAgentLoop(
        worker,  # type: ignore[arg-type]
        wait_for_stop=lambda _event, delay: waits.append(delay) or False,
    ).run(Event())

    assert summary.fatal_error == "device_revoked"
    assert summary.transport_errors == 1
    assert waits == []


def test_edge_device_lock_rejects_a_second_worker(tmp_path: Path):
    credential_path = tmp_path / "device.json"

    with EdgeDeviceLock(credential_path):
        with pytest.raises(RuntimeError, match="another Edge worker"):
            with EdgeDeviceLock(credential_path):
                pass

    with EdgeDeviceLock(credential_path):
        assert (tmp_path / "device.json.lock").stat().st_mode & 0o077 == 0
