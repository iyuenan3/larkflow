"""Personal Agent Edge proof: device identity and leased work delivery."""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import secrets
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from .model import ExecutorKind, FrozenDict, NodeActivation, QualityResult
from .repository import ConcurrentUpdateError, WorkflowRepository
from .runtime import ExecutionRequest
from .serde import to_json_value
from .service import WorkflowService


PERSONAL_READONLY_CAPABILITY = "personal.readonly"
DEFAULT_EDGE_CAPABILITIES = frozenset({PERSONAL_READONLY_CAPABILITY})


class EdgeError(RuntimeError):
    """Base error for the Edge control boundary."""


class InvalidPairingCodeError(EdgeError):
    pass


class PairingCodeExpiredError(EdgeError):
    pass


class PairingCodeUsedError(EdgeError):
    pass


class InvalidDeviceCredentialError(EdgeError):
    pass


class DeviceRevokedError(EdgeError):
    pass


class EdgeDeviceNotFoundError(EdgeError):
    pass


class UnsupportedEdgeCapabilityError(EdgeError):
    pass


class EdgeDeviceStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass(frozen=True)
class EdgePairingTicket:
    id: str
    tenant_id: str
    person_id: str
    code_hash: str
    allowed_capabilities: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    created_by_person_id: str
    used_at: datetime | None = None


@dataclass(frozen=True)
class EdgePairingGrant:
    code: str
    expires_at: datetime
    allowed_capabilities: tuple[str, ...]


@dataclass(frozen=True)
class EdgeDevice:
    id: str
    tenant_id: str
    person_id: str
    name: str
    capabilities: tuple[str, ...]
    credential_hash: str
    status: EdgeDeviceStatus
    created_at: datetime
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", EdgeDeviceStatus(self.status))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


@dataclass(frozen=True)
class EdgeDeviceCredential:
    device: EdgeDevice
    credential: str


@dataclass(frozen=True)
class EdgeAuditEvent:
    id: str
    tenant_id: str
    person_id: str
    event_type: str
    actor_person_id: str | None
    occurred_at: datetime
    device_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", FrozenDict(self.payload))


@dataclass(frozen=True)
class EdgeLease:
    device_id: str
    request: ExecutionRequest


class EdgeStore(Protocol):
    def create_pairing(
        self,
        ticket: EdgePairingTicket,
        event: EdgeAuditEvent,
    ) -> None:
        ...

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
        ...

    def authenticate_device(
        self,
        device_id: str,
        credential_hash: str,
        *,
        now: datetime,
    ) -> EdgeDevice:
        ...

    def list_devices(self, tenant_id: str) -> tuple[EdgeDevice, ...]:
        ...

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
        ...


