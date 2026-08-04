"""Target worker loop, config, and development executor tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from larkflow.workflow import (
    CompletionPollReport,
    DevelopmentToolExecutor,
    ExecutionRequest,
    InboundWorkerLoop,
    InboundWorkerReport,
    InteractiveWorker,
    InteractiveWorkerLoop,
    InteractiveWorkerReport,
    ProjectionWorkerLoop,
    ProjectionReconciliationReport,
    ProjectionWorkerReport,
    TargetInboundSettings,
    TargetInteractiveSettings,
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
        self.reconcile_calls = 0

    def reconcile_all(self, **_kwargs):
        self.reconcile_calls += 1
        return ProjectionReconciliationReport(
            instances_scanned=2,
            nodes_scanned=3,
            tasks_created=1,
            unchanged=2,
        )

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


class RecordingWakeup:
    def __init__(self, *, stop_after_waits: int):
        self.stop_after_waits = stop_after_waits
        self.waits = []

    def __call__(self, stop, seconds):
        self.waits.append(seconds)
        if len(self.waits) >= self.stop_after_waits:
            stop.stopped = True
        return stop.stopped


class ScriptedPoller:
    def __init__(self, reports):
        self.reports = iter(reports)
        self.calls = 0

    def run_once(self, **_kwargs):
        self.calls += 1
        return next(self.reports)


class ScriptedMonotonic:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


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

    events = []
    summary = ProjectionWorkerLoop(
        worker,
        settings=WorkerLoopSettings(0.25, 1.0),
        log=lambda event, fields: events.append((event, fields)),
    ).run(stop)

    assert worker.reconcile_calls == 1
    assert stop.waits == [0.25, 0.25]
    assert summary.ticks == 3
    assert summary.claimed == 1
    assert summary.published == 1
    assert summary.reconciled_instances == 2
    assert summary.reconciled_nodes == 3
    assert summary.tasks_rebuilt == 1
    assert events[0][0] == "projection_reconciled"


def test_projection_loop_runs_completion_poll_immediately_and_periodically():
    worker = ScriptedWorker([ProjectionWorkerReport()] * 3)
    poller = ScriptedPoller(
        [
            CompletionPollReport(
                instances_scanned=1,
                nodes_scanned=1,
                tasks_read=1,
                pending=1,
            ),
            CompletionPollReport(
                instances_scanned=1,
                nodes_scanned=1,
                tasks_read=1,
                completions_observed=1,
                signals_appended=1,
            ),
        ]
    )
    stop = RecordingStop(stop_after_waits=3)
    events = []

    summary = ProjectionWorkerLoop(
        worker,
        settings=WorkerLoopSettings(0.25, 1.0),
        completion_poller=poller,
        completion_poll_seconds=2,
        monotonic=ScriptedMonotonic([0, 0, 1, 2]),
        log=lambda event, fields: events.append((event, fields)),
    ).run(stop)

    assert poller.calls == 2
    assert summary.completion_poll_runs == 2
    assert summary.completion_poll_tasks_read == 2
    assert summary.completion_poll_pending == 1
    assert summary.completions_observed == 1
    assert summary.completion_signals_appended == 1
    assert [event for event, _ in events].count("completion_poll") == 2


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


def test_all_persistent_loops_accept_notification_driven_waits():
    cases = (
        (
            WorkflowWorkerLoop,
            ScriptedWorker([WorkflowWorkerReport()]),
        ),
        (
            ProjectionWorkerLoop,
            ScriptedWorker([ProjectionWorkerReport()]),
        ),
        (
            InboundWorkerLoop,
            ScriptedWorker([InboundWorkerReport()]),
        ),
        (
            VerificationWorkerLoop,
            ScriptedWorker([VerificationWorkerReport()]),
        ),
        (
            InteractiveWorkerLoop,
            ScriptedWorker([InteractiveWorkerReport()]),
        ),
    )

    for loop_type, worker in cases:
        stop = RecordingStop(stop_after_waits=99)
        wakeup = RecordingWakeup(stop_after_waits=1)

        summary = loop_type(worker, wait_for_work=wakeup).run(stop)

        assert wakeup.waits == [0.25]
        assert stop.waits == []
        assert summary.ticks == 1


def test_interactive_loop_keeps_draining_after_any_lane_claims_work():
    worker = ScriptedWorker(
        [
            InteractiveWorkerReport(
                claimed=1,
                role_bindings_verified=1,
            ),
            InteractiveWorkerReport(),
        ]
    )
    stop = RecordingStop(stop_after_waits=1)

    summary = InteractiveWorkerLoop(
        worker,
        settings=WorkerLoopSettings(0.25, 1.0),
    ).run(stop)

    assert worker.calls == 2
    assert stop.waits == [0.25]
    assert summary.ticks == 2
    assert summary.claimed == 1
    assert summary.role_bindings_verified == 1


def test_interactive_worker_isolates_lanes_and_records_service_time():
    class Lane:
        def __init__(self, outcome):
            self.outcome = outcome

        def run_once(self):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    events = []
    worker = InteractiveWorker(
        im_verification_worker=Lane(RuntimeError("directory unavailable")),
        role_binding_verification_worker=Lane(
            SimpleNamespace(
                claimed=1,
                verified=1,
                rejected=0,
                failed=0,
                errors=(),
            )
        ),
        monotonic=ScriptedMonotonic([0, 0.125, 1, 1.375]),
        log=lambda event, fields: events.append((event, fields)),
    )

    report = worker.run_once()

    assert report.claimed == 1
    assert report.role_bindings_verified == 1
    assert report.lane_errors == 1
    assert report.errors == (
        "im_verification: RuntimeError: directory unavailable",
    )
    assert events == [
        (
            "interactive_lane_failed",
            {
                "lane": "im_verification",
                "elapsed_ms": 125,
                "error_type": "RuntimeError",
            },
        ),
        (
            "interactive_lane_tick",
            {
                "lane": "role_verification",
                "claimed": 1,
                "elapsed_ms": 375,
                "error_count": 0,
            },
        ),
    ]


def test_interactive_lane_metrics_cannot_break_durable_processing():
    class Lane:
        def run_once(self):
            return SimpleNamespace(
                claimed=1,
                sent=1,
                failed=0,
                errors=(),
            )

    def fail_log(_event, _fields):
        raise RuntimeError("metrics sink unavailable")

    report = InteractiveWorker(
        im_reply_worker=Lane(),
        monotonic=ScriptedMonotonic([0, 0.1]),
        log=fail_log,
    ).run_once()

    assert report.claimed == 1
    assert report.im_replies_sent == 1
    assert report.lane_errors == 0


def test_interactive_loop_metrics_cannot_stop_queue_draining():
    worker = ScriptedWorker(
        [
            InteractiveWorkerReport(claimed=1, im_replies_sent=1),
            InteractiveWorkerReport(),
        ]
    )
    stop = RecordingStop(stop_after_waits=1)

    def fail_log(_event, _fields):
        raise RuntimeError("metrics sink unavailable")

    summary = InteractiveWorkerLoop(worker, log=fail_log).run(stop)

    assert worker.calls == 2
    assert summary.ticks == 2
    assert summary.claimed == 1
    assert summary.im_replies_sent == 1


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
            "LARKFLOW_TARGET_PROJECTION_RECONCILE_BATCH_SIZE": "11",
            "LARKFLOW_TARGET_COMPLETION_POLL_SECONDS": "17.5",
            "LARKFLOW_TARGET_COMPLETION_POLL_BATCH_SIZE": "13",
            "LARKFLOW_TARGET_PROJECTION_IDLE_MIN_SECONDS": "0.5",
            "LARKFLOW_TARGET_PROJECTION_IDLE_MAX_SECONDS": "2",
        }
    )

    assert settings.worker_id == "host-a:123:projection"
    assert settings.claim_ttl == timedelta(seconds=60)
    assert settings.claim_limit == 7
    assert settings.retry_base == timedelta(seconds=2)
    assert settings.retry_max == timedelta(seconds=30)
    assert settings.reconcile_batch_size == 11
    assert settings.completion_poll_seconds == 17.5
    assert settings.completion_poll_batch_size == 13
    assert settings.loop == WorkerLoopSettings(0.5, 2.0)


def test_interactive_settings_enforce_one_claim_per_replica(monkeypatch):
    monkeypatch.setattr("larkflow.workflow.config.socket.gethostname", lambda: "host-a")
    monkeypatch.setattr("larkflow.workflow.config.os.getpid", lambda: 123)

    settings = TargetInteractiveSettings.from_environ(
        {
            "LARKFLOW_TARGET_DSN": "postgresql:///larkflow_target_dev",
            "LARKFLOW_TARGET_TENANT": "dev",
            "LARKFLOW_TARGET_INTERACTIVE_CLAIM_TTL_SECONDS": "60",
            "LARKFLOW_TARGET_INTERACTIVE_CLAIM_LIMIT": "1",
            "LARKFLOW_TARGET_INTERACTIVE_RETRY_BASE_SECONDS": "2",
            "LARKFLOW_TARGET_INTERACTIVE_RETRY_MAX_SECONDS": "30",
            "LARKFLOW_TARGET_INTERACTIVE_IDLE_MIN_SECONDS": "0.5",
            "LARKFLOW_TARGET_INTERACTIVE_IDLE_MAX_SECONDS": "2",
        }
    )

    assert settings.worker_id == "host-a:123:interactive"
    assert settings.claim_ttl == timedelta(seconds=60)
    assert settings.claim_limit == 1
    assert settings.retry_base == timedelta(seconds=2)
    assert settings.retry_max == timedelta(seconds=30)
    assert settings.loop == WorkerLoopSettings(0.5, 2.0)

    with pytest.raises(ValueError, match="claim_limit must be 1"):
        TargetInteractiveSettings.from_environ(
            {
                "LARKFLOW_TARGET_DSN": "postgresql:///larkflow_target_dev",
                "LARKFLOW_TARGET_TENANT": "dev",
                "LARKFLOW_TARGET_INTERACTIVE_CLAIM_LIMIT": "2",
            }
        )


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
