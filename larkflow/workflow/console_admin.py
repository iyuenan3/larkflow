"""Tenant-scoped, read-only operations overview for Console administrators."""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from typing import Any, Protocol

from .console import ConsolePrincipal, ConsoleResourceNotFoundError
from .migrate import available_migrations


ConnectionFactory = Callable[[], Any]
INSTANCE_STATUSES = (
    "draft",
    "running",
    "paused",
    "done",
    "failed",
    "canceled",
    "discarded",
)


@dataclass(frozen=True)
class QueueLaneSnapshot:
    key: str
    total: int
    ready: int
    in_flight: int
    failed: int
    exhausted: int
    expired_claims: int
    oldest_ready_at: datetime | None


@dataclass(frozen=True)
class ConsoleAdminSnapshot:
    instance_counts: Mapping[str, int]
    distinct_owners: int
    active_sessions: int
    active_session_people: int
    sessions_expiring_within_hour: int
    expired_sessions: int
    queue_lanes: tuple[QueueLaneSnapshot, ...]
    applied_migrations: tuple[str, ...]


class ConsoleAdminRepository(Protocol):
    def read_admin_snapshot(
        self,
        tenant_id: str,
        *,
        now: datetime,
    ) -> ConsoleAdminSnapshot:
        """Return bounded aggregate signals for exactly one tenant."""


@dataclass(frozen=True)
class _QueueLane:
    key: str
    table: str
    status: str
    available_at: str
    claim_token: str
    claim_expires_at: str
    active_statuses: tuple[str, ...]
    in_flight_statuses: tuple[str, ...]


_QUEUE_LANES = (
    _QueueLane(
        "outbox",
        "workflow_outbox_events",
        "status",
        "available_at",
        "claim_token",
        "claim_expires_at",
        ("pending", "processing", "failed"),
        ("processing",),
    ),
    _QueueLane(
        "inbox",
        "workflow_inbox_events",
        "status",
        "available_at",
        "claim_token",
        "claim_expires_at",
        ("pending", "verifying", "verified", "processing", "failed"),
        ("verifying", "processing"),
    ),
    _QueueLane(
        "im_commands",
        "workflow_im_commands",
        "status",
        "available_at",
        "claim_token",
        "claim_expires_at",
        ("pending", "verifying", "verified", "processing", "failed"),
        ("verifying", "processing"),
    ),
    _QueueLane(
        "im_replies",
        "workflow_im_commands",
        "reply_status",
        "reply_available_at",
        "reply_claim_token",
        "reply_claim_expires_at",
        ("pending", "sending", "failed"),
        ("sending",),
    ),
    _QueueLane(
        "role_actions",
        "workflow_role_binding_actions",
        "status",
        "available_at",
        "claim_token",
        "claim_expires_at",
        ("pending", "verifying", "verified", "processing", "failed"),
        ("verifying", "processing"),
    ),
    _QueueLane(
        "role_replies",
        "workflow_role_binding_actions",
        "reply_status",
        "reply_available_at",
        "reply_claim_token",
        "reply_claim_expires_at",
        ("pending", "sending", "failed"),
        ("sending",),
    ),
    _QueueLane(
        "role_progress",
        "workflow_role_binding_actions",
        "progress_status",
        "progress_available_at",
        "progress_claim_token",
        "progress_claim_expires_at",
        ("pending", "sending", "failed"),
        ("sending",),
    ),
)


class PostgresConsoleAdminRepository:
    """Read sanitized tenant aggregates from the authoritative database."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self.connection_factory = connection_factory

    def read_admin_snapshot(
        self,
        tenant_id: str,
        *,
        now: datetime,
    ) -> ConsoleAdminSnapshot:
        with self.connection_factory() as connection:
            instance_rows = connection.execute(
                """
                SELECT status, count(*)::integer AS count
                FROM workflow_instances
                WHERE tenant_id = %s
                GROUP BY status
                """,
                (tenant_id,),
            ).fetchall()
            owner_row = connection.execute(
                """
                SELECT count(DISTINCT owner_person_id)::integer AS count
                FROM workflow_instances
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            ).fetchone()
            session_row = connection.execute(
                """
                SELECT
                    count(*) FILTER (WHERE expires_at > %s)::integer AS active,
                    count(DISTINCT person_id) FILTER (
                        WHERE expires_at > %s
                    )::integer AS active_people,
                    count(*) FILTER (
                        WHERE expires_at > %s AND expires_at <= %s
                    )::integer AS expiring_soon,
                    count(*) FILTER (WHERE expires_at <= %s)::integer AS expired
                FROM workflow_console_sessions
                WHERE tenant_id = %s
                """,
                (
                    now,
                    now,
                    now,
                    now + timedelta(hours=1),
                    now,
                    tenant_id,
                ),
            ).fetchone()
            lane_rows = connection.execute(
                _queue_health_sql(),
                _queue_health_parameters(tenant_id, now),
            ).fetchall()
            migration_rows = connection.execute(
                """
                SELECT version
                FROM workflow_schema_migrations
                ORDER BY version
                """
            ).fetchall()

        counts = {status: 0 for status in INSTANCE_STATUSES}
        for row in instance_rows:
            status = row["status"]
            if status in counts:
                counts[status] = int(row["count"])
        sessions = session_row or {}
        return ConsoleAdminSnapshot(
            instance_counts=counts,
            distinct_owners=int((owner_row or {}).get("count") or 0),
            active_sessions=int(sessions.get("active") or 0),
            active_session_people=int(sessions.get("active_people") or 0),
            sessions_expiring_within_hour=int(sessions.get("expiring_soon") or 0),
            expired_sessions=int(sessions.get("expired") or 0),
            queue_lanes=tuple(
                QueueLaneSnapshot(
                    key=row["lane"],
                    total=int(row["total"] or 0),
                    ready=int(row["ready"] or 0),
                    in_flight=int(row["in_flight"] or 0),
                    failed=int(row["failed"] or 0),
                    exhausted=int(row["exhausted"] or 0),
                    expired_claims=int(row["expired_claims"] or 0),
                    oldest_ready_at=row["oldest_ready_at"],
                )
                for row in lane_rows
            ),
            applied_migrations=tuple(row["version"] for row in migration_rows),
        )


