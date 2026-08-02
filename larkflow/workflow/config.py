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
    enable_agent_executor: bool = False
    enable_development_executor: bool = False
    enable_content_check_executor: bool = False
    agent_claim_safety: timedelta = timedelta(seconds=30)
    agent_max_prompt_chars: int = 20_000
    agent_max_result_chars: int = 50_000
    content_check_max_chars: int = 50_000

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
        if self.agent_claim_safety <= timedelta(0):
            raise ValueError("agent_claim_safety must be positive")
        if self.agent_max_prompt_chars < 1:
            raise ValueError("agent_max_prompt_chars must be positive")
        if self.agent_max_result_chars < 1:
            raise ValueError("agent_max_result_chars must be positive")
        if self.content_check_max_chars < 1:
            raise ValueError("content_check_max_chars must be positive")

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
            enable_agent_executor=_boolean(
                values.get("LARKFLOW_TARGET_ENABLE_AGENT_EXECUTOR", "false")
            ),
            enable_development_executor=_boolean(
                values.get("LARKFLOW_TARGET_ENABLE_DEVELOPMENT_EXECUTOR", "false")
            ),
            enable_content_check_executor=_boolean(
                values.get("LARKFLOW_TARGET_ENABLE_CONTENT_CHECK_EXECUTOR", "false")
            ),
            agent_claim_safety=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_AGENT_CLAIM_SAFETY_SECONDS",
                    30.0,
                )
            ),
            agent_max_prompt_chars=_positive_int(
                values,
                "LARKFLOW_TARGET_AGENT_MAX_PROMPT_CHARS",
                20_000,
            ),
            agent_max_result_chars=_positive_int(
                values,
                "LARKFLOW_TARGET_AGENT_MAX_RESULT_CHARS",
                50_000,
            ),
            content_check_max_chars=_positive_int(
                values,
                "LARKFLOW_TARGET_CONTENT_CHECK_MAX_CHARS",
                50_000,
            ),
        )


@dataclass(frozen=True)
class TargetProjectionSettings:
    dsn: str
    tenant_id: str
    worker_id: str
    claim_ttl: timedelta = timedelta(minutes=2)
    claim_limit: int = 20
    retry_base: timedelta = timedelta(seconds=5)
    retry_max: timedelta = timedelta(minutes=5)
    reconcile_batch_size: int = 100
    completion_poll_seconds: float = 30.0
    completion_poll_batch_size: int = 100
    loop: WorkerLoopSettings = WorkerLoopSettings()

    def __post_init__(self) -> None:
        if not self.dsn.strip():
            raise ValueError("Target PostgreSQL DSN is required")
        if not self.tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        if not self.worker_id.strip():
            raise ValueError("Target projection worker_id is required")
        if self.claim_ttl <= timedelta(0):
            raise ValueError("projection claim_ttl must be positive")
        if self.claim_limit < 1:
            raise ValueError("projection claim_limit must be positive")
        if self.retry_base <= timedelta(0) or self.retry_max < self.retry_base:
            raise ValueError("projection retry delays are invalid")
        if self.reconcile_batch_size < 1:
            raise ValueError("projection reconcile_batch_size must be positive")
        if self.completion_poll_seconds <= 0:
            raise ValueError("completion_poll_seconds must be positive")
        if self.completion_poll_batch_size < 1:
            raise ValueError("completion_poll_batch_size must be positive")

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        dsn: str | None = None,
        tenant_id: str | None = None,
        worker_id: str | None = None,
    ) -> TargetProjectionSettings:
        values = os.environ if environ is None else environ
        identity = (
            worker_id
            or values.get("LARKFLOW_TARGET_PROJECTION_WORKER_ID")
            or f"{socket.gethostname()}:{os.getpid()}:projection"
        )
        return cls(
            dsn=dsn or values.get("LARKFLOW_TARGET_DSN", ""),
            tenant_id=tenant_id or values.get("LARKFLOW_TARGET_TENANT", ""),
            worker_id=identity,
            claim_ttl=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_PROJECTION_CLAIM_TTL_SECONDS",
                    120.0,
                )
            ),
            claim_limit=_positive_int(
                values,
                "LARKFLOW_TARGET_PROJECTION_CLAIM_LIMIT",
                20,
            ),
            retry_base=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_PROJECTION_RETRY_BASE_SECONDS",
                    5.0,
                )
            ),
            retry_max=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_PROJECTION_RETRY_MAX_SECONDS",
                    300.0,
                )
            ),
            reconcile_batch_size=_positive_int(
                values,
                "LARKFLOW_TARGET_PROJECTION_RECONCILE_BATCH_SIZE",
                100,
            ),
            completion_poll_seconds=_positive_float(
                values,
                "LARKFLOW_TARGET_COMPLETION_POLL_SECONDS",
                30.0,
            ),
            completion_poll_batch_size=_positive_int(
                values,
                "LARKFLOW_TARGET_COMPLETION_POLL_BATCH_SIZE",
                100,
            ),
            loop=WorkerLoopSettings(
                idle_min_seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_PROJECTION_IDLE_MIN_SECONDS",
                    0.25,
                ),
                idle_max_seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_PROJECTION_IDLE_MAX_SECONDS",
                    5.0,
                ),
            ),
        )


