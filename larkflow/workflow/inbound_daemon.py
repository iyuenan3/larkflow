"""Persistent polling loop for Target inbound event workers."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

from .daemon import WorkerLoopSettings
from .inbound import (
    InboundWorkerReport,
    TaskVerificationWorker,
    VerificationWorkerReport,
    WorkflowInboundWorker,
)


LogEvent = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class InboundLoopSummary:
    ticks: int = 0
    tick_errors: int = 0
    claimed: int = 0
    submitted: int = 0
    noops: int = 0
    rejected: int = 0
    failed: int = 0


class InboundWorkerLoop:
    def __init__(
        self,
        worker: WorkflowInboundWorker,
        *,
        settings: WorkerLoopSettings | None = None,
        log: LogEvent | None = None,
    ) -> None:
        self.worker = worker
        self.settings = settings or WorkerLoopSettings()
        self.log = log or (lambda _event, _fields: None)

    def run(self, stop_event: Event) -> InboundLoopSummary:
        idle_seconds = self.settings.idle_min_seconds
        totals = {
            "ticks": 0,
            "tick_errors": 0,
            "claimed": 0,
            "submitted": 0,
            "noops": 0,
            "rejected": 0,
            "failed": 0,
        }
        while not stop_event.is_set():
            try:
                report = self.worker.run_once()
            except Exception as exc:
                totals["tick_errors"] += 1
                self.log(
                    "inbound_tick_failed",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
                if stop_event.wait(idle_seconds):
                    break
                idle_seconds = min(
                    self.settings.idle_max_seconds,
                    idle_seconds * 2,
                )
                continue
            totals["ticks"] += 1
            for field in (
                "claimed",
                "submitted",
                "noops",
                "rejected",
                "failed",
            ):
                totals[field] += int(getattr(report, field))
            if report.claimed or report.errors:
                self.log("inbound_tick", self._report_fields(report))
            if report.claimed:
                idle_seconds = self.settings.idle_min_seconds
                continue
            if stop_event.wait(idle_seconds):
                break
            idle_seconds = min(self.settings.idle_max_seconds, idle_seconds * 2)
        summary = InboundLoopSummary(**totals)
        self.log("inbound_stopped", self._summary_fields(summary))
        return summary

    @staticmethod
    def _report_fields(report: InboundWorkerReport) -> dict[str, Any]:
        return {
            "claimed": report.claimed,
            "submitted": report.submitted,
            "noops": report.noops,
            "rejected": report.rejected,
            "failed": report.failed,
            "errors": list(report.errors),
        }

    @staticmethod
    def _summary_fields(summary: InboundLoopSummary) -> dict[str, Any]:
        return {
            field: getattr(summary, field)
            for field in summary.__dataclass_fields__
        }


@dataclass(frozen=True)
class VerificationLoopSummary:
    ticks: int = 0
    tick_errors: int = 0
    claimed: int = 0
    verified: int = 0
    failed: int = 0
    exhausted: int = 0


class VerificationWorkerLoop:
    def __init__(
        self,
        worker: TaskVerificationWorker,
        *,
        settings: WorkerLoopSettings | None = None,
        log: LogEvent | None = None,
    ) -> None:
        self.worker = worker
        self.settings = settings or WorkerLoopSettings()
        self.log = log or (lambda _event, _fields: None)

    def run(self, stop_event: Event) -> VerificationLoopSummary:
        idle_seconds = self.settings.idle_min_seconds
        totals = {
            "ticks": 0,
            "tick_errors": 0,
            "claimed": 0,
            "verified": 0,
            "failed": 0,
            "exhausted": 0,
        }
        while not stop_event.is_set():
            try:
                report = self.worker.run_once()
            except Exception as exc:
                totals["tick_errors"] += 1
                self.log(
                    "inbound_verification_tick_failed",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
                if stop_event.wait(idle_seconds):
                    break
                idle_seconds = min(
                    self.settings.idle_max_seconds,
                    idle_seconds * 2,
                )
                continue
            totals["ticks"] += 1
            for field in ("claimed", "verified", "failed", "exhausted"):
                totals[field] += int(getattr(report, field))
            if report.claimed or report.errors:
                self.log(
                    "inbound_verification_tick",
                    self._report_fields(report),
                )
            if report.claimed:
                idle_seconds = self.settings.idle_min_seconds
                continue
            if stop_event.wait(idle_seconds):
                break
            idle_seconds = min(self.settings.idle_max_seconds, idle_seconds * 2)
        summary = VerificationLoopSummary(**totals)
        self.log(
            "inbound_verification_stopped",
            self._summary_fields(summary),
        )
        return summary

    @staticmethod
    def _report_fields(report: VerificationWorkerReport) -> dict[str, Any]:
        return {
            "claimed": report.claimed,
            "verified": report.verified,
            "failed": report.failed,
            "exhausted": report.exhausted,
            "errors": list(report.errors),
        }

    @staticmethod
    def _summary_fields(summary: VerificationLoopSummary) -> dict[str, Any]:
        return {
            field: getattr(summary, field)
            for field in summary.__dataclass_fields__
        }
