"""Participant-scoped Human task work surface for the central console."""
from __future__ import annotations

from collections.abc import Mapping
import json
import secrets
from typing import Any

from .console import ConsolePrincipal
from .decision import (
    HumanDecision,
    HumanDecisionFeedbackError,
    HumanDecisionNotAllowedError,
    human_decision_config,
)
from .directory import DirectoryValidationError
from .deliverables import validate_human_deliverable
from .model import AttemptStatus, ExecutorKind, NodeStatus
from .repository import ConcurrentUpdateError, InstanceNotFoundError
from .runner import AuthorizationError, StaleAttemptError
from .serde import to_json_value
from .service import HumanTaskTransferNotAllowedError, WorkflowService
from .transitions import TransitionError


MAX_HUMAN_TASK_CONTENT_CHARS = 12_000
MAX_HUMAN_TASK_CONTEXT_BYTES = 32_000


class ConsoleTaskNotFoundError(KeyError):
    """A task is absent or not assigned to the current principal."""


class ConsoleTaskConflictError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConsoleTaskService:
    """Expose the current principal's bounded Human work bindings."""

    def __init__(self, service: WorkflowService) -> None:
        self.service = service

    def list_tasks(
        self,
        principal: ConsolePrincipal,
        *,
        limit: int = 30,
    ) -> Mapping[str, Any]:
        summaries = self.service.repository.list_human_tasks_for_owner(
            principal.tenant_id,
            owner_person_id=principal.person_id,
            limit=limit,
        )
        tasks = []
        for summary in summaries:
            instance = self.service.repository.get(
                principal.tenant_id,
                summary.instance_id,
            )
            spec = instance.snapshot.node(summary.node_key)
            decision = human_decision_config(spec.work)
            tasks.append(
                {
                    "id": f"{summary.instance_id}:{summary.node_key}",
                    "instance_id": summary.instance_id,
                    "goal": summary.goal,
                    "instance_status": summary.instance_status.value,
                    "instance_owner_relation": self._relation(
                        summary.instance_owner_person_id,
                        principal.person_id,
                    ),
                    "node": {
                        "key": summary.node_key,
                        "title": summary.node_title,
                        "attempt_no": summary.attempt_no,
                        "version": summary.node_version,
                    },
                    "kind": "decision" if decision is not None else "task",
                    "started_at": summary.started_at.isoformat(),
                }
            )
        return {"tasks": tasks, "total": len(tasks), "limit": limit}

    def get_task(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
        node_key: str,
    ) -> Mapping[str, Any]:
        instance, node, spec, attempt = self._current_task(
            principal,
            instance_id,
            node_key,
            decision=None,
        )
        decision = human_decision_config(spec.work)
        return {
            "task": {
                "kind": "decision" if decision is not None else "task",
                "instance_id": instance.id,
                "instance_version": instance.version,
                "goal": instance.snapshot.goal,
                "instance_status": instance.status.value,
                "instance_owner_relation": self._relation(
                    instance.owner_person_id,
                    principal.person_id,
                ),
                "node": {
                    "key": node.node_key,
                    "title": spec.title,
                    "attempt_no": node.current_attempt_no,
                    "version": node.version,
                    "started_at": to_json_value(node.started_at),
                },
                "work": {
                    "objective": str(spec.work.get("objective") or ""),
                    "outputs": to_json_value(spec.work.get("outputs", ())),
                    "acceptance": [
                        str(item) for item in spec.work.get("acceptance", ())
                    ],
                    "context": self._bounded_context(attempt.input_snapshot),
                    "decision": (
                        {
                            "kind": "accept_reject",
                            "reject_target": decision.get("reject_target"),
                        }
                        if decision is not None
                        else None
                    ),
                },
                "actions": {
                    "submit": decision is None,
                    "transfer": decision is None and callable(
                        getattr(self.service.directory, "list_candidate_people", None)
                    ),
                    "accept": decision is not None,
                    "reject": decision is not None,
                },
            }
        }

    def list_people(
        self,
        principal: ConsolePrincipal,
        *,
        limit: int = 100,
    ) -> Mapping[str, Any]:
        directory = self.service.directory
        lister = getattr(directory, "list_candidate_people", None)
        if not callable(lister):
            raise ConsoleTaskNotFoundError("directory candidates")
        try:
            people = lister(principal.tenant_id, limit=limit)
        except DirectoryValidationError as exc:
            raise ConsoleTaskConflictError(
                "directory_unavailable",
                "企业成员列表暂时不可用，请稍后重试。",
            ) from exc
        visible = [
            {
                "person_id": person.person_id,
                "name": person.name or "企业成员",
            }
            for person in people
            if person.active
            and not secrets.compare_digest(person.person_id, principal.person_id)
        ]
        visible.sort(key=lambda item: (item["name"], item["person_id"]))
        return {"people": visible, "total": len(visible), "limit": limit}

    def submit(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        content: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        _instance, _node, spec, _attempt = self._current_task(
            principal,
            instance_id,
            node_key,
            decision=False,
        )
        if (content is None) == (result is None):
            raise ValueError("submit requires exactly one of content or result")
        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("content is required")
            if len(content) > MAX_HUMAN_TASK_CONTENT_CHARS:
                raise ValueError(
                    f"content exceeds {MAX_HUMAN_TASK_CONTENT_CHARS} characters"
                )
            submitted = {"content": content}
        else:
            assert result is not None
            submitted = validate_human_deliverable(spec.work, result)
        try:
            instance = self.service.submit_human(
                principal.tenant_id,
                instance_id,
                node_key,
                actor_person_id=principal.person_id,
                attempt_no=attempt_no,
                expected_node_version=expected_node_version,
                result=submitted,
            )
        except (AuthorizationError, InstanceNotFoundError):
            raise ConsoleTaskNotFoundError((instance_id, node_key)) from None
        except (ConcurrentUpdateError, StaleAttemptError, TransitionError):
            raise ConsoleTaskConflictError(
                "task_stale",
                "待办状态已经变化，请刷新后再提交。",
            ) from None
        node = instance.nodes[node_key]
        return {
            "action": "submit_human_task",
            "instance_id": instance.id,
            "node_key": node_key,
            "attempt_no": attempt_no,
            "node_status": node.status.value,
            "instance_status": instance.status.value,
        }

    def transfer(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
        node_key: str,
        *,
        attempt_no: int,
        expected_node_version: int,
        new_owner_person_id: str,
    ) -> Mapping[str, Any]:
        self._current_task(principal, instance_id, node_key, decision=False)
        try:
            instance = self.service.transfer_human_task(
                principal.tenant_id,
                instance_id,
                node_key,
                actor_person_id=principal.person_id,
                new_owner_person_id=new_owner_person_id,
                attempt_no=attempt_no,
                expected_node_version=expected_node_version,
            )
        except (AuthorizationError, InstanceNotFoundError):
            raise ConsoleTaskNotFoundError((instance_id, node_key)) from None
        except (ConcurrentUpdateError, StaleAttemptError, TransitionError):
            raise ConsoleTaskConflictError(
                "task_stale",
                "待办状态已经变化，请刷新后再转交。",
            ) from None
        except HumanTaskTransferNotAllowedError as exc:
            raise ConsoleTaskConflictError(
                "task_not_transferable",
                str(exc),
            ) from None
        node = instance.nodes[node_key]
        return {
            "action": "transfer_human_task",
            "instance_id": instance.id,
            "node_key": node_key,
            "attempt_no": attempt_no,
            "node_version": node.version,
            "assigned_to": "collaborator",
            "projection": {
                "kind": "feishu_task",
                "status": "queued",
                "message": "负责人已更换，飞书待办正在同步。",
            },
        }

    def submit_decision(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
        node_key: str,
        *,
        attempt_no: int,
        expected_instance_version: int,
        expected_node_version: int,
        decision: str,
        feedback: str | None,
    ) -> Mapping[str, Any]:
        """Submit one version-bound accept or reject decision from the workbench."""

        try:
            normalized_decision = HumanDecision(decision)
        except ValueError as exc:
            raise ValueError("decision must be accept or reject") from exc
        self._current_task(principal, instance_id, node_key, decision=True)
        try:
            instance = self.service.submit_human_decision(
                principal.tenant_id,
                instance_id,
                node_key,
                normalized_decision,
                actor_person_id=principal.person_id,
                attempt_no=attempt_no,
                expected_instance_version=expected_instance_version,
                expected_node_version=expected_node_version,
                feedback=feedback,
            )
        except (AuthorizationError, InstanceNotFoundError):
            raise ConsoleTaskNotFoundError((instance_id, node_key)) from None
        except HumanDecisionFeedbackError as exc:
            raise ValueError(str(exc)) from None
        except (
            ConcurrentUpdateError,
            HumanDecisionNotAllowedError,
            StaleAttemptError,
            TransitionError,
        ):
            raise ConsoleTaskConflictError(
                "task_stale",
                "待办状态已经变化，请刷新后再判断。",
            ) from None
        node = instance.nodes[node_key]
        return {
            "action": "submit_human_decision",
            "decision": normalized_decision.value,
            "instance_id": instance.id,
            "node_key": node_key,
            "attempt_no": attempt_no,
            "node_status": node.status.value,
            "instance_status": instance.status.value,
        }

    def _current_task(
        self,
        principal: ConsolePrincipal,
        instance_id: str,
        node_key: str,
        *,
        decision: bool | None = False,
    ) -> tuple[Any, Any, Any, Any]:
        try:
            instance = self.service.repository.get(
                principal.tenant_id,
                instance_id,
            )
            node = instance.nodes[node_key]
            spec = instance.snapshot.node(node_key)
            attempt = instance.current_attempt(node_key)
        except (InstanceNotFoundError, KeyError):
            raise ConsoleTaskNotFoundError((instance_id, node_key)) from None
        is_decision = human_decision_config(spec.work) is not None
        if (
            node.executor != ExecutorKind.HUMAN
            or node.status != NodeStatus.WAITING_HUMAN
            or attempt.status != AttemptStatus.WAITING_HUMAN
            or (decision is not None and is_decision != decision)
            or not secrets.compare_digest(node.owner_person_id, principal.person_id)
        ):
            raise ConsoleTaskNotFoundError((instance_id, node_key))
        return instance, node, spec, attempt

    @staticmethod
    def _relation(value: str, person_id: str) -> str:
        return "you" if secrets.compare_digest(value, person_id) else "collaborator"

    @staticmethod
    def _bounded_context(value: Mapping[str, Any]) -> Any:
        converted = to_json_value(value)
        encoded = json.dumps(
            converted,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) <= MAX_HUMAN_TASK_CONTEXT_BYTES:
            return converted
        preview = encoded[:MAX_HUMAN_TASK_CONTEXT_BYTES].decode(
            "utf-8",
            errors="ignore",
        )
        return {
            "_truncated": True,
            "original_bytes": len(encoded),
            "preview": preview,
        }
