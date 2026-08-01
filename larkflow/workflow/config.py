"""Configuration for the Target PostgreSQL runtime."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import os
import socket
from collections.abc import Mapping

from .daemon import WorkerLoopSettings


@dataclass(frozen=True)
class TargetRuntimeSettings:
    dsn: str
    tenant_id: str
    worker_id: str
    claim_ttl: timedelta = timedelta(minutes=5)
    candidate_limit: int = 100
    loop: WorkerLoopSettings = WorkerLoopSettings()
    enable_development_executor: bool = False

    def __post_init__(self) -> None:
        if not self.dsn.strip():
            raise ValueError("Target PostgreSQL DSN is required")
        if not self.tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        if not self.worker_id.strip():
            raise ValueError("Target worker_id is required")
        if self.claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        if self.candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        dsn: str | None = None,
        tenant_id: str | None = None,
        worker_id: str | None = None,
    ) -> TargetRuntimeSettings:
        values = os.environ if environ is None else environ
        return cls(
            dsn=dsn or values.get("LARKFLOW_TARGET_DSN", ""),
            tenant_id=tenant_id or values.get("LARKFLOW_TARGET_TENANT", ""),
            worker_id=(
                worker_id
                or values.get("LARKFLOW_TARGET_WORKER_ID")
                or f"{socket.gethostname()}:{os.getpid()}"
            ),
            claim_ttl=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_CLAIM_TTL_SECONDS",
                    300.0,
                )
            ),
            candidate_limit=_positive_int(
                values,
                "LARKFLOW_TARGET_CANDIDATE_LIMIT",
                100,
            ),
            loop=WorkerLoopSettings(
                idle_min_seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_IDLE_MIN_SECONDS",
                    0.25,
                ),
                idle_max_seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_IDLE_MAX_SECONDS",
                    5.0,
                ),
            ),
            enable_development_executor=_boolean(
                values.get("LARKFLOW_TARGET_ENABLE_DEVELOPMENT_EXECUTOR", "false")
            ),
        )


def _positive_float(
    values: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    raw = values.get(key)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def _positive_int(
    values: Mapping[str, str],
    key: str,
    default: int,
) -> int:
    raw = values.get(key)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{key} must be positive")
    return value


def _boolean(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean value must be true or false")
