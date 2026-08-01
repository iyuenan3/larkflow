"""Persistent polling loop for Target projection workers."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

from .daemon import WorkerLoopSettings
from .projection import ProjectionWorkerReport, WorkflowProjectionWorker


LogEvent = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class ProjectionLoopSummary:
    ticks: int = 0
    tick_errors: int = 0
    claimed: int = 0
    published: int = 0
    tasks_created: int = 0
    tasks_completed: int = 0
    noops: int = 0
    failed: int = 0


class ProjectionWorkerLoop:
    """Run projection ticks until an interruptible stop signal is set."""

    def __init__(
        self,
        worker: WorkflowProjectionWorker,
        *,
        settings: WorkerLoopSettings | None = None,
        log: LogEvent | None = None,
    ) -> None:
        self.worker = worker
        self.settings = settings or WorkerLoopSettings()
        self.log = log or (lambda _event, _fields: None)

    def run(self, stop_event: Event) -> ProjectionLoopSummary:
        idle_seconds = self.settings.idle_min_seconds
        totals = {
            "ticks": 0,
            "tick_errors": 0,
            "claimed": 0,
            "published": 0,
            "tasks_created": 0,
            "tasks_completed": 0,
            "noops": 0,
            "failed": 0,
        }
        while not stop_event.is_set():
            try:
                report = self.worker.run_once()
            except Exception as exc:
                totals["tick_errors"] += 1
                self.log(
                    "projection_tick_failed",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
                if stop_event.wait(idle_seconds):
                    break
                idle_seconds = self._next_idle(idle_seconds)
                continue

            totals["ticks"] += 1
            self._add_report(totals, report)
            if report.claimed:
                self.log("projection_tick", self._report_fields(report))
                idle_seconds = self.settings.idle_min_seconds
                continue
            if report.errors:
                self.log("projection_tick", self._report_fields(report))
            if stop_event.wait(idle_seconds):
                break
            idle_seconds = self._next_idle(idle_seconds)

        summary = ProjectionLoopSummary(**totals)
        self.log("projection_stopped", self._summary_fields(summary))
        return summary

    def _next_idle(self, current: float) -> float:
        return min(self.settings.idle_max_seconds, current * 2)

    @staticmethod
    def _add_report(
        totals: dict[str, int],
        report: ProjectionWorkerReport,
    ) -> None:
        for field in (
            "claimed",
            "published",
            "tasks_created",
            "tasks_completed",
            "noops",
            "failed",
        ):
            totals[field] += int(getattr(report, field))

    @staticmethod
    def _report_fields(report: ProjectionWorkerReport) -> dict[str, Any]:
        return {
            "claimed": report.claimed,
            "published": report.published,
            "tasks_created": report.tasks_created,
            "tasks_completed": report.tasks_completed,
            "noops": report.noops,
            "failed": report.failed,
            "errors": list(report.errors),
        }

    @staticmethod
    def _summary_fields(summary: ProjectionLoopSummary) -> dict[str, int]:
        return {
            field: getattr(summary, field)
            for field in summary.__dataclass_fields__
        }
