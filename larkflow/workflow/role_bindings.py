"""Durable person-selection cards for multi-owner workflow drafts."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from typing import Any, Protocol

from .card_feedback import (
    CARD_FEEDBACK_FALLBACK,
    processing_card,
    rejected_card,
    report_card_feedback,
)
from .directory import CandidateDirectory, DirectoryValidationError
from .draft_generation import (
    DraftDefinitionGenerator,
    DraftGenerationRejected,
    MAX_WIZARD_TEXT_CHARS,
    draft_wizard_form,
)
from .event_time import feishu_event_time
from .model import InstanceStatus, TemplateStatus
from .repository import (
    InstanceAlreadyExistsError,
    InstanceNotFoundError,
    TemplateNotFoundError,
)
from .service import WorkflowService
from .template_service import (
    InvalidTemplateTransitionError,
    TemplateService,
    TemplateValidationError,
    inline_owner_roles,
    instantiate_inline_definition,
    instantiate_template_version,
)


CARD_ACTION_EVENT = "card.action.trigger"
ROLE_FIELD_PREFIX = "role__"
ROLE_FORM_NAME = "role_binding_form"
ROLE_SUBMIT_NAME = "role_binding_submit"
DRAFT_WIZARD_KIND = "draft_wizard"
DRAFT_WIZARD_SUBMIT_NAME = "draft_wizard_submit"
MAX_CARD_CANDIDATES = 100


class InvalidRoleBindingClaimError(RuntimeError):
    """A worker tried to settle a role-binding claim it no longer owns."""


class RoleBindingRejected(ValueError):
    """A person-selection request or callback failed a durable invariant."""


@dataclass(frozen=True)
class RoleBindingRequest:
    command_id: str
    tenant_id: str
    message_id: str
    chat_id: str
    initiator_person_id: str
    template_id: str
    template_version: int
    goal: str
    inputs: Mapping[str, Any]
    roles: tuple[str, ...]
    kind: str = "template"
    candidate_person_ids: tuple[str, ...] = field(default_factory=tuple)
    card_message_id: str | None = None


@dataclass(frozen=True)
class RoleBindingCardClaim:
    request: RoleBindingRequest
    claim_token: str
    attempt_count: int


@dataclass(frozen=True)
class RoleBindingActionSignal:
    id: str
    tenant_id: str
    message_id: str
    chat_id: str
    operator_person_id: str
    action_tag: str
    action_name: str
    form_value: str
    update_token: str
    occurred_at: datetime
    received_at: datetime
    available_at: datetime | None = None


@dataclass(frozen=True)
class RoleBindingActionClaim:
    action: RoleBindingActionSignal
    request: RoleBindingRequest | None
    claim_token: str
    attempt_count: int
    owner_bindings: Mapping[str, str] = field(default_factory=dict)
    instance_id: str | None = None
    reply_text: str | None = None


@dataclass(frozen=True)
class RoleBindingReplyClaim:
    action: RoleBindingActionSignal
    request: RoleBindingRequest | None
    owner_bindings: Mapping[str, str]
    instance_id: str | None
    text: str
    claim_token: str
    attempt_count: int


@dataclass(frozen=True)
class RoleBindingProgressClaim:
    action: RoleBindingActionSignal
    stage: str
    revision: int
    claim_token: str
    attempt_count: int


class RoleBindingStore(Protocol):
    def claim_role_binding_cards(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[RoleBindingCardClaim, ...]:
        ...

    def mark_role_binding_card_sent(
        self,
        tenant_id: str,
        command_id: str,
        *,
        claim_token: str,
        candidate_person_ids: tuple[str, ...],
        external_id: str,
        now: datetime,
    ) -> None:
        ...

    def mark_role_binding_card_failed(
        self,
        tenant_id: str,
        command_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...

    def append_role_binding_action(self, event: RoleBindingActionSignal) -> bool:
        ...

    def release_role_binding_action(
        self,
        tenant_id: str,
        event_id: str,
        *,
        available_at: datetime,
        feedback_status: str,
        feedback_elapsed_ms: int,
    ) -> None:
        ...

    def claim_role_binding_verification(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[RoleBindingActionClaim, ...]:
        ...

    def mark_role_binding_verified(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        owner_bindings: Mapping[str, str],
        progress_stage: str | None,
        now: datetime,
    ) -> None:
        ...

    def mark_role_binding_rejected(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        outcome: str,
        reply_text: str | None,
        now: datetime,
    ) -> None:
        ...

    def mark_role_binding_verification_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...

    def claim_role_binding_actions(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[RoleBindingActionClaim, ...]:
        ...

    def claim_draft_generation_actions(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[RoleBindingActionClaim, ...]:
        ...

    def queue_role_binding_progress(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        stage: str,
        now: datetime,
    ) -> None:
        ...

    def mark_role_binding_processed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        instance_id: str,
        reply_text: str,
        now: datetime,
    ) -> None:
        ...

    def mark_role_binding_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...

    def claim_role_binding_progress(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[RoleBindingProgressClaim, ...]:
        ...

    def mark_role_binding_progress_sent(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        now: datetime,
    ) -> None:
        ...

    def mark_role_binding_progress_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...

    def claim_role_binding_replies(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> tuple[RoleBindingReplyClaim, ...]:
        ...

    def mark_role_binding_reply_sent(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        external_id: str,
        now: datetime,
    ) -> None:
        ...

    def mark_role_binding_reply_failed(
        self,
        tenant_id: str,
        event_id: str,
        *,
        claim_token: str,
        error: str,
        retry_at: datetime,
    ) -> None:
        ...


class RoleBindingCardSender(Protocol):
    def send_chat_card(
        self,
        *,
        chat_id: str,
        card: Mapping[str, Any],
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

    def update_chat_card_message(
        self,
        *,
        message_id: str,
        card: Mapping[str, Any],
    ) -> None:
        ...

    def send_chat_message(
        self,
        *,
        chat_id: str,
        text: str,
        idempotency_key: str,
    ) -> str:
        ...


class RoleBindingActionInboxBridge:
    """Persist only callbacks emitted by the role-binding form."""

    def __init__(
        self,
        store: RoleBindingStore,
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
        if event_type != CARD_ACTION_EVENT:
            return False
        action_name = _optional_text(payload.get("action_name"))
        if action_name not in {ROLE_SUBMIT_NAME, DRAFT_WIZARD_SUBMIT_NAME}:
            return False
        feedback_started = self.monotonic()
        now = self.clock()
        signal = RoleBindingActionSignal(
            id=_required_text(payload.get("event_id"), "event_id"),
            tenant_id=self.tenant_id,
            message_id=_required_text(payload.get("message_id"), "message_id"),
            chat_id=_required_text(payload.get("chat_id"), "chat_id"),
            operator_person_id=_required_text(
                payload.get("operator_id"),
                "operator_id",
            ),
            action_tag=_required_text(payload.get("action_tag"), "action_tag"),
            action_name=action_name,
            form_value=_required_text(payload.get("form_value"), "form_value"),
            update_token=_required_text(payload.get("token"), "token"),
            occurred_at=feishu_event_time(payload.get("timestamp"), fallback=now),
            received_at=now,
            available_at=(
                now + CARD_FEEDBACK_FALLBACK
                if self.card_updater is not None
                else None
            ),
        )
        appended = self.store.append_role_binding_action(signal)
        if appended and self.card_updater is not None:
            feedback_status = "updated"
            try:
                self.card_updater(
                    token=signal.update_token,
                    card=processing_card(
                        title=(
                            "草稿需求已提交"
                            if action_name == DRAFT_WIZARD_SUBMIT_NAME
                            else "人员分工已提交"
                        ),
                        content=(
                            "正在核验身份和输入，随后由中央 Agent 生成候选流程。"
                            if action_name == DRAFT_WIZARD_SUBMIT_NAME
                            else "正在核验操作人、候选成员与模板状态。"
                        ),
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
                self.store.release_role_binding_action(
                    self.tenant_id,
                    signal.id,
                    available_at=completed_at,
                    feedback_status=feedback_status,
                    feedback_elapsed_ms=elapsed_ms,
                )
                report_card_feedback(
                    self.feedback_reporter,
                    card_kind=(
                        "draft_wizard"
                        if action_name == DRAFT_WIZARD_SUBMIT_NAME
                        else "role_binding"
                    ),
                    status=feedback_status,
                    elapsed_ms=elapsed_ms,
                )
        return appended


@dataclass(frozen=True)
class RoleBindingCardReport:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class RoleBindingCardWorker:
    """Resolve a bounded directory snapshot and project selection cards."""

    def __init__(
        self,
        store: RoleBindingStore,
        directory: CandidateDirectory,
        sender: RoleBindingCardSender,
        *,
        tenant_id: str,
        worker_id: str,
        candidate_limit: int = MAX_CARD_CANDIDATES,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
    ) -> None:
        _validate_worker(tenant_id, worker_id, claim_limit, claim_ttl)
        if candidate_limit < 1 or candidate_limit > MAX_CARD_CANDIDATES:
            raise ValueError("role-binding candidate limit must be between 1 and 100")
        self.store = store
        self.directory = directory
        self.sender = sender
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.candidate_limit = candidate_limit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max

    def run_once(self) -> RoleBindingCardReport:
        claim_now = self.clock()
        claims = self.store.claim_role_binding_cards(
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
                candidates = self.directory.list_candidate_people(
                    self.tenant_id,
                    limit=self.candidate_limit,
                )
                candidate_ids = {person.person_id for person in candidates if person.active}
                initiator = self.directory.get_person(
                    self.tenant_id,
                    claim.request.initiator_person_id,
                )
                if not initiator.active:
                    raise DirectoryValidationError("workflow initiator is inactive")
                candidate_ids.add(initiator.person_id)
                if len(candidate_ids) > self.candidate_limit:
                    raise DirectoryValidationError(
                        "directory candidate snapshot exceeds the card limit"
                    )
                frozen_candidates = tuple(sorted(candidate_ids))
                external_id = self.sender.send_chat_card(
                    chat_id=claim.request.chat_id,
                    card=role_binding_card(claim.request, frozen_candidates),
                    idempotency_key=_card_key(claim.request.command_id),
                )
                if not external_id.strip():
                    raise ValueError("Feishu card send returned no message_id")
                completed_at = self.clock()
                self.store.mark_role_binding_card_sent(
                    self.tenant_id,
                    claim.request.command_id,
                    claim_token=claim.claim_token,
                    candidate_person_ids=frozen_candidates,
                    external_id=external_id,
                    now=completed_at,
                )
                sent += 1
            except Exception as exc:
                failed_at = self.clock()
                failed += 1
                error = (
                    f"{claim.request.command_id}: {type(exc).__name__}: {exc}"
                )
                errors.append(error)
                self.store.mark_role_binding_card_failed(
                    self.tenant_id,
                    claim.request.command_id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=failed_at + _retry_delay(
                        claim.attempt_count,
                        self.retry_base,
                        self.retry_max,
                    ),
                )
        return RoleBindingCardReport(
            claimed=len(claims),
            sent=sent,
            failed=failed,
            errors=tuple(errors),
        )


@dataclass(frozen=True)
class RoleBindingVerificationReport:
    claimed: int = 0
    verified: int = 0
    rejected: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class RoleBindingVerificationWorker:
    """Authenticate the callback and revalidate every selected person."""

    def __init__(
        self,
        store: RoleBindingStore,
        directory: CandidateDirectory,
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
        self.store = store
        self.directory = directory
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max

    def run_once(self) -> RoleBindingVerificationReport:
        claim_now = self.clock()
        claims = self.store.claim_role_binding_verification(
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
                bindings = self._verify(claim)
            except RoleBindingRejected as exc:
                completed_at = self.clock()
                rejected += 1
                wizard = (
                    claim.request is not None
                    and claim.request.kind == DRAFT_WIZARD_KIND
                )
                self.store.mark_role_binding_rejected(
                    self.tenant_id,
                    claim.action.id,
                    claim_token=claim.claim_token,
                    outcome="rejected:role_binding",
                    reply_text=(
                        "流程草稿未生成。请重新发送 /larkflow draft 后再试。"
                        if wizard
                        else "人员分工未执行。请重新发送流程启动命令后再试。"
                    ),
                    now=completed_at,
                )
                continue
            except Exception as exc:
                failed_at = self.clock()
                failed += 1
                error = f"{claim.action.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.store.mark_role_binding_verification_failed(
                    self.tenant_id,
                    claim.action.id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=failed_at + _retry_delay(
                        claim.attempt_count,
                        self.retry_base,
                        self.retry_max,
                    ),
                )
                continue
            completed_at = self.clock()
            self.store.mark_role_binding_verified(
                self.tenant_id,
                claim.action.id,
                claim_token=claim.claim_token,
                owner_bindings=bindings,
                progress_stage=(
                    "generating"
                    if claim.request is not None
                    and claim.request.kind == DRAFT_WIZARD_KIND
                    else None
                ),
                now=completed_at,
            )
            verified += 1
        return RoleBindingVerificationReport(
            claimed=len(claims),
            verified=verified,
            rejected=rejected,
            failed=failed,
            errors=tuple(errors),
        )

    def _verify(self, claim: RoleBindingActionClaim) -> dict[str, str]:
        request = claim.request
        action = claim.action
        if request is None:
            raise RoleBindingRejected("callback does not reference a known card")
        expected_action = (
            DRAFT_WIZARD_SUBMIT_NAME
            if request.kind == DRAFT_WIZARD_KIND
            else ROLE_SUBMIT_NAME
        )
        if action.action_tag != "button" or action.action_name != expected_action:
            raise RoleBindingRejected("callback is not the expected form submit")
        if action.operator_person_id != request.initiator_person_id:
            raise RoleBindingRejected("only the workflow initiator may bind roles")
        if action.message_id != request.card_message_id:
            raise RoleBindingRejected("callback message does not match the request")
        if action.chat_id != request.chat_id:
            raise RoleBindingRejected("callback chat does not match the request")
        if request.kind == DRAFT_WIZARD_KIND:
            return self._verify_draft_wizard(request, action)
        try:
            raw_form = json.loads(action.form_value)
        except json.JSONDecodeError as exc:
            raise RoleBindingRejected("role-binding form is not valid JSON") from exc
        if not isinstance(raw_form, dict):
            raise RoleBindingRejected("role-binding form must be an object")
        expected_fields = {f"{ROLE_FIELD_PREFIX}{role}" for role in request.roles}
        if set(raw_form) != expected_fields:
            raise RoleBindingRejected("role-binding form fields do not match the template")
        candidates = set(request.candidate_person_ids)
        if not candidates:
            raise RoleBindingRejected("role-binding candidate snapshot is empty")
        bindings: dict[str, str] = {}
        for role in request.roles:
            person_id = raw_form.get(f"{ROLE_FIELD_PREFIX}{role}")
            if not isinstance(person_id, str) or person_id not in candidates:
                raise RoleBindingRejected("selected person is outside the frozen candidates")
            bindings[role] = person_id
        for person_id in sorted({action.operator_person_id, *bindings.values()}):
            person = self.directory.get_person(self.tenant_id, person_id)
            if person.person_id != person_id or not person.active:
                raise RoleBindingRejected("selected person is not an active tenant member")
        return bindings

    def _verify_draft_wizard(
        self,
        request: RoleBindingRequest,
        action: RoleBindingActionSignal,
    ) -> dict[str, str]:
        try:
            _brief, _context, collaborator = draft_wizard_form(action.form_value)
        except DraftGenerationRejected as exc:
            raise RoleBindingRejected(str(exc)) from exc
        candidates = set(request.candidate_person_ids)
        if collaborator not in candidates:
            raise RoleBindingRejected("selected person is outside the frozen candidates")
        for person_id in sorted({action.operator_person_id, collaborator}):
            person = self.directory.get_person(self.tenant_id, person_id)
            if person.person_id != person_id or not person.active:
                raise RoleBindingRejected("selected person is not an active tenant member")
        return {"collaborator": collaborator}


@dataclass(frozen=True)
class RoleBindingActionReport:
    claimed: int = 0
    processed: int = 0
    rejected: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class RoleBindingActionWorker:
    """Freeze verified role bindings into one idempotent workflow draft."""

    def __init__(
        self,
        store: RoleBindingStore,
        service: WorkflowService,
        templates: TemplateService,
        *,
        tenant_id: str,
        worker_id: str,
        draft_generator: DraftDefinitionGenerator | None = None,
        draft_only: bool = False,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 20,
        claim_ttl: timedelta = timedelta(minutes=2),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
    ) -> None:
        _validate_worker(tenant_id, worker_id, claim_limit, claim_ttl)
        self.store = store
        self.service = service
        self.templates = templates
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.draft_generator = draft_generator
        self.draft_only = draft_only
        if draft_only and draft_generator is None:
            raise ValueError("draft-only worker requires a draft generator")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max

    def run_once(self) -> RoleBindingActionReport:
        claim_now = self.clock()
        claim_method = (
            self.store.claim_draft_generation_actions
            if self.draft_only
            else self.store.claim_role_binding_actions
        )
        claims = claim_method(
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
                instance_id, reply = self._apply(claim)
            except RoleBindingRejected as exc:
                completed_at = self.clock()
                rejected += 1
                wizard = (
                    claim.request is not None
                    and claim.request.kind == DRAFT_WIZARD_KIND
                )
                self.store.mark_role_binding_rejected(
                    self.tenant_id,
                    claim.action.id,
                    claim_token=claim.claim_token,
                    outcome="rejected:role_binding_domain",
                    reply_text=(
                        f"流程草稿未生成：{exc}\n请重新发送 /larkflow draft 后再试。"
                        if wizard
                        else f"人员分工未应用：{exc}"
                    ),
                    now=completed_at,
                )
                continue
            except Exception as exc:
                failed_at = self.clock()
                failed += 1
                error = f"{claim.action.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.store.mark_role_binding_failed(
                    self.tenant_id,
                    claim.action.id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=failed_at + _retry_delay(
                        claim.attempt_count,
                        self.retry_base,
                        self.retry_max,
                    ),
                )
                continue
            completed_at = self.clock()
            self.store.mark_role_binding_processed(
                self.tenant_id,
                claim.action.id,
                claim_token=claim.claim_token,
                instance_id=instance_id,
                reply_text=reply,
                now=completed_at,
            )
            processed += 1
        return RoleBindingActionReport(
            claimed=len(claims),
            processed=processed,
            rejected=rejected,
            failed=failed,
            errors=tuple(errors),
        )

    def _apply(self, claim: RoleBindingActionClaim) -> tuple[str, str]:
        request = claim.request
        if request is None:
            raise RoleBindingRejected("role-binding request no longer exists")
        if request.kind == DRAFT_WIZARD_KIND:
            if not self.draft_only:
                raise RoleBindingRejected("draft wizard reached the regular action worker")
            return self._apply_draft_wizard(claim, request)
        if self.draft_only:
            raise RoleBindingRejected("non-wizard action reached the draft generator")
        if set(claim.owner_bindings) != set(request.roles):
            raise RoleBindingRejected("verified role bindings do not match the request")
        try:
            template = self.templates.get_template(
                self.tenant_id,
                request.template_id,
            )
            if template.status != TemplateStatus.ENABLED:
                raise RoleBindingRejected("template is no longer enabled")
            version = self.templates.get_version(
                self.tenant_id,
                request.template_id,
                request.template_version,
            )
            snapshot = instantiate_template_version(
                version,
                inputs=request.inputs,
                owner_bindings=claim.owner_bindings,
            )
        except (
            InvalidTemplateTransitionError,
            TemplateNotFoundError,
            TemplateValidationError,
        ) as exc:
            raise RoleBindingRejected(str(exc)) from exc
        instance_id = role_binding_instance_id(
            self.tenant_id,
            request.message_id,
        )
        try:
            instance = self.service.create_draft(
                instance_id=instance_id,
                tenant_id=self.tenant_id,
                owner_person_id=request.initiator_person_id,
                actor_person_id=request.initiator_person_id,
                snapshot=snapshot,
                correlation_id=request.message_id,
            )
        except InstanceAlreadyExistsError:
            try:
                instance = self.service.get(self.tenant_id, instance_id)
            except InstanceNotFoundError as exc:
                raise RoleBindingRejected("existing draft cannot be read") from exc
            if (
                instance.owner_person_id != request.initiator_person_id
                or instance.snapshot != snapshot
            ):
                raise RoleBindingRejected("message id is already bound to another draft")
        other_count = sum(
            person_id != request.initiator_person_id
            for person_id in claim.owner_bindings.values()
        )
        if instance.status == InstanceStatus.DRAFT:
            tail = "请核对后回复：\n" f"/larkflow confirm {instance.id}"
        else:
            tail = f"当前状态：{instance.status.value}"
        reply = (
            "人员分工已确认，流程草稿已创建。\n"
            f"模板：{request.template_id}\n"
            f"实例：{instance.id}\n"
            f"目标：{instance.snapshot.goal}\n"
            f"节点数：{len(instance.snapshot.nodes)}\n"
            f"角色数：{len(request.roles)}，绑定给其他成员的角色：{other_count}\n"
            f"{tail}"
        )
        return instance.id, reply

    def _apply_draft_wizard(
        self,
        claim: RoleBindingActionClaim,
        request: RoleBindingRequest,
    ) -> tuple[str, str]:
        if self.draft_generator is None:
            raise RoleBindingRejected("中央 Agent 尚未配置，暂时不能根据描述生成草稿")
        if set(claim.owner_bindings) != {"collaborator"}:
            raise RoleBindingRejected("verified draft participants do not match the request")
        try:
            brief, context, collaborator = draft_wizard_form(claim.action.form_value)
        except DraftGenerationRejected as exc:
            raise RoleBindingRejected(str(exc)) from exc
        if collaborator != claim.owner_bindings["collaborator"]:
            raise RoleBindingRejected("verified draft participant changed before processing")
        instance_id = role_binding_instance_id(
            self.tenant_id,
            request.message_id,
        )
        try:
            instance = self.service.get(self.tenant_id, instance_id)
        except InstanceNotFoundError:
            try:
                definition = self.draft_generator.generate(
                    brief=brief,
                    context=context,
                    on_repair=lambda: self.store.queue_role_binding_progress(
                        self.tenant_id,
                        claim.action.id,
                        claim_token=claim.claim_token,
                        stage="repairing",
                        now=self.clock(),
                    ),
                )
                roles = set(inline_owner_roles(definition))
                available_bindings = {
                    "requester": request.initiator_person_id,
                    "collaborator": collaborator,
                }
                snapshot = instantiate_inline_definition(
                    definition,
                    owner_bindings={
                        role: available_bindings[role] for role in roles
                    },
                )
            except (DraftGenerationRejected, TemplateValidationError) as exc:
                raise RoleBindingRejected(str(exc)) from exc
            try:
                instance = self.service.create_draft(
                    instance_id=instance_id,
                    tenant_id=self.tenant_id,
                    owner_person_id=request.initiator_person_id,
                    actor_person_id=request.initiator_person_id,
                    snapshot=snapshot,
                    correlation_id=request.message_id,
                )
            except InstanceAlreadyExistsError:
                instance = self.service.get(self.tenant_id, instance_id)
        if instance.owner_person_id != request.initiator_person_id:
            raise RoleBindingRejected("message id is already bound to another draft")
        if instance.status != InstanceStatus.DRAFT:
            raise RoleBindingRejected("该消息对应的实例已不再是草稿")
        labels = {
            request.initiator_person_id: "发起人",
            collaborator: (
                "发起人" if collaborator == request.initiator_person_id else "协作成员"
            ),
        }
        lines = [
            "中央 Agent 已生成流程草稿。",
            f"实例：{instance.id}",
            f"目标：{instance.snapshot.goal}",
            f"节点数：{len(instance.snapshot.nodes)}",
            "",
            "节点预览：",
        ]
        executor_labels = {"human": "Human", "agent": "Agent", "tool": "Tool"}
        for index, node in enumerate(instance.snapshot.nodes, start=1):
            deps = "、".join(node.deps) if node.deps else "无"
            lines.append(
                f"{index}. {node.title} ({node.key})｜"
                f"{executor_labels[node.executor.value]}｜"
                f"Owner：{labels.get(node.owner_person_id, '协作成员')}｜依赖：{deps}"
            )
        lines.extend(
            [
                "",
                "该草稿尚未运行。请核对后回复：",
                f"/larkflow confirm {instance.id}",
            ]
        )
        return instance.id, "\n".join(lines)


@dataclass(frozen=True)
class RoleBindingProgressReport:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class RoleBindingProgressWorker:
    """Project durable draft-generation stages onto the original card."""

    def __init__(
        self,
        store: RoleBindingStore,
        sender: RoleBindingCardSender,
        *,
        tenant_id: str,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        claim_limit: int = 1,
        claim_ttl: timedelta = timedelta(minutes=2),
        retry_base: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=5),
    ) -> None:
        _validate_worker(tenant_id, worker_id, claim_limit, claim_ttl)
        self.store = store
        self.sender = sender
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max

    def run_once(self) -> RoleBindingProgressReport:
        claims = self.store.claim_role_binding_progress(
            self.tenant_id,
            worker_id=self.worker_id,
            now=self.clock(),
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
        )
        sent = failed = 0
        errors: list[str] = []
        for claim in claims:
            try:
                self.sender.update_chat_card_message(
                    message_id=claim.action.message_id,
                    card=draft_wizard_progress_card(claim.stage),
                )
                self.store.mark_role_binding_progress_sent(
                    self.tenant_id,
                    claim.action.id,
                    claim_token=claim.claim_token,
                    now=self.clock(),
                )
                sent += 1
            except Exception as exc:
                failed += 1
                failed_at = self.clock()
                error = f"{claim.action.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.store.mark_role_binding_progress_failed(
                    self.tenant_id,
                    claim.action.id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=failed_at + _retry_delay(
                        claim.attempt_count,
                        self.retry_base,
                        self.retry_max,
                    ),
                )
        return RoleBindingProgressReport(
            claimed=len(claims),
            sent=sent,
            failed=failed,
            errors=tuple(errors),
        )


@dataclass(frozen=True)
class RoleBindingReplyReport:
    claimed: int = 0
    sent: int = 0
    card_updates_failed: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class RoleBindingReplyWorker:
    """Settle the card visually and deliver the durable draft reply."""

    def __init__(
        self,
        store: RoleBindingStore,
        sender: RoleBindingCardSender,
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
        self.store = store
        self.sender = sender
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_limit = claim_limit
        self.claim_ttl = claim_ttl
        self.retry_base = retry_base
        self.retry_max = retry_max

    def run_once(self) -> RoleBindingReplyReport:
        claim_now = self.clock()
        claims = self.store.claim_role_binding_replies(
            self.tenant_id,
            worker_id=self.worker_id,
            now=claim_now,
            limit=self.claim_limit,
            claim_ttl=self.claim_ttl,
        )
        sent = failed = card_updates_failed = 0
        errors = []
        for claim in claims:
            try:
                try:
                    if (
                        claim.request is not None
                        and claim.request.kind == DRAFT_WIZARD_KIND
                        and claim.instance_id is not None
                    ):
                        card = draft_wizard_result_card(
                            claim.text,
                            instance_id=claim.instance_id,
                        )
                    elif claim.request is not None and claim.instance_id is not None:
                        card = role_binding_card(
                            claim.request,
                            claim.request.candidate_person_ids,
                            owner_bindings=claim.owner_bindings,
                            settled_instance_id=claim.instance_id,
                        )
                    else:
                        retry_guidance = (
                            "请重新发送 /larkflow draft 后再试。"
                            if claim.request is not None
                            and claim.request.kind == DRAFT_WIZARD_KIND
                            else "原选择已失效，请重新发送流程启动命令。"
                        )
                        card = rejected_card(
                            title=(
                                "流程草稿未生成"
                                if claim.request is not None
                                and claim.request.kind == DRAFT_WIZARD_KIND
                                else "人员分工未执行"
                            ),
                            content=(
                                f"{claim.text}\n\n"
                                f"{retry_guidance}"
                            ),
                        )
                    self.sender.update_chat_card_message(
                        message_id=claim.action.message_id,
                        card=card,
                    )
                except Exception as exc:
                    card_updates_failed += 1
                    errors.append(
                        f"{claim.action.id}: card update "
                        f"{type(exc).__name__}: {exc}"
                    )
                external_id = self.sender.send_chat_message(
                    chat_id=(
                        claim.request.chat_id
                        if claim.request is not None
                        else claim.action.chat_id
                    ),
                    text=claim.text,
                    idempotency_key=_action_reply_key(claim.action.id),
                )
                if not external_id.strip():
                    raise ValueError("Feishu role-binding reply returned no message_id")
                completed_at = self.clock()
                self.store.mark_role_binding_reply_sent(
                    self.tenant_id,
                    claim.action.id,
                    claim_token=claim.claim_token,
                    external_id=external_id,
                    now=completed_at,
                )
                sent += 1
            except Exception as exc:
                failed_at = self.clock()
                failed += 1
                error = f"{claim.action.id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                self.store.mark_role_binding_reply_failed(
                    self.tenant_id,
                    claim.action.id,
                    claim_token=claim.claim_token,
                    error=error,
                    retry_at=failed_at + _retry_delay(
                        claim.attempt_count,
                        self.retry_base,
                        self.retry_max,
                    ),
                )
        return RoleBindingReplyReport(
            claimed=len(claims),
            sent=sent,
            card_updates_failed=card_updates_failed,
            failed=failed,
            errors=tuple(errors),
        )


def role_binding_card(
    request: RoleBindingRequest,
    candidate_person_ids: tuple[str, ...],
    *,
    owner_bindings: Mapping[str, str] | None = None,
    settled_instance_id: str | None = None,
) -> dict[str, Any]:
    if not candidate_person_ids:
        raise ValueError("role-binding card requires candidates")
    if request.kind == DRAFT_WIZARD_KIND:
        if owner_bindings is not None or settled_instance_id is not None:
            raise ValueError("draft wizard result requires draft_wizard_result_card")
        return draft_wizard_card(request, candidate_person_ids)
    bindings = dict(owner_bindings or {})
    settled = settled_instance_id is not None
    options = [{"value": person_id} for person_id in candidate_person_ids]
    form_elements: list[dict[str, Any]] = []
    for role in request.roles:
        form_elements.append(
            {
                "tag": "markdown",
                "content": f"**角色：{_escape_markdown(role)}**",
            }
        )
        initial = bindings.get(role, request.initiator_person_id)
        selector = {
            "tag": "select_person",
            "name": f"{ROLE_FIELD_PREFIX}{role}",
            "width": "fill",
            "placeholder": {"tag": "plain_text", "content": "请选择成员"},
            "options": options,
            "disabled": settled,
        }
        if not settled:
            selector["required"] = True
        if initial in candidate_person_ids:
            selector["initial_option"] = initial
        form_elements.append(selector)
    form_elements.append(
        {
            "tag": "button",
            "name": ROLE_SUBMIT_NAME,
            "text": {
                "tag": "plain_text",
                "content": "已确认" if settled else "确认人员分工",
            },
            "type": "primary_filled",
            "width": "fill",
            "action_type": "form_submit",
            "disabled": settled,
        }
    )
    intro = (
        f"<text_tag color='green'>已冻结</text_tag> 实例 `{settled_instance_id}`"
        if settled
        else "为每个角色选择负责人。提交后会冻结本次人员分工，再创建流程草稿。"
    )
    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "update_multi": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "人员分工已确认" if settled else "选择流程参与人",
            },
            "subtitle": {
                "tag": "plain_text",
                "content": request.template_id,
            },
            "template": "green" if settled else "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**{_escape_markdown(request.goal or '流程草稿')}**\n{intro}"
                    ),
                },
                {
                    "tag": "form",
                    "name": ROLE_FORM_NAME,
                    "direction": "vertical",
                    "vertical_spacing": "medium",
                    "elements": form_elements,
                },
            ]
        },
    }


def draft_wizard_card(
    request: RoleBindingRequest,
    candidate_person_ids: tuple[str, ...],
) -> dict[str, Any]:
    if request.kind != DRAFT_WIZARD_KIND or not candidate_person_ids:
        raise ValueError("draft wizard card requires a wizard request and candidates")
    options = [{"value": person_id} for person_id in candidate_person_ids]
    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "生成流程草稿"},
            "subtitle": {"tag": "plain_text", "content": "自然语言引导"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "描述你想完成的事情。中央 Agent 会生成候选流程，"
                        "服务端校验后只保存为草稿，不会自动运行。\n"
                        "请勿填写密码、Token、密钥或其他敏感凭据。"
                    ),
                },
                {
                    "tag": "form",
                    "name": "draft_wizard_form",
                    "direction": "vertical",
                    "vertical_spacing": "medium",
                    "elements": [
                        {
                            "tag": "input",
                            "element_id": "draft_brief",
                            "name": "draft_brief",
                            "required": True,
                            "input_type": "multiline_text",
                            "rows": 4,
                            "auto_resize": True,
                            "max_rows": 8,
                            "max_length": MAX_WIZARD_TEXT_CHARS,
                            "width": "fill",
                            "label": {"tag": "plain_text", "content": "想完成什么"},
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "例如：确认需求，Agent 生成摘要，再由同事复核",
                            },
                        },
                        {
                            "tag": "input",
                            "element_id": "draft_context",
                            "name": "draft_context",
                            "required": False,
                            "input_type": "multiline_text",
                            "rows": 2,
                            "auto_resize": True,
                            "max_rows": 6,
                            "max_length": MAX_WIZARD_TEXT_CHARS,
                            "width": "fill",
                            "label": {"tag": "plain_text", "content": "补充背景（选填）"},
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "材料、限制、验收要求或期望产出",
                            },
                        },
                        {
                            "tag": "markdown",
                            "content": "**协作成员**",
                        },
                        {
                            "tag": "select_person",
                            "element_id": "draft_collab",
                            "name": "role__collaborator",
                            "required": True,
                            "width": "fill",
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "默认由自己负责，可选择一名协作成员",
                            },
                            "options": options,
                            "initial_option": request.initiator_person_id,
                        },
                        {
                            "tag": "button",
                            "name": DRAFT_WIZARD_SUBMIT_NAME,
                            "text": {"tag": "plain_text", "content": "生成候选流程"},
                            "type": "primary_filled",
                            "width": "fill",
                            "action_type": "form_submit",
                        },
                    ],
                },
            ]
        },
    }


def draft_wizard_result_card(
    text: str,
    *,
    instance_id: str,
) -> dict[str, Any]:
    if not text.strip() or not instance_id.strip():
        raise ValueError("draft wizard result requires text and instance_id")
    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "流程草稿已生成"},
            "subtitle": {"tag": "plain_text", "content": instance_id},
            "template": "green",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": _escape_markdown(text),
                }
            ]
        },
    }


def draft_wizard_progress_card(stage: str) -> dict[str, Any]:
    content_by_stage = {
        "generating": (
            "参与人和输入已核验。中央 Agent 正在生成候选流程并执行服务端校验，"
            "完成后只会保存为草稿，不会自动运行。"
        ),
        "repairing": (
            "第一个候选未通过确定性校验。中央 Agent 正在根据校验结果重新生成，"
            "完成后仍只会保存为草稿。"
        ),
    }
    title_by_stage = {
        "generating": "正在生成流程草稿",
        "repairing": "正在修复候选图",
    }
    template_by_stage = {"generating": "blue", "repairing": "orange"}
    if stage not in content_by_stage:
        raise ValueError("unknown draft wizard progress stage")
    return processing_card(
        title=title_by_stage[stage],
        content=content_by_stage[stage],
        template=template_by_stage[stage],
    )


def role_binding_instance_id(tenant_id: str, message_id: str) -> str:
    digest = hashlib.sha256(
        f"{tenant_id}:{message_id}".encode("utf-8")
    ).hexdigest()[:24]
    return f"im_{digest}"


def _card_key(command_id: str) -> str:
    return "lf-role-card-" + hashlib.sha256(command_id.encode()).hexdigest()[:32]


def _action_reply_key(event_id: str) -> str:
    return "lf-role-reply-" + hashlib.sha256(event_id.encode()).hexdigest()[:32]


def _escape_markdown(value: str) -> str:
    replacements = {
        "&": "&#38;",
        "<": "&#60;",
        ">": "&#62;",
        "*": "&#42;",
        "_": "&#95;",
        "`": "&#96;",
    }
    return "".join(replacements.get(char, char) for char in value)


def _retry_delay(
    attempt_count: int,
    retry_base: timedelta,
    retry_max: timedelta,
) -> timedelta:
    multiplier = 2 ** min(max(attempt_count - 1, 0), 10)
    return min(retry_max, retry_base * multiplier)


def _validate_worker(
    tenant_id: str,
    worker_id: str,
    claim_limit: int,
    claim_ttl: timedelta,
) -> None:
    if not tenant_id.strip() or not worker_id.strip():
        raise ValueError("role-binding tenant_id and worker_id are required")
    if claim_limit < 1 or claim_ttl <= timedelta(0):
        raise ValueError("role-binding claim settings are invalid")


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"card action requires {field_name}")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


__all__ = [
    "CARD_ACTION_EVENT",
    "DRAFT_WIZARD_KIND",
    "DRAFT_WIZARD_SUBMIT_NAME",
    "InvalidRoleBindingClaimError",
    "MAX_CARD_CANDIDATES",
    "ROLE_FIELD_PREFIX",
    "ROLE_FORM_NAME",
    "ROLE_SUBMIT_NAME",
    "RoleBindingActionClaim",
    "RoleBindingActionInboxBridge",
    "RoleBindingActionReport",
    "RoleBindingActionSignal",
    "RoleBindingActionWorker",
    "RoleBindingCardClaim",
    "RoleBindingCardReport",
    "RoleBindingCardWorker",
    "RoleBindingRejected",
    "RoleBindingReplyClaim",
    "RoleBindingReplyReport",
    "RoleBindingReplyWorker",
    "RoleBindingProgressClaim",
    "RoleBindingProgressReport",
    "RoleBindingProgressWorker",
    "RoleBindingRequest",
    "RoleBindingVerificationReport",
    "RoleBindingVerificationWorker",
    "draft_wizard_card",
    "draft_wizard_progress_card",
    "draft_wizard_result_card",
    "role_binding_card",
    "role_binding_instance_id",
]
