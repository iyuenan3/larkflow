"""Durable Feishu IM commands for the Target workflow boundary."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import re
import time
from typing import Any, Protocol

from .card_feedback import (
    CARD_FEEDBACK_FALLBACK,
    processing_card,
    report_card_feedback,
)
from .directory import PersonDirectory
from .decision import (
    HUMAN_DECISION_ACTION_NAME,
    HUMAN_DECISION_FEEDBACK_FIELD,
    HumanDecision,
    HumanDecisionFeedbackError,
    HumanDecisionNotAllowedError,
    StaleHumanDecisionError,
    human_decision_action_name,
    normalize_human_decision_feedback,
)
from .editing import (
    GraphEditConfirmation,
    GraphEditNotAllowedError,
    GraphEditPreview,
    GraphEditPreviewExpiredError,
    GraphEditPreviewNotFoundError,
    StaleGraphEditPreviewError,
)
from .event_time import feishu_event_time
from .model import InstanceStatus, WorkflowInstance, WorkflowInstanceSummary
from .repository import (
    InstanceAlreadyExistsError,
    InstanceNotFoundError,
    TemplateNotFoundError,
)
from .recovery import (
    RECOVERY_ACTION_NAME,
    RecoveryAction,
    RecoveryNotAllowedError,
    StaleRecoveryError,
    recovery_action_name,
)
from .runner import AuthorizationError
from .role_bindings import DRAFT_WIZARD_KIND, RoleBindingRequest
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
    inline_owner_roles,
    instantiate_inline_definition,
)
from .transitions import TransitionError


IM_MESSAGE_EVENT = "im.message.receive_v1"
COMMAND_PREFIX = "/larkflow"
MAX_STATUS_NODES = 20
MAX_STATUS_FIELD_CHARS = 120
MAX_LIST_INSTANCES = 10
MAX_OWNER_BINDINGS = 100
ROLE_BINDING_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MENTION_KEY_RE = re.compile(r"^@_user_[1-9][0-9]*$")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


STRICT_JSON_DECODER = json.JSONDecoder(
    object_pairs_hook=_strict_json_object,
    parse_constant=_reject_non_json_constant,
)


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


class _RoleBindingRequired(RuntimeError):
    def __init__(self, request: RoleBindingRequest) -> None:
        super().__init__(request.command_id)
        self.request = request


@dataclass(frozen=True)
class IMMention:
    key: str
    person_id: str


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
    mentions: tuple[IMMention, ...] = field(default_factory=tuple)
    card_update_token: str | None = None
    available_at: datetime | None = None


@dataclass(frozen=True)
class _ParsedIMCommand:
    command: str
    argument: str | None
    inputs: dict[str, Any] = field(default_factory=dict)
    owner_mentions: dict[str, str] = field(default_factory=dict)


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
    card_update_token: str | None = None
    command_text: str | None = None


class IMCommandStore(Protocol):
    def append_im_command(self, event: IMCommandSignal) -> bool:
        ...

    def release_im_command(
        self,
        tenant_id: str,
        event_id: str,
        *,
        available_at: datetime,
        feedback_status: str,
        feedback_elapsed_ms: int,
    ) -> None:
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

    def mark_im_role_binding_requested(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        request: RoleBindingRequest,
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

    def update_chat_card(
        self,
        *,
        token: str,
        card: Mapping[str, Any],
    ) -> None:
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
            raw_mentions = message.get("mentions")
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
            raw_mentions = payload.get("mentions")
            text = payload.get("content")
        if message_type != "text":
            return False
        mentions = _normalize_mentions(raw_mentions)
        if not (isinstance(header, Mapping) and isinstance(body, Mapping)):
            text = _restore_flattened_mention_keys(text, raw_mentions)
        command_text = _extract_command_text(text, mentions)
        if command_text is None:
            return False
        now = self.clock()
        event = IMCommandSignal(
            id=_required_text(event_id, "event_id"),
            tenant_id=self.tenant_id,
            message_id=_required_text(message_id, "message_id"),
            chat_id=_required_text(chat_id, "chat_id"),
            sender_person_id=_required_text(sender_person_id, "sender.open_id"),
            text=command_text,
            occurred_at=feishu_event_time(occurred_at, fallback=now),
            received_at=now,
            mentions=mentions,
        )
        return self.store.append_im_command(event)


class RecoveryActionInboxBridge:
    """Turn trusted recovery-card callbacks into durable commands."""

    def __init__(
        self,
        store: IMCommandStore,
        *,
        tenant_id: str,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        card_updater: Callable[..., None] | None = None,
        feedback_reporter: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if not tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        self.store = store
        self.tenant_id = tenant_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.card_updater = card_updater
        self.feedback_reporter = feedback_reporter

    def __call__(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        if event_type != "card.action.trigger":
            return False
        action_name = payload.get("action_name")
        if action_name not in {
            recovery_action_name(action) for action in RecoveryAction
        }:
            value = payload.get("action_value")
            if not (
                isinstance(value, Mapping)
                and value.get("kind") == RECOVERY_ACTION_NAME
            ):
                return False
            if action_name is not None:
                raise ValueError(
                    "unexpected recovery action name: "
                    f"{_safe_callback_name(action_name)}"
                )
        if payload.get("action_tag") != "button":
            raise ValueError("recovery action must come from a button")
        value = payload.get("action_value")
        if not isinstance(value, Mapping):
            raise ValueError("recovery action_value must be an object")
        command_payload = _validated_recovery_payload(value)
        if (
            action_name is not None
            and action_name != recovery_action_name(command_payload["action"])
        ):
            raise ValueError("recovery action name does not match its value")
        feedback_started = self.monotonic()
        now = self.clock()
        event = IMCommandSignal(
            id=_required_text(payload.get("event_id"), "event_id"),
            tenant_id=self.tenant_id,
            message_id=_required_text(payload.get("message_id"), "message_id"),
            chat_id=_required_text(payload.get("chat_id"), "chat_id"),
            sender_person_id=_required_text(
                payload.get("operator_id"),
                "operator_id",
            ),
            text=(
                f"{COMMAND_PREFIX} recover "
                + json.dumps(command_payload, separators=(",", ":"))
            ),
            occurred_at=feishu_event_time(payload.get("timestamp"), fallback=now),
            received_at=now,
            card_update_token=_required_text(payload.get("token"), "token"),
            available_at=(
                now + CARD_FEEDBACK_FALLBACK
                if self.card_updater is not None
                else None
            ),
        )
        appended = self.store.append_im_command(event)
        if appended and self.card_updater is not None:
            label = {
                RecoveryAction.RETRY: "重新执行",
                RecoveryAction.HUMAN_TAKEOVER: "人工接管",
            }[RecoveryAction(command_payload["action"])]
            feedback_status = "updated"
            try:
                self.card_updater(
                    token=event.card_update_token,
                    card=processing_card(
                        title="恢复操作已收到",
                        content=f"已收到“{label}”，正在重新校验权限与流程状态。",
                    ),
                )
            except Exception:
                feedback_status = "failed"
                raise
            finally:
                completed_at = self.clock()
                elapsed_ms = max(
                    0,
                    int((self.monotonic() - feedback_started) * 1000 + 0.5),
                )
                self.store.release_im_command(
                    self.tenant_id,
                    event.id,
                    available_at=completed_at,
                    feedback_status=feedback_status,
                    feedback_elapsed_ms=elapsed_ms,
                )
                report_card_feedback(
                    self.feedback_reporter,
                    card_kind="recovery",
                    status=feedback_status,
                    elapsed_ms=elapsed_ms,
                )
        return appended


class HumanDecisionActionInboxBridge:
    """Turn trusted Human decision-card callbacks into durable commands."""

    def __init__(
        self,
        store: IMCommandStore,
        *,
        tenant_id: str,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        card_updater: Callable[..., None] | None = None,
        feedback_reporter: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if not tenant_id.strip():
            raise ValueError("Target tenant_id is required")
        self.store = store
        self.tenant_id = tenant_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.card_updater = card_updater
        self.feedback_reporter = feedback_reporter

    def __call__(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        if event_type != "card.action.trigger":
            return False
        action_name = payload.get("action_name")
        if action_name not in {
            human_decision_action_name(decision) for decision in HumanDecision
        }:
            value = payload.get("action_value")
            if not (
                isinstance(value, Mapping)
                and value.get("kind") == HUMAN_DECISION_ACTION_NAME
            ):
                return False
            if action_name is not None:
                raise ValueError(
                    "unexpected Human decision action name: "
                    f"{_safe_callback_name(action_name)}"
                )
        if payload.get("action_tag") != "button":
            raise ValueError("Human decision must come from a button")
        value = payload.get("action_value")
        if not isinstance(value, Mapping):
            raise ValueError("Human decision action_value must be an object")
        card_payload = _validated_human_decision_card_payload(value)
        decision = HumanDecision(card_payload["decision"])
        if (
            action_name is not None
            and action_name != human_decision_action_name(decision)
        ):
            raise ValueError("Human decision action name does not match its value")
        feedback = _human_decision_form_feedback(
            decision,
            payload.get("form_value"),
        )
        command_payload = _validated_human_decision_payload(
            {**card_payload, "feedback": feedback}
        )

        feedback_started = self.monotonic()
        now = self.clock()
        event = IMCommandSignal(
            id=_required_text(payload.get("event_id"), "event_id"),
            tenant_id=self.tenant_id,
            message_id=_required_text(payload.get("message_id"), "message_id"),
            chat_id=_required_text(payload.get("chat_id"), "chat_id"),
            sender_person_id=_required_text(payload.get("operator_id"), "operator_id"),
            text=(
                f"{COMMAND_PREFIX} decide "
                + json.dumps(command_payload, separators=(",", ":"))
            ),
            occurred_at=feishu_event_time(payload.get("timestamp"), fallback=now),
            received_at=now,
            card_update_token=_required_text(payload.get("token"), "token"),
            available_at=(
                now + CARD_FEEDBACK_FALLBACK
                if self.card_updater is not None
                else None
            ),
        )
        appended = self.store.append_im_command(event)
        if appended and self.card_updater is not None:
            label = {
                HumanDecision.ACCEPT: "接受",
                HumanDecision.REJECT: "退回",
            }[decision]
            feedback_status = "updated"
            try:
                self.card_updater(
                    token=event.card_update_token,
                    card=processing_card(
                        title="复核决定已收到",
                        content=f"已收到“{label}”，正在重新校验身份与流程状态。",
                    ),
                )
            except Exception:
                feedback_status = "failed"
                raise
            finally:
                completed_at = self.clock()
                elapsed_ms = max(
                    0,
                    int((self.monotonic() - feedback_started) * 1000 + 0.5),
                )
                self.store.release_im_command(
                    self.tenant_id,
                    event.id,
                    available_at=completed_at,
                    feedback_status=feedback_status,
                    feedback_elapsed_ms=elapsed_ms,
                )
                report_card_feedback(
                    self.feedback_reporter,
                    card_kind="human_decision",
                    status=feedback_status,
                    elapsed_ms=elapsed_ms,
                )
        return appended


def _safe_callback_name(value: Any) -> str:
    """Bound callback diagnostics to a non-sensitive, log-safe token."""

    if not isinstance(value, str) or not value:
        return "<missing>"
    if len(value) > 64 or not all(
        character.isascii()
        and (character.isalnum() or character in {"_", "-", ".", ":"})
        for character in value
    ):
        return "<invalid>"
    return value


@dataclass(frozen=True)
class IMVerificationReport:
    claimed: int = 0
    verified: int = 0
    rejected: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class IMCommandVerificationWorker:
    """Prove the sender and referenced owners are active before domain intake."""

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
        claim_now = self.clock()
        claims = self.store.claim_im_verification(
            self.tenant_id,
            worker_id=self.worker_id,
            now=claim_now,
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
                    completed_at = self.clock()
                    self.store.mark_im_verification_rejected(
                        self.tenant_id,
                        claim.event.id,
                        claim_token=claim.claim_token,
                        outcome="rejected:inactive_sender",
                        reply_text="无法确认你的在职状态，流程命令未执行。",
                        now=completed_at,
                    )
                    rejected += 1
                    continue
                try:
                    owner_person_ids = _owner_person_ids_from_mentions(claim.event)
                except IMCommandRejected:
                    completed_at = self.clock()
                    self.store.mark_im_verification_rejected(
                        self.tenant_id,
                        claim.event.id,
                        claim_token=claim.claim_token,
                        outcome="rejected:invalid_owner_mention",
                        reply_text="无法确认角色绑定中的飞书成员，流程命令未执行。",
                        now=completed_at,
                    )
                    rejected += 1
                    continue
                inactive_owner = False
                for person_id in sorted(
                    set(owner_person_ids) - {claim.event.sender_person_id}
                ):
                    owner = self.directory.get_person(self.tenant_id, person_id)
                    if owner.person_id != person_id or not owner.active:
                        inactive_owner = True
                        break
                if inactive_owner:
                    completed_at = self.clock()
                    self.store.mark_im_verification_rejected(
                        self.tenant_id,
                        claim.event.id,
                        claim_token=claim.claim_token,
                        outcome="rejected:inactive_owner",
                        reply_text="无法确认角色负责人仍在当前企业，流程命令未执行。",
                        now=completed_at,
                    )
                    rejected += 1
                    continue
                completed_at = self.clock()
                self.store.mark_im_verified(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    now=completed_at,
                )
                verified += 1
            except Exception as exc:
                failed_at = self.clock()
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
                        now=failed_at,
                    )
                else:
                    self.store.mark_im_verification_failed(
                        self.tenant_id,
                        claim.event.id,
                        claim_token=claim.claim_token,
                        error=error,
                        retry_at=failed_at + self._retry_delay(claim.attempt_count),
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
        claim_now = self.clock()
        claims = self.store.claim_im_commands(
            self.tenant_id,
            worker_id=self.worker_id,
            now=claim_now,
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
        )
        processed = rejected = failed = 0
        errors = []
        for claim in claims:
            try:
                outcome, instance_id, reply = self._apply(claim.event)
            except _RoleBindingRequired as required:
                completed_at = self.clock()
                self.store.mark_im_role_binding_requested(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    request=required.request,
                    now=completed_at,
                )
                processed += 1
                continue
            except IMCommandRejected as exc:
                completed_at = self.clock()
                rejected += 1
                self.store.mark_im_processed(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    outcome="rejected:command",
                    instance_id=None,
                    reply_text=f"命令未执行：{exc}",
                    now=completed_at,
                )
                continue
            except Exception as exc:
                failed_at = self.clock()
                failed += 1
                error = f"{claim.event.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.store.mark_im_failed(
                    self.tenant_id,
                    claim.event.id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=failed_at + self._retry_delay(claim.attempt_count),
                )
                continue
            completed_at = self.clock()
            self.store.mark_im_processed(
                self.tenant_id,
                claim.event.id,
                claim_token=claim.claim_token,
                outcome=outcome,
                instance_id=instance_id,
                reply_text=reply,
                now=completed_at,
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
        parsed = _parse_im_command(event.text)
        command = parsed.command
        argument = parsed.argument
        inputs = parsed.inputs
        if command == "help":
            return (
                "helped",
                None,
                "可用命令：\n"
                "/larkflow draft（打开自然语言草稿引导卡）\n"
                "/larkflow draft <JSON定义> [role=@成员 ...]（高级入口）\n"
                "/larkflow start <template_id> [JSON输入] [role=@成员 ...]\n"
                "模板多角色流程在单聊中会发送人员选择卡片；"
                "无模板多角色草稿必须显式 @成员。\n"
                "/larkflow confirm <instance_id>\n"
                "/larkflow status <instance_id>\n"
                "/larkflow list\n"
                "/larkflow edit <instance_id> <JSON操作数组>\n"
                "/larkflow edit-confirm <preview_id>\n"
                "/larkflow restart <instance_id> <node_key>\n"
                "/larkflow restart-all <instance_id>\n"
                "/larkflow restart-confirm <preview_id>\n"
                "Agent 失败时，节点负责人可在失败卡片中选择重新执行或人工接管。",
            )
        if command == "recover":
            action = RecoveryAction(str(inputs["action"]))
            node_key = str(inputs["node_key"])
            try:
                instance = self.service.recover_failed_node(
                    self.tenant_id,
                    argument or "",
                    node_key,
                    action,
                    actor_person_id=event.sender_person_id,
                    expected_instance_version=int(inputs["instance_version"]),
                    expected_node_version=int(inputs["node_version"]),
                    expected_attempt_no=int(inputs["attempt_no"]),
                    correlation_id=event.message_id,
                )
            except StaleRecoveryError:
                raise IMCommandRejected(
                    "失败卡片已失效，流程或节点已有新的状态"
                ) from None
            except (
                AuthorizationError,
                InstanceNotFoundError,
                RecoveryNotAllowedError,
            ):
                raise IMCommandRejected(
                    "实例或节点不存在、你不是节点负责人，或该失败已不可恢复"
                ) from None
            node = instance.nodes[node_key]
            if action == RecoveryAction.RETRY:
                reply = (
                    "已创建新的自动执行 Attempt。\n"
                    f"实例：{instance.id}\n节点：{node_key}\n"
                    f"Attempt：{node.current_attempt_no}\n状态：待调度"
                )
                outcome = "automated_retry_started"
            else:
                reply = (
                    "已转为人工接管，并向节点负责人创建飞书待办。\n"
                    f"实例：{instance.id}\n节点：{node_key}\n"
                    f"Attempt：{node.current_attempt_no}\n状态：等待人工"
                )
                outcome = "human_takeover_started"
            return outcome, instance.id, reply
        if command == "decide":
            decision = HumanDecision(str(inputs["decision"]))
            node_key = str(inputs["node_key"])
            try:
                instance = self.service.submit_human_decision(
                    self.tenant_id,
                    argument or "",
                    node_key,
                    decision,
                    actor_person_id=event.sender_person_id,
                    attempt_no=int(inputs["attempt_no"]),
                    expected_instance_version=int(inputs["instance_version"]),
                    expected_node_version=int(inputs["node_version"]),
                    feedback=inputs.get("feedback"),
                    correlation_id=event.message_id,
                )
            except StaleHumanDecisionError:
                raise IMCommandRejected(
                    "复核卡片已失效，流程或节点已有新的状态"
                ) from None
            except (
                AuthorizationError,
                HumanDecisionNotAllowedError,
                InstanceNotFoundError,
                TransitionError,
            ):
                raise IMCommandRejected(
                    "实例或节点不存在、你不是节点负责人，或该复核已不可操作"
                ) from None
            if decision == HumanDecision.ACCEPT:
                return (
                    "human_decision_accepted",
                    instance.id,
                    "已接受节点结果。\n"
                    f"实例：{instance.id}\n节点：{node_key}\n"
                    f"Attempt：{inputs['attempt_no']}\n状态：{instance.status.value}",
                )
            config = instance.snapshot.node(node_key).work["decision"]
            reject_target = str(config["reject_target"])
            feedback = _display_human_decision_feedback(str(inputs["feedback"]))
            return (
                "human_decision_rejected",
                instance.id,
                "已退回节点结果，流程已进入失败状态，旧结果与审计均已保留。\n"
                f"实例：{instance.id}\n节点：{node_key}\n"
                f"Attempt：{inputs['attempt_no']}\n"
                f"退回意见：{feedback}\n"
                "请由 Instance Owner 预览返工范围：\n"
                f"/larkflow restart {instance.id} {reject_target}",
            )
        if command == "draft":
            definition = inputs.get("definition")
            if inputs.get("wizard") is True:
                raise _RoleBindingRequired(
                    RoleBindingRequest(
                        command_id=event.id,
                        tenant_id=self.tenant_id,
                        message_id=event.message_id,
                        chat_id=event.chat_id,
                        initiator_person_id=event.sender_person_id,
                        template_id="generated_inline",
                        template_version=0,
                        goal="根据描述生成一次性流程草稿",
                        inputs={},
                        roles=("collaborator",),
                        kind=DRAFT_WIZARD_KIND,
                    )
                )
            if not isinstance(definition, Mapping):
                raise IMCommandRejected("无模板定义必须是 JSON 对象")
            try:
                roles = set(inline_owner_roles(definition))
                if len(roles) > 1 and not parsed.owner_mentions:
                    raise IMCommandRejected(
                        "无模板多角色草稿必须在同一条消息中使用 role=@成员；"
                        "如由一人负责全部节点，请统一使用一个 owner_role"
                    )
                owner_bindings = {
                    role: event.sender_person_id for role in roles
                }
                mentions_by_key = {
                    mention.key: mention.person_id for mention in event.mentions
                }
                for role, mention_key in parsed.owner_mentions.items():
                    person_id = mentions_by_key.get(mention_key)
                    if person_id is None:
                        raise IMCommandRejected(
                            "角色绑定必须引用本条消息中的真实 @成员"
                        )
                    owner_bindings[role] = person_id
                snapshot = instantiate_inline_definition(
                    definition,
                    owner_bindings=owner_bindings,
                )
            except TemplateValidationError as exc:
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
                "inline_draft_created",
                instance.id,
                "已创建无模板流程草稿。\n"
                f"实例：{instance.id}\n"
                f"目标：{instance.snapshot.goal}\n"
                f"节点数：{len(instance.snapshot.nodes)}\n"
                f"角色数：{len(roles)}，绑定给其他成员的角色："
                f"{sum(person_id != event.sender_person_id for person_id in owner_bindings.values())}\n"
                "请核对后回复：\n"
                f"/larkflow confirm {instance.id}",
            )
        if command == "start":
            template_id = argument or ""
            try:
                version = self.templates.get_version(self.tenant_id, template_id)
                roles = {
                    str(node["owner_role"])
                    for node in version.definition.get("nodes", ())
                }
                owner_bindings = {
                    role: event.sender_person_id for role in roles
                }
                mentions_by_key = {
                    mention.key: mention.person_id for mention in event.mentions
                }
                for role, mention_key in parsed.owner_mentions.items():
                    person_id = mentions_by_key.get(mention_key)
                    if person_id is None:
                        raise IMCommandRejected(
                            "角色绑定必须引用本条消息中的真实 @成员"
                        )
                    owner_bindings[role] = person_id
                snapshot = self.templates.instantiate(
                    self.tenant_id,
                    template_id,
                    inputs=inputs,
                    owner_bindings=owner_bindings,
                )
                if len(roles) > 1 and not parsed.owner_mentions:
                    raise _RoleBindingRequired(
                        RoleBindingRequest(
                            command_id=event.id,
                            tenant_id=self.tenant_id,
                            message_id=event.message_id,
                            chat_id=event.chat_id,
                            initiator_person_id=event.sender_person_id,
                            template_id=template_id,
                            template_version=version.version,
                            goal=snapshot.goal,
                            inputs=inputs,
                            roles=tuple(sorted(roles)),
                        )
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
                f"角色数：{len(roles)}，绑定给其他成员的角色："
                f"{sum(person_id != event.sender_person_id for person_id in owner_bindings.values())}\n"
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
        if command == "edit":
            instance_id = argument or ""
            operations = inputs.get("operations")
            if not isinstance(operations, list):
                raise IMCommandRejected("编辑操作必须是 JSON 数组")
            try:
                preview = self.service.preview_graph_edit(
                    self.tenant_id,
                    instance_id,
                    operations,
                    actor_person_id=event.sender_person_id,
                )
                instance = self.service.get_for_owner(
                    self.tenant_id,
                    instance_id,
                    actor_person_id=event.sender_person_id,
                )
                if instance.version != preview.expected_instance_version:
                    raise StaleGraphEditPreviewError(
                        "instance changed while rendering graph edit preview"
                    )
            except (
                AuthorizationError,
                GraphEditNotAllowedError,
                InstanceNotFoundError,
            ):
                raise IMCommandRejected(
                    "实例不存在、无权操作，编辑触及已开始区域，或变更后的图无效"
                ) from None
            except StaleGraphEditPreviewError:
                raise IMCommandRejected("流程状态已变化，请重新预览") from None
            return (
                "graph_edit_previewed",
                instance.id,
                _graph_edit_preview_reply(preview, instance),
            )
        if command == "edit-confirm":
            preview_id = argument or ""
            try:
                confirmation = self.service.confirm_graph_edit(
                    self.tenant_id,
                    preview_id,
                    actor_person_id=event.sender_person_id,
                )
            except GraphEditPreviewExpiredError:
                raise IMCommandRejected("编辑预览已过期，请重新预览") from None
            except StaleGraphEditPreviewError:
                raise IMCommandRejected("流程状态已变化，请重新预览") from None
            except (
                AuthorizationError,
                GraphEditNotAllowedError,
                GraphEditPreviewNotFoundError,
                InstanceNotFoundError,
            ):
                raise IMCommandRejected(
                    "编辑预览不存在、已失效或你无权确认"
                ) from None
            return (
                "graph_edit_confirmed",
                confirmation.instance.id,
                _graph_edit_confirmation_reply(confirmation),
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
        claim_now = self.clock()
        claims = self.store.claim_im_replies(
            self.tenant_id,
            worker_id=self.worker_id,
            now=claim_now,
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
        )
        sent = failed = 0
        errors = []
        for claim in claims:
            try:
                if claim.card_update_token is not None:
                    from .projection import (
                        human_decision_result_card,
                        recovery_result_card,
                    )

                    result_card = (
                        human_decision_result_card(claim.text)
                        if isinstance(claim.command_text, str)
                        and claim.command_text.startswith(
                            f"{COMMAND_PREFIX} decide "
                        )
                        else recovery_result_card(claim.text)
                    )

                    self.sender.update_chat_card(
                        token=claim.card_update_token,
                        card=result_card,
                    )
                external_id = self.sender.send_chat_message(
                    chat_id=claim.chat_id,
                    text=claim.text,
                    idempotency_key=claim.idempotency_key,
                )
                if not external_id.strip():
                    raise ValueError("Feishu IM send returned no message_id")
                completed_at = self.clock()
                self.store.mark_im_reply_sent(
                    self.tenant_id,
                    claim.event_id,
                    claim_token=claim.claim_token,
                    external_id=external_id,
                    now=completed_at,
                )
                sent += 1
            except Exception as exc:
                failed_at = self.clock()
                failed += 1
                error = f"{claim.event_id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.store.mark_im_reply_failed(
                    self.tenant_id,
                    claim.event_id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=failed_at + self._retry_delay(claim.attempt_count),
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
    parsed = _parse_im_command(text)
    return parsed.command, parsed.argument, dict(parsed.inputs)


def _parse_im_command(text: str) -> _ParsedIMCommand:
    parts = text.strip().split(maxsplit=3)
    if not parts or parts[0] != COMMAND_PREFIX:
        raise IMCommandRejected("命令必须以 /larkflow 开头")
    if len(parts) == 1 or parts[1] == "help":
        if len(parts) > 2:
            raise IMCommandRejected("help 命令不接受其他参数")
        return _ParsedIMCommand("help", None)
    if parts[1] == "list":
        if len(parts) != 2:
            raise IMCommandRejected("list 命令不接受其他参数")
        return _ParsedIMCommand("list", None)
    if parts[1] == "recover":
        if len(parts) < 3:
            raise IMCommandRejected("恢复命令缺少服务端卡片参数")
        try:
            payload = json.loads(" ".join(parts[2:]))
        except json.JSONDecodeError as exc:
            raise IMCommandRejected("恢复命令参数无效") from exc
        if not isinstance(payload, Mapping):
            raise IMCommandRejected("恢复命令参数必须是对象")
        normalized = _validated_recovery_payload(payload)
        return _ParsedIMCommand(
            "recover",
            str(normalized.pop("instance_id")),
            inputs=normalized,
        )
    if parts[1] == "decide":
        if len(parts) < 3:
            raise IMCommandRejected("复核决定缺少服务端卡片参数")
        try:
            payload = json.loads(" ".join(parts[2:]))
        except json.JSONDecodeError as exc:
            raise IMCommandRejected("复核决定参数无效") from exc
        if not isinstance(payload, Mapping):
            raise IMCommandRejected("复核决定参数必须是对象")
        normalized = _validated_human_decision_payload(payload)
        return _ParsedIMCommand(
            "decide",
            str(normalized.pop("instance_id")),
            inputs=normalized,
        )
    if parts[1] == "draft":
        prefix = f"{COMMAND_PREFIX} draft"
        tail = text.strip()[len(prefix) :].strip()
        if not tail:
            return _ParsedIMCommand(
                "draft",
                None,
                inputs={"wizard": True},
            )
        definition, owner_mentions = _parse_json_object_and_bindings(
            tail,
            field="无模板定义",
        )
        return _ParsedIMCommand(
            "draft",
            None,
            inputs={"definition": definition},
            owner_mentions=owner_mentions,
        )
    if parts[1] == "edit":
        if len(parts) != 4:
            raise IMCommandRejected(
                "用法：/larkflow edit <instance_id> <JSON操作数组>"
            )
        try:
            parsed = json.loads(parts[3])
        except json.JSONDecodeError as exc:
            raise IMCommandRejected("编辑操作必须是 JSON 数组") from exc
        if not isinstance(parsed, list) or not all(
            isinstance(item, Mapping) for item in parsed
        ):
            raise IMCommandRejected("编辑操作必须是 JSON 对象数组")
        return _ParsedIMCommand(
            "edit",
            parts[2],
            inputs={"operations": parsed},
        )
    if parts[1] == "edit-confirm":
        if len(parts) != 3:
            raise IMCommandRejected(
                "用法：/larkflow edit-confirm <preview_id>"
            )
        return _ParsedIMCommand("edit-confirm", parts[2])
    if parts[1] == "restart":
        if len(parts) != 4:
            raise IMCommandRejected(
                "用法：/larkflow restart <instance_id> <node_key>"
            )
        return _ParsedIMCommand(
            "restart",
            parts[2],
            inputs={"node_key": parts[3]},
        )
    if parts[1] == "restart-all":
        if len(parts) != 3:
            raise IMCommandRejected(
                "用法：/larkflow restart-all <instance_id>"
            )
        return _ParsedIMCommand("restart-all", parts[2])
    if parts[1] == "restart-confirm":
        if len(parts) != 3:
            raise IMCommandRejected(
                "用法：/larkflow restart-confirm <preview_id>"
            )
        return _ParsedIMCommand("restart-confirm", parts[2])
    if parts[1] in {"confirm", "status"}:
        if len(parts) != 3:
            raise IMCommandRejected(
                f"用法：/larkflow {parts[1]} <instance_id>"
            )
        return _ParsedIMCommand(parts[1], parts[2])
    if parts[1] != "start" or len(parts) < 3:
        raise IMCommandRejected(
            "仅支持 draft、start、confirm、status、list、edit、edit-confirm、restart、"
            "restart-all、restart-confirm 和 help"
        )
    inputs, owner_mentions = _parse_start_tail(parts[3] if len(parts) == 4 else "")
    return _ParsedIMCommand(
        "start",
        parts[2],
        inputs=inputs,
        owner_mentions=owner_mentions,
    )


def _parse_start_tail(tail: str) -> tuple[dict[str, Any], dict[str, str]]:
    return _parse_json_object_and_bindings(tail, field="模板输入")


def _parse_json_object_and_bindings(
    tail: str,
    *,
    field: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    remaining = tail.strip()
    inputs: dict[str, Any] = {}
    if remaining.startswith("{"):
        try:
            parsed, end = STRICT_JSON_DECODER.raw_decode(remaining)
        except (json.JSONDecodeError, ValueError) as exc:
            raise IMCommandRejected(f"{field}必须是 JSON 对象") from exc
        if not isinstance(parsed, Mapping):
            raise IMCommandRejected(f"{field}必须是 JSON 对象")
        inputs = {str(key): value for key, value in parsed.items()}
        remaining = remaining[end:].strip()
    elif remaining.startswith("["):
        raise IMCommandRejected(f"{field}必须是 JSON 对象")
    elif field == "无模板定义":
        raise IMCommandRejected(f"{field}必须是 JSON 对象")

    owner_mentions: dict[str, str] = {}
    for token in remaining.split():
        role, separator, mention_key = token.partition("=")
        if (
            separator != "="
            or not ROLE_BINDING_RE.fullmatch(role)
            or not MENTION_KEY_RE.fullmatch(mention_key)
        ):
            raise IMCommandRejected(
                "角色绑定必须使用 lower_snake_case=@成员"
            )
        if role in owner_mentions:
            raise IMCommandRejected(f"角色重复绑定：{role}")
        owner_mentions[role] = mention_key
    if len(owner_mentions) > MAX_OWNER_BINDINGS:
        raise IMCommandRejected("角色绑定数量超过上限")
    return inputs, owner_mentions


def _validated_recovery_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "kind",
        "action",
        "instance_id",
        "node_key",
        "attempt_no",
        "node_version",
        "instance_version",
    }
    if set(value) != expected_keys or value.get("kind") != RECOVERY_ACTION_NAME:
        raise IMCommandRejected("恢复卡片参数不完整")
    try:
        action = RecoveryAction(str(value["action"]))
    except ValueError as exc:
        raise IMCommandRejected("恢复动作无效") from exc
    normalized: dict[str, Any] = {
        "kind": RECOVERY_ACTION_NAME,
        "action": action.value,
        "instance_id": _required_text(value.get("instance_id"), "instance_id"),
        "node_key": _required_text(value.get("node_key"), "node_key"),
    }
    for field_name in ("attempt_no", "node_version", "instance_version"):
        raw = value.get(field_name)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise IMCommandRejected(f"恢复卡片 {field_name} 无效")
        if field_name == "attempt_no" and raw < 1:
            raise IMCommandRejected("恢复卡片 attempt_no 无效")
        normalized[field_name] = raw
    return normalized


def _validated_human_decision_card_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    expected_keys = {
        "kind",
        "decision",
        "instance_id",
        "node_key",
        "attempt_no",
        "node_version",
        "instance_version",
    }
    if (
        set(value) != expected_keys
        or value.get("kind") != HUMAN_DECISION_ACTION_NAME
    ):
        raise IMCommandRejected("复核卡片参数不完整")
    try:
        decision = HumanDecision(str(value["decision"]))
    except ValueError as exc:
        raise IMCommandRejected("复核决定无效") from exc
    normalized: dict[str, Any] = {
        "kind": HUMAN_DECISION_ACTION_NAME,
        "decision": decision.value,
        "instance_id": _required_text(value.get("instance_id"), "instance_id"),
        "node_key": _required_text(value.get("node_key"), "node_key"),
    }
    for field_name in ("attempt_no", "node_version", "instance_version"):
        raw = value.get(field_name)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise IMCommandRejected(f"复核卡片 {field_name} 无效")
        if field_name == "attempt_no" and raw < 1:
            raise IMCommandRejected("复核卡片 attempt_no 无效")
        normalized[field_name] = raw
    return normalized


def _validated_human_decision_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "kind",
        "decision",
        "instance_id",
        "node_key",
        "attempt_no",
        "node_version",
        "instance_version",
        "feedback",
    }:
        raise IMCommandRejected("复核卡片参数不完整")
    normalized = _validated_human_decision_card_payload(
        {key: item for key, item in value.items() if key != "feedback"}
    )
    try:
        normalized["feedback"] = normalize_human_decision_feedback(
            HumanDecision(normalized["decision"]),
            value.get("feedback"),
        )
    except HumanDecisionFeedbackError as exc:
        raise IMCommandRejected(str(exc)) from None
    return normalized


def _human_decision_form_feedback(
    decision: HumanDecision,
    raw_form: Any,
) -> str | None:
    if decision == HumanDecision.ACCEPT:
        return None
    if isinstance(raw_form, str):
        try:
            raw_form = json.loads(raw_form)
        except json.JSONDecodeError as exc:
            raise IMCommandRejected("退回意见表单无效") from exc
    if not isinstance(raw_form, Mapping):
        raise IMCommandRejected("退回时必须填写具体意见")
    if set(raw_form) != {HUMAN_DECISION_FEEDBACK_FIELD}:
        raise IMCommandRejected("退回意见表单字段无效")
    try:
        return normalize_human_decision_feedback(
            decision,
            raw_form.get(HUMAN_DECISION_FEEDBACK_FIELD),
        )
    except HumanDecisionFeedbackError as exc:
        raise IMCommandRejected(str(exc)) from None


def _display_human_decision_feedback(value: str) -> str:
    """Keep Human text readable without enabling Card markdown controls."""

    return value.replace("<", "＜").replace(">", "＞").replace("`", "'")


def _normalize_mentions(raw_mentions: Any) -> tuple[IMMention, ...]:
    if raw_mentions is None:
        return ()
    if not isinstance(raw_mentions, Sequence) or isinstance(
        raw_mentions, (str, bytes, bytearray)
    ):
        raise ValueError("IM mentions must be an array")
    by_key: dict[str, IMMention] = {}
    for index, item in enumerate(raw_mentions):
        if not isinstance(item, Mapping):
            raise ValueError(f"IM mention {index} must be an object")
        key = _required_text(item.get("key"), f"mentions[{index}].key")
        if not MENTION_KEY_RE.fullmatch(key):
            raise ValueError(f"IM mention {index} has an invalid key")
        raw_id = item.get("id")
        if isinstance(raw_id, Mapping):
            person_id = raw_id.get("open_id")
        else:
            id_type = item.get("id_type")
            if id_type not in (None, "open_id"):
                raise ValueError(f"IM mention {index} is not an open_id")
            person_id = raw_id or item.get("open_id")
        mention = IMMention(
            key=key,
            person_id=_required_text(
                person_id,
                f"mentions[{index}].open_id",
            ),
        )
        existing = by_key.get(key)
        if existing is not None and existing != mention:
            raise ValueError(f"IM mention key is ambiguous: {key}")
        by_key[key] = mention
    return tuple(by_key.values())


def _restore_flattened_mention_keys(text: Any, raw_mentions: Any) -> Any:
    """Undo lark-cli display-name rendering using its authenticated mentions.

    The flattened ``im.message.receive_v1`` contract renders each real mention
    as ``@<display name>`` while retaining the original placeholder key in the
    sibling ``mentions`` array.  Domain parsing must use those keys, never the
    display names.  Ambiguous or incomplete rendering is left untouched so the
    command boundary fails closed.
    """
    if not isinstance(text, str) or not raw_mentions:
        return text
    if not isinstance(raw_mentions, Sequence) or isinstance(
        raw_mentions, (str, bytes, bytearray)
    ):
        return text

    replacements: list[tuple[str, str]] = []
    rendered_counts: dict[str, int] = {}
    for item in raw_mentions:
        if not isinstance(item, Mapping):
            return text
        key = item.get("key")
        name = item.get("name")
        if not isinstance(key, str) or not MENTION_KEY_RE.fullmatch(key):
            return text
        if key in text:
            continue
        if not isinstance(name, str) or not name.strip():
            return text
        rendered = f"@{name}"
        replacements.append((rendered, key))
        rendered_counts[rendered] = rendered_counts.get(rendered, 0) + 1

    if any(
        text.count(rendered) != count
        for rendered, count in rendered_counts.items()
    ):
        return text

    restored = text
    search_from = 0
    for rendered, key in replacements:
        index = restored.find(rendered, search_from)
        if index < 0:
            return text
        restored = restored[:index] + key + restored[index + len(rendered) :]
        search_from = index + len(key)
    return restored


def _extract_command_text(
    text: Any,
    mentions: tuple[IMMention, ...],
) -> str | None:
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.startswith(COMMAND_PREFIX):
        return stripped
    command_index = stripped.find(COMMAND_PREFIX)
    if command_index < 1:
        return None
    leading = stripped[:command_index].strip().split()
    mention_keys = {mention.key for mention in mentions}
    if not leading or any(token not in mention_keys for token in leading):
        return None
    return stripped[command_index:]


def _owner_person_ids_from_mentions(event: IMCommandSignal) -> tuple[str, ...]:
    try:
        parsed = _parse_im_command(event.text)
    except IMCommandRejected:
        return ()
    if parsed.command not in {"draft", "start"} or not parsed.owner_mentions:
        return ()
    mentions_by_key = {
        mention.key: mention.person_id for mention in event.mentions
    }
    person_ids = []
    for mention_key in parsed.owner_mentions.values():
        person_id = mentions_by_key.get(mention_key)
        if person_id is None:
            raise IMCommandRejected(
                "角色绑定必须引用本条消息中的真实 @成员"
            )
        person_ids.append(person_id)
    return tuple(person_ids)


def _mention_to_dict(mention: IMMention) -> dict[str, str]:
    return {"key": mention.key, "person_id": mention.person_id}


def _mention_from_dict(raw: Mapping[str, Any]) -> IMMention:
    key = _required_text(raw.get("key"), "mention.key")
    person_id = _required_text(raw.get("person_id"), "mention.person_id")
    if not MENTION_KEY_RE.fullmatch(key):
        raise ValueError("persisted mention has an invalid key")
    return IMMention(key=key, person_id=person_id)


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
            "使用 /larkflow start <template_id> [JSON输入] "
            "[role=@成员 ...] 创建流程。"
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


def _graph_edit_preview_reply(
    preview: GraphEditPreview,
    instance: WorkflowInstance,
) -> str:
    lines = [
        "运行中编辑预览",
        f"实例：{_status_field(instance.id)}",
        f"图版本：{preview.graph_revision} -> {preview.proposed_graph_revision}",
        f"操作数：{len(preview.operations)}",
    ]
    for label, keys in (
        ("新增", preview.added_node_keys),
        ("修改", preview.updated_node_keys),
        ("删除", preview.removed_node_keys),
    ):
        if keys:
            lines.append(f"{label}：{', '.join(keys)}")
    lines.extend(
        (
            "确认后只修改尚未开始的节点与边；Template 和已执行历史不变。",
            "若流程状态或图版本发生变化，该预览会自动失效。",
            "请确认：",
            f"/larkflow edit-confirm {preview.id}",
        )
    )
    return "\n".join(lines)


def _graph_edit_confirmation_reply(
    confirmation: GraphEditConfirmation,
) -> str:
    preview = confirmation.preview
    instance = confirmation.instance
    prefix = (
        "该编辑已执行，无需重复操作。"
        if confirmation.already_applied
        else "运行中编辑已应用。"
    )
    lines = [
        prefix,
        f"实例：{_status_field(instance.id)}",
        f"图版本：{instance.graph_revision}",
        f"状态：{_instance_status_label(instance.status.value)}",
    ]
    for label, keys in (
        ("新增", preview.added_node_keys),
        ("修改", preview.updated_node_keys),
        ("删除", preview.removed_node_keys),
    ):
        if keys:
            lines.append(f"{label}：{', '.join(keys)}")
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
    "IMMention",
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
    "HumanDecisionActionInboxBridge",
    "RecoveryActionInboxBridge",
    "parse_im_command",
]
