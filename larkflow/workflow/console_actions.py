"""Authenticated Owner actions exposed by the central workflow console."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .console import ConsolePrincipal, ConsoleResourceNotFoundError
from .lifecycle import CancellationNotAllowedError, StaleCancellationError
from .model import InstanceStatus, WorkflowInstance
from .repository import ConcurrentUpdateError, InstanceNotFoundError
from .restart import (
    RestartNotAllowedError,
    RestartPreviewExpiredError,
    RestartPreviewNotFoundError,
    StaleRestartPreviewError,
)
from .runner import AuthorizationError
from .serde import to_json_value
from .service import WorkflowService
from .transitions import TransitionError


class ConsoleActionConflictError(RuntimeError):
    """A requested action no longer applies to the current aggregate state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConsoleActionService:
    """Map a server-authenticated principal onto existing domain commands."""

    def __init__(self, service: WorkflowService) -> None:
        self.service = service

    def confirm_draft(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
    ) -> Mapping[str, Any]:
        already_applied = False
        try:
            instance = self.service.confirm_draft(
                principal.tenant_id,
                instance_id,
                actor_person_id=principal.person_id,
            )
        except TransitionError:
            instance = self._owned_instance(principal, instance_id)
            if instance.status not in {InstanceStatus.RUNNING, InstanceStatus.DONE}:
                raise self._state_conflict() from None
            already_applied = True
        except (AuthorizationError, InstanceNotFoundError):
            raise ConsoleResourceNotFoundError(instance_id) from None
        except ConcurrentUpdateError:
            instance = self._owned_instance(principal, instance_id)
            if instance.status not in {InstanceStatus.RUNNING, InstanceStatus.DONE}:
                raise self._state_conflict() from None
            already_applied = True
        return self._completed("confirm_draft", instance, already_applied)

    def pause(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
    ) -> Mapping[str, Any]:
        before = self._owned_instance(principal, instance_id)
        try:
            instance = self.service.pause_instance(
                principal.tenant_id,
                instance_id,
                actor_person_id=principal.person_id,
            )
        except TransitionError:
            raise self._state_conflict() from None
        return self._completed(
            "pause",
            instance,
            before.status == InstanceStatus.PAUSED,
        )

    def resume(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
    ) -> Mapping[str, Any]:
        before = self._owned_instance(principal, instance_id)
        try:
            instance = self.service.resume_instance(
                principal.tenant_id,
                instance_id,
                actor_person_id=principal.person_id,
            )
        except TransitionError:
            raise self._state_conflict() from None
        return self._completed(
            "resume",
            instance,
            before.status == InstanceStatus.RUNNING,
        )

    def preview_cancellation(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
    ) -> Mapping[str, Any]:
        try:
            preview = self.service.preview_cancellation(
                principal.tenant_id,
                instance_id,
                actor_person_id=principal.person_id,
            )
            instance = self._owned_instance(principal, instance_id)
        except CancellationNotAllowedError:
            raise self._state_conflict() from None
        except (AuthorizationError, InstanceNotFoundError):
            raise ConsoleResourceNotFoundError(instance_id) from None
        return {
            "action": "cancel",
            "stage": "preview",
            "preview": {
                "instance_id": preview.instance_id,
                "expected_instance_version": preview.expected_instance_version,
                "affected_nodes": [
                    {
                        "key": key,
                        "title": instance.snapshot.node(key).title,
                        "active": key in preview.active_node_keys,
                    }
                    for key in preview.affected_node_keys
                ],
            },
        }

    def confirm_cancellation(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
        expected_instance_version: int,
    ) -> Mapping[str, Any]:
        try:
            confirmation = self.service.confirm_cancellation(
                principal.tenant_id,
                instance_id,
                actor_person_id=principal.person_id,
                expected_instance_version=expected_instance_version,
            )
        except StaleCancellationError:
            raise ConsoleActionConflictError(
                "preview_stale",
                "流程状态已变化，请重新预览后再确认。",
            ) from None
        except CancellationNotAllowedError:
            raise self._state_conflict() from None
        except (AuthorizationError, InstanceNotFoundError):
            raise ConsoleResourceNotFoundError(instance_id) from None
        return {
            **self._completed(
                "cancel",
                confirmation.instance,
                confirmation.already_applied,
            ),
            "canceled_node_keys": list(confirmation.canceled_node_keys),
            "revoked_claim_node_keys": list(
                confirmation.revoked_claim_node_keys
            ),
        }

    def preview_restart(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
        *,
        node_key: str | None = None,
    ) -> Mapping[str, Any]:
        try:
            preview = (
                self.service.preview_node_restart(
                    principal.tenant_id,
                    instance_id,
                    node_key,
                    actor_person_id=principal.person_id,
                )
                if node_key is not None
                else self.service.preview_instance_restart(
                    principal.tenant_id,
                    instance_id,
                    actor_person_id=principal.person_id,
                )
            )
            instance = self._owned_instance(principal, instance_id)
            if instance.version != preview.expected_instance_version:
                raise StaleRestartPreviewError(
                    "instance changed while rendering restart preview"
                )
        except RestartNotAllowedError:
            raise self._state_conflict() from None
        except (AuthorizationError, InstanceNotFoundError):
            raise ConsoleResourceNotFoundError(instance_id) from None
        except StaleRestartPreviewError:
            raise ConsoleActionConflictError(
                "preview_stale",
                "流程状态已变化，请重新预览后再确认。",
            ) from None
        return {
            "action": "restart",
            "stage": "preview",
            "preview": {
                "id": preview.id,
                "instance_id": preview.instance_id,
                "scope": preview.scope.value,
                "target_node": (
                    {
                        "key": preview.node_key,
                        "title": instance.snapshot.node(preview.node_key).title,
                    }
                    if preview.node_key is not None
                    else None
                ),
                "affected_nodes": [
                    {
                        "key": key,
                        "title": instance.snapshot.node(key).title,
                        "current_attempt_no": instance.nodes[key].current_attempt_no,
                    }
                    for key in preview.affected_node_keys
                ],
                "expected_instance_version": preview.expected_instance_version,
                "expires_at": to_json_value(preview.expires_at),
            },
        }

    def confirm_restart(
        self,
        principal: ConsolePrincipal,
        preview_id: str,
    ) -> Mapping[str, Any]:
        try:
            confirmation = self.service.confirm_restart(
                principal.tenant_id,
                preview_id,
                actor_person_id=principal.person_id,
            )
        except RestartPreviewExpiredError:
            raise ConsoleActionConflictError(
                "preview_expired",
                "重启预览已过期，请重新预览。",
            ) from None
        except StaleRestartPreviewError:
            raise ConsoleActionConflictError(
                "preview_stale",
                "流程状态已变化，请重新预览后再确认。",
            ) from None
        except RestartNotAllowedError:
            raise self._state_conflict() from None
        except (
            AuthorizationError,
            InstanceNotFoundError,
            RestartPreviewNotFoundError,
        ):
            raise ConsoleResourceNotFoundError(preview_id) from None
        return {
            **self._completed(
                "restart",
                confirmation.instance,
                confirmation.already_applied,
            ),
            "scope": confirmation.preview.scope.value,
            "affected_nodes": [
                {
                    "key": key,
                    "attempt_no": confirmation.instance.nodes[
                        key
                    ].current_attempt_no,
                    "status": confirmation.instance.nodes[key].status.value,
                }
                for key in confirmation.preview.affected_node_keys
            ],
        }

    def _owned_instance(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
    ) -> WorkflowInstance:
        try:
            return self.service.get_for_owner(
                principal.tenant_id,
                instance_id,
                actor_person_id=principal.person_id,
            )
        except (AuthorizationError, InstanceNotFoundError):
            raise ConsoleResourceNotFoundError(instance_id) from None

    @staticmethod
    def _completed(
        action: str,
        instance: WorkflowInstance,
        already_applied: bool,
    ) -> dict[str, Any]:
        return {
            "action": action,
            "stage": "completed",
            "already_applied": already_applied,
            "instance": {
                "id": instance.id,
                "status": instance.status.value,
                "version": instance.version,
            },
        }

    @staticmethod
    def _state_conflict() -> ConsoleActionConflictError:
        return ConsoleActionConflictError(
            "state_conflict",
            "流程当前状态不允许执行该操作，请刷新后重试。",
        )
