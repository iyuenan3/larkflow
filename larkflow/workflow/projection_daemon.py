"""Persistent polling loop for Target projection workers."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
import time
from typing import Any

from .completion_poll import CompletionPollReport, TaskCompletionPoller
from .daemon import WorkerLoopSettings
from .projection import (
    ProjectionReconciliationReport,
    ProjectionWorkerReport,
    WorkflowProjectionWorker,
)
from .wakeup import WaitForWork, wait_for_stop


LogEvent = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class ProjectionLoopSummary:
    reconciled_instances: int = 0
    reconciled_nodes: int = 0
    tasks_rebuilt: int = 0
    reconciliation_unchanged: int = 0
    reconciliation_failed: int = 0
    reconciliation_interrupted: int = 0
    completion_poll_runs: int = 0
    completion_poll_instances: int = 0
    completion_poll_nodes: int = 0
    completion_poll_tasks_read: int = 0
    completions_observed: int = 0
    completion_signals_appended: int = 0
    completion_signal_duplicates: int = 0
    completion_poll_pending: int = 0
    completion_poll_missing_projections: int = 0
    completion_poll_failed: int = 0
    completion_poll_interrupted: int = 0
    ticks: int = 0
    tick_errors: int = 0
    claimed: int = 0
    published: int = 0
    tasks_created: int = 0
    tasks_completed: int = 0
    messages_sent: int = 0
    cards_updated: int = 0
    documents_created: int = 0
    noops: int = 0
    failed: int = 0


class ProjectionWorkerLoop:
    """Run projection ticks until an interruptible stop signal is set."""

    def __init__(
        self,
        worker: WorkflowProjectionWorker,
        *,
        settings: WorkerLoopSettings | None = None,
        reconcile_batch_size: int = 100,
        completion_poller: TaskCompletionPoller | None = None,
        completion_poll_seconds: float = 30.0,
        monotonic: Callable[[], float] | None = None,
        wait_for_work: WaitForWork | None = None,
        log: LogEvent | None = None,
    ) -> None:
        if reconcile_batch_size < 1:
            raise ValueError("reconcile_batch_size must be positive")
        if completion_poll_seconds <= 0:
            raise ValueError("completion_poll_seconds must be positive")
        self.worker = worker
        self.settings = settings or WorkerLoopSettings()
        self.reconcile_batch_size = reconcile_batch_size
        self.completion_poller = completion_poller
        self.completion_poll_seconds = completion_poll_seconds
        self.monotonic = monotonic or time.monotonic
        self.wait_for_work = wait_for_work or wait_for_stop
        self.log = log or (lambda _event, _fields: None)

    def run(self, stop_event: Event) -> ProjectionLoopSummary:
        idle_seconds = self.settings.idle_min_seconds
        totals = {
            "reconciled_instances": 0,
            "reconciled_nodes": 0,
            "tasks_rebuilt": 0,
            "reconciliation_unchanged": 0,
            "reconciliation_failed": 0,
            "reconciliation_interrupted": 0,
            "completion_poll_runs": 0,
            "completion_poll_instances": 0,
            "completion_poll_nodes": 0,
            "completion_poll_tasks_read": 0,
            "completions_observed": 0,
            "completion_signals_appended": 0,
            "completion_signal_duplicates": 0,
            "completion_poll_pending": 0,
            "completion_poll_missing_projections": 0,
            "completion_poll_failed": 0,
            "completion_poll_interrupted": 0,
            "ticks": 0,
            "tick_errors": 0,
            "claimed": 0,
            "published": 0,
            "tasks_created": 0,
            "tasks_completed": 0,
            "messages_sent": 0,
            "cards_updated": 0,
            "documents_created": 0,
            "noops": 0,
            "failed": 0,
        }
        try:
            reconciliation = self.worker.reconcile_all(
                batch_size=self.reconcile_batch_size,
                stop_requested=stop_event.is_set,
            )
        except Exception as exc:
            totals["reconciliation_failed"] = 1
            self.log(
                "projection_reconciliation_failed",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
        else:
            totals["reconciled_instances"] = reconciliation.instances_scanned
            totals["reconciled_nodes"] = reconciliation.nodes_scanned
            totals["tasks_rebuilt"] = (
                reconciliation.tasks_created + reconciliation.tasks_recreated
            )
            totals["reconciliation_unchanged"] = reconciliation.unchanged
            totals["reconciliation_failed"] = reconciliation.failed
            totals["reconciliation_interrupted"] = int(reconciliation.interrupted)
            self.log(
                "projection_reconciled",
                self._reconciliation_fields(reconciliation),
            )
        next_completion_poll = self.monotonic()
        while not stop_event.is_set():
            poll_now = self.monotonic()
            if (
                self.completion_poller is not None
                and poll_now >= next_completion_poll
            ):
                totals["completion_poll_runs"] += 1
                try:
                    completion = self.completion_poller.run_once(
                        stop_requested=stop_event.is_set,
                    )
                except Exception as exc:
                    totals["completion_poll_failed"] += 1
                    self.log(
                        "completion_poll_failed",
                        {
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                else:
                    self._add_completion_report(totals, completion)
                    self.log(
                        "completion_poll",
                        self._completion_fields(completion),
                    )
                next_completion_poll = poll_now + self.completion_poll_seconds
            try:
                report = self.worker.run_once()
            except Exception as exc:
                totals["tick_errors"] += 1
                self.log(
                    "projection_tick_failed",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
                if self.wait_for_work(stop_event, idle_seconds):
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
            if self.wait_for_work(stop_event, idle_seconds):
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
            "messages_sent",
            "cards_updated",
            "documents_created",
            "noops",
            "failed",
        ):
            totals[field] += int(getattr(report, field))

    @staticmethod
    def _add_completion_report(
        totals: dict[str, int],
        report: CompletionPollReport,
    ) -> None:
        mapping = {
            "completion_poll_instances": "instances_scanned",
            "completion_poll_nodes": "nodes_scanned",
            "completion_poll_tasks_read": "tasks_read",
            "completions_observed": "completions_observed",
            "completion_signals_appended": "signals_appended",
            "completion_signal_duplicates": "duplicates",
            "completion_poll_pending": "pending",
            "completion_poll_missing_projections": "missing_projections",
            "completion_poll_failed": "failed",
            "completion_poll_interrupted": "interrupted",
        }
        for total_field, report_field in mapping.items():
            totals[total_field] += int(getattr(report, report_field))

    @staticmethod
    def _report_fields(report: ProjectionWorkerReport) -> dict[str, Any]:
        return {
            "claimed": report.claimed,
            "published": report.published,
            "tasks_created": report.tasks_created,
            "tasks_completed": report.tasks_completed,
            "messages_sent": report.messages_sent,
            "cards_updated": report.cards_updated,
            "documents_created": report.documents_created,
            "noops": report.noops,
            "failed": report.failed,
            "errors": list(report.errors),
        }

    @staticmethod
    def _reconciliation_fields(
        report: ProjectionReconciliationReport,
    ) -> dict[str, Any]:
        return {
            "instances_scanned": report.instances_scanned,
            "nodes_scanned": report.nodes_scanned,
            "tasks_created": report.tasks_created,
            "tasks_recreated": report.tasks_recreated,
            "tasks_completed": report.tasks_completed,
            "unchanged": report.unchanged,
            "failed": report.failed,
            "interrupted": report.interrupted,
            "errors": list(report.errors),
        }

    @staticmethod
    def _completion_fields(report: CompletionPollReport) -> dict[str, Any]:
        return {
            field: getattr(report, field)
            if field != "errors"
            else list(report.errors)
            for field in report.__dataclass_fields__
        }

    @staticmethod
    def _summary_fields(summary: ProjectionLoopSummary) -> dict[str, int]:
        return {
            field: getattr(summary, field)
            for field in summary.__dataclass_fields__
        }
