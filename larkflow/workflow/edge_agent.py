"""Foreground Personal Agent Edge service loop."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import random
from threading import Event
import time
from typing import Any

from .edge_client import EdgeTransportError, EdgeWorker


@dataclass(frozen=True)
class EdgeAgentSummary:
    ticks: int = 0
    tasks_claimed: int = 0
    completed: int = 0
    no_work: int = 0
    executor_errors: int = 0
    lease_lost: int = 0
    stale: int = 0
    transport_errors: int = 0
    loop_errors: int = 0
    fatal_error: str | None = None


class EdgeAgentLoop:
    """Continuously long-poll one narrow capability with bounded recovery."""

    def __init__(
        self,
        worker: EdgeWorker,
        *,
        wait_seconds: float = 20.0,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 30.0,
        heartbeat_seconds: float = 60.0,
        max_tasks: int = 0,
        monotonic: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None,
        random_value: Callable[[], float] = random.random,
        wait_for_stop: Callable[[Event, float], bool] | None = None,
        log: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        if wait_seconds < 0 or wait_seconds > 25:
            raise ValueError("wait_seconds must be between 0 and 25")
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("retry bounds must be positive and ordered")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if max_tasks < 0:
            raise ValueError("max_tasks cannot be negative")
        self.worker = worker
        self.wait_seconds = wait_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.max_tasks = max_tasks
        self.monotonic = monotonic or time.monotonic
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.random_value = random_value
        self.wait_for_stop = wait_for_stop or (lambda event, delay: event.wait(delay))
        self.log = log or (lambda _event, _fields: None)

    def run(self, stop_event: Event) -> EdgeAgentSummary:
        totals = {
            "ticks": 0,
            "tasks_claimed": 0,
            "completed": 0,
            "no_work": 0,
            "executor_errors": 0,
            "lease_lost": 0,
            "stale": 0,
            "transport_errors": 0,
            "loop_errors": 0,
        }
        fatal_error: str | None = None
        consecutive_failures = 0
        next_heartbeat = self.monotonic() + self.heartbeat_seconds
        last_server_ok_at: datetime | None = None

        while not stop_event.is_set():
            try:
                report = self.worker.run_once(
                    wait_seconds=self.wait_seconds,
                    stop_event=stop_event,
                )
            except EdgeTransportError as exc:
                if stop_event.is_set():
                    break
                totals["transport_errors"] += 1
                if _fatal_edge_transport_error(exc):
                    fatal_error = exc.code
                    self._safe_log(
                        "edge_agent_fatal",
                        {"code": exc.code, "status": exc.status},
                    )
                    break
                consecutive_failures += 1
                self._safe_log(
                    "edge_agent_transport_error",
                    {"code": exc.code, "status": exc.status},
                )
                if self._backoff(stop_event, consecutive_failures):
                    break
                continue
            except Exception as exc:
                if stop_event.is_set():
                    break
                totals["loop_errors"] += 1
                consecutive_failures += 1
                self._safe_log(
                    "edge_agent_loop_error",
                    {"error_type": type(exc).__name__},
                )
                if self._backoff(stop_event, consecutive_failures):
                    break
                continue

            totals["ticks"] += 1
            last_server_ok_at = self.clock()
            if report.lease is not None:
                totals["tasks_claimed"] += 1
            if report.status == "completed":
                totals["completed"] += 1
                consecutive_failures = 0
            elif report.status == "no_work":
                totals["no_work"] += 1
                consecutive_failures = 0
            elif report.status == "executor_error":
                totals["executor_errors"] += 1
                consecutive_failures += 1
            elif report.status == "lease_lost":
                totals["lease_lost"] += 1
                consecutive_failures += 1
            elif report.status == "stale":
                totals["stale"] += 1
                consecutive_failures += 1
            elif report.status == "stopped":
                break
            else:
                totals["loop_errors"] += 1
                consecutive_failures += 1
                self._safe_log(
                    "edge_agent_loop_error",
                    {"error_type": "UnknownWorkerStatus"},
                )

            if report.lease is not None:
                self._safe_log(
                    "edge_agent_task",
                    {
                        "status": report.status,
                        "attempt_no": report.lease.attempt_no,
                        "error_type": report.error,
                    },
                )
            now = self.monotonic()
            if now >= next_heartbeat:
                self._safe_log(
                    "edge_agent_heartbeat",
                    {
                        **totals,
                        "last_server_ok_at": (
                            last_server_ok_at.isoformat()
                            if last_server_ok_at is not None
                            else None
                        ),
                    },
                )
                next_heartbeat = now + self.heartbeat_seconds
            if self.max_tasks and totals["tasks_claimed"] >= self.max_tasks:
                break
            if report.status not in {"completed", "no_work", "stopped"}:
                if self._backoff(stop_event, consecutive_failures):
                    break

        summary = EdgeAgentSummary(**totals, fatal_error=fatal_error)
        self._safe_log("edge_agent_stopped", _edge_summary_fields(summary))
        return summary

    def _backoff(self, stop_event: Event, failures: int) -> bool:
        base = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** min(max(0, failures - 1), 30)),
        )
        random_value = min(max(float(self.random_value()), 0.0), 1.0)
        delay = base * (0.8 + 0.4 * random_value)
        return self.wait_for_stop(stop_event, delay)

    def _safe_log(self, event: str, fields: Mapping[str, Any]) -> None:
        try:
            self.log(event, fields)
        except Exception:
            pass


def _fatal_edge_transport_error(error: EdgeTransportError) -> bool:
    if error.code in {"invalid_device_credential", "device_revoked"}:
        return True
    return 400 <= error.status < 500 and error.status not in {408, 409, 425, 429}


def _edge_summary_fields(summary: EdgeAgentSummary) -> dict[str, Any]:
    return {
        field_name: getattr(summary, field_name)
        for field_name in EdgeAgentSummary.__dataclass_fields__
    }
