"""Persistent polling loop for the Target workflow worker."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

from .runtime import WorkflowWorker, WorkflowWorkerReport


LogEvent = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class WorkerLoopSettings:
    idle_min_seconds: float = 0.25
    idle_max_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.idle_min_seconds <= 0:
            raise ValueError("idle_min_seconds must be positive")
        if self.idle_max_seconds < self.idle_min_seconds:
            raise ValueError("idle_max_seconds must be at least idle_min_seconds")


@dataclass(frozen=True)
class WorkerLoopSummary:
    ticks: int = 0
    tick_errors: int = 0
    human_dispatched: int = 0
    automated_claimed: int = 0
    recovered: int = 0
    completed: int = 0
    failed: int = 0
    conflicts: int = 0
    stale_results: int = 0


class WorkflowWorkerLoop:
    """Run worker ticks until an interruptible stop signal is set."""

    def __init__(
        self,
        worker: WorkflowWorker,
        *,
        settings: WorkerLoopSettings | None = None,
        log: LogEvent | None = None,
    ) -> None:
        self.worker = worker
        self.settings = settings or WorkerLoopSettings()
        self.log = log or (lambda _event, _fields: None)

    def run(self, stop_event: Event) -> WorkerLoopSummary:
        idle_seconds = self.settings.idle_min_seconds
        totals = {
            "ticks": 0,
            "tick_errors": 0,
            "human_dispatched": 0,
            "automated_claimed": 0,
            "recovered": 0,
            "completed": 0,
            "failed": 0,
            "conflicts": 0,
            "stale_results": 0,
        }
        while not stop_event.is_set():
            try:
                report = self.worker.run_once()
            except Exception as exc:
                totals["tick_errors"] += 1
                self.log(
                    "worker_tick_failed",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
                if stop_event.wait(idle_seconds):
                    break
                idle_seconds = self._next_idle(idle_seconds)
                continue

            totals["ticks"] += 1
            self._add_report(totals, report)
            if self._made_progress(report):
                self.log("worker_tick", self._report_fields(report))
                idle_seconds = self.settings.idle_min_seconds
                continue

            if report.conflicts or report.stale_results or report.errors:
                self.log("worker_tick", self._report_fields(report))
            if stop_event.wait(idle_seconds):
                break
            idle_seconds = self._next_idle(idle_seconds)

        summary = WorkerLoopSummary(**totals)
        self.log("worker_stopped", self._summary_fields(summary))
        return summary

    def _next_idle(self, current: float) -> float:
        return min(self.settings.idle_max_seconds, current * 2)

    @staticmethod
    def _made_progress(report: WorkflowWorkerReport) -> bool:
        return bool(report.human_dispatched or report.automated_claimed)

    @staticmethod
    def _add_report(totals: dict[str, int], report: WorkflowWorkerReport) -> None:
        for field in (
            "human_dispatched",
            "automated_claimed",
            "recovered",
            "completed",
            "failed",
            "conflicts",
            "stale_results",
        ):
            totals[field] += int(getattr(report, field))

    @staticmethod
    def _report_fields(report: WorkflowWorkerReport) -> dict[str, Any]:
        return {
            "candidates": report.candidates,
            "human_dispatched": report.human_dispatched,
            "automated_claimed": report.automated_claimed,
            "recovered": report.recovered,
            "completed": report.completed,
            "failed": report.failed,
            "conflicts": report.conflicts,
            "stale_results": report.stale_results,
            "errors": list(report.errors),
        }

    @staticmethod
    def _summary_fields(summary: WorkerLoopSummary) -> dict[str, Any]:
        return {
            field: getattr(summary, field)
            for field in summary.__dataclass_fields__
        }
