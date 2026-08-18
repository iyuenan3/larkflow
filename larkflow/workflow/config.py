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
    enable_web_search_executor: bool = False
    agent_runtime: str = "completion"
    agent_claim_safety: timedelta = timedelta(seconds=30)
    agent_max_prompt_chars: int = 20_000
    agent_max_result_chars: int = 50_000
    agent_context_max_chars: int = 12_000
    attachment_blob_root: str | None = None
    attachment_model_egress_policy: str = "deny"
    content_check_max_chars: int = 50_000
    web_search_max_prompt_chars: int = 20_000
    web_search_max_result_chars: int = 50_000

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
        if self.agent_context_max_chars < 1:
            raise ValueError("agent_context_max_chars must be positive")
        if self.agent_context_max_chars >= self.agent_max_prompt_chars:
            raise ValueError("Agent context budget must be smaller than prompt budget")
        if self.attachment_blob_root is not None:
            root = self.attachment_blob_root.strip()
            if not root or not os.path.isabs(root):
                raise ValueError("Target attachment blob root must be absolute")
        if self.attachment_model_egress_policy not in {"allow", "deny"}:
            raise ValueError("Target attachment model egress policy is invalid")
        if self.content_check_max_chars < 1:
            raise ValueError("content_check_max_chars must be positive")
        if self.web_search_max_prompt_chars < 1:
            raise ValueError("web_search_max_prompt_chars must be positive")
        if self.web_search_max_result_chars < 1:
            raise ValueError("web_search_max_result_chars must be positive")
        if self.agent_runtime != "completion":
            raise ValueError("Target agent_runtime must be completion")

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
            enable_web_search_executor=_boolean(
                values.get("LARKFLOW_TARGET_ENABLE_WEB_SEARCH_EXECUTOR", "false")
            ),
            agent_runtime=values.get(
                "LARKFLOW_TARGET_AGENT_RUNTIME",
                "completion",
            ).strip(),
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
            agent_context_max_chars=_positive_int(
                values,
                "LARKFLOW_TARGET_AGENT_CONTEXT_MAX_CHARS",
                12_000,
            ),
            attachment_blob_root=(
                values.get("LARKFLOW_TARGET_ATTACHMENT_ROOT", "").strip()
                or None
            ),
            attachment_model_egress_policy=values.get(
                "LARKFLOW_TARGET_ATTACHMENT_MODEL_EGRESS",
                "deny",
            ).strip(),
            content_check_max_chars=_positive_int(
                values,
                "LARKFLOW_TARGET_CONTENT_CHECK_MAX_CHARS",
                50_000,
            ),
            web_search_max_prompt_chars=_positive_int(
                values,
                "LARKFLOW_TARGET_WEB_SEARCH_MAX_PROMPT_CHARS",
                20_000,
            ),
            web_search_max_result_chars=_positive_int(
                values,
                "LARKFLOW_TARGET_WEB_SEARCH_MAX_RESULT_CHARS",
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
    max_attempts: int = 24
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
        if self.max_attempts < 1:
            raise ValueError("projection max_attempts must be positive")
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
            max_attempts=_positive_int(
                values,
                "LARKFLOW_TARGET_PROJECTION_MAX_ATTEMPTS",
                24,
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
class TargetInteractiveSettings:
    """Credential-side interactive lane, scaled by separate processes."""

    dsn: str
    tenant_id: str
    worker_id: str
    claim_ttl: timedelta = timedelta(minutes=2)
    claim_limit: int = 1
    retry_base: timedelta = timedelta(seconds=5)
    retry_max: timedelta = timedelta(minutes=5)
    loop: WorkerLoopSettings = WorkerLoopSettings()

    def __post_init__(self) -> None:
        if not self.dsn.strip():
            raise ValueError("Target PostgreSQL DSN is required")
        if not self.tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        if not self.worker_id.strip():
            raise ValueError("Target interactive worker_id is required")
        if self.claim_ttl <= timedelta(0):
            raise ValueError("interactive claim_ttl must be positive")
        if self.claim_limit != 1:
            raise ValueError("interactive claim_limit must be 1")
        if self.retry_base <= timedelta(0) or self.retry_max < self.retry_base:
            raise ValueError("interactive retry delays are invalid")

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        dsn: str | None = None,
        tenant_id: str | None = None,
        worker_id: str | None = None,
    ) -> TargetInteractiveSettings:
        values = os.environ if environ is None else environ
        identity = (
            worker_id
            or values.get("LARKFLOW_TARGET_INTERACTIVE_WORKER_ID")
            or f"{socket.gethostname()}:{os.getpid()}:interactive"
        )
        return cls(
            dsn=dsn or values.get("LARKFLOW_TARGET_DSN", ""),
            tenant_id=tenant_id or values.get("LARKFLOW_TARGET_TENANT", ""),
            worker_id=identity,
            claim_ttl=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_INTERACTIVE_CLAIM_TTL_SECONDS",
                    120.0,
                )
            ),
            claim_limit=_positive_int(
                values,
                "LARKFLOW_TARGET_INTERACTIVE_CLAIM_LIMIT",
                1,
            ),
            retry_base=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_INTERACTIVE_RETRY_BASE_SECONDS",
                    5.0,
                )
            ),
            retry_max=timedelta(
                seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_INTERACTIVE_RETRY_MAX_SECONDS",
                    300.0,
                )
            ),
            loop=WorkerLoopSettings(
                idle_min_seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_INTERACTIVE_IDLE_MIN_SECONDS",
                    0.25,
                ),
                idle_max_seconds=_positive_float(
                    values,
                    "LARKFLOW_TARGET_INTERACTIVE_IDLE_MAX_SECONDS",
                    1.0,
                ),
            ),
        )


