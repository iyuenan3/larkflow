"""Isolated credential-side loop for latency-sensitive Feishu interactions."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import Event
import time
from typing import Any

from .daemon import WorkerLoopSettings
from .wakeup import WaitForWork, wait_for_stop


LogEvent = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class InteractiveWorkerReport:
    claimed: int = 0
    im_verified: int = 0
    im_verification_rejected: int = 0
    im_verification_failed: int = 0
    im_replies_sent: int = 0
    im_replies_failed: int = 0
    role_cards_sent: int = 0
    role_cards_failed: int = 0
    role_bindings_verified: int = 0
    role_bindings_rejected: int = 0
    role_binding_verification_failed: int = 0
    role_binding_replies_sent: int = 0
    role_binding_card_updates_failed: int = 0
    role_binding_replies_failed: int = 0
    lane_errors: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InteractiveLoopSummary:
    ticks: int = 0
    tick_errors: int = 0
    claimed: int = 0
    im_verified: int = 0
    im_verification_rejected: int = 0
    im_verification_failed: int = 0
    im_replies_sent: int = 0
    im_replies_failed: int = 0
    role_cards_sent: int = 0
    role_cards_failed: int = 0
    role_bindings_verified: int = 0
    role_bindings_rejected: int = 0
    role_binding_verification_failed: int = 0
    role_binding_replies_sent: int = 0
    role_binding_card_updates_failed: int = 0
    role_binding_replies_failed: int = 0
    lane_errors: int = 0


class InteractiveWorker:
    """Run one item from each credential-side lane without sharing threads."""

    def __init__(
        self,
        *,
        im_verification_worker: Any | None = None,
        im_reply_worker: Any | None = None,
        role_binding_card_worker: Any | None = None,
        role_binding_verification_worker: Any | None = None,
        role_binding_reply_worker: Any | None = None,
        monotonic: Callable[[], float] | None = None,
        log: LogEvent | None = None,
    ) -> None:
        self.im_verification_worker = im_verification_worker
        self.im_reply_worker = im_reply_worker
        self.role_binding_card_worker = role_binding_card_worker
        self.role_binding_verification_worker = role_binding_verification_worker
        self.role_binding_reply_worker = role_binding_reply_worker
        self.monotonic = monotonic or time.monotonic
        self.log = log or (lambda _event, _fields: None)

    def run_once(self) -> InteractiveWorkerReport:
        totals = {
            field_name: 0
            for field_name in InteractiveWorkerReport.__dataclass_fields__
            if field_name != "errors"
        }
        errors: list[str] = []
        lanes = (
            (
                "im_verification",
                self.im_verification_worker,
                {
                    "im_verified": "verified",
                    "im_verification_rejected": "rejected",
                    "im_verification_failed": "failed",
                },
            ),
            (
                "im_reply",
                self.im_reply_worker,
                {
                    "im_replies_sent": "sent",
                    "im_replies_failed": "failed",
                },
            ),
            (
                "role_card",
                self.role_binding_card_worker,
                {
                    "role_cards_sent": "sent",
                    "role_cards_failed": "failed",
                },
            ),
            (
                "role_verification",
                self.role_binding_verification_worker,
                {
                    "role_bindings_verified": "verified",
                    "role_bindings_rejected": "rejected",
                    "role_binding_verification_failed": "failed",
                },
            ),
            (
                "role_reply",
                self.role_binding_reply_worker,
                {
                    "role_binding_replies_sent": "sent",
                    "role_binding_card_updates_failed": "card_updates_failed",
                    "role_binding_replies_failed": "failed",
                },
            ),
        )
        for lane, worker, mapping in lanes:
            if worker is None:
                continue
            self._run_lane(lane, worker, mapping, totals, errors)
        return InteractiveWorkerReport(**totals, errors=tuple(errors))

    def _run_lane(
        self,
        lane: str,
        worker: Any,
        mapping: Mapping[str, str],
        totals: dict[str, int],
        errors: list[str],
    ) -> None:
        started_at = self.monotonic()
        try:
            report = worker.run_once()
        except Exception as exc:
            elapsed_ms = _elapsed_ms(started_at, self.monotonic())
            totals["lane_errors"] += 1
            errors.append(f"{lane}: {type(exc).__name__}: {exc}")
            self._safe_log(
                "interactive_lane_failed",
                {
                    "lane": lane,
                    "elapsed_ms": elapsed_ms,
                    "error_type": type(exc).__name__,
                },
            )
            return
        elapsed_ms = _elapsed_ms(started_at, self.monotonic())
        claimed = int(report.claimed)
        totals["claimed"] += claimed
        for total_field, report_field in mapping.items():
            totals[total_field] += int(getattr(report, report_field))
        report_errors = tuple(str(error) for error in report.errors)
        errors.extend(f"{lane}: {error}" for error in report_errors)
        if claimed or report_errors:
            self._safe_log(
                "interactive_lane_tick",
                {
                    "lane": lane,
                    "claimed": claimed,
                    "elapsed_ms": elapsed_ms,
                    "error_count": len(report_errors),
                },
            )

    def _safe_log(self, event: str, fields: dict[str, Any]) -> None:
        try:
            self.log(event, fields)
        except Exception:
            pass


class InteractiveWorkerLoop:
    """Run one isolated interactive lane until an interruptible stop."""

    def __init__(
        self,
        worker: InteractiveWorker,
        *,
        settings: WorkerLoopSettings | None = None,
        wait_for_work: WaitForWork | None = None,
        log: LogEvent | None = None,
    ) -> None:
        self.worker = worker
        self.settings = settings or WorkerLoopSettings()
        self.wait_for_work = wait_for_work or wait_for_stop
        self.log = log or (lambda _event, _fields: None)

    def run(self, stop_event: Event) -> InteractiveLoopSummary:
        idle_seconds = self.settings.idle_min_seconds
        totals = {
            field_name: 0
            for field_name in InteractiveLoopSummary.__dataclass_fields__
        }
        while not stop_event.is_set():
            try:
                report = self.worker.run_once()
            except Exception as exc:
                totals["tick_errors"] += 1
                self._safe_log(
                    "interactive_tick_failed",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
                if self.wait_for_work(stop_event, idle_seconds):
                    break
                idle_seconds = self._next_idle(idle_seconds)
                continue
            totals["ticks"] += 1
            self._add_report(totals, report)
            if report.claimed:
                self._safe_log("interactive_tick", self._report_fields(report))
                idle_seconds = self.settings.idle_min_seconds
                continue
            if report.errors or report.lane_errors:
                self._safe_log("interactive_tick", self._report_fields(report))
            if self.wait_for_work(stop_event, idle_seconds):
                break
            idle_seconds = self._next_idle(idle_seconds)

        summary = InteractiveLoopSummary(**totals)
        self._safe_log("interactive_stopped", self._summary_fields(summary))
        return summary

    def _safe_log(self, event: str, fields: dict[str, Any]) -> None:
        try:
            self.log(event, fields)
        except Exception:
            pass

    def _next_idle(self, current: float) -> float:
        return min(self.settings.idle_max_seconds, current * 2)

    @staticmethod
    def _add_report(
        totals: dict[str, int],
        report: InteractiveWorkerReport,
    ) -> None:
        for field_name in InteractiveLoopSummary.__dataclass_fields__:
            if field_name in {"ticks", "tick_errors"}:
                continue
            totals[field_name] += int(getattr(report, field_name))

    @staticmethod
    def _report_fields(report: InteractiveWorkerReport) -> dict[str, Any]:
        return {
            field_name: (
                list(report.errors)
                if field_name == "errors"
                else getattr(report, field_name)
            )
            for field_name in InteractiveWorkerReport.__dataclass_fields__
        }

    @staticmethod
    def _summary_fields(summary: InteractiveLoopSummary) -> dict[str, int]:
        return {
            field_name: int(getattr(summary, field_name))
            for field_name in InteractiveLoopSummary.__dataclass_fields__
        }


def _elapsed_ms(started_at: float, completed_at: float) -> int:
    return max(0, round((completed_at - started_at) * 1000))
