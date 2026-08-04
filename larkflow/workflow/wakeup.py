"""Best-effort PostgreSQL wakeups with polling as the reliability fallback."""
from __future__ import annotations

from collections.abc import Callable
from threading import Event
import time
from typing import Any


WORKER_WAKEUP_CHANNEL = "larkflow_work_available"

LogEvent = Callable[[str, dict[str, Any]], None]
WaitForWork = Callable[[Event, float], bool]


def wait_for_stop(stop_event: Event, timeout: float) -> bool:
    """Preserve the original interruptible polling wait contract."""

    return stop_event.wait(timeout)


class PostgresWorkerWakeup:
    """Wake a worker after committed queue changes without carrying state."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        log: LogEvent | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.log = log or (lambda _event, _fields: None)
        self.monotonic = monotonic or time.monotonic
        self._connection: Any | None = None
        self.notifications_received = 0

    def start(self) -> bool:
        """Start listening early so work cannot race the first idle wait."""

        if self._connection is not None:
            return True
        connection = None
        try:
            connection = self.connection_factory()
            connection.execute(f"LISTEN {WORKER_WAKEUP_CHANNEL}")
        except Exception as exc:
            self._discard_connection(connection)
            self._log_failure("listen", exc)
            return False
        self._connection = connection
        self._safe_log(
            "worker_wakeup_listening",
            {"channel": WORKER_WAKEUP_CHANNEL},
        )
        return True

    def wait(self, stop_event: Event, timeout: float) -> bool:
        """Return on notification, timeout, or stop; failures fall back to polling."""

        if timeout < 0:
            raise ValueError("wakeup timeout cannot be negative")
        if stop_event.is_set():
            return True
        started_at = self.monotonic()
        try:
            if not self.start():
                elapsed = max(0.0, self.monotonic() - started_at)
                return stop_event.wait(max(0.0, timeout - elapsed))
            assert self._connection is not None
            for _notification in self._connection.notifies(
                timeout=timeout,
                stop_after=1,
            ):
                self.notifications_received += 1
                break
        except Exception as exc:
            self._discard_connection(self._connection)
            self._connection = None
            self._log_failure("wait", exc)
            elapsed = max(0.0, self.monotonic() - started_at)
            return stop_event.wait(max(0.0, timeout - elapsed))
        return stop_event.is_set()

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        self._discard_connection(connection)

    @staticmethod
    def _discard_connection(connection: Any | None) -> None:
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            pass

    def _log_failure(self, phase: str, exc: Exception) -> None:
        self._safe_log(
            "worker_wakeup_failed",
            {"phase": phase, "error_type": type(exc).__name__},
        )

    def _safe_log(self, event: str, fields: dict[str, Any]) -> None:
        try:
            self.log(event, fields)
        except Exception:
            pass
