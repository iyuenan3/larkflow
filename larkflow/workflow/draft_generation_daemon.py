"""Dedicated loop for slow natural-language draft generation."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

from .daemon import WorkerLoopSettings
from .role_bindings import RoleBindingActionWorker
from .wakeup import WaitForWork, wait_for_stop


LogEvent = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class DraftGenerationLoopSummary:
    ticks: int = 0
    tick_errors: int = 0
    claimed: int = 0
    processed: int = 0
    rejected: int = 0
    failed: int = 0
    canceled: int = 0


class DraftGenerationWorkerLoop:
    """Drain only verified draft-wizard actions in a credential-free process."""

    def __init__(
        self,
        worker: RoleBindingActionWorker,
        *,
        settings: WorkerLoopSettings | None = None,
        wait_for_work: WaitForWork | None = None,
        log: LogEvent | None = None,
    ) -> None:
        self.worker = worker
        self.settings = settings or WorkerLoopSettings()
        self.wait_for_work = wait_for_work or wait_for_stop
        self.log = log or (lambda _event, _fields: None)

    def run(self, stop_event: Event) -> DraftGenerationLoopSummary:
        idle_seconds = self.settings.idle_min_seconds
        totals = {name: 0 for name in DraftGenerationLoopSummary.__dataclass_fields__}
        while not stop_event.is_set():
            try:
                report = self.worker.run_once()
            except Exception as exc:
                totals["tick_errors"] += 1
                self._safe_log(
                    "draft_generation_tick_failed",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
                if self.wait_for_work(stop_event, idle_seconds):
                    break
                idle_seconds = min(self.settings.idle_max_seconds, idle_seconds * 2)
                continue
            totals["ticks"] += 1
            for name in ("claimed", "processed", "rejected", "failed", "canceled"):
                totals[name] += int(getattr(report, name, 0))
            if report.claimed or report.errors:
                self._safe_log("draft_generation_tick", self.report_fields(report))
            if report.claimed:
                idle_seconds = self.settings.idle_min_seconds
                continue
            if self.wait_for_work(stop_event, idle_seconds):
                break
            idle_seconds = min(self.settings.idle_max_seconds, idle_seconds * 2)
        summary = DraftGenerationLoopSummary(**totals)
        self._safe_log(
            "draft_generation_stopped",
            {name: int(getattr(summary, name)) for name in totals},
        )
        return summary

    @staticmethod
    def report_fields(report: Any) -> dict[str, Any]:
        return {
            "claimed": report.claimed,
            "processed": report.processed,
            "rejected": report.rejected,
            "failed": report.failed,
            "canceled": getattr(report, "canceled", 0),
            "errors": list(report.errors),
        }

    def _safe_log(self, event: str, fields: dict[str, Any]) -> None:
        try:
            self.log(event, fields)
        except Exception:
            pass


__all__ = ["DraftGenerationLoopSummary", "DraftGenerationWorkerLoop"]