class ConsoleAdminReadService:
    """Authorize server-side administrators and return sanitized aggregates."""

    def __init__(
        self,
        repository: ConsoleAdminRepository,
        *,
        tenant_id: str,
        allowed_person_ids: Collection[str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        tenant_id = tenant_id.strip()
        person_ids = tuple(dict.fromkeys(item.strip() for item in allowed_person_ids))
        if not tenant_id:
            raise ValueError("admin service requires a tenant")
        if not person_ids or any(not item for item in person_ids):
            raise ValueError("admin service requires at least one person")
        if len(person_ids) > 100:
            raise ValueError("admin service accepts at most 100 people")
        self.repository = repository
        self.tenant_id = tenant_id
        self._allowed_person_ids = person_ids
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._expected_migrations = tuple(
            version for version, _ in available_migrations()
        )

    def is_admin(self, principal: ConsolePrincipal) -> bool:
        return secrets.compare_digest(principal.tenant_id, self.tenant_id) and any(
            secrets.compare_digest(principal.person_id, allowed)
            for allowed in self._allowed_person_ids
        )

    def overview(self, principal: ConsolePrincipal) -> dict[str, Any]:
        if not self.is_admin(principal):
            raise ConsoleResourceNotFoundError("admin overview")
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("admin clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        snapshot = self.repository.read_admin_snapshot(
            principal.tenant_id,
            now=now,
        )
        applied = set(snapshot.applied_migrations)
        expected = set(self._expected_migrations)
        instance_counts = {
            status: int(snapshot.instance_counts.get(status, 0))
            for status in INSTANCE_STATUSES
        }
        return {
            "scope": "current_tenant",
            "read_only": True,
            "generated_at": now.isoformat(),
            "workflows": {
                "total": sum(instance_counts.values()),
                "distinct_owners": snapshot.distinct_owners,
                "by_status": instance_counts,
            },
            "sessions": {
                "active": snapshot.active_sessions,
                "active_people": snapshot.active_session_people,
                "expiring_within_hour": snapshot.sessions_expiring_within_hour,
                "expired_stored": snapshot.expired_sessions,
            },
            "queues": {
                "lanes": [
                    {
                        "key": lane.key,
                        "total": lane.total,
                        "ready": lane.ready,
                        "in_flight": lane.in_flight,
                        "failed": lane.failed,
                        "exhausted": lane.exhausted,
                        "expired_claims": lane.expired_claims,
                        "oldest_ready_at": (
                            lane.oldest_ready_at.isoformat()
                            if lane.oldest_ready_at is not None
                            else None
                        ),
                    }
                    for lane in snapshot.queue_lanes
                ],
                "attention_total": sum(
                    lane.failed + lane.exhausted + lane.expired_claims
                    for lane in snapshot.queue_lanes
                ),
            },
            "migrations": {
                "applied_count": len(applied),
                "expected_count": len(expected),
                "latest_applied": max(applied) if applied else None,
                "latest_expected": max(expected) if expected else None,
                "missing_count": len(expected - applied),
                "unexpected_count": len(applied - expected),
                "up_to_date": applied == expected,
            },
        }


def _sql_literals(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _queue_health_sql() -> str:
    statements = []
    for lane in _QUEUE_LANES:
        active = _sql_literals(lane.active_statuses)
        in_flight = _sql_literals(lane.in_flight_statuses)
        ready = (
            f"{lane.status} IN ({active}) "
            f"AND {lane.available_at} <= %s "
            f"AND ({lane.claim_token} IS NULL "
            f"OR {lane.claim_expires_at} IS NULL "
            f"OR {lane.claim_expires_at} <= %s)"
        )
        statements.append(
            f"""
            SELECT '{lane.key}'::text AS lane,
                   count({lane.status})::integer AS total,
                   count(*) FILTER (WHERE {ready})::integer AS ready,
                   count(*) FILTER (
                       WHERE {lane.status} IN ({in_flight})
                         AND {lane.claim_token} IS NOT NULL
                         AND {lane.claim_expires_at} > %s
                   )::integer AS in_flight,
                   count(*) FILTER (
                       WHERE {lane.status} = 'failed'
                   )::integer AS failed,
                   count(*) FILTER (
                       WHERE {lane.status} = 'exhausted'
                   )::integer AS exhausted,
                   count(*) FILTER (
                       WHERE {lane.status} IN ({in_flight})
                         AND {lane.claim_token} IS NOT NULL
                         AND {lane.claim_expires_at} <= %s
                   )::integer AS expired_claims,
                   min({lane.available_at}) FILTER (WHERE {ready})
                       AS oldest_ready_at
            FROM {lane.table}
            WHERE tenant_id = %s
              AND {lane.status} IS NOT NULL
            """
        )
    return " UNION ALL ".join(statements) + " ORDER BY lane"


def _queue_health_parameters(tenant_id: str, now: datetime) -> tuple[Any, ...]:
    parameters: list[Any] = []
    for _lane in _QUEUE_LANES:
        parameters.extend((now, now, now, now, now, now, tenant_id))
    return tuple(parameters)
