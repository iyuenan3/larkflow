"""Audit and transactional outbox contracts."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .model import FrozenDict


class OutboxStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclass(frozen=True)
class AuditEvent:
    id: str
    tenant_id: str
    instance_id: str
    event_type: str
    source: str
    correlation_id: str
    aggregate_version: int
    occurred_at: datetime
    actor_person_id: str | None = None
    node_key: str | None = None
    attempt_no: int | None = None
    payload: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", FrozenDict(self.payload))


@dataclass(frozen=True)
class OutboxEvent:
    id: str
    tenant_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    event_type: str
    payload: Mapping[str, Any]
    created_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", FrozenDict(self.payload))

    @property
    def dedupe_key(self) -> tuple[str, str, str, int]:
        return (
            self.event_type,
            self.aggregate_type,
            self.aggregate_id,
            self.aggregate_version,
        )


@dataclass(frozen=True)
class OutboxClaim:
    event: OutboxEvent
    claim_token: str
    claimed_by: str
    claim_expires_at: datetime
    attempt_count: int


@dataclass
class OutboxRecord:
    event: OutboxEvent
    status: OutboxStatus = OutboxStatus.PENDING
    attempt_count: int = 0
    claimed_by: str | None = None
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    published_at: datetime | None = None
    last_error: str | None = None


class InvalidOutboxClaimError(RuntimeError):
    pass
