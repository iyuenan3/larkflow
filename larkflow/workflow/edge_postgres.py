"""PostgreSQL store for Personal Agent Edge proof identities."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import secrets
from typing import Any

from psycopg.types.json import Jsonb

from .edge import (
    DeviceRevokedError,
    EdgeAuditEvent,
    EdgeDevice,
    EdgeDeviceNotFoundError,
    EdgeDeviceStatus,
    EdgePairingTicket,
    InvalidDeviceCredentialError,
    InvalidPairingCodeError,
    PairingCodeExpiredError,
    PairingCodeUsedError,
    UnsupportedEdgeCapabilityError,
)
from .serde import to_json_value


class PostgresEdgeStore:
    """Keep one-time pairing and revocation atomic in PostgreSQL."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def create_pairing(
        self,
        ticket: EdgePairingTicket,
        event: EdgeAuditEvent,
    ) -> None:
        with self.connection_factory() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO workflow_edge_pairing_tickets (
                        id, tenant_id, person_id, code_hash,
                        allowed_capabilities, created_at, expires_at,
                        created_by_person_id, used_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        ticket.id,
                        ticket.tenant_id,
                        ticket.person_id,
                        ticket.code_hash,
                        Jsonb(list(ticket.allowed_capabilities)),
                        ticket.created_at,
                        ticket.expires_at,
                        ticket.created_by_person_id,
                        ticket.used_at,
                    ),
                )
                self._insert_event(connection, event)

    def pair_device(
        self,
        ticket_id: str,
        code_hash: str,
        *,
        device_id: str,
        name: str,
        capabilities: tuple[str, ...],
        credential_hash: str,
        now: datetime,
        event_id: str,
    ) -> EdgeDevice:
        with self.connection_factory() as connection:
            with connection.transaction():
                ticket = connection.execute(
                    """
                    SELECT * FROM workflow_edge_pairing_tickets
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (ticket_id,),
                ).fetchone()
                if ticket is None or not secrets.compare_digest(
                    str(ticket["code_hash"]),
                    code_hash,
                ):
                    raise InvalidPairingCodeError("pairing code is invalid")
                if ticket["used_at"] is not None:
                    raise PairingCodeUsedError("pairing code has already been used")
                if now >= ticket["expires_at"]:
                    raise PairingCodeExpiredError("pairing code has expired")
                allowed = {str(item) for item in ticket["allowed_capabilities"]}
                if not capabilities or not set(capabilities).issubset(allowed):
                    raise UnsupportedEdgeCapabilityError(
                        "requested capabilities are not allowed by this pairing code"
                    )

                device = EdgeDevice(
                    id=device_id,
                    tenant_id=str(ticket["tenant_id"]),
                    person_id=str(ticket["person_id"]),
                    name=name,
                    capabilities=capabilities,
                    credential_hash=credential_hash,
                    status=EdgeDeviceStatus.ACTIVE,
                    created_at=now,
                    last_seen_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO workflow_edge_devices (
                        id, tenant_id, person_id, name, capabilities,
                        credential_hash, status, created_at, last_seen_at,
                        revoked_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    """,
                    (
                        device.id,
                        device.tenant_id,
                        device.person_id,
                        device.name,
                        Jsonb(list(device.capabilities)),
                        device.credential_hash,
                        device.status.value,
                        device.created_at,
                        device.last_seen_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE workflow_edge_pairing_tickets
                    SET used_at = %s
                    WHERE id = %s AND used_at IS NULL
                    """,
                    (now, ticket_id),
                )
                self._insert_event(
                    connection,
                    EdgeAuditEvent(
                        id=event_id,
                        tenant_id=device.tenant_id,
                        person_id=device.person_id,
                        device_id=device.id,
                        event_type="edge.device_paired",
                        actor_person_id=device.person_id,
                        occurred_at=now,
                        payload={
                            "name": device.name,
                            "capabilities": device.capabilities,
                        },
                    ),
                )
        return device

    def authenticate_device(
        self,
        device_id: str,
        credential_hash: str,
        *,
        now: datetime,
    ) -> EdgeDevice:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT * FROM workflow_edge_devices
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (device_id,),
                ).fetchone()
                if row is None or not secrets.compare_digest(
                    str(row["credential_hash"]),
                    credential_hash,
                ):
                    raise InvalidDeviceCredentialError(
                        "device credential is invalid"
                    )
                if row["status"] == EdgeDeviceStatus.REVOKED.value:
                    raise DeviceRevokedError("device has been revoked")
                row = connection.execute(
                    """
                    UPDATE workflow_edge_devices
                    SET last_seen_at = %s
                    WHERE id = %s AND status = 'active'
                    RETURNING *
                    """,
                    (now, device_id),
                ).fetchone()
                if row is None:
                    raise DeviceRevokedError("device has been revoked")
        return self._device_from_row(row)

    def list_devices(self, tenant_id: str) -> tuple[EdgeDevice, ...]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_edge_devices
                WHERE tenant_id = %s
                ORDER BY id
                """,
                (tenant_id,),
            ).fetchall()
        return tuple(self._device_from_row(row) for row in rows)

    def revoke_device(
        self,
        tenant_id: str,
        device_id: str,
        *,
        actor_person_id: str,
        reason: str,
        now: datetime,
        event_id: str,
    ) -> EdgeDevice:
        with self.connection_factory() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT * FROM workflow_edge_devices
                    WHERE tenant_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, device_id),
                ).fetchone()
                if row is None:
                    raise EdgeDeviceNotFoundError(device_id)
                if row["status"] == EdgeDeviceStatus.REVOKED.value:
                    return self._device_from_row(row)
                row = connection.execute(
                    """
                    UPDATE workflow_edge_devices
                    SET status = 'revoked', revoked_at = %s
                    WHERE tenant_id = %s AND id = %s AND status = 'active'
                    RETURNING *
                    """,
                    (now, tenant_id, device_id),
                ).fetchone()
                if row is None:
                    raise EdgeDeviceNotFoundError(device_id)
                device = self._device_from_row(row)
                self._insert_event(
                    connection,
                    EdgeAuditEvent(
                        id=event_id,
                        tenant_id=tenant_id,
                        person_id=device.person_id,
                        device_id=device.id,
                        event_type="edge.device_revoked",
                        actor_person_id=actor_person_id,
                        occurred_at=now,
                        payload={"reason": reason},
                    ),
                )
        return device

    def audit_log(self, tenant_id: str) -> tuple[EdgeAuditEvent, ...]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_edge_events
                WHERE tenant_id = %s
                ORDER BY occurred_at, id
                """,
                (tenant_id,),
            ).fetchall()
        return tuple(
            EdgeAuditEvent(
                id=str(row["id"]),
                tenant_id=str(row["tenant_id"]),
                person_id=str(row["person_id"]),
                device_id=(str(row["device_id"]) if row["device_id"] else None),
                event_type=str(row["event_type"]),
                actor_person_id=(
                    str(row["actor_person_id"])
                    if row["actor_person_id"]
                    else None
                ),
                payload=row["payload"],
                occurred_at=row["occurred_at"],
            )
            for row in rows
        )

    @staticmethod
    def _insert_event(connection: Any, event: EdgeAuditEvent) -> None:
        connection.execute(
            """
            INSERT INTO workflow_edge_events (
                id, tenant_id, person_id, device_id, event_type,
                actor_person_id, payload, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.id,
                event.tenant_id,
                event.person_id,
                event.device_id,
                event.event_type,
                event.actor_person_id,
                Jsonb(to_json_value(event.payload)),
                event.occurred_at,
            ),
        )

    @staticmethod
    def _device_from_row(row: Any) -> EdgeDevice:
        return EdgeDevice(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            person_id=str(row["person_id"]),
            name=str(row["name"]),
            capabilities=tuple(str(item) for item in row["capabilities"]),
            credential_hash=str(row["credential_hash"]),
            status=EdgeDeviceStatus(str(row["status"])),
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            revoked_at=row["revoked_at"],
        )
