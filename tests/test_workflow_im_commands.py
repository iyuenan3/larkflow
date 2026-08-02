"""Durable Feishu IM command boundary tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from larkflow.workflow import (
    DirectoryPerson,
    ExternalMessage,
    IMCommandClaim,
    IMCommandRejected,
    IMCommandSignal,
    IMCommandVerificationWorker,
    IMCommandWorker,
    IMEventInboxBridge,
    IMReplyClaim,
    IMReplyWorker,
    InMemoryTemplateStore,
    InMemoryWorkflowRepository,
    InstanceStatus,
    TemplateService,
    WorkflowService,
    parse_im_command,
)


NOW = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
TENANT = "tenant_im"


def signal(text: str, *, event_id: str = "event_1") -> IMCommandSignal:
    return IMCommandSignal(
        id=event_id,
        tenant_id=TENANT,
        message_id=f"message_{event_id}",
        chat_id="chat_1",
        sender_person_id="person_owner",
        text=text,
        occurred_at=NOW,
        received_at=NOW,
    )


class MemoryStore:
    def __init__(self) -> None:
        self.appended = []
        self.verification_claims = []
        self.command_claims = []
        self.reply_claims = []
        self.verified = []
        self.verification_failed = []
        self.verification_rejected = []
        self.processed = []
        self.failed = []
        self.reply_sent = []
        self.reply_failed = []

    def append_im_command(self, event):
        if any(item.id == event.id for item in self.appended):
            return False
        self.appended.append(event)
        return True

    def claim_im_verification(self, *_args, **_kwargs):
        claims, self.verification_claims = self.verification_claims, []
        return tuple(claims)

    def mark_im_verified(self, tenant_id, event_id, **kwargs):
        self.verified.append((tenant_id, event_id, kwargs))

    def mark_im_verification_failed(self, tenant_id, event_id, **kwargs):
        self.verification_failed.append((tenant_id, event_id, kwargs))

    def mark_im_verification_rejected(self, tenant_id, event_id, **kwargs):
        self.verification_rejected.append((tenant_id, event_id, kwargs))

    def claim_im_commands(self, *_args, **_kwargs):
        claims, self.command_claims = self.command_claims, []
        return tuple(claims)

    def mark_im_processed(self, tenant_id, event_id, **kwargs):
        self.processed.append((tenant_id, event_id, kwargs))

    def mark_im_failed(self, tenant_id, event_id, **kwargs):
        self.failed.append((tenant_id, event_id, kwargs))

    def claim_im_replies(self, *_args, **_kwargs):
        claims, self.reply_claims = self.reply_claims, []
        return tuple(claims)

    def mark_im_reply_sent(self, tenant_id, event_id, **kwargs):
        self.reply_sent.append((tenant_id, event_id, kwargs))

    def mark_im_reply_failed(self, tenant_id, event_id, **kwargs):
        self.reply_failed.append((tenant_id, event_id, kwargs))


def template_document():
    return {
        "schema_version": "0.2",
        "template": {
            "id": "quick_review",
            "version": 1,
            "name": "Quick review",
            "status": "draft",
            "locked": True,
        },
        "goal": "Review one brief",
        "parameters": {"brief": {"type": "text", "required": True}},
        "nodes": [
            {
                "id": "review",
                "title": "Review brief",
                "owner_role": "reviewer",
                "executor": "human",
                "deps": [],
                "work": {
                    "objective": "Review the submitted brief",
                    "inputs": ["instance_inputs.brief"],
                    "outputs": [{"id": "decision", "type": "data"}],
                    "acceptance": ["A decision is recorded"],
                },
            }
        ],
    }


def event_payload(text: str):
    return {
        "header": {"event_id": "event_1", "create_time": "1785656400000"},
        "event": {
            "sender": {"sender_id": {"open_id": "person_owner"}},
            "message": {
                "message_id": "message_1",
                "chat_id": "chat_1",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def flattened_event_payload(text: str):
    return {
        "event_id": "event_flat_1",
        "message_id": "message_flat_1",
        "chat_id": "chat_flat_1",
        "message_type": "text",
        "content": text,
        "create_time": "1785656400000",
        "sender_id": "person_flat_owner",
        "sender_type": "user",
        "type": "im.message.receive_v1",
    }


def test_bridge_persists_only_native_larkflow_text_commands():
    store = MemoryStore()
    bridge = IMEventInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)

    assert bridge("im.message.receive_v1", event_payload("hello")) is False
    assert bridge(
        "im.message.receive_v1",
        event_payload('/larkflow start quick_review {"brief":"ready"}'),
    ) is True
    assert bridge(
        "im.message.receive_v1",
        event_payload('/larkflow start quick_review {"brief":"ready"}'),
    ) is False
    assert store.appended[0].sender_person_id == "person_owner"
    assert store.appended[0].chat_id == "chat_1"


def test_bridge_accepts_flattened_lark_cli_event_payload():
    store = MemoryStore()
    bridge = IMEventInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)

    assert bridge(
        "im.message.receive_v1",
        flattened_event_payload('/larkflow start quick_review {"brief":"ready"}'),
    ) is True
    event = store.appended[0]
    assert event.id == "event_flat_1"
    assert event.message_id == "message_flat_1"
    assert event.chat_id == "chat_flat_1"
    assert event.sender_person_id == "person_flat_owner"
    assert event.text == '/larkflow start quick_review {"brief":"ready"}'


def test_verification_requires_matching_active_directory_person():
    store = MemoryStore()
    store.verification_claims = [IMCommandClaim(signal("/larkflow help"), "token", 1)]

    class Directory:
        def get_person(self, tenant_id, person_id):
            assert tenant_id == TENANT
            return DirectoryPerson(person_id=person_id, active=True)

    report = IMCommandVerificationWorker(
        store,
        Directory(),
        tenant_id=TENANT,
        worker_id="verify_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.verified == 1
    assert store.verified[0][1] == "event_1"


def test_inactive_sender_is_rejected_before_domain_processing():
    store = MemoryStore()
    store.verification_claims = [IMCommandClaim(signal("/larkflow help"), "token", 1)]

    class Directory:
        def get_person(self, _tenant_id, person_id):
            return DirectoryPerson(person_id=person_id, active=False)

    report = IMCommandVerificationWorker(
        store,
        Directory(),
        tenant_id=TENANT,
        worker_id="verify_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.rejected == 1
    assert store.verification_rejected[0][2]["outcome"] == "rejected:inactive_sender"


def test_start_then_confirm_uses_sender_as_every_owner_and_keeps_preview_gate():
    repository = InMemoryWorkflowRepository()
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=template_document(),
    )
    templates.enable(TENANT, "quick_review", actor_person_id="person_admin")
    service = WorkflowService(repository, clock=lambda: NOW)
    store = MemoryStore()
    start = signal('/larkflow start quick_review {"brief":"ready"}')
    store.command_claims = [IMCommandClaim(start, "start-token", 1)]
    worker = IMCommandWorker(
        store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    )

    start_report = worker.run_once()

    assert start_report.processed == 1
    instance_id = store.processed[0][2]["instance_id"]
    draft = service.get(TENANT, instance_id)
    assert draft.status == InstanceStatus.DRAFT
    assert draft.owner_person_id == "person_owner"
    assert {node.owner_person_id for node in draft.snapshot.nodes} == {"person_owner"}
    assert "/larkflow confirm" in store.processed[0][2]["reply_text"]

    confirm = signal(
        f"/larkflow confirm {instance_id}",
        event_id="event_confirm",
    )
    store.command_claims = [IMCommandClaim(confirm, "confirm-token", 1)]
    confirm_report = worker.run_once()

    assert confirm_report.processed == 1
    assert service.get(TENANT, instance_id).status == InstanceStatus.RUNNING


def test_unknown_template_is_rejected_without_retrying_forever():
    store = MemoryStore()
    store.command_claims = [
        IMCommandClaim(signal("/larkflow start missing_template"), "token", 1)
    ]
    worker = IMCommandWorker(
        store,
        WorkflowService(InMemoryWorkflowRepository(), clock=lambda: NOW),
        TemplateService(InMemoryTemplateStore(), clock=lambda: NOW),
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    )

    report = worker.run_once()

    assert report.rejected == 1
    assert report.failed == 0
    assert not store.failed
    assert store.processed[0][2]["outcome"] == "rejected:command"


def test_non_owner_confirm_is_rejected_without_retrying_forever():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=lambda: NOW)
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=template_document(),
    )
    templates.enable(TENANT, "quick_review", actor_person_id="person_admin")
    owner_store = MemoryStore()
    owner_store.command_claims = [
        IMCommandClaim(
            signal('/larkflow start quick_review {"brief":"ready"}'),
            "start-token",
            1,
        )
    ]
    owner_worker = IMCommandWorker(
        owner_store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    )
    owner_worker.run_once()
    instance_id = owner_store.processed[0][2]["instance_id"]
    intruder_event = IMCommandSignal(
        **{
            **signal(
                f"/larkflow confirm {instance_id}",
                event_id="event_intruder",
            ).__dict__,
            "sender_person_id": "person_intruder",
        }
    )
    intruder_store = MemoryStore()
    intruder_store.command_claims = [
        IMCommandClaim(intruder_event, "confirm-token", 1)
    ]
    intruder_worker = IMCommandWorker(
        intruder_store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="command_2",
        clock=lambda: NOW,
    )

    report = intruder_worker.run_once()

    assert report.rejected == 1
    assert report.failed == 0
    assert not intruder_store.failed


@pytest.mark.parametrize(
    "text",
    (
        "/other help",
        "/larkflow confirm",
        "/larkflow start quick_review []",
        "/larkflow unsupported",
    ),
)
def test_command_parser_rejects_ambiguous_or_broad_grammar(text):
    with pytest.raises(IMCommandRejected):
        parse_im_command(text)


def test_reply_worker_uses_stable_key_and_records_external_message():
    store = MemoryStore()
    store.reply_claims = [
        IMReplyClaim(
            event_id="event_1",
            tenant_id=TENANT,
            chat_id="chat_1",
            text="draft ready",
            idempotency_key="lf-im-stable",
            claim_token="reply-token",
            attempt_count=1,
        )
    ]

    class Sender:
        def __init__(self):
            self.calls = []

        def send_chat_message(self, **kwargs):
            self.calls.append(kwargs)
            return "message_external"

    sender = Sender()
    report = IMReplyWorker(
        store,
        sender,
        tenant_id=TENANT,
        worker_id="reply_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.sent == 1
    assert sender.calls[0]["idempotency_key"] == "lf-im-stable"
    assert store.reply_sent[0][2]["external_id"] == "message_external"
