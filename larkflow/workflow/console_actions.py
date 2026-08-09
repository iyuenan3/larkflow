"""Authenticated Owner actions exposed by the central workflow console."""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
import unicodedata

from .console import ConsolePrincipal, ConsoleResourceNotFoundError
from .editing import (
    GraphEditNotAllowedError,
    GraphEditPreviewExpiredError,
    GraphEditPreviewNotFoundError,
    StaleGraphEditPreviewError,
)
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

    def preview_graph_edit(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
        operations: list[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Create one owner-scoped future-region graph edit preview."""

        try:
            instance = self._owned_instance(principal, instance_id)
            translated = self._translate_graph_edit_owners(
                principal,
                instance,
                operations,
            )
            preview = self.service.preview_graph_edit(
                principal.tenant_id,
                instance_id,
                translated,
                actor_person_id=principal.person_id,
            )
            instance = self._owned_instance(principal, instance_id)
        except GraphEditNotAllowedError as exc:
            raise ConsoleActionConflictError(
                "edit_not_allowed",
                self._graph_edit_error_message(exc),
            ) from None
        except (AuthorizationError, InstanceNotFoundError):
            raise ConsoleResourceNotFoundError(instance_id) from None

        titles = {spec.key: spec.title for spec in instance.snapshot.nodes}
        for operation in preview.operations:
            if operation["op"] == "add_node":
                node = operation["node"]
                titles[str(node["key"])] = str(node["title"])
            elif operation["op"] == "update_node":
                changes = operation["set"]
                if "title" in changes:
                    titles[str(operation["node_key"])] = str(changes["title"])
        return {
            "action": "graph_edit",
            "stage": "preview",
            "preview": {
                "id": preview.id,
                "instance_id": preview.instance_id,
                "graph_revision": preview.graph_revision,
                "proposed_graph_revision": preview.proposed_graph_revision,
                "expected_instance_version": preview.expected_instance_version,
                "added_nodes": [
                    {"key": key, "title": titles.get(key, key)}
                    for key in preview.added_node_keys
                ],
                "updated_nodes": [
                    {"key": key, "title": titles.get(key, key)}
                    for key in preview.updated_node_keys
                ],
                "removed_nodes": [
                    {"key": key, "title": titles.get(key, key)}
                    for key in preview.removed_node_keys
                ],
                "expires_at": to_json_value(preview.expires_at),
            },
        }

    def confirm_graph_edit(
        self,
        principal: ConsolePrincipal,
        preview_id: str,
    ) -> Mapping[str, Any]:
        """Apply or safely replay one graph edit preview."""

        try:
            confirmation = self.service.confirm_graph_edit(
                principal.tenant_id,
                preview_id,
                actor_person_id=principal.person_id,
            )
        except GraphEditPreviewExpiredError:
            raise ConsoleActionConflictError(
                "preview_expired",
                "流程编辑预览已过期，请重新预览。",
            ) from None
        except StaleGraphEditPreviewError:
            raise ConsoleActionConflictError(
                "preview_stale",
                "流程状态已变化，请重新预览后再确认。",
            ) from None
        except GraphEditNotAllowedError as exc:
            raise ConsoleActionConflictError(
                "edit_not_allowed",
                self._graph_edit_error_message(exc),
            ) from None
        except (
            AuthorizationError,
            InstanceNotFoundError,
            GraphEditPreviewNotFoundError,
        ):
            raise ConsoleResourceNotFoundError(preview_id) from None
        return {
            **self._completed(
                "graph_edit",
                confirmation.instance,
                confirmation.already_applied,
            ),
            "graph_revision": confirmation.instance.graph_revision,
            "added_node_keys": list(confirmation.preview.added_node_keys),
            "updated_node_keys": list(confirmation.preview.updated_node_keys),
            "removed_node_keys": list(confirmation.preview.removed_node_keys),
        }

    @classmethod
    def _translate_graph_edit_owners(
        cls,
        principal: ConsolePrincipal,
        instance: WorkflowInstance,
        operations: list[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        translated: list[Mapping[str, Any]] = []
        known_keys = {spec.key for spec in instance.snapshot.nodes}
        specs = {spec.key: spec for spec in instance.snapshot.nodes}
        for operation in operations:
            current = dict(operation)
            if current.get("op") == "add_node" and isinstance(
                current.get("node"), Mapping
            ):
                node = dict(current["node"])
                insert_before = node.pop("insert_before", [])
                if isinstance(insert_before, (str, bytes)) or not isinstance(
                    insert_before,
                    list,
                ):
                    raise GraphEditNotAllowedError(
                        "insert_before must be an array"
                    )
                if len(insert_before) != len(set(insert_before)) or not all(
                    isinstance(item, str) and item in known_keys
                    for item in insert_before
                ):
                    raise GraphEditNotAllowedError(
                        "insert_before contains an unknown or duplicate node"
                    )
                key = node.get("key")
                if not isinstance(key, str) or not key.strip():
                    key = cls._generated_node_key(
                        str(node.get("title") or ""),
                        str(node.get("executor") or "human"),
                        known_keys,
                    )
                    node["key"] = key
                known_keys.add(key)
                if node.get("owner_person_id") == "__current_user__":
                    node["owner_person_id"] = principal.person_id
                current["node"] = node
                translated.append(current)
                for target_key in insert_before:
                    target = specs[target_key]
                    deps = [*target.deps]
                    if key not in deps:
                        deps.append(key)
                    translated.append(
                        {
                            "op": "update_node",
                            "node_key": target_key,
                            "set": {
                                "deps": deps,
                                "work": cls._work_with_dependencies(
                                    target.work,
                                    deps,
                                ),
                            },
                        }
                    )
                continue
            elif current.get("op") == "update_node" and isinstance(
                current.get("set"), Mapping
            ):
                changes = dict(current["set"])
                if changes.get("owner_person_id") == "__current_user__":
                    changes["owner_person_id"] = principal.person_id
                node_key = current.get("node_key")
                if "deps" in changes and isinstance(node_key, str):
                    spec = specs.get(node_key)
                    if spec is None:
                        raise GraphEditNotAllowedError(
                            f"unknown graph edit node: {node_key}"
                        )
                    changes["work"] = cls._work_with_dependencies(
                        changes.get("work")
                        if isinstance(changes.get("work"), Mapping)
                        else spec.work,
                        changes["deps"],
                    )
                current["set"] = changes
            translated.append(current)
        return translated

    @staticmethod
    def _generated_node_key(
        title: str,
        executor: str,
        existing: set[str],
    ) -> str:
        ascii_title = unicodedata.normalize("NFKD", title).encode(
            "ascii",
            "ignore",
        ).decode("ascii")
        base = re.sub(r"[^a-z0-9]+", "_", ascii_title.lower()).strip("_")
        if not base or not base[0].isalpha():
            prefix = executor if executor in {"human", "agent", "tool"} else "node"
            base = f"{prefix}_step"
        base = base[:96].rstrip("_")
        candidate = base
        suffix = 2
        while candidate in existing:
            candidate = f"{base[:120 - len(str(suffix))]}_{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _work_with_dependencies(
        work: Mapping[str, Any],
        dependencies: Any,
    ) -> Mapping[str, Any]:
        if isinstance(dependencies, (str, bytes)) or not isinstance(
            dependencies,
            (list, tuple),
        ):
            raise GraphEditNotAllowedError("deps must be an array")
        normalized = dict(to_json_value(work))
        inputs = normalized.get("inputs", ())
        if isinstance(inputs, (str, bytes)) or not isinstance(inputs, list):
            inputs = []
        preserved = [
            item
            for item in inputs
            if isinstance(item, str) and not item.startswith("dependencies.")
        ]
        normalized["inputs"] = [
            *preserved,
            *(f"dependencies.{dependency}" for dependency in dependencies),
        ]
        return normalized

    @staticmethod
    def _graph_edit_error_message(error: GraphEditNotAllowedError) -> str:
        message = str(error)
        if "crossed the edit frontier" in message or "execution history" in message:
            return "该节点已经开始执行，不能直接修改。需要返工时请使用打回到此节点。"
        if "only draft or running instances" in message:
            return "只有草稿或运行中的流程可以修改。"
        if "unexpected runtime state" in message:
            return "该草稿存在异常运行状态，暂时不能修改。"
        if "locked instance" in message:
            return "该流程图已经锁定，不能修改。"
        if "cycle" in message:
            return "该修改会形成循环依赖，请重新选择上游节点。"
        if "graph exceeds" in message or "exceeds" in message:
            return "本次修改超过流程规模限制，请拆分后重试。"
        return "流程修改不合法，请检查节点、依赖和执行方式。"

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
