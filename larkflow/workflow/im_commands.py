"""Durable Feishu IM commands for the Target workflow boundary."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Protocol

from .directory import PersonDirectory
from .model import InstanceStatus, WorkflowInstance, WorkflowInstanceSummary
from .repository import (
    InstanceAlreadyExistsError,
    InstanceNotFoundError,
    TemplateNotFoundError,
)
from .runner import AuthorizationError
from .restart import (
    RestartConfirmation,
    RestartNotAllowedError,
    RestartPreview,
    RestartPreviewExpiredError,
    RestartPreviewNotFoundError,
    RestartScope,
    StaleRestartPreviewError,
)
from .service import WorkflowService
from .template_service import (
    InvalidTemplateTransitionError,
    TemplateService,
    TemplateValidationError,
)
from .transitions import TransitionError


IM_MESSAGE_EVENT = "im.message.receive_v1"
COMMAND_PREFIX = "/larkflow"
MAX_STATUS_NODES = 20
MAX_STATUS_FIELD_CHARS = 120
MAX_LIST_INSTANCES = 10


class IMCommandStatus(str, Enum):
    PENDING = "pending"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


class InvalidIMCommandClaimError(RuntimeError):
    """A worker tried to settle a command it no longer owns."""


class IMCommandRejected(ValueError):
    """The authenticated sender supplied an invalid or unauthorized command."""


@dataclass(frozen=True)
class IMCommandSignal:
    id: str
    tenant_id: str
    message_id: str
    chat_id: str
    sender_person_id: str
    text: str
    occurred_at: datetime
    received_at: datetime


@dataclass(frozen=True)
class IMCommandClaim:
    event: IMCommandSignal
    claim_token: str
    attempt_count: int


@dataclass(frozen=True)
class IMReplyClaim:
    event_id: str
    tenant_id: str
    chat_id: str
    text: str
    idempotency_key: str
    claim_token: str
    attempt_count: int


class IMCommandStore(Protocol):
    def append_im_command(self, event: IMCommandSignal) -> bool:
        ...

    def claim_im_verification(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[IMCommandClaim, ...]:
        ...

    def mark_im_verified(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        now: datetime,
    ) -> None:
        ...

    def mark_im_verification_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...

    def mark_im_verification_rejected(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        outcome: str,
        reply_text: str,
        now: datetime,
    ) -> None:
        ...

    def claim_im_commands(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[IMCommandClaim, ...]:
        ...

    def mark_im_processed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        outcome: str,
        instance_id: str | None,
        reply_text: str,
        now: datetime,
    ) -> None:
        ...

    def mark_im_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...

    def claim_im_replies(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[IMReplyClaim, ...]:
        ...

    def mark_im_reply_sent(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        external_id: str,
        now: datetime,
    ) -> None:
        ...

    def mark_im_reply_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...


class IMReplySender(Protocol):
    def send_chat_message(
        self,
        *,
        chat_id: str,
        text: str,
        idempotency_key: str,
    ) -> str:
        ...


class IMEventInboxBridge:
    """Persist authenticated bot message envelopes without domain writes."""

    def __init__(
        self,
        store: IMCommandStore,
        *,
        tenant_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        self.store = store
        self.tenant_id = tenant_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, key: str, payload: Mapping[str, Any]) -> bool:
        if key != IM_MESSAGE_EVENT:
            return False
        header = payload.get("header")
        body = payload.get("event")
        if isinstance(header, Mapping) and isinstance(body, Mapping):
            sender = body.get("sender")
            message = body.get("message")
            if not isinstance(sender, Mapping) or not isinstance(message, Mapping):
                raise ValueError("IM event requires sender and message objects")
            sender_id = sender.get("sender_id")
            if not isinstance(sender_id, Mapping):
                raise ValueError("IM event requires sender_id")
            event_id = header.get("event_id")
            message_id = message.get("message_id")
            chat_id = message.get("chat_id")
            message_type = message.get("message_type")
            occurred_at = header.get("create_time")
            sender_person_id = sender_id.get("open_id")
            content = message.get("content")
            try:
                parsed_content = json.loads(content) if isinstance(content, str) else None
            except json.JSONDecodeError as exc:
                raise ValueError("IM text content is not valid JSON") from exc
            text = (
                parsed_content.get("text")
                if isinstance(parsed_content, Mapping)
                else None
            )
        else:
            event_id = payload.get("event_id")
            message_id = payload.get("message_id") or payload.get("id")
            chat_id = payload.get("chat_id")
            message_type = payload.get("message_type")
            occurred_at = payload.get("create_time") or payload.get("timestamp")
            sender_person_id = payload.get("sender_id")
            text = payload.get("content")
        if message_type != "text":
            return False
        if not isinstance(text, str) or not text.strip().startswith(COMMAND_PREFIX):
            return False
        now = self.clock()
        event = IMCommandSignal(
            id=_required_text(event_id, "event_id"),
            tenant_id=self.tenant_id,
            message_id=_required_text(message_id, "message_id"),
            chat_id=_required_text(chat_id, "chat_id"),
            sender_person_id=_required_text(sender_person_id, "sender.open_id"),
            text=text.strip(),
            occurred_at=_event_time(occurred_at, fallback=now),
            received_at=now,
        )
        return self.store.append_im_command(event)


@dataclass(frozen=True)
class IMVerificationReport:
    claimed: int = 0
    verified: int = 0
    rejected: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class IMCommandVerificationWorker:
    """Prove the sender is an active directory member before domain intake."""

    def __init__(
        self,
        store: IMCommandStore,
        directory: PersonDirectory,
        *,
        tenant_id: str,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
        max_attempts: int = 24,
    ) -> None:
        _validate_worker(tenant_id, worker_id, claim_limit, claim_ttl)
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("IM verification retry delays are invalid")
        if max_attempts < 1:
            raise ValueError("IM verification max_attempts must be positive")
        self.store = store
        self.directory = directory
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.max_attempts = max_attempts

    def run_once(self) -> IMVerificationReport:
        now = self.clock()
        claims = self.store.claim_im_verification(
            self.tenant_id,
            worker_id=self.worker_id,
            now=now,
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
        )
        verified = rejected = failed = 0
        errors = []
        for claim in claims:
            try:
                person = self.directory.get_person(
                    self.tenant_id,
                    claim.event.sender_person_id,
                )
                if (
                    person.person_id != claim.event.sender_person_id
                    or not person.active
                ):
                    self.store.mark_im_verification_rejected(
                        self.tenant_id,
                        claim.event.id,
                        claim_token=claim.claim_token,
                        outcome="rejected:inactive_sender",
                        reply_text="无法确认你的在职状态，流程命令未执行。",
                        now=now,
                    )
                    rejected += 1
                    continue
                self.store.mark_im_verified(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    now=now,
                )
                verified += 1
            except Exception as exc:
                failed += 1
                error = f"{claim.event.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                if claim.attempt_count >= self.max_attempts:
                    self.store.mark_im_verification_rejected(
                        self.tenant_id,
                        claim.event.id,
                        claim_token=claim.claim_token,
                        outcome="exhausted:directory_verification",
                        reply_text="暂时无法验证企业成员状态，流程命令未执行。",
                        now=now,
                    )
                else:
                    self.store.mark_im_verification_failed(
                        self.tenant_id,
                        claim.event.id,
                        claim_token=claim.claim_token,
                        error=error,
                        retry_at=now + self._retry_delay(claim.attempt_count),
                    )
        return IMVerificationReport(
            claimed=len(claims),
            verified=verified,
            rejected=rejected,
            failed=failed,
            errors=tuple(errors),
        )

    def _retry_delay(self, attempt_count: int) -> timedelta:
        multiplier = 2 ** min(max(attempt_count - 1, 0), 10)
        return min(self.retry_max, self.retry_base * multiplier)


@dataclass(frozen=True)
class IMCommandReport:
    claimed: int = 0
    processed: int = 0
    rejected: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class IMCommandWorker:
    """Apply verified workflow commands through domain services."""

    def __init__(
        self,
        store: IMCommandStore,
        service: WorkflowService,
        templates: TemplateService,
        *,
        tenant_id: str,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
    ) -> None:
        _validate_worker(tenant_id, worker_id, claim_limit, claim_ttl)
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("IM command retry delays are invalid")
        self.store = store
        self.service = service
        self.templates = templates
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max

    def run_once(self) -> IMCommandReport:
        now = self.clock()
        claims = self.store.claim_im_commands(
            self.tenant_id,
            worker_id=self.worker_id,
            now=now,
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
        )
        processed = rejected = failed = 0
        errors = []
        for claim in claims:
            try:
                outcome, instance_id, reply = self._apply(claim.event)
            except IMCommandRejected as exc:
                rejected += 1
                self.store.mark_im_processed(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    outcome="rejected:command",
                    instance_id=None,
                    reply_text=f"命令未执行：{exc}",
                    now=now,
                )
                continue
            except Exception as exc:
                failed += 1
                error = f"{claim.event.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.store.mark_im_failed(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=now + self._retry_delay(claim.attempt_count),
                )
                continue
            self.store.mark_im_processed(
                self.tenant_id,
                claim.event.id,
                claim_token=claim.claim_token,
                outcome=outcome,
                instance_id=instance_id,
                reply_text=reply,
                now=now,
            )
            processed += 1
        return IMCommandReport(
            claimed=len(claims),
            processed=processed,
            rejected=rejected,
            failed=failed,
            errors=tuple(errors),
        )

    def _apply(self, event: IMCommandSignal) -> tuple[str, str | None, str]:
        command, argument, inputs = parse_im_command(event.text)
        if command == "help":
            return (
                "helped",
                None,
                "可用命令：\n"
                "/larkflow start <template_id> [JSON输入]\n"
                "/larkflow confirm <instance_id>\n"
                "/larkflow status <instance_id>\n"
                "/larkflow list\n"
                "/larkflow restart <instance_id> <node_key>\n"
                "/larkflow restart-all <instance_id>\n"
                "/larkflow restart-confirm <preview_id>",
            )
        if command == "start":
            template_id = argument or ""
            try:
                version = self.templates.get_version(self.tenant_id, template_id)
                roles = {
                    str(node["owner_role"])
                    for node in version.definition.get("nodes", ())
                }
                snapshot = self.templates.instantiate(
                    self.tenant_id,
                    template_id,
                    inputs=inputs,
                    owner_bindings={role: event.sender_person_id for role in roles},
                )
            except (
                InvalidTemplateTransitionError,
                TemplateNotFoundError,
                TemplateValidationError,
            ) as exc:
                raise IMCommandRejected(str(exc)) from exc
            instance_id = _instance_id(self.tenant_id, event.message_id)
            try:
                instance = self.service.create_draft(
                    instance_id=instance_id,
                    tenant_id=self.tenant_id,
                    owner_person_id=event.sender_person_id,
                    actor_person_id=event.sender_person_id,
                    snapshot=snapshot,
                    correlation_id=event.message_id,
                )
            except InstanceAlreadyExistsError:
                instance = self.service.get(self.tenant_id, instance_id)
                if instance.owner_person_id != event.sender_person_id:
                    raise IMCommandRejected("同一消息对应了其他 Owner 的实例")
            if instance.status != InstanceStatus.DRAFT:
                raise IMCommandRejected("该消息对应的实例已不再是草稿")
            return (
                "draft_created",
                instance.id,
                "已创建流程草稿。\n"
                f"模板：{template_id}\n"
                f"实例：{instance.id}\n"
                f"目标：{instance.snapshot.goal}\n"
                f"节点数：{len(instance.snapshot.nodes)}\n"
                "请核对后回复：\n"
                f"/larkflow confirm {instance.id}",
            )
        if command == "confirm":
            instance_id = argument or ""
            try:
                instance = self.service.confirm_draft(
                    self.tenant_id,
                    instance_id,
                    actor_person_id=event.sender_person_id,
                    correlation_id=event.message_id,
                )
            except (AuthorizationError, InstanceNotFoundError, TransitionError):
                try:
                    existing = self.service.get(self.tenant_id, instance_id)
                except InstanceNotFoundError:
                    raise IMCommandRejected(
                        "实例不存在、无权确认或已不可确认"
                    ) from None
                if (
                    existing.owner_person_id == event.sender_person_id
                    and existing.status in {InstanceStatus.RUNNING, InstanceStatus.DONE}
                ):
                    instance = existing
                else:
                    raise IMCommandRejected("实例不存在、无权确认或已不可确认")
            return (
                "draft_confirmed",
                instance.id,
                f"流程已确认启动。\n实例：{instance.id}\n状态：{instance.status.value}",
            )
        if command == "status":
            instance_id = argument or ""
            try:
                instance = self.service.get_for_owner(
                    self.tenant_id,
                    instance_id,
                    actor_person_id=event.sender_person_id,
                )
            except (AuthorizationError, InstanceNotFoundError):
                raise IMCommandRejected("实例不存在或你无权查看") from None
            return (
                "status_shown",
                instance.id,
                _status_reply(instance, viewer_person_id=event.sender_person_id),
            )
        if command == "list":
            summaries = self.service.list_for_owner(
                self.tenant_id,
                actor_person_id=event.sender_person_id,
                limit=MAX_LIST_INSTANCES + 1,
            )
            return (
                "instances_listed",
                None,
                _list_reply(summaries),
            )
        if command == "restart":
            instance_id = argument or ""
            node_key = str(inputs.get("node_key") or "")
            try:
                preview = self.service.preview_node_restart(
                    self.tenant_id,
                    instance_id,
                    node_key,
                    actor_person_id=event.sender_person_id,
                )
                instance = self.service.get_for_owner(
                    self.tenant_id,
                    instance_id,
                    actor_person_id=event.sender_person_id,
                )
                if instance.version != preview.expected_instance_version:
                    raise StaleRestartPreviewError(
                        "instance changed while rendering restart preview"
                    )
            except (
                AuthorizationError,
                InstanceNotFoundError,
                RestartNotAllowedError,
            ):
                raise IMCommandRejected(
                    "实例或节点不存在、无权操作，或当前状态不可重启"
                ) from None
            except StaleRestartPreviewError:
                raise IMCommandRejected("流程状态已变化，请重新预览") from None
            return (
                "restart_previewed",
                instance.id,
                _restart_preview_reply(preview, instance),
            )
        if command == "restart-all":
            instance_id = argument or ""
            try:
                preview = self.service.preview_instance_restart(
                    self.tenant_id,
                    instance_id,
                    actor_person_id=event.sender_person_id,
                )
                instance = self.service.get_for_owner(
                    self.tenant_id,
                    instance_id,
                    actor_person_id=event.sender_person_id,
                )
                if instance.version != preview.expected_instance_version:
                    raise StaleRestartPreviewError(
                        "instance changed while rendering restart preview"
                    )
            except (
                AuthorizationError,
                InstanceNotFoundError,
                RestartNotAllowedError,
            ):
                raise IMCommandRejected(
                    "实例不存在、无权操作，或当前状态不可完整重启"
                ) from None
            except StaleRestartPreviewError:
                raise IMCommandRejected("流程状态已变化，请重新预览") from None
            return (
                "instance_restart_previewed",
                instance.id,
                _restart_preview_reply(preview, instance),
            )
        if command == "restart-confirm":
            preview_id = argument or ""
            try:
                confirmation = self.service.confirm_restart(
                    self.tenant_id,
                    preview_id,
                    actor_person_id=event.sender_person_id,
                )
            except RestartPreviewExpiredError:
                raise IMCommandRejected("重启预览已过期，请重新预览") from None
            except StaleRestartPreviewError:
                raise IMCommandRejected("流程状态已变化，请重新预览") from None
            except (
                AuthorizationError,
                InstanceNotFoundError,
                RestartNotAllowedError,
                RestartPreviewNotFoundError,
            ):
                raise IMCommandRejected(
                    "重启预览不存在、已失效或你无权确认"
                ) from None
            return (
                "restart_confirmed",
                confirmation.instance.id,
                _restart_confirmation_reply(confirmation),
            )
        raise IMCommandRejected("不支持的命令")

    def _retry_delay(self, attempt_count: int) -> timedelta:
        multiplier = 2 ** min(max(attempt_count - 1, 0), 10)
        return min(self.retry_max, self.retry_base * multiplier)


@dataclass(frozen=True)
class IMReplyReport:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class IMReplyWorker:
    """Deliver durable command replies through the credential-side adapter."""

    def __init__(
        self,
        store: IMCommandStore,
        sender: IMReplySender,
        *,
        tenant_id: str,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
    ) -> None:
        _validate_worker(tenant_id, worker_id, claim_limit, claim_ttl)
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("IM reply retry delays are invalid")
        self.store = store
        self.sender = sender
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max

    def run_once(self) -> IMReplyReport:
        now = self.clock()
        claims = self.store.claim_im_replies(
            self.tenant_id,
            worker_id=self.worker_id,
            now=now,
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
        )
        sent = failed = 0
        errors = []
        for claim in claims:
            try:
                external_id = self.sender.send_chat_message(
                    chat_id=claim.chat_id,
                    text=claim.text,
                    idempotency_key=claim.idempotency_key,
                )
                if not external_id.strip():
                    raise ValueError("Feishu IM send returned no message_id")
                self.store.mark_im_reply_sent(
                    self.tenant_id,
                    claim.event_id,
                    claim_token=claim.claim_token,
                    external_id=external_id,
                    now=now,
                )
                sent += 1
            except Exception as exc:
                failed += 1
                error = f"{claim.event_id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.store.mark_im_reply_failed(
                    self.tenant_id,
                    claim.event_id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=now + self._retry_delay(claim.attempt_count),
                )
        return IMReplyReport(
            claimed=len(claims),
            sent=sent,
            failed=failed,
            errors=tuple(errors),
        )

    def _retry_delay(self, attempt_count: int) -> timedelta:
        multiplier = 2 ** min(max(attempt_count - 1, 0), 10)
        return min(self.retry_max, self.retry_base * multiplier)


def parse_im_command(text: str) -> tuple[str, str | None, dict[str, Any]]:
    """Parse the intentionally narrow v0 command grammar."""
    parts = text.strip().split(maxsplit=3)
    if not parts or parts[0] != COMMAND_PREFIX:
        raise IMCommandRejected("命令必须以 /larkflow 开头")
    if len(parts) == 1 or parts[1] == "help":
        if len(parts) > 2:
            raise IMCommandRejected("help 命令不接受其他参数")
        return "help", None, {}
    if parts[1] == "list":
        if len(parts) != 2:
            raise IMCommandRejected("list 命令不接受其他参数")
        return "list", None, {}
    if parts[1] == "restart":
        if len(parts) != 4:
            raise IMCommandRejected(
                "用法：/larkflow restart <instance_id> <node_key>"
            )
        return "restart", parts[2], {"node_key": parts[3]}
    if parts[1] == "restart-all":
        if len(parts) != 3:
            raise IMCommandRejected(
                "用法：/larkflow restart-all <instance_id>"
            )
        return "restart-all", parts[2], {}
    if parts[1] == "restart-confirm":
        if len(parts) != 3:
            raise IMCommandRejected(
                "用法：/larkflow restart-confirm <preview_id>"
            )
        return "restart-confirm", parts[2], {}
    if parts[1] in {"confirm", "status"}:
        if len(parts) != 3:
            raise IMCommandRejected(
                f"用法：/larkflow {parts[1]} <instance_id>"
            )
        return parts[1], parts[2], {}
    if parts[1] != "start" or len(parts) < 3:
        raise IMCommandRejected(
            "仅支持 start、confirm、status、list、restart、restart-all、"
            "restart-confirm 和 help"
        )
    inputs: dict[str, Any] = {}
    if len(parts) == 4:
        try:
            parsed = json.loads(parts[3])
        except json.JSONDecodeError as exc:
            raise IMCommandRejected("模板输入必须是 JSON 对象") from exc
        if not isinstance(parsed, Mapping):
            raise IMCommandRejected("模板输入必须是 JSON 对象")
        inputs = {str(key): value for key, value in parsed.items()}
    return "start", parts[2], inputs


def _instance_id(tenant_id: str, message_id: str) -> str:
    digest = hashlib.sha256(
        f"{tenant_id}:{message_id}".encode("utf-8")
    ).hexdigest()[:24]
    return f"im_{digest}"


def _reply_key(event_id: str) -> str:
    return "lf-im-" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:40]


def _status_reply(instance: WorkflowInstance, *, viewer_person_id: str) -> str:
    specs = instance.snapshot.nodes
    completed = sum(
        1
        for node in instance.nodes.values()
        if node.status.value == "done"
    )
    lines = [
        "流程状态",
        f"实例：{instance.id}",
        f"目标：{_status_field(instance.snapshot.goal or '未填写')}",
        f"状态：{_instance_status_label(instance.status.value)}",
        f"进度：{completed}/{len(specs)}",
        "节点：",
    ]
    for spec in specs[:MAX_STATUS_NODES]:
        runtime_node = instance.nodes.get(spec.key)
        status = (
            _node_status_label(runtime_node.status.value)
            if runtime_node is not None
            else "未启动"
        )
        owner_person_id = (
            runtime_node.owner_person_id
            if runtime_node is not None
            else spec.owner_person_id
        )
        owner = "你" if owner_person_id == viewer_person_id else "其他负责人"
        lines.append(
            f"- {_status_field(spec.title)} ({spec.key})｜"
            f"{spec.executor.value}｜{status}｜责任人：{owner}"
        )
    omitted = len(specs) - MAX_STATUS_NODES
    if omitted > 0:
        lines.append(f"其余 {omitted} 个节点已省略。")
    return "\n".join(lines)


def _list_reply(summaries: tuple[WorkflowInstanceSummary, ...]) -> str:
    if not summaries:
        return (
            "我的最近流程\n"
            "暂无由你发起的流程。\n"
            "使用 /larkflow start <template_id> [JSON输入] 创建流程。"
        )
    lines = ["我的最近流程"]
    for index, summary in enumerate(summaries[:MAX_LIST_INSTANCES], start=1):
        lines.extend(
            (
                f"{index}. {_instance_status_label(summary.status.value)}｜"
                f"{summary.completed_nodes}/{summary.total_nodes}｜"
                f"{_status_field(summary.goal or '未填写')}",
                f"   实例：{_status_field(summary.id)}",
            )
        )
    if len(summaries) > MAX_LIST_INSTANCES:
        lines.append(f"仅显示最近 {MAX_LIST_INSTANCES} 个流程。")
    lines.append("使用 /larkflow status <instance_id> 查看节点详情。")
    return "\n".join(lines)


def _restart_preview_reply(
    preview: RestartPreview,
    instance: WorkflowInstance,
) -> str:
    if preview.scope == RestartScope.INSTANCE:
        lines = [
            "完整实例重启预览",
            f"实例：{_status_field(instance.id)}",
            "范围：全部节点",
            f"影响节点：{len(preview.affected_node_keys)} 个",
        ]
    else:
        node_key = preview.node_key or ""
        lines = [
            "节点重启预览",
            f"实例：{_status_field(instance.id)}",
            f"目标节点：{_status_field(instance.snapshot.node(node_key).title)} "
            f"({node_key})",
            f"影响节点：{len(preview.affected_node_keys)} 个",
        ]
    for node_key in preview.affected_node_keys:
        spec = instance.snapshot.node(node_key)
        attempt_no = instance.nodes[node_key].current_attempt_no
        lines.append(
            f"- {_status_field(spec.title)} ({node_key})｜"
            f"当前 Attempt {attempt_no}"
        )
    lines.extend(
        (
            "确认后，上述节点将创建新 Attempt；旧 Attempt、结果和审计保留。",
            "若流程状态发生变化，该预览会自动失效。",
            "请确认：",
            f"/larkflow restart-confirm {preview.id}",
        )
    )
    return "\n".join(lines)


def _restart_confirmation_reply(confirmation: RestartConfirmation) -> str:
    preview = confirmation.preview
    instance = confirmation.instance
    if confirmation.already_applied:
        prefix = "该重启已执行，无需重复操作。"
    elif preview.scope == RestartScope.INSTANCE:
        prefix = "完整实例已重启。"
    else:
        prefix = "节点已重启。"
    lines = [
        prefix,
        f"实例：{_status_field(instance.id)}",
        (
            "范围：全部节点"
            if preview.scope == RestartScope.INSTANCE
            else f"目标节点：{preview.node_key}"
        ),
        f"影响节点：{len(preview.affected_node_keys)} 个",
        f"状态：{_instance_status_label(instance.status.value)}",
        "当前 Attempt：" if confirmation.already_applied else "新 Attempt：",
    ]
    for node_key in preview.affected_node_keys:
        lines.append(
            f"- {node_key}｜Attempt {instance.nodes[node_key].current_attempt_no}｜"
            f"{_node_status_label(instance.nodes[node_key].status.value)}"
        )
    return "\n".join(lines)


def _status_field(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= MAX_STATUS_FIELD_CHARS:
        return normalized
    return normalized[:MAX_STATUS_FIELD_CHARS].rstrip() + "…"


def _instance_status_label(value: str) -> str:
    return {
        "draft": "草稿",
        "running": "进行中",
        "paused": "已暂停",
        "done": "已完成",
        "failed": "失败",
        "canceled": "已取消",
        "discarded": "已丢弃",
    }.get(value, value)


def _node_status_label(value: str) -> str:
    return {
        "pending": "等待依赖",
        "ready": "待调度",
        "running": "执行中",
        "waiting_human": "等待人工",
        "done": "已完成",
        "failed": "失败",
        "canceled": "已取消",
    }.get(value, value)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"IM event requires {field_name}")
    return value.strip()


def _event_time(value: Any, *, fallback: datetime) -> datetime:
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return fallback
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)


def _validate_worker(
    tenant_id: str,
    worker_id: str,
    claim_limit: int,
    claim_ttl: timedelta,
) -> None:
    if not tenant_id.strip():
        raise ValueError("IM tenant_id is required")
    if not worker_id.strip():
        raise ValueError("IM worker_id is required")
    if claim_limit < 1:
        raise ValueError("IM claim_limit must be positive")
    if claim_ttl <= timedelta(0):
        raise ValueError("IM claim_ttl must be positive")


__all__ = [
    "COMMAND_PREFIX",
    "IMCommandClaim",
    "IMCommandRejected",
    "IMCommandReport",
    "IMCommandSignal",
    "IMCommandStatus",
    "IMCommandStore",
    "IMCommandVerificationWorker",
    "IMCommandWorker",
    "IMEventInboxBridge",
    "IM_MESSAGE_EVENT",
    "IMReplyClaim",
    "IMReplyReport",
    "IMReplyWorker",
    "IMVerificationReport",
    "InvalidIMCommandClaimError",
    "parse_im_command",
]
