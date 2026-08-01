"""Target worker loop, config, and development executor tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from larkflow.workflow import (
    DevelopmentToolExecutor,
    ExecutionRequest,
    InboundWorkerLoop,
    InboundWorkerReport,
    ProjectionWorkerLoop,
    ProjectionWorkerReport,
    TargetInboundSettings,
    TargetProjectionSettings,
    TargetRuntimeSettings,
    VerificationWorkerLoop,
    VerificationWorkerReport,
    WorkflowWorkerLoop,
    WorkflowWorkerReport,
    WorkerLoopSettings,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class ScriptedWorker:
    def __init__(self, items):
        self.items = iter(items)
        self.calls = 0

    def run_once(self):
        self.calls += 1
        item = next(self.items)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingStop:
    def __init__(self, *, stop_after_waits: int):
        self.stop_after_waits = stop_after_waits
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        if len(self.waits) >= self.stop_after_waits:
            self.stopped = True
        return self.stopped


def test_idle_loop_uses_bounded_exponential_backoff():
    worker = ScriptedWorker([WorkflowWorkerReport()] * 4)
    stop = RecordingStop(stop_after_waits=4)

    summary = WorkflowWorkerLoop(
        worker,
        settings=WorkerLoopSettings(0.25, 1.0),
    ).run(stop)

    assert stop.waits == [0.25, 0.5, 1.0, 1.0]
    assert summary.ticks == 4
    assert summary.tick_errors == 0


def test_progress_resets_backoff_and_transient_errors_do_not_kill_loop():
    worker = ScriptedWorker(
        [
            WorkflowWorkerReport(),
            RuntimeError("database restarted"),
            WorkflowWorkerReport(automated_claimed=1, completed=1),
            WorkflowWorkerReport(),
        ]
    )
    stop = RecordingStop(stop_after_waits=3)
    events = []

    summary = WorkflowWorkerLoop(
        worker,
        settings=WorkerLoopSettings(0.25, 1.0),
        log=lambda event, fields: events.append((event, fields)),
    ).run(stop)

    assert stop.waits == [0.25, 0.5, 0.25]
    assert summary.ticks == 3
    assert summary.tick_errors == 1
    assert summary.completed == 1
    assert [event for event, _ in events] == [
        "worker_tick_failed",
        "worker_tick",
        "worker_stopped",
    ]


def test_projection_loop_uses_the_same_bounded_backoff_contract():
    worker = ScriptedWorker(
        [
            ProjectionWorkerReport(),
            ProjectionWorkerReport(claimed=1, published=1, noops=1),
            ProjectionWorkerReport(),
        ]
    )
    stop = RecordingStop(stop_after_waits=2)

    summary = ProjectionWorkerLoop(
        worker,
        settings=WorkerLoopSettings(0.25, 1.0),
    ).run(stop)

    assert stop.waits == [0.25, 0.25]
    assert summary.ticks == 3
    assert summary.claimed == 1
    assert summary.published == 1


def test_inbound_loop_uses_the_same_bounded_backoff_contract():
    worker = ScriptedWorker(
        [
            InboundWorkerReport(),
            InboundWorkerReport(claimed=1, submitted=1),
            InboundWorkerReport(),
        ]
    )
    stop = RecordingStop(stop_after_waits=2)

    summary = InboundWorkerLoop(
        worker,
        settings=WorkerLoopSettings(0.25, 1.0),
    ).run(stop)

    assert stop.waits == [0.25, 0.25]
    assert summary.ticks == 3
    assert summary.claimed == 1
    assert summary.submitted == 1


def test_verification_loop_uses_the_same_bounded_backoff_contract():
    worker = ScriptedWorker(
        [
            VerificationWorkerReport(),
            VerificationWorkerReport(
                claimed=2,
                verified=1,
                failed=1,
                exhausted=1,
            ),
            VerificationWorkerReport(),
        ]
    )
    stop = RecordingStop(stop_after_waits=2)

    summary = VerificationWorkerLoop(
        worker,
        settings=WorkerLoopSettings(0.25, 1.0),
    ).run(stop)

    assert stop.waits == [0.25, 0.25]
    assert summary.ticks == 3
    assert summary.claimed == 2
    assert summary.verified == 1
    assert summary.failed == 1
    assert summary.exhausted == 1


def request(*, kind="development.echo", args=None, executor="tool"):
    return ExecutionRequest(
        tenant_id="tenant_dev",
        instance_id="instance_dev",
        node_key="echo",
        attempt_id="attempt_dev",
        attempt_no=1,
        owner_person_id="person_owner",
        executor=executor,
        work={
            "objective": "Echo a deterministic result",
            "inputs": [],
            "outputs": [{"id": "result", "type": "data"}],
            "acceptance": ["The result exists"],
            "tool": {"kind": kind, "args": args or {}},
        },
        input_snapshot={},
        expected_node_version=1,
        claim_token="claim",
        claim_expires_at=NOW + timedelta(minutes=5),
    )


def test_development_executor_returns_declared_result_and_delay_is_injectable():
    sleeps = []
    executor = DevelopmentToolExecutor(sleep=sleeps.append)

    result = executor.execute(
        request(args={"delay_seconds": 2, "result": {"ok": True}})
    )

    assert sleeps == [2.0]
    assert result.result == {"ok": True}


@pytest.mark.parametrize(
    "execution_request, message",
    [
        (request(kind="unknown"), "unsupported development tool kind"),
        (request(args={"delay_seconds": 61}), "between 0 and 60"),
        (request(executor="agent"), "unsupported development tool kind"),
    ],
)
def test_development_executor_rejects_anything_outside_its_narrow_contract(
    execution_request,
    message,
):
    with pytest.raises(ValueError, match=message):
        DevelopmentToolExecutor(sleep=lambda _: None).execute(execution_request)


def test_target_settings_use_peer_dsn_and_derive_worker_identity(monkeypatch):
    monkeypatch.setattr("larkflow.workflow.config.socket.gethostname", lambda: "host-a")
    monkeypatch.setattr("larkflow.workflow.config.os.getpid", lambda: 123)
    settings = TargetRuntimeSettings.from_environ(
        {
            "LARKFLOW_TARGET_DSN": "postgresql:///larkflow_target_dev",
            "LARKFLOW_TARGET_TENANT": "dev",
            "LARKFLOW_TARGET_CLAIM_TTL_SECONDS": "30",
            "LARKFLOW_TARGET_IDLE_MIN_SECONDS": "0.5",
            "LARKFLOW_TARGET_IDLE_MAX_SECONDS": "2",
            "LARKFLOW_TARGET_ENABLE_DEVELOPMENT_EXECUTOR": "true",
        }
    )

    assert settings.worker_id == "host-a:123"
    assert settings.claim_ttl == timedelta(seconds=30)
    assert settings.loop == WorkerLoopSettings(0.5, 2.0)
    assert settings.enable_development_executor is True


def test_projection_settings_have_independent_claim_and_retry_controls(monkeypatch):
    monkeypatch.setattr("larkflow.workflow.config.socket.gethostname", lambda: "host-a")
    monkeypatch.setattr("larkflow.workflow.config.os.getpid", lambda: 123)

    settings = TargetProjectionSettings.from_environ(
        {
            "LARKFLOW_TARGET_DSN": "postgresql:///larkflow_target_dev",
            "LARKFLOW_TARGET_TENANT": "dev",
            "LARKFLOW_TARGET_PROJECTION_CLAIM_TTL_SECONDS": "60",
            "LARKFLOW_TARGET_PROJECTION_CLAIM_LIMIT": "7",
            "LARKFLOW_TARGET_PROJECTION_RETRY_BASE_SECONDS": "2",
            "LARKFLOW_TARGET_PROJECTION_RETRY_MAX_SECONDS": "30",
            "LARKFLOW_TARGET_PROJECTION_IDLE_MIN_SECONDS": "0.5",
            "LARKFLOW_TARGET_PROJECTION_IDLE_MAX_SECONDS": "2",
        }
    )

    assert settings.worker_id == "host-a:123:projection"
    assert settings.claim_ttl == timedelta(seconds=60)
    assert settings.claim_limit == 7
    assert settings.retry_base == timedelta(seconds=2)
    assert settings.retry_max == timedelta(seconds=30)
    assert settings.loop == WorkerLoopSettings(0.5, 2.0)


def test_inbound_settings_have_independent_claim_and_retry_controls(monkeypatch):
    monkeypatch.setattr("larkflow.workflow.config.socket.gethostname", lambda: "host-a")
    monkeypatch.setattr("larkflow.workflow.config.os.getpid", lambda: 123)

    settings = TargetInboundSettings.from_environ(
        {
            "LARKFLOW_TARGET_DSN": "postgresql:///larkflow_target_dev",
            "LARKFLOW_TARGET_TENANT": "dev",
            "LARKFLOW_TARGET_INBOUND_CLAIM_TTL_SECONDS": "60",
            "LARKFLOW_TARGET_INBOUND_CLAIM_LIMIT": "7",
            "LARKFLOW_TARGET_INBOUND_RETRY_BASE_SECONDS": "2",
            "LARKFLOW_TARGET_INBOUND_RETRY_MAX_SECONDS": "30",
            "LARKFLOW_TARGET_INBOUND_VERIFICATION_MAX_ATTEMPTS": "9",
            "LARKFLOW_TARGET_INBOUND_IDLE_MIN_SECONDS": "0.5",
            "LARKFLOW_TARGET_INBOUND_IDLE_MAX_SECONDS": "2",
        }
    )

    assert settings.worker_id == "host-a:123:inbound"
    assert settings.claim_ttl == timedelta(seconds=60)
    assert settings.claim_limit == 7
    assert settings.retry_base == timedelta(seconds=2)
    assert settings.retry_max == timedelta(seconds=30)
    assert settings.verification_max_attempts == 9
    assert settings.loop == WorkerLoopSettings(0.5, 2.0)
