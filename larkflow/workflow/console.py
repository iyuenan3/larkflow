"""Owner-scoped read model for the central workflow console."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import secrets
from typing import Any

from .events import AuditEvent
from .model import NodeAttempt, WorkflowInstance, WorkflowInstanceSummary
from .repository import InstanceNotFoundError, WorkflowRepository
from .serde import quality_to_dict, to_json_value


class InvalidConsoleCredentialError(PermissionError):
    pass


class ConsoleResourceNotFoundError(KeyError):
    pass


@dataclass(frozen=True)
class ConsolePrincipal:
    tenant_id: str
    person_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.person_id.strip():
            raise ValueError("console principal requires tenant and person")


class StaticConsoleAuthenticator:
    """Resolve one loopback bearer credential to one server-side principal."""

    def __init__(self, access_token: str, principal: ConsolePrincipal) -> None:
        token = access_token.strip()
        if len(token) < 32:
            raise ValueError("console access token must contain at least 32 characters")
        self._access_token = token
        self._principal = principal

    def authenticate(self, headers: Mapping[str, str]) -> ConsolePrincipal:
        authorization = next(
            (
                value
                for key, value in headers.items()
                if key.lower() == "authorization"
            ),
            "",
        )
        scheme, separator, credential = authorization.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not separator
            or not secrets.compare_digest(credential.strip(), self._access_token)
        ):
            raise InvalidConsoleCredentialError("console credential is invalid")
        return self._principal


class ConsoleReadService:
    """Build bounded DTOs without exposing repository objects or identities."""

    def __init__(
        self,
        repository: WorkflowRepository,
        *,
        max_audit_events: int = 200,
        max_result_bytes: int = 32_000,
    ) -> None:
        if max_audit_events < 1 or max_audit_events > 500:
            raise ValueError("max_audit_events must be between 1 and 500")
        if max_result_bytes < 256:
            raise ValueError("max_result_bytes must be at least 256")
        self.repository = repository
        self.max_audit_events = max_audit_events
        self.max_result_bytes = max_result_bytes

    def list_instances(
        self,
        principal: ConsolePrincipal,
        *,
        limit: int = 30,
    ) -> dict[str, Any]:
        summaries = self.repository.list_for_owner(
            principal.tenant_id,
            owner_person_id=principal.person_id,
            limit=limit,
        )
        return {
            "instances": [self._summary(item) for item in summaries],
            "limit": limit,
        }

    def get_instance(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
    ) -> dict[str, Any]:
        try:
            instance = self.repository.get(principal.tenant_id, instance_id)
        except InstanceNotFoundError as exc:
            raise ConsoleResourceNotFoundError(instance_id) from exc
        if not secrets.compare_digest(instance.owner_person_id, principal.person_id):
            raise ConsoleResourceNotFoundError(instance_id)

        audit_events = self.repository.recent_audit_log(
            principal.tenant_id,
            instance.id,
            limit=self.max_audit_events,
        )
        completed_nodes = sum(
            1 for node in instance.nodes.values() if node.status.value == "done"
        )
        return {
            "instance": {
                "id": instance.id,
                "goal": instance.snapshot.goal,
                "status": instance.status.value,
                "graph_revision": instance.graph_revision,
                "version": instance.version,
                "created_at": to_json_value(instance.created_at),
                "confirmed_at": to_json_value(instance.confirmed_at),
                "completed_at": to_json_value(instance.completed_at),
                "progress": {
                    "completed_nodes": completed_nodes,
                    "total_nodes": len(instance.snapshot.nodes),
                },
            },
            "nodes": [
                self._node(instance, spec.key, principal.person_id)
                for spec in instance.snapshot.nodes
            ],
            "audit": [
                self._audit(event, principal.person_id) for event in audit_events
            ],
        }

    @staticmethod
    def _summary(summary: WorkflowInstanceSummary) -> dict[str, Any]:
        return {
            "id": summary.id,
            "goal": summary.goal,
            "status": summary.status.value,
            "completed_nodes": summary.completed_nodes,
            "total_nodes": summary.total_nodes,
            "created_at": summary.created_at.isoformat(),
        }

    def _node(
        self,
        instance: WorkflowInstance,
        node_key: str,
        person_id: str,
    ) -> dict[str, Any]:
        spec = instance.snapshot.node(node_key)
        node = instance.nodes.get(node_key)
        if node is None:
            return {
                "key": spec.key,
                "title": spec.title,
                "executor": spec.executor.value,
                "deps": list(spec.deps),
                "owner_relation": self._person_relation(
                    spec.owner_person_id,
                    person_id,
                ),
                "status": "pending",
                "current_attempt_no": 0,
                "version": 0,
                "ready_at": None,
                "started_at": None,
                "completed_at": None,
                "attempts": [],
            }
        attempts = sorted(
            (
                attempt
                for (candidate_key, _), attempt in instance.attempts.items()
                if candidate_key == node_key
            ),
            key=lambda item: item.attempt_no,
        )
        return {
            "key": spec.key,
            "title": spec.title,
            "executor": spec.executor.value,
            "deps": list(spec.deps),
            "owner_relation": self._person_relation(spec.owner_person_id, person_id),
            "status": node.status.value,
            "current_attempt_no": node.current_attempt_no,
            "version": node.version,
            "ready_at": to_json_value(node.ready_at),
            "started_at": to_json_value(node.started_at),
            "completed_at": to_json_value(node.completed_at),
            "attempts": [self._attempt(item, person_id) for item in attempts],
        }

    def _attempt(self, attempt: NodeAttempt, person_id: str) -> dict[str, Any]:
        return {
            "attempt_no": attempt.attempt_no,
            "status": attempt.status.value,
            "started_at": to_json_value(attempt.started_at),
            "completed_at": to_json_value(attempt.completed_at),
            "submitted_by": self._person_relation(
                attempt.submitted_by_person_id,
                person_id,
            ),
            "claimed": attempt.claimed_by is not None,
            "result": self._bounded_result(attempt.result),
            "quality": quality_to_dict(attempt.quality_result),
            "error_code": attempt.error_code,
            "has_error_detail": bool(attempt.error_message),
        }

    def _bounded_result(self, result: Mapping[str, Any] | None) -> Any:
        if result is None:
            return None
        value = to_json_value(result)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) <= self.max_result_bytes:
            return value
        preview = encoded[: self.max_result_bytes].decode("utf-8", errors="ignore")
        return {
            "_truncated": True,
            "original_bytes": len(encoded),
            "preview": preview,
        }

    @staticmethod
    def _audit(event: AuditEvent, person_id: str) -> dict[str, Any]:
        return {
            "id": event.id,
            "event_type": event.event_type,
            "source": event.source,
            "aggregate_version": event.aggregate_version,
            "occurred_at": event.occurred_at.isoformat(),
            "actor_relation": ConsoleReadService._person_relation(
                event.actor_person_id,
                person_id,
            ),
            "node_key": event.node_key,
            "attempt_no": event.attempt_no,
        }

    @staticmethod
    def _person_relation(value: str | None, person_id: str) -> str:
        if value is None:
            return "system"
        if secrets.compare_digest(value, person_id):
            return "you"
        return "collaborator"