class InMemoryEdgeStore:
    """Thread-safe proof store with the same one-time pairing semantics as SQL."""

    def __init__(self) -> None:
        self._pairings: dict[str, EdgePairingTicket] = {}
        self._devices: dict[str, EdgeDevice] = {}
        self._events: list[EdgeAuditEvent] = []
        self._lock = RLock()

    def create_pairing(
        self,
        ticket: EdgePairingTicket,
        event: EdgeAuditEvent,
    ) -> None:
        with self._lock:
            if ticket.id in self._pairings:
                raise ValueError(f"pairing ticket already exists: {ticket.id}")
            self._pairings[ticket.id] = ticket
            self._events.append(event)

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
        with self._lock:
            ticket = self._pairings.get(ticket_id)
            if ticket is None or not secrets.compare_digest(
                ticket.code_hash,
                code_hash,
            ):
                raise InvalidPairingCodeError("pairing code is invalid")
            if ticket.used_at is not None:
                raise PairingCodeUsedError("pairing code has already been used")
            if now >= ticket.expires_at:
                raise PairingCodeExpiredError("pairing code has expired")
            if not capabilities or not set(capabilities).issubset(
                ticket.allowed_capabilities
            ):
                raise UnsupportedEdgeCapabilityError(
                    "requested capabilities are not allowed by this pairing code"
                )
            if device_id in self._devices:
                raise ValueError(f"device already exists: {device_id}")

            device = EdgeDevice(
                id=device_id,
                tenant_id=ticket.tenant_id,
                person_id=ticket.person_id,
                name=name,
                capabilities=capabilities,
                credential_hash=credential_hash,
                status=EdgeDeviceStatus.ACTIVE,
                created_at=now,
                last_seen_at=now,
            )
            self._pairings[ticket_id] = replace(ticket, used_at=now)
            self._devices[device_id] = device
            self._events.append(
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
                )
            )
            return device

    def authenticate_device(
        self,
        device_id: str,
        credential_hash: str,
        *,
        now: datetime,
    ) -> EdgeDevice:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or not secrets.compare_digest(
                device.credential_hash,
                credential_hash,
            ):
                raise InvalidDeviceCredentialError("device credential is invalid")
            if device.status == EdgeDeviceStatus.REVOKED:
                raise DeviceRevokedError("device has been revoked")
            device = replace(device, last_seen_at=now)
            self._devices[device_id] = device
            return device

    def list_devices(self, tenant_id: str) -> tuple[EdgeDevice, ...]:
        with self._lock:
            return tuple(
                device
                for device in sorted(self._devices.values(), key=lambda item: item.id)
                if device.tenant_id == tenant_id
            )

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
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or device.tenant_id != tenant_id:
                raise EdgeDeviceNotFoundError(device_id)
            if device.status == EdgeDeviceStatus.REVOKED:
                return device
            device = replace(
                device,
                status=EdgeDeviceStatus.REVOKED,
                revoked_at=now,
            )
            self._devices[device_id] = device
            self._events.append(
                EdgeAuditEvent(
                    id=event_id,
                    tenant_id=tenant_id,
                    person_id=device.person_id,
                    device_id=device.id,
                    event_type="edge.device_revoked",
                    actor_person_id=actor_person_id,
                    occurred_at=now,
                    payload={"reason": reason},
                )
            )
            return device

    def audit_log(self, tenant_id: str) -> tuple[EdgeAuditEvent, ...]:
        with self._lock:
            return tuple(
                event for event in self._events if event.tenant_id == tenant_id
            )


