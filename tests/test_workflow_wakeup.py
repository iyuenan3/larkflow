"""PostgreSQL notification wakeup tests."""
from __future__ import annotations

from threading import Event

import pytest

from larkflow.workflow import PostgresWorkerWakeup, WORKER_WAKEUP_CHANNEL


class FakeConnection:
    def __init__(self, *, notifications=(), error: Exception | None = None):
        self.notifications = tuple(notifications)
        self.error = error
        self.executed = []
        self.waits = []
        self.closed = False

    def execute(self, statement):
        self.executed.append(statement)

    def notifies(self, *, timeout, stop_after):
        self.waits.append((timeout, stop_after))
        if self.error is not None:
            raise self.error
        yield from self.notifications

    def close(self):
        self.closed = True


class RecordingStop:
    def __init__(self):
        self.waits = []

    def is_set(self):
        return False

    def wait(self, timeout):
        self.waits.append(timeout)
        return False


def test_listener_starts_before_wait_and_consumes_one_notification():
    connection = FakeConnection(notifications=(object(), object()))
    events = []
    wakeup = PostgresWorkerWakeup(
        lambda: connection,
        log=lambda event, fields: events.append((event, fields)),
    )

    assert wakeup.start() is True
    assert connection.executed == [f"LISTEN {WORKER_WAKEUP_CHANNEL}"]
    assert wakeup.wait(Event(), 1.25) is False
    assert connection.waits == [(1.25, 1)]
    assert wakeup.notifications_received == 1
    assert events == [
        ("worker_wakeup_listening", {"channel": WORKER_WAKEUP_CHANNEL})
    ]

    wakeup.close()
    assert connection.closed is True


def test_listener_failure_closes_connection_and_falls_back_without_sensitive_text():
    connection = FakeConnection(error=RuntimeError("secret connection detail"))
    events = []
    wakeup = PostgresWorkerWakeup(
        lambda: connection,
        log=lambda event, fields: events.append((event, fields)),
    )

    assert wakeup.wait(Event(), 0) is False
    assert connection.closed is True
    assert events[-1] == (
        "worker_wakeup_failed",
        {"phase": "wait", "error_type": "RuntimeError"},
    )
    assert "secret connection detail" not in repr(events)


def test_listener_rejects_negative_timeout():
    wakeup = PostgresWorkerWakeup(lambda: FakeConnection())

    with pytest.raises(ValueError, match="cannot be negative"):
        wakeup.wait(Event(), -0.1)


def test_listener_observability_cannot_break_worker_wakeup():
    connection = FakeConnection(notifications=(object(),))

    def broken_log(_event, _fields):
        raise RuntimeError("logger unavailable")

    wakeup = PostgresWorkerWakeup(lambda: connection, log=broken_log)

    assert wakeup.start() is True
    assert wakeup.wait(Event(), 0) is False
    assert wakeup.notifications_received == 1


def test_listener_connect_failure_uses_only_the_remaining_poll_interval():
    clock = iter((10.0, 10.75))
    stop = RecordingStop()

    def fail_connect():
        raise RuntimeError("database unavailable")

    wakeup = PostgresWorkerWakeup(
        fail_connect,
        monotonic=lambda: next(clock),
    )

    assert wakeup.wait(stop, 1.0) is False
    assert stop.waits == [0.25]