@dataclass(frozen=True)
class TargetDraftGenerationSettings:
    """Credential-free process for bounded natural-language draft generation."""

    dsn: str
    tenant_id: str
    worker_id: str
    claim_ttl: timedelta = timedelta(minutes=10)
    claim_limit: int = 1
    retry_base: timedelta = timedelta(seconds=5)
    retry_max: timedelta = timedelta(minutes=5)
    max_attempts: int = 5
    claim_safety: timedelta = timedelta(seconds=30)
    max_result_chars: int = 30_000
    enable_web_search: bool = False
    planner_runtime: str = "bounded"
    attachment_blob_root: str | None = None
    attachment_model_egress_policy: str = "deny"
    loop: WorkerLoopSettings = WorkerLoopSettings()

    def __post_init__(self) -> None:
        if not self.dsn.strip():
            raise ValueError("Target PostgreSQL DSN is required")
        if not self.tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        if not self.worker_id.strip():
            raise ValueError("Target draft worker_id is required")
        if self.claim_ttl <= timedelta(0):
            raise ValueError("draft claim_ttl must be positive")
        if self.claim_limit != 1:
            raise ValueError("draft claim_limit must be 1")
        if self.retry_base <= timedelta(0) or self.retry_max < self.retry_base:
            raise ValueError("draft retry delays are invalid")
        if self.max_attempts < 1 or self.max_attempts > 100:
            raise ValueError("draft max_attempts must be between 1 and 100")
        if self.claim_safety <= timedelta(0):
            raise ValueError("draft claim_safety must be positive")
        if self.max_result_chars < 1:
            raise ValueError("draft max_result_chars must be positive")
        if self.planner_runtime != "bounded":
            raise ValueError("Target planner_runtime must be bounded")
        if self.attachment_blob_root is not None:
            root = self.attachment_blob_root.strip()
            if not root or not os.path.isabs(root):
                raise ValueError("Target attachment blob root must be absolute")
        if self.attachment_model_egress_policy not in {"allow", "deny"}:
            raise ValueError("Target attachment model egress policy is invalid")

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        dsn: str | None = None,
        tenant_id: str | None = None,
        worker_id: str | None = None,
    ) -> TargetDraftGenerationSettings:
        values = os.environ if environ is None else environ
        identity = (
            worker_id
            or values.get("LARKFLOW_TARGET_DRAFT_WORKER_ID")
            or f"{socket.gethostname()}:{os.getpid()}:draft"
        )
        return cls(
            dsn=dsn or values.get("LARKFLOW_TARGET_DSN", ""),
            tenant_id=tenant_id or values.get("LARKFLOW_TARGET_TENANT", ""),
            worker_id=identity,
            claim_ttl=timedelta(
                seconds=_positive_float(
                    values, "LARKFLOW_TARGET_DRAFT_CLAIM_TTL_SECONDS", 600.0
                )
            ),
            claim_limit=_positive_int(
                values, "LARKFLOW_TARGET_DRAFT_CLAIM_LIMIT", 1
            ),
            retry_base=timedelta(
                seconds=_positive_float(
                    values, "LARKFLOW_TARGET_DRAFT_RETRY_BASE_SECONDS", 5.0
                )
            ),
            retry_max=timedelta(
                seconds=_positive_float(
                    values, "LARKFLOW_TARGET_DRAFT_RETRY_MAX_SECONDS", 300.0
                )
            ),
            max_attempts=_positive_int(
                values, "LARKFLOW_TARGET_DRAFT_MAX_ATTEMPTS", 5
            ),
            claim_safety=timedelta(
                seconds=_positive_float(
                    values, "LARKFLOW_TARGET_DRAFT_CLAIM_SAFETY_SECONDS", 30.0
                )
            ),
            max_result_chars=_positive_int(
                values, "LARKFLOW_TARGET_DRAFT_MAX_RESULT_CHARS", 30_000
            ),
            enable_web_search=_boolean(
                values.get("LARKFLOW_TARGET_DRAFT_ENABLE_WEB_SEARCH", "false")
            ),
            planner_runtime=values.get(
                "LARKFLOW_TARGET_PLANNER_RUNTIME",
                "bounded",
            ).strip(),
            attachment_blob_root=(
                values.get("LARKFLOW_TARGET_ATTACHMENT_ROOT", "").strip()
                or None
            ),
            attachment_model_egress_policy=values.get(
                "LARKFLOW_TARGET_ATTACHMENT_MODEL_EGRESS",
                "deny",
            ).strip(),
            loop=WorkerLoopSettings(
                idle_min_seconds=_positive_float(
                    values, "LARKFLOW_TARGET_DRAFT_IDLE_MIN_SECONDS", 0.25
                ),
                idle_max_seconds=_positive_float(
                    values, "LARKFLOW_TARGET_DRAFT_IDLE_MAX_SECONDS", 1.0
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