@dataclass(frozen=True)
class TargetInboundSettings:
    dsn: str
    tenant_id: str
    worker_id: str
    claim_ttl: timedelta = timedelta(minutes=2)
    claim_limit: int = 20
    retry_base: timedelta = timedelta(seconds=5)
    retry_max: timedelta = timedelta(minutes=5)
    verification_max_attempts: int = 24
    loop: WorkerLoopSettings = WorkerLoopSettings()

    def __post_init__(self) -> None:
        if not self.dsn.strip():
            raise ValueError("Target PostgreSQL DSN is required")
        if not self.tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        if not self.worker_id.strip():
            raise ValueError("Target inbound worker_id is required")
        if self.claim_ttl <= timedelta(0):
            raise ValueError("inbound claim_ttl must be positive")
        if self.claim_limit < 1:
            raise ValueError("inbound claim_limit must be positive")
        if self.retry_base <= timedelta(0) or self.retry_max < self.retry_base:
            raise ValueError("inbound retry delays are invalid")
        if self.verification_max_attempts < 1:
            raise ValueError("inbound verification_max_attempts must be positive")

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        dsn: str | None = None,
        tenant_id: str | None = None,
        worker_id: str | None = None,
    ) -> TargetInboundSettings:
        values = os.environ if environ is None else environ
        identity = (
            worker_id
            or values.get("LARKFLOW_TARGET_INBOUND_WORKER_ID")
            or f"{socket.gethostname()}:{os.getpid()}:inbound"
        )
        return cls(
            dsn=dsn or values.get("LARKFLOW_TARGET_DSN", ""),
            tenant_id=tenant_id or values.get("LARKFLOW_TARGET_TENANT", ""),
            worker_id=identity,
            claim_ttl=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_INBOUND_CLAIM_TTL_SECONDS",
                    120.0,
                )
            ),
            claim_limit=_positive_int(
                values,
                "LARKFLOW_TARGET_INBOUND_CLAIM_LIMIT",
                20,
            ),
            retry_base=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_INBOUND_RETRY_BASE_SECONDS",
                    5.0,
                )
            ),
            retry_max=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_INBOUND_RETRY_MAX_SECONDS",
                    300.0,
                )
            ),
            verification_max_attempts=_positive_int(
                values,
                "LARKFLOW_TARGET_INBOUND_VERIFICATION_MAX_ATTEMPTS",
                24,
            ),
            loop=WorkerLoopSettings(
                idle_min_seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_INBOUND_IDLE_MIN_SECONDS",
                    0.25,
                ),
                idle_max_seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_INBOUND_IDLE_MAX_SECONDS",
                    5.0,
                ),
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