class EdgeControlService:
    """Authorize devices before handing work to the existing Node Runner."""

    def __init__(
        self,
        store: EdgeStore,
        workflow_service: WorkflowService,
        workflow_repository: WorkflowRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        secret_factory: Callable[[], str] | None = None,
        supported_capabilities: Collection[str] = DEFAULT_EDGE_CAPABILITIES,
        candidate_limit: int = 100,
        max_result_bytes: int = 100_000,
    ) -> None:
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        if max_result_bytes < 1:
            raise ValueError("max_result_bytes must be positive")
        if isinstance(supported_capabilities, (str, bytes)):
            raise ValueError("supported_capabilities must be a collection")
        capabilities = frozenset(str(item) for item in supported_capabilities)
        if not capabilities:
            raise ValueError("supported_capabilities cannot be empty")
        self.store = store
        self.workflow_service = workflow_service
        self.workflow_repository = workflow_repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.id_factory = id_factory or (lambda: str(uuid4()))
        self.secret_factory = secret_factory or (lambda: secrets.token_urlsafe(32))
        self.supported_capabilities = capabilities
        self.candidate_limit = candidate_limit
        self.max_result_bytes = max_result_bytes

    def issue_pairing(
        self,
        *,
        tenant_id: str,
        person_id: str,
        actor_person_id: str,
        ttl: timedelta = timedelta(minutes=10),
        allowed_capabilities: Collection[str] = DEFAULT_EDGE_CAPABILITIES,
    ) -> EdgePairingGrant:
        tenant_id = _required_text(tenant_id, "tenant_id")
        person_id = _required_text(person_id, "person_id")
        actor_person_id = _required_text(actor_person_id, "actor_person_id")
        if ttl <= timedelta(0) or ttl > timedelta(hours=1):
            raise ValueError("pairing ttl must be between 0 and 1 hour")
        capabilities = _capabilities(allowed_capabilities)
        if not set(capabilities).issubset(self.supported_capabilities):
            raise UnsupportedEdgeCapabilityError("unsupported pairing capability")

        now = self.clock()
        ticket_id = self.id_factory()
        secret = self.secret_factory()
        ticket = EdgePairingTicket(
            id=ticket_id,
            tenant_id=tenant_id,
            person_id=person_id,
            code_hash=_secret_hash("pairing", secret),
            allowed_capabilities=capabilities,
            created_at=now,
            expires_at=now + ttl,
            created_by_person_id=actor_person_id,
        )
        event = EdgeAuditEvent(
            id=self.id_factory(),
            tenant_id=tenant_id,
            person_id=person_id,
            event_type="edge.pairing_issued",
            actor_person_id=actor_person_id,
            occurred_at=now,
            payload={
                "expires_at": ticket.expires_at.isoformat(),
                "allowed_capabilities": capabilities,
            },
        )
        self.store.create_pairing(ticket, event)
        return EdgePairingGrant(
            code=f"{ticket_id}.{secret}",
            expires_at=ticket.expires_at,
            allowed_capabilities=capabilities,
        )

    def pair_device(
        self,
        code: str,
        *,
        name: str,
        capabilities: Collection[str],
    ) -> EdgeDeviceCredential:
        ticket_id, secret = _split_secret(code, "pairing code")
        name = _required_text(name, "device name")
        if len(name) > 120:
            raise ValueError("device name is too long")
        requested = _capabilities(capabilities)
        if not set(requested).issubset(self.supported_capabilities):
            raise UnsupportedEdgeCapabilityError("unsupported device capability")
        now = self.clock()
        device_id = self.id_factory()
        credential_secret = self.secret_factory()
        device = self.store.pair_device(
            ticket_id,
            _secret_hash("pairing", secret),
            device_id=device_id,
            name=name,
            capabilities=requested,
            credential_hash=_secret_hash("device", credential_secret),
            now=now,
            event_id=self.id_factory(),
        )
        return EdgeDeviceCredential(
            device=device,
            credential=f"{device.id}.{credential_secret}",
        )

    def authenticate(self, credential: str) -> EdgeDevice:
        device_id, secret = _split_secret(credential, "device credential")
        return self.store.authenticate_device(
            device_id,
            _secret_hash("device", secret),
            now=self.clock(),
        )

    def claim(self, credential: str) -> EdgeLease | None:
        device = self.authenticate(credential)
        now = self.clock()
        instance_ids = self.workflow_repository.runnable_instance_ids(
            device.tenant_id,
            now=now,
            limit=self.candidate_limit,
        )
        worker_id = self._worker_id(device.id)
        for instance_id in instance_ids:
            instance = self.workflow_service.get(device.tenant_id, instance_id)
            eligible = {
                spec.key
                for spec in instance.snapshot.nodes
                if self._device_can_run(device, spec.owner_person_id, spec.executor, spec.work)
            }
            if not eligible:
                continue
            try:
                activations = self.workflow_service.dispatch_due(
                    device.tenant_id,
                    instance_id,
                    worker_id=worker_id,
                    max_automated=1,
                    automated_node_keys=eligible,
                )
            except ConcurrentUpdateError:
                continue
            for activation in activations:
                if activation.claimed_by == worker_id:
                    return EdgeLease(
                        device_id=device.id,
                        request=self._execution_request(device.tenant_id, activation),
                    )
        return None

    def renew(
        self,
        credential: str,
        *,
        instance_id: str,
        node_key: str,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
    ) -> datetime:
        device = self.authenticate(credential)
        self._require_lease_owner(device, instance_id, node_key)
        return self.workflow_service.renew_automated_claim(
            device.tenant_id,
            instance_id,
            node_key,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
            claim_token=claim_token,
            worker_id=self._worker_id(device.id),
        )

    def complete(
        self,
        credential: str,
        *,
        instance_id: str,
        node_key: str,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        result: Mapping[str, Any],
        quality_result: QualityResult | None = None,
    ) -> None:
        device = self.authenticate(credential)
        self._require_lease_owner(device, instance_id, node_key)
        encoded = json.dumps(
            to_json_value(result),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > self.max_result_bytes:
            raise ValueError("Edge result exceeds the configured size limit")
        self.workflow_service.complete_automated(
            device.tenant_id,
            instance_id,
            node_key,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
            claim_token=claim_token,
            result=result,
            quality_result=quality_result,
            worker_id=self._worker_id(device.id),
        )

    def fail(
        self,
        credential: str,
        *,
        instance_id: str,
        node_key: str,
        attempt_no: int,
        expected_node_version: int,
        claim_token: str,
        error_code: str,
        error_message: str,
    ) -> None:
        device = self.authenticate(credential)
        self._require_lease_owner(device, instance_id, node_key)
        error_code = _required_text(error_code, "error_code")[:100]
        error_message = _required_text(error_message, "error_message")[:2_000]
        self.workflow_service.fail_automated(
            device.tenant_id,
            instance_id,
            node_key,
            attempt_no=attempt_no,
            expected_node_version=expected_node_version,
            claim_token=claim_token,
            error_code=error_code,
            error_message=error_message,
            worker_id=self._worker_id(device.id),
        )

    def revoke_device(
        self,
        *,
        tenant_id: str,
        device_id: str,
        actor_person_id: str,
        reason: str,
    ) -> EdgeDevice:
        return self.store.revoke_device(
            _required_text(tenant_id, "tenant_id"),
            _required_text(device_id, "device_id"),
            actor_person_id=_required_text(actor_person_id, "actor_person_id"),
            reason=_required_text(reason, "reason"),
            now=self.clock(),
            event_id=self.id_factory(),
        )

    def _execution_request(
        self,
        tenant_id: str,
        activation: NodeActivation,
    ) -> ExecutionRequest:
        instance = self.workflow_service.get(tenant_id, activation.instance_id)
        attempt = instance.current_attempt(activation.node_key)
        if activation.claim_token is None or activation.claim_expires_at is None:
            raise RuntimeError("Edge activation is missing its execution lease")
        return ExecutionRequest(
            tenant_id=tenant_id,
            instance_id=activation.instance_id,
            node_key=activation.node_key,
            attempt_id=activation.attempt_id,
            attempt_no=activation.attempt_no,
            owner_person_id=activation.owner_person_id,
            executor=activation.executor,
            work=instance.snapshot.node(activation.node_key).work,
            input_snapshot=attempt.input_snapshot,
            expected_node_version=activation.expected_node_version,
            claim_token=activation.claim_token,
            claim_expires_at=activation.claim_expires_at,
        )

    def _require_lease_owner(
        self,
        device: EdgeDevice,
        instance_id: str,
        node_key: str,
    ) -> None:
        instance = self.workflow_service.get(device.tenant_id, instance_id)
        spec = instance.snapshot.node(node_key)
        if not self._device_can_run(
            device,
            spec.owner_person_id,
            spec.executor,
            spec.work,
        ):
            raise InvalidDeviceCredentialError(
                "device is not authorized for this work contract"
            )

    @staticmethod
    def _worker_id(device_id: str) -> str:
        return f"edge:{device_id}"

    @staticmethod
    def _device_can_run(
        device: EdgeDevice,
        owner_person_id: str,
        executor: ExecutorKind,
        work: Mapping[str, Any],
    ) -> bool:
        agent = work.get("agent")
        kind = agent.get("kind") if isinstance(agent, Mapping) else None
        return (
            executor == ExecutorKind.AGENT
            and owner_person_id == device.person_id
            and isinstance(kind, str)
            and kind in device.capabilities
        )


def _secret_hash(purpose: str, secret: str) -> str:
    return hashlib.sha256(f"{purpose}:{secret}".encode("utf-8")).hexdigest()


def _split_secret(value: str, label: str) -> tuple[str, str]:
    identifier, separator, secret = value.strip().partition(".")
    if not separator or not identifier or not secret:
        if label == "device credential":
            raise InvalidDeviceCredentialError(f"{label} is invalid")
        raise InvalidPairingCodeError(f"{label} is invalid")
    return identifier, secret


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _capabilities(values: Collection[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("capabilities must be a collection")
    capabilities = tuple(sorted({_required_text(item, "capability") for item in values}))
    if not capabilities:
        raise ValueError("at least one capability is required")
    if len(capabilities) > 20:
        raise ValueError("too many capabilities")
    return capabilities
