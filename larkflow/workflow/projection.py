"""Transactional outbox consumer for external workflow projections."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
from typing import Any, Protocol
from uuid import uuid4

from .model import ExecutorKind, FrozenDict, NodeStatus, WorkflowInstance
from .recovery import RECOVERY_ACTION_NAME, RecoveryAction
from .repository import OutboxStore, ProjectionStore, WorkflowRepository
from .serde import to_json_value


FEISHU_TASK_KIND = "feishu_task"
FEISHU_MESSAGE_KIND = "feishu_message"
FEISHU_DOCUMENT_KIND = "feishu_document"
FEISHU_INSTANCE_MESSAGE_KIND = "feishu_instance_message"
PROJECTION_EVENTS = {
    "node.projection_create_requested",
    "node.projection_sync_requested",
    "instance.projection_completed_requested",
}
MAX_DEPENDENCY_CONTEXT_CHARS = 6_000
MAX_MESSAGE_RESULT_CHARS = 2_000
MAX_DOCUMENT_RESULT_CHARS = 12_000


@dataclass(frozen=True)
class ProjectionRecord:
    id: str
    tenant_id: str
    instance_id: str
    node_instance_id: str
    attempt_no: int
    kind: str
    idempotency_key: str
    sync_version: int
    state: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime
    external_id: str | None = None
    external_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", FrozenDict(self.state))


@dataclass(frozen=True)
class ExternalTask:
    guid: str
    url: str | None = None


@dataclass(frozen=True)
class ExternalMessage:
    message_id: str


@dataclass(frozen=True)
class ExternalDocument:
    document_id: str
    url: str | None = None


@dataclass(frozen=True)
class TaskProjectionRequest:
    tenant_id: str
    instance_id: str
    node_key: str
    node_instance_id: str
    attempt_no: int
    owner_person_id: str
    summary: str
    description: str
    idempotency_key: str


class TaskProjectionAdapter(Protocol):
    def create_task(self, request: TaskProjectionRequest) -> ExternalTask:
        ...

    def complete_task(self, task_guid: str) -> None:
        ...

    def task_exists(self, task_guid: str) -> bool:
        ...


@dataclass(frozen=True)
class MessageProjectionRequest:
    recipient_person_id: str
    text: str
    idempotency_key: str
    card: Mapping[str, Any] | None = None


class MessageProjectionAdapter(Protocol):
    def send_message(self, request: MessageProjectionRequest) -> ExternalMessage:
        ...


@dataclass(frozen=True)
class DocumentProjectionRequest:
    title: str
    content_xml: str


class DocumentProjectionAdapter(Protocol):
    def create_document(
        self,
        request: DocumentProjectionRequest,
    ) -> ExternalDocument:
        ...


@dataclass(frozen=True)
class ProjectionWorkerReport:
    claimed: int = 0
    published: int = 0
    tasks_created: int = 0
    tasks_completed: int = 0
    messages_sent: int = 0
    documents_created: int = 0
    noops: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProjectionReconciliationReport:
    instances_scanned: int = 0
    nodes_scanned: int = 0
    tasks_created: int = 0
    tasks_recreated: int = 0
    tasks_completed: int = 0
    unchanged: int = 0
    failed: int = 0
    interrupted: bool = False
    errors: tuple[str, ...] = field(default_factory=tuple)


class WorkflowProjectionWorker:
    """Claim outbox rows, perform external I/O, then publish or retry."""

    def __init__(
        self,
        repository: WorkflowRepository,
        outbox: OutboxStore,
        projections: ProjectionStore,
        task_adapter: TaskProjectionAdapter,
        message_adapter: MessageProjectionAdapter | None = None,
        document_adapter: DocumentProjectionAdapter | None = None,
        *,
        tenant_id: str,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if claim_limit < 1:
            raise ValueError("claim_limit must be positive")
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("retry delays are invalid")
        self.repository = repository
        self.outbox = outbox
        self.projections = projections
        self.task_adapter = task_adapter
        self.message_adapter = message_adapter
        self.document_adapter = document_adapter
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.id_factory = id_factory or (lambda: str(uuid4()))

    def run_once(self) -> ProjectionWorkerReport:
        now = self.clock()
        claims = self.outbox.claim_outbox(
            self.tenant_id,
            worker_id=self.worker_id,
            now=now,
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
            event_types=PROJECTION_EVENTS,
        )
        published = 0
        tasks_created = 0
        tasks_completed = 0
        messages_sent = 0
        documents_created = 0
        noops = 0
        failed = 0
        errors = []
        for claim in claims:
            try:
                outcome = self._project(claim.event, now=now)
            except Exception as exc:
                failed += 1
                error = f"{claim.event.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.outbox.mark_outbox_failed(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=now + self._retry_delay(claim.attempt_count),
                )
                continue
            self.outbox.mark_outbox_published(
                self.tenant_id,
                claim.event.id,
                claim_token=claim.claim_token,
                now=now,
            )
            published += 1
            tasks_created += int(outcome.created)
            tasks_completed += int(outcome.completed)
            messages_sent += int(outcome.message_sent)
            documents_created += int(outcome.document_created)
            noops += int(outcome.noop)
        return ProjectionWorkerReport(
            claimed=len(claims),
            published=published,
            tasks_created=tasks_created,
            tasks_completed=tasks_completed,
            messages_sent=messages_sent,
            documents_created=documents_created,
            noops=noops,
            failed=failed,
            errors=tuple(errors),
        )

    def reconcile_instance_completion(
        self,
        instance_id: str,
    ) -> ProjectionWorkerReport:
        """Repair one completed instance when its completion outbox was missed."""
        if not instance_id.strip():
            raise ValueError("instance_id is required")
        instance = self.repository.get(self.tenant_id, instance_id)
        outcome = self._project_instance_completion(instance, now=self.clock())
        return ProjectionWorkerReport(
            messages_sent=int(outcome.message_sent),
            documents_created=int(outcome.document_created),
            noops=int(outcome.noop),
        )

    def reconcile_all(
        self,
        *,
        batch_size: int = 100,
        stop_requested: Callable[[], bool] | None = None,
    ) -> ProjectionReconciliationReport:
        """Repair current Human projections from PostgreSQL authority."""
        if batch_size < 1:
            raise ValueError("reconciliation batch_size must be positive")
        should_stop = stop_requested or (lambda: False)
        totals = {
            "instances_scanned": 0,
            "nodes_scanned": 0,
            "tasks_created": 0,
            "tasks_recreated": 0,
            "tasks_completed": 0,
            "unchanged": 0,
            "failed": 0,
        }
        errors = []
        after_id = None
        interrupted = False
        while True:
            if should_stop():
                interrupted = True
                break
            instance_ids = self.repository.projection_instance_ids(
                self.tenant_id,
                after_id=after_id,
                limit=batch_size,
            )
            if not instance_ids:
                break
            for instance_id in instance_ids:
                after_id = instance_id
                if should_stop():
                    interrupted = True
                    break
                totals["instances_scanned"] += 1
                try:
                    instance = self.repository.get(self.tenant_id, instance_id)
                except Exception as exc:
                    totals["failed"] += 1
                    errors.append(
                        f"{instance_id}: {type(exc).__name__}: {exc}"
                    )
                    continue
                for node_key in sorted(instance.nodes):
                    node = instance.nodes[node_key]
                    if node.status in {NodeStatus.PENDING, NodeStatus.READY}:
                        continue
                    attempt = instance.current_attempt(node_key)
                    if (
                        node.executor != ExecutorKind.HUMAN
                        and attempt.status.value
                        != NodeStatus.WAITING_HUMAN.value
                        and attempt.submitted_by_person_id is None
                    ):
                        continue
                    if should_stop():
                        interrupted = True
                        break
                    totals["nodes_scanned"] += 1
                    try:
                        outcome = self._project_node(
                            instance,
                            node_key,
                            node.current_attempt_no,
                            now=self.clock(),
                            verify_external=True,
                        )
                    except Exception as exc:
                        totals["failed"] += 1
                        errors.append(
                            f"{instance_id}/{node_key}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        continue
                    totals["tasks_created"] += int(outcome.created)
                    totals["tasks_recreated"] += int(outcome.recreated)
                    totals["tasks_completed"] += int(outcome.completed)
                    totals["unchanged"] += int(outcome.noop)
                if interrupted:
                    break
            if interrupted or len(instance_ids) < batch_size:
                break
        return ProjectionReconciliationReport(
            **totals,
            interrupted=interrupted,
            errors=tuple(errors),
        )

    def _project(self, event: Any, *, now: datetime) -> _ProjectionOutcome:
        if event.event_type not in PROJECTION_EVENTS:
            raise ValueError(f"unsupported projection event: {event.event_type}")
        instance_id = _text(event.payload.get("instance_id"), "instance_id")
        instance = self.repository.get(self.tenant_id, instance_id)
        if event.event_type == "instance.projection_completed_requested":
            if (
                event.aggregate_type != "workflow_instance"
                or event.aggregate_id != instance.id
            ):
                raise ValueError("instance projection aggregate does not match")
            return self._project_instance_completion(instance, now=now)
        if event.aggregate_type != "node_instance":
            raise ValueError("node projection requires node_instance aggregate")
        node_key = _text(event.payload.get("node_key"), "node_key")
        attempt_no = _positive_int(event.payload.get("attempt_no"), "attempt_no")
        if node_key not in instance.nodes:
            return _ProjectionOutcome(noop=True)
        node = instance.nodes[node_key]
        if node.id != event.aggregate_id:
            raise ValueError("projection event aggregate does not match the current node")
        return self._project_node(instance, node_key, attempt_no, now=now)

    def _project_node(
        self,
        instance: WorkflowInstance,
        node_key: str,
        attempt_no: int,
        *,
        now: datetime,
        verify_external: bool = False,
    ) -> _ProjectionOutcome:
        node = instance.nodes[node_key]
        attempt = instance.attempts[(node_key, attempt_no)]
        outcome = _ProjectionOutcome()
        human_work = (
            node.executor == ExecutorKind.HUMAN
            or attempt.status.value == NodeStatus.WAITING_HUMAN.value
            or attempt.submitted_by_person_id is not None
        )
        if human_work:
            outcome = self._project_task(
                instance,
                node_key,
                attempt_no,
                now=now,
                verify_external=verify_external,
            )
        if (
            not verify_external
            and node.executor != ExecutorKind.HUMAN
            and node.status in {
                NodeStatus.DONE,
                NodeStatus.FAILED,
                NodeStatus.CANCELED,
            }
        ):
            message_sent = self._project_node_message(
                instance,
                node_key,
                attempt_no,
                now=now,
            )
            outcome = outcome.merged(message_sent=message_sent)
        if outcome == _ProjectionOutcome():
            return _ProjectionOutcome(noop=True)
        return outcome

    def _project_task(
        self,
        instance: WorkflowInstance,
        node_key: str,
        attempt_no: int,
        *,
        now: datetime,
        verify_external: bool,
    ) -> _ProjectionOutcome:
        node = instance.nodes[node_key]
        attempt = instance.attempts[(node_key, attempt_no)]
        effective_status = (
            node.status
            if attempt_no == node.current_attempt_no
            else NodeStatus(attempt.status.value)
        )
        if effective_status in {NodeStatus.PENDING, NodeStatus.READY}:
            return _ProjectionOutcome(noop=True)

        record = self.projections.get_projection(
            self.tenant_id,
            node.id,
            attempt_no,
            FEISHU_TASK_KIND,
        )
        created = False
        recreated = False
        terminal = effective_status in {
            NodeStatus.DONE,
            NodeStatus.FAILED,
            NodeStatus.CANCELED,
        }
        if record is None:
            if terminal:
                return _ProjectionOutcome(noop=True)
            request = self._task_request(instance, node_key, attempt_no)
            external = self.task_adapter.create_task(request)
            if not external.guid.strip():
                raise ValueError("Feishu task create returned an empty guid")
            record = ProjectionRecord(
                id=self.id_factory(),
                tenant_id=self.tenant_id,
                instance_id=instance.id,
                node_instance_id=node.id,
                attempt_no=attempt_no,
                kind=FEISHU_TASK_KIND,
                external_id=external.guid,
                external_url=external.url,
                idempotency_key=request.idempotency_key,
                sync_version=node.version,
                state={"node_status": node.status.value, "completed": False},
                created_at=now,
                updated_at=now,
            )
            self.projections.save_projection(record)
            created = True

        if not record.external_id:
            raise ValueError("Feishu task projection has no external id")
        if (
            verify_external
            and not created
            and not terminal
            and not self.task_adapter.task_exists(record.external_id)
        ):
            record = self._recreate_task(
                instance,
                node_key,
                attempt_no,
                record=record,
                now=now,
            )
            recreated = True
        completed = False
        if terminal and not bool(record.state.get("completed")):
            self.task_adapter.complete_task(record.external_id)
            completed = True
        desired_state = {
            "node_status": effective_status.value,
            "completed": terminal,
        }
        repair_generation = _repair_generation(record.state)
        if repair_generation:
            desired_state["repair_generation"] = repair_generation
        if (
            not created
            and not recreated
            and not completed
            and record.sync_version >= node.version
            and dict(record.state) == desired_state
        ):
            return _ProjectionOutcome(noop=True)
        updated = ProjectionRecord(
            id=record.id,
            tenant_id=record.tenant_id,
            instance_id=record.instance_id,
            node_instance_id=record.node_instance_id,
            attempt_no=record.attempt_no,
            kind=record.kind,
            external_id=record.external_id,
            external_url=record.external_url,
            idempotency_key=record.idempotency_key,
            sync_version=max(record.sync_version, node.version),
            state=desired_state,
            created_at=record.created_at,
            updated_at=now,
        )
        self.projections.save_projection(updated)
        return _ProjectionOutcome(
            created=created,
            recreated=recreated,
            completed=completed,
        )

    def _project_node_message(
        self,
        instance: WorkflowInstance,
        node_key: str,
        attempt_no: int,
        *,
        now: datetime,
    ) -> bool:
        if self.message_adapter is None:
            return False
        node = instance.nodes[node_key]
        existing = self.projections.get_projection(
            self.tenant_id,
            node.id,
            attempt_no,
            FEISHU_MESSAGE_KIND,
        )
        if existing is not None:
            return False
        request = self._node_message_request(instance, node_key, attempt_no)
        external = self.message_adapter.send_message(request)
        if not external.message_id.strip():
            raise ValueError("Feishu message projection returned no message_id")
        self.projections.save_projection(
            ProjectionRecord(
                id=self.id_factory(),
                tenant_id=self.tenant_id,
                instance_id=instance.id,
                node_instance_id=node.id,
                attempt_no=attempt_no,
                kind=FEISHU_MESSAGE_KIND,
                external_id=external.message_id,
                idempotency_key=request.idempotency_key,
                sync_version=node.version,
                state={"node_status": node.status.value},
                created_at=now,
                updated_at=now,
            )
        )
        return True

    def _project_instance_completion(
        self,
        instance: WorkflowInstance,
        *,
        now: datetime,
    ) -> _ProjectionOutcome:
        if instance.status.value != "done":
            return _ProjectionOutcome(noop=True)
        node_key = _canonical_terminal_node(instance)
        node = instance.nodes[node_key]
        attempt_no = node.current_attempt_no
        document = self.projections.get_projection(
            self.tenant_id,
            node.id,
            attempt_no,
            FEISHU_DOCUMENT_KIND,
        )
        document_created = False
        if document is None and self.document_adapter is not None:
            request = self._document_request(instance)
            external = self.document_adapter.create_document(request)
            if not external.document_id.strip():
                raise ValueError("Feishu document projection returned no document_id")
            document = ProjectionRecord(
                id=self.id_factory(),
                tenant_id=self.tenant_id,
                instance_id=instance.id,
                node_instance_id=node.id,
                attempt_no=attempt_no,
                kind=FEISHU_DOCUMENT_KIND,
                external_id=external.document_id,
                external_url=external.url,
                idempotency_key=_completion_projection_key(
                    self.tenant_id,
                    instance.id,
                    attempt_no,
                    FEISHU_DOCUMENT_KIND,
                ),
                sync_version=instance.version,
                state={"instance_status": instance.status.value},
                created_at=now,
                updated_at=now,
            )
            self.projections.save_projection(document)
            document_created = True

        message_sent = False
        if self.message_adapter is not None:
            message = self.projections.get_projection(
                self.tenant_id,
                node.id,
                attempt_no,
                FEISHU_INSTANCE_MESSAGE_KIND,
            )
            if message is None:
                url_line = (
                    f"\n汇总文档：{document.external_url}"
                    if document is not None and document.external_url
                    else ""
                )
                request = MessageProjectionRequest(
                    recipient_person_id=instance.owner_person_id,
                    text=(
                        f"流程已完成。\n实例：{instance.id}\n"
                        f"目标：{instance.snapshot.goal}{url_line}"
                    ),
                    idempotency_key=_completion_projection_key(
                        self.tenant_id,
                        instance.id,
                        attempt_no,
                        FEISHU_INSTANCE_MESSAGE_KIND,
                    ),
                )
                external = self.message_adapter.send_message(request)
                if not external.message_id.strip():
                    raise ValueError(
                        "Feishu instance message returned no message_id"
                    )
                self.projections.save_projection(
                    ProjectionRecord(
                        id=self.id_factory(),
                        tenant_id=self.tenant_id,
                        instance_id=instance.id,
                        node_instance_id=node.id,
                        attempt_no=attempt_no,
                        kind=FEISHU_INSTANCE_MESSAGE_KIND,
                        external_id=external.message_id,
                        idempotency_key=request.idempotency_key,
                        sync_version=instance.version,
                        state={"instance_status": instance.status.value},
                        created_at=now,
                        updated_at=now,
                    )
                )
                message_sent = True
        if not document_created and not message_sent:
            return _ProjectionOutcome(noop=True)
        return _ProjectionOutcome(
            document_created=document_created,
            message_sent=message_sent,
        )

    def _recreate_task(
        self,
        instance: WorkflowInstance,
        node_key: str,
        attempt_no: int,
        *,
        record: ProjectionRecord,
        now: datetime,
    ) -> ProjectionRecord:
        generation = _repair_generation(record.state) + 1
        request = self._task_request(
            instance,
            node_key,
            attempt_no,
            repair_generation=generation,
        )
        external = self.task_adapter.create_task(request)
        if not external.guid.strip():
            raise ValueError("Feishu task recreate returned an empty guid")
        node = instance.nodes[node_key]
        replacement = ProjectionRecord(
            id=record.id,
            tenant_id=record.tenant_id,
            instance_id=record.instance_id,
            node_instance_id=record.node_instance_id,
            attempt_no=record.attempt_no,
            kind=record.kind,
            external_id=external.guid,
            external_url=external.url,
            idempotency_key=request.idempotency_key,
            sync_version=max(record.sync_version, node.version),
            state={
                "node_status": node.status.value,
                "completed": False,
                "repair_generation": generation,
            },
            created_at=record.created_at,
            updated_at=now,
        )
        self.projections.replace_projection_external(
            replacement,
            expected_external_id=record.external_id or "",
            expected_idempotency_key=record.idempotency_key,
        )
        return replacement

    def _task_request(
        self,
        instance: WorkflowInstance,
        node_key: str,
        attempt_no: int,
        *,
        repair_generation: int = 0,
    ) -> TaskProjectionRequest:
        node = instance.nodes[node_key]
        spec = instance.snapshot.node(node_key)
        identity = (
            f"{self.tenant_id}:{node.id}:{attempt_no}:{FEISHU_TASK_KIND}"
        )
        if repair_generation:
            identity += f":repair:{repair_generation}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48]
        acceptance = "\n".join(
            f"- {item}" for item in spec.work.get("acceptance", ())
        )
        sections = [
            f"目标：{spec.work.get('objective', '')}",
            f"验收条件：\n{acceptance}",
        ]
        if node.executor != ExecutorKind.HUMAN:
            sections.insert(0, "处理方式：Agent 失败后的人工接管")
        instance_input_context = _instance_input_context(instance, node_key)
        if instance_input_context:
            sections.append(f"流程输入：\n{instance_input_context}")
        dependency_context = _dependency_context(instance, node_key)
        if dependency_context:
            sections.append(f"上游结果：\n{dependency_context}")
        sections.append(
            f"流程：{instance.id}\n节点：{node_key}\nAttempt：{attempt_no}"
        )
        description = "\n\n".join(sections)
        return TaskProjectionRequest(
            tenant_id=self.tenant_id,
            instance_id=instance.id,
            node_key=node_key,
            node_instance_id=node.id,
            attempt_no=attempt_no,
            owner_person_id=node.owner_person_id,
            summary=spec.title,
            description=description,
            idempotency_key=f"lf-{digest}",
        )

    def _node_message_request(
        self,
        instance: WorkflowInstance,
        node_key: str,
        attempt_no: int,
    ) -> MessageProjectionRequest:
        node = instance.nodes[node_key]
        spec = instance.snapshot.node(node_key)
        attempt = instance.attempts[(node_key, attempt_no)]
        result = ""
        if attempt.result is not None:
            content = attempt.result.get("content")
            if isinstance(content, str) and content.strip():
                result = content.strip()
            else:
                result = json.dumps(
                    to_json_value(attempt.result),
                    ensure_ascii=False,
                    sort_keys=True,
                )
        if len(result) > MAX_MESSAGE_RESULT_CHARS:
            result = result[:MAX_MESSAGE_RESULT_CHARS].rstrip() + "\n[结果已截断]"
        human_takeover = attempt.submitted_by_person_id is not None
        subject = "人工接管节点" if human_takeover else "自动节点"
        text = (
            f"{subject}已{_node_status_label(node.status)}。\n"
            f"流程：{instance.id}\n节点：{spec.title} ({node_key})"
        )
        if result:
            text += f"\n\n结果：\n{result}"
        card = None
        if node.status == NodeStatus.FAILED and not human_takeover:
            error_code = _safe_error_code(attempt.error_code)
            text += (
                f"\nAttempt：{attempt_no}\n错误码：{error_code}\n"
                "失败 Attempt 与错误详情已保留，请选择重新执行或人工接管。"
            )
            card = _recovery_card(
                instance,
                node_key,
                attempt_no,
                error_code=error_code,
            )
        return MessageProjectionRequest(
            recipient_person_id=node.owner_person_id,
            text=text,
            idempotency_key=_projection_key(
                self.tenant_id,
                node.id,
                str(attempt_no),
                FEISHU_MESSAGE_KIND,
            ),
            card=card,
        )

    def _document_request(
        self,
        instance: WorkflowInstance,
    ) -> DocumentProjectionRequest:
        sections = [
            f"<title>{html.escape(instance.snapshot.goal or instance.id)}</title>",
            '<callout emoji="✅" background-color="light-green" '
            'border-color="green"><p>流程已完成，以下内容来自中央工作流记录。</p></callout>',
            "<h1>流程信息</h1>",
            f"<p><b>实例：</b>{html.escape(instance.id)}</p>",
            f"<p><b>目标：</b>{html.escape(instance.snapshot.goal)}</p>",
            "<h1>节点结果</h1>",
        ]
        for spec in instance.snapshot.nodes:
            node = instance.nodes[spec.key]
            attempt = instance.current_attempt(spec.key)
            sections.append(
                f"<h2>{html.escape(spec.title)} ({html.escape(spec.key)})</h2>"
            )
            sections.append(
                f"<p><b>状态：</b>{html.escape(node.status.value)} "
                f"<b>执行器：</b>{html.escape(node.executor.value)} "
                f"<b>Owner：</b><cite type=\"user\" "
                f"user-id=\"{html.escape(node.owner_person_id, quote=True)}\"></cite></p>"
            )
            if attempt.result is None:
                continue
            content = attempt.result.get("content")
            rendered = (
                content.strip()
                if isinstance(content, str) and content.strip()
                else json.dumps(
                    to_json_value(attempt.result),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            if len(rendered) > MAX_DOCUMENT_RESULT_CHARS:
                rendered = (
                    rendered[:MAX_DOCUMENT_RESULT_CHARS].rstrip()
                    + "\n[结果已截断，完整值保存在流程记录中]"
                )
            sections.append(f"<p>{html.escape(rendered).replace(chr(10), '<br/>')}</p>")
        return DocumentProjectionRequest(
            title=instance.snapshot.goal or instance.id,
            content_xml="".join(sections),
        )

    def _retry_delay(self, attempt_count: int) -> timedelta:
        multiplier = 2 ** min(max(attempt_count - 1, 0), 10)
        return min(self.retry_max, self.retry_base * multiplier)


@dataclass(frozen=True)
class _ProjectionOutcome:
    created: bool = False
    recreated: bool = False
    completed: bool = False
    message_sent: bool = False
    document_created: bool = False
    noop: bool = False

    def merged(self, **changes: bool) -> _ProjectionOutcome:
        values = {
            "created": self.created,
            "recreated": self.recreated,
            "completed": self.completed,
            "message_sent": self.message_sent,
            "document_created": self.document_created,
            "noop": self.noop,
        }
        values.update(changes)
        if any(values[key] for key in values if key != "noop"):
            values["noop"] = False
        return _ProjectionOutcome(**values)


def _projection_key(*parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:44]
    return f"lf-{digest}"


def _completion_projection_key(
    tenant_id: str,
    instance_id: str,
    attempt_no: int,
    kind: str,
) -> str:
    """Keep first-run keys stable while versioning restarted completions."""

    if attempt_no == 1:
        return _projection_key(tenant_id, instance_id, kind)
    return _projection_key(tenant_id, instance_id, str(attempt_no), kind)


def _canonical_terminal_node(instance: WorkflowInstance) -> str:
    depended_on = {
        dependency
        for spec in instance.snapshot.nodes
        for dependency in spec.deps
    }
    sinks = sorted(spec.key for spec in instance.snapshot.nodes if spec.key not in depended_on)
    if not sinks:
        raise ValueError("completed instance has no terminal node")
    return sinks[0]


def _node_status_label(status: NodeStatus) -> str:
    return {
        NodeStatus.DONE: "完成",
        NodeStatus.FAILED: "失败",
        NodeStatus.CANCELED: "取消",
    }.get(status, status.value)


def _safe_error_code(value: str | None) -> str:
    if not value:
        return "execution_failed"
    normalized = "".join(
        character
        for character in value[:64]
        if character.isascii()
        and (character.isalnum() or character in {"_", "-", ".", ":"})
    )
    return normalized or "execution_failed"


def _recovery_card(
    instance: WorkflowInstance,
    node_key: str,
    attempt_no: int,
    *,
    error_code: str,
) -> dict[str, Any]:
    node = instance.nodes[node_key]
    spec = instance.snapshot.node(node_key)

    def button(label: str, action: RecoveryAction, style: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "name": RECOVERY_ACTION_NAME,
            "text": {"tag": "plain_text", "content": label},
            "type": style,
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "kind": RECOVERY_ACTION_NAME,
                        "action": action.value,
                        "instance_id": instance.id,
                        "node_key": node_key,
                        "attempt_no": attempt_no,
                        "node_version": node.version,
                        "instance_version": instance.version,
                    },
                }
            ],
        }

    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Agent 执行失败"},
            "subtitle": {"tag": "plain_text", "content": spec.title},
            "template": "red",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**流程**：`{instance.id}`\n"
                        f"**节点**：{spec.title} (`{node_key}`)\n"
                        f"**Attempt**：{attempt_no}\n"
                        f"**错误码**：`{error_code}`\n\n"
                        "失败 Attempt 与内部错误详情已保留。重新执行会创建新的 "
                        "Attempt，人工接管会向节点负责人创建飞书待办。"
                    ),
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "elements": [
                                button(
                                    "重新执行",
                                    RecoveryAction.RETRY,
                                    "primary_filled",
                                )
                            ],
                        },
                        {
                            "tag": "column",
                            "elements": [
                                button(
                                    "人工接管",
                                    RecoveryAction.HUMAN_TAKEOVER,
                                    "default",
                                )
                            ],
                        },
                    ],
                },
            ]
        },
    }


def recovery_result_card(text: str) -> dict[str, Any]:
    """Render a settled recovery callback without leaving live buttons behind."""

    rejected = text.startswith("命令未执行") or "未执行" in text[:40]
    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "update_multi": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "恢复操作未执行" if rejected else "恢复操作已处理",
            },
            "template": "orange" if rejected else "green",
        },
        "body": {"elements": [{"tag": "markdown", "content": text}]},
    }


def _repair_generation(state: Mapping[str, Any]) -> int:
    value = state.get("repair_generation", 0)
    if isinstance(value, bool):
        raise ValueError("projection repair_generation is invalid")
    try:
        generation = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("projection repair_generation is invalid") from exc
    if generation < 0:
        raise ValueError("projection repair_generation is invalid")
    return generation


def _dependency_context(instance: WorkflowInstance, node_key: str) -> str:
    sections = []
    for dependency_key in instance.snapshot.node(node_key).deps:
        attempt = instance.current_attempt(dependency_key)
        if attempt.result is None:
            continue
        dependency = instance.snapshot.node(dependency_key)
        content = attempt.result.get("content")
        if isinstance(content, str) and content.strip():
            rendered = content.strip()
        else:
            rendered = json.dumps(
                to_json_value(attempt.result),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        sections.append(f"[{dependency.title} / {dependency_key}]\n{rendered}")
    text = "\n\n".join(sections)
    if len(text) <= MAX_DEPENDENCY_CONTEXT_CHARS:
        return text
    omitted = len(text) - MAX_DEPENDENCY_CONTEXT_CHARS
    return (
        text[:MAX_DEPENDENCY_CONTEXT_CHARS].rstrip()
        + f"\n\n[内容过长，任务描述省略 {omitted} 个字符，完整结果保存在流程记录中]"
    )


def _instance_input_context(instance: WorkflowInstance, node_key: str) -> str:
    sections = []
    for declared_input in instance.snapshot.node(node_key).work.get("inputs", ()):
        if isinstance(declared_input, str):
            reference = declared_input
        elif isinstance(declared_input, Mapping):
            reference = declared_input.get("ref")
        else:
            continue
        if not isinstance(reference, str) or not reference.startswith(
            "instance_inputs."
        ):
            continue
        path = reference.removeprefix("instance_inputs.")
        found, value = _lookup_path(instance.snapshot.inputs, path)
        if not found:
            continue
        if isinstance(value, str):
            rendered = value
        else:
            rendered = json.dumps(
                to_json_value(value),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        sections.append(f"[{path}]\n{rendered}")
    text = "\n\n".join(sections)
    if len(text) <= MAX_DEPENDENCY_CONTEXT_CHARS:
        return text
    omitted = len(text) - MAX_DEPENDENCY_CONTEXT_CHARS
    return (
        text[:MAX_DEPENDENCY_CONTEXT_CHARS].rstrip()
        + f"\n\n[内容过长，任务描述省略 {omitted} 个字符，完整输入保存在流程记录中]"
    )


def _lookup_path(root: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = root
    for part in path.split("."):
        if not part or not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"projection event requires {field_name}")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"projection event requires positive {field_name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"projection event requires positive {field_name}") from exc
    if parsed < 1:
        raise ValueError(f"projection event requires positive {field_name}")
    return parsed
