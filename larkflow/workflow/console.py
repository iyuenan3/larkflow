"""Owner-scoped read model for the central workflow console."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import secrets
from typing import Any

from .events import AuditEvent
from .model import (
    InstanceStatus,
    NodeAttempt,
    NodeStatus,
    WorkflowAttentionCandidate,
    WorkflowInstance,
    WorkflowInstanceSummary,
)
from .repository import InstanceNotFoundError, WorkflowRepository
from .serde import quality_to_dict, to_json_value


RESTART_EVENT_TYPES = frozenset(
    {"instance.node_restarted", "instance.restarted"}
)


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

    mode = "static"

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
        candidates = self.repository.list_attention_for_owner(
            principal.tenant_id,
            owner_person_id=principal.person_id,
            limit=limit,
        )
        attention = self._attention(candidates, principal.person_id)
        return {
            "instances": [self._summary(item) for item in summaries],
            "attention": {
                "items": attention,
                "total": len(attention),
                "counts": {
                    kind: sum(1 for item in attention if item["kind"] == kind)
                    for kind in (
                        "recover_failed",
                        "complete_human",
                        "resume_flow",
                        "confirm_draft",
                    )
                },
                "instance_limit": limit,
            },
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
            "insights": self._insights(
                instance,
                audit_events,
                principal.person_id,
            ),
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

    @staticmethod
    def _attention(
        candidates: tuple[WorkflowAttentionCandidate, ...],
        person_id: str,
    ) -> list[dict[str, Any]]:
        by_instance: dict[str, list[WorkflowAttentionCandidate]] = {}
        for candidate in candidates:
            by_instance.setdefault(candidate.instance_id, []).append(candidate)

        items: list[dict[str, Any]] = []
        for instance_id, group in by_instance.items():
            first = group[0]
            failed = [
                candidate
                for candidate in group
                if candidate.node_status == NodeStatus.FAILED
            ]
            waiting = [
                candidate
                for candidate in group
                if candidate.node_status == NodeStatus.WAITING_HUMAN
                and candidate.instance_status
                in {InstanceStatus.RUNNING, InstanceStatus.PAUSED}
                and candidate.node_owner_person_id is not None
                and secrets.compare_digest(
                    candidate.node_owner_person_id,
                    person_id,
                )
            ]

            if len(failed) == 1:
                candidate = failed[0]
                target = candidate.reject_target or candidate.node_key
                items.append(
                    ConsoleReadService._attention_item(
                        candidate,
                        kind="recover_failed",
                        priority=0,
                        title=(
                            "恢复失败节点："
                            f"{candidate.node_title or candidate.node_key}"
                        ),
                        detail="先预览该节点及下游的重启影响，再确认执行。",
                        command=f"/larkflow restart {instance_id} {target}",
                        action_hint="把命令发送给 larkflow，按回复中的确认命令执行。",
                    )
                )
            elif len(failed) > 1:
                items.append(
                    ConsoleReadService._attention_item(
                        first,
                        kind="recover_failed",
                        priority=0,
                        title=f"恢复失败流程：{len(failed)} 个失败节点",
                        detail="存在多个失败节点，先预览完整实例重启影响。",
                        command=f"/larkflow restart-all {instance_id}",
                        action_hint="把命令发送给 larkflow，按回复中的确认命令执行。",
                    )
                )
            elif first.instance_status == InstanceStatus.FAILED:
                items.append(
                    ConsoleReadService._attention_item(
                        first,
                        kind="recover_failed",
                        priority=0,
                        title="恢复失败流程",
                        detail="未定位到单一失败节点，先预览完整实例重启影响。",
                        command=f"/larkflow restart-all {instance_id}",
                        action_hint="把命令发送给 larkflow，按回复中的确认命令执行。",
                    )
                )

            for candidate in waiting:
                items.append(
                    ConsoleReadService._attention_item(
                        candidate,
                        kind="complete_human",
                        priority=1,
                        title=f"完成待办：{candidate.node_title}",
                        detail="该 Human 节点正在等待你的输入或决定。",
                        command=None,
                        action_hint="在飞书完成该节点对应的任务或决定卡。",
                    )
                )

            if first.instance_status == InstanceStatus.PAUSED:
                items.append(
                    ConsoleReadService._attention_item(
                        first,
                        kind="resume_flow",
                        priority=2,
                        title="继续已暂停流程",
                        detail="恢复后，中央调度器会从现有待调度节点继续。",
                        command=f"/larkflow resume {instance_id}",
                        action_hint="把命令发送给 larkflow。该操作不会创建新 Attempt。",
                    )
                )
            elif first.instance_status == InstanceStatus.DRAFT:
                items.append(
                    ConsoleReadService._attention_item(
                        first,
                        kind="confirm_draft",
                        priority=3,
                        title="确认流程草稿",
                        detail="该草稿尚未启动，可以先在详情中核对节点。",
                        command=f"/larkflow confirm {instance_id}",
                        action_hint="核对后把命令发送给 larkflow。",
                    )
                )

        items.sort(
            key=lambda item: (
                item["priority"],
                -datetime.fromisoformat(item["occurred_at"]).timestamp(),
                item["id"],
            )
        )
        return items

    @staticmethod
    def _attention_item(
        candidate: WorkflowAttentionCandidate,
        *,
        kind: str,
        priority: int,
        title: str,
        detail: str,
        command: str | None,
        action_hint: str,
    ) -> dict[str, Any]:
        occurred_at = candidate.node_occurred_at or candidate.created_at
        node = (
            {"key": candidate.node_key, "title": candidate.node_title}
            if candidate.node_key is not None
            else None
        )
        suffix = candidate.node_key or "instance"
        return {
            "id": f"{kind}:{candidate.instance_id}:{suffix}",
            "kind": kind,
            "priority": priority,
            "instance_id": candidate.instance_id,
            "goal": candidate.goal,
            "instance_status": candidate.instance_status.value,
            "title": title,
            "detail": detail,
            "occurred_at": occurred_at.isoformat(),
            "node": node,
            "command": command,
            "action_hint": action_hint,
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
    def _insights(
        instance: WorkflowInstance,
        audit_events: tuple[AuditEvent, ...],
        person_id: str,
    ) -> dict[str, Any]:
        node_titles = {
            spec.key: spec.title for spec in instance.snapshot.nodes
        }
        reworked_nodes = [
            {
                "key": spec.key,
                "title": spec.title,
                "current_attempt_no": node.current_attempt_no,
            }
            for spec in instance.snapshot.nodes
            if (node := instance.nodes.get(spec.key)) is not None
            and node.current_attempt_no > 1
        ]
        restart_event = next(
            (
                event
                for event in reversed(audit_events)
                if event.event_type in RESTART_EVENT_TYPES
            ),
            None,
        )
        if restart_event is None:
            latest_restart = None
        else:
            affected_keys = restart_event.payload.get("affected_node_keys", ())
            if not isinstance(affected_keys, (tuple, list)):
                affected_keys = ()
            affected_nodes = [
                {"key": key, "title": node_titles[key]}
                for key in affected_keys
                if isinstance(key, str) and key in node_titles
            ]
            target_key = restart_event.node_key
            target_node = (
                {"key": target_key, "title": node_titles[target_key]}
                if target_key in node_titles
                else None
            )
            latest_restart = {
                "event_type": restart_event.event_type,
                "scope": (
                    "instance"
                    if restart_event.event_type == "instance.restarted"
                    else "node"
                ),
                "occurred_at": restart_event.occurred_at.isoformat(),
                "actor_relation": ConsoleReadService._person_relation(
                    restart_event.actor_person_id,
                    person_id,
                ),
                "target_node": target_node,
                "attempt_no": restart_event.attempt_no,
                "affected_nodes": affected_nodes,
            }
        return {
            "reworked_nodes": reworked_nodes,
            "latest_restart": latest_restart,
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
