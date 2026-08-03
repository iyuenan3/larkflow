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
    IMMention,
    IMReplyClaim,
    IMReplyWorker,
    InMemoryTemplateStore,
    InMemoryWorkflowRepository,
    InstanceSnapshot,
    InstanceStatus,
    NodeSpec,
    NodeStatus,
    RecoveryActionInboxBridge,
    TemplateService,
    WorkflowService,
    parse_im_command,
)


NOW = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
TENANT = "tenant_im"


def signal(
    text: str,
    *,
    event_id: str = "event_1",
    mentions: tuple[IMMention, ...] = (),
) -> IMCommandSignal:
    return IMCommandSignal(
        id=event_id,
        tenant_id=TENANT,
        message_id=f"message_{event_id}",
        chat_id="chat_1",
        sender_person_id="person_owner",
        text=text,
        occurred_at=NOW,
        received_at=NOW,
        mentions=mentions,
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
        self.role_binding_requested = []
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

    def mark_im_role_binding_requested(self, tenant_id, event_id, **kwargs):
        self.role_binding_requested.append((tenant_id, event_id, kwargs))

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


def editable_template_document():
    document = template_document()
    document["template"]["locked"] = False
    document["nodes"].append(
        {
            "id": "publish",
            "title": "Publish decision",
            "owner_role": "reviewer",
            "executor": "human",
            "deps": ["review"],
            "work": {
                "objective": "Publish the reviewed decision",
                "inputs": ["dependencies.review"],
                "outputs": [{"id": "receipt", "type": "data"}],
                "acceptance": ["A receipt is recorded"],
            },
        }
    )
    return document


def collaborative_template_document():
    document = template_document()
    document["template"]["id"] = "collaborative_review"
    document["nodes"] = [
        {
            **document["nodes"][0],
            "id": "confirm",
            "title": "Confirm brief",
            "owner_role": "requester",
        },
        {
            **document["nodes"][0],
            "id": "review",
            "title": "Review result",
            "owner_role": "reviewer",
            "deps": ["confirm"],
            "work": {
                **document["nodes"][0]["work"],
                "inputs": ["dependencies.confirm"],
            },
        },
    ]
    return document


def event_payload(text: str, *, mentions=None):
    return {
        "header": {"event_id": "event_1", "create_time": "1785656400000"},
        "event": {
            "sender": {"sender_id": {"open_id": "person_owner"}},
            "message": {
                "message_id": "message_1",
                "chat_id": "chat_1",
                "message_type": "text",
                "content": json.dumps({"text": text}),
                "mentions": mentions or [],
            },
        },
    }


def flattened_event_payload(text: str, *, mentions=None):
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
        "mentions": mentions or [],
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


def test_recovery_card_bridge_persists_a_verified_version_bound_command():
    store = MemoryStore()
    bridge = RecoveryActionInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)
    payload = {
        "event_id": "event_recovery_1",
        "message_id": "message_failure_card_1",
        "chat_id": "chat_owner",
        "operator_id": "person_node_owner",
        "action_tag": "button",
        "action_name": "workflow_recovery_human_takeover",
        "action_value": {
            "kind": "workflow_recovery",
            "action": "human_takeover",
            "instance_id": "instance_1",
            "node_key": "draft",
            "attempt_no": 1,
            "node_version": 2,
            "instance_version": 4,
        },
        "token": "card-update-token",
        "timestamp": "1785830400000",
    }

    assert bridge("card.action.trigger", payload) is True
    assert bridge("card.action.trigger", payload) is False
    event = store.appended[0]
    assert event.sender_person_id == "person_node_owner"
    assert event.card_update_token == "card-update-token"
    assert event.text.startswith("/larkflow recover ")
    assert parse_im_command(event.text) == (
        "recover",
        "instance_1",
        {
            "kind": "workflow_recovery",
            "action": "human_takeover",
            "node_key": "draft",
            "attempt_no": 1,
            "node_version": 2,
            "instance_version": 4,
        },
    )


def test_recovery_card_bridge_never_trusts_identity_inside_action_value():
    store = MemoryStore()
    bridge = RecoveryActionInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)
    value = {
        "kind": "workflow_recovery",
        "action": "retry",
        "instance_id": "instance_1",
        "node_key": "draft",
        "attempt_no": 1,
        "node_version": 2,
        "instance_version": 4,
        "operator_id": "forged-owner",
    }

    with pytest.raises(IMCommandRejected, match="参数不完整"):
        bridge(
            "card.action.trigger",
            {
                "event_id": "event_recovery_forged",
                "message_id": "message_failure_card_forged",
                "chat_id": "chat_owner",
                "operator_id": "actual-clicker",
                "action_tag": "button",
                "action_name": "workflow_recovery_retry",
                "action_value": value,
                "token": "card-update-token",
            },
        )


def test_recovery_card_bridge_rejects_a_mismatched_unique_button_name():
    store = MemoryStore()
    bridge = RecoveryActionInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)

    with pytest.raises(ValueError, match="does not match"):
        bridge(
            "card.action.trigger",
            {
                "event_id": "event_recovery_mismatch",
                "message_id": "message_failure_card_mismatch",
                "chat_id": "chat_owner",
                "operator_id": "actual-clicker",
                "action_tag": "button",
                "action_name": "workflow_recovery_retry",
                "action_value": {
                    "kind": "workflow_recovery",
                    "action": "human_takeover",
                    "instance_id": "instance_1",
                    "node_key": "draft",
                    "attempt_no": 1,
                    "node_version": 2,
                    "instance_version": 4,
                },
                "token": "card-update-token",
            },
        )


def test_bridge_accepts_group_command_with_authenticated_mentions():
    store = MemoryStore()
    bridge = IMEventInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)
    text = (
        '@_user_1 /larkflow start collaborative_review {"brief":"ready"} '
        "reviewer=@_user_2"
    )

    assert bridge(
        "im.message.receive_v1",
        event_payload(
            text,
            mentions=[
                {
                    "key": "@_user_1",
                    "id": {"open_id": "bot_open_id"},
                    "name": "Larkflow",
                },
                {
                    "key": "@_user_2",
                    "id": {"open_id": "person_reviewer"},
                    "name": "Reviewer",
                },
            ],
        ),
    ) is True
    event = store.appended[0]
    assert event.text.startswith("/larkflow start collaborative_review")
    assert event.mentions == (
        IMMention(key="@_user_1", person_id="bot_open_id"),
        IMMention(key="@_user_2", person_id="person_reviewer"),
    )
    assert all(not hasattr(mention, "name") for mention in event.mentions)


def test_bridge_accepts_flattened_string_open_id_mentions():
    store = MemoryStore()
    bridge = IMEventInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)

    assert bridge(
        "im.message.receive_v1",
        flattened_event_payload(
            "/larkflow start quick_review reviewer=@_user_1",
            mentions=[
                {
                    "key": "@_user_1",
                    "id": "person_reviewer",
                    "id_type": "open_id",
                }
            ],
        ),
    ) is True
    assert store.appended[0].mentions == (
        IMMention(key="@_user_1", person_id="person_reviewer"),
    )


def test_bridge_restores_lark_cli_rendered_mentions_before_parsing():
    store = MemoryStore()
    bridge = IMEventInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)
    text = (
        '@Larkflow /larkflow start collaborative_review {"brief":"ready"} '
        "reviewer=@Reviewer"
    )

    assert bridge(
        "im.message.receive_v1",
        flattened_event_payload(
            text,
            mentions=[
                {
                    "key": "@_user_1",
                    "id": "bot_open_id",
                    "name": "Larkflow",
                },
                {
                    "key": "@_user_2",
                    "id": "person_reviewer",
                    "name": "Reviewer",
                },
            ],
        ),
    ) is True
    event = store.appended[0]
    assert event.text == (
        '/larkflow start collaborative_review {"brief":"ready"} '
        "reviewer=@_user_2"
    )
    assert event.mentions == (
        IMMention(key="@_user_1", person_id="bot_open_id"),
        IMMention(key="@_user_2", person_id="person_reviewer"),
    )


def test_bridge_rejects_ambiguous_rendered_mention_text():
    store = MemoryStore()
    bridge = IMEventInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)

    assert bridge(
        "im.message.receive_v1",
        flattened_event_payload(
            "@Larkflow /larkflow help copied-from @Larkflow",
            mentions=[
                {
                    "key": "@_user_1",
                    "id": "bot_open_id",
                    "name": "Larkflow",
                }
            ],
        ),
    ) is False
    assert store.appended == []


def test_bridge_rejects_non_mention_text_before_command():
    store = MemoryStore()
    bridge = IMEventInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)

    assert bridge(
        "im.message.receive_v1",
        event_payload(
            "please /larkflow help",
            mentions=[
                {"key": "@_user_1", "id": {"open_id": "person_1"}}
            ],
        ),
    ) is False
    assert store.appended == []


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


def test_verification_checks_every_referenced_role_owner():
    store = MemoryStore()
    event = signal(
        "/larkflow start collaborative_review reviewer=@_user_1",
        mentions=(IMMention("@_user_1", "person_reviewer"),),
    )
    store.verification_claims = [IMCommandClaim(event, "token", 1)]
    calls = []

    class Directory:
        def get_person(self, tenant_id, person_id):
            calls.append((tenant_id, person_id))
            return DirectoryPerson(person_id=person_id, active=True)

    report = IMCommandVerificationWorker(
        store,
        Directory(),
        tenant_id=TENANT,
        worker_id="verify_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.verified == 1
    assert calls == [
        (TENANT, "person_owner"),
        (TENANT, "person_reviewer"),
    ]


def test_verification_rejects_role_binding_without_matching_mention_metadata():
    store = MemoryStore()
    event = signal(
        "/larkflow start collaborative_review reviewer=@_user_1"
    )
    store.verification_claims = [IMCommandClaim(event, "token", 1)]

    class Directory:
        def get_person(self, _tenant_id, person_id):
            return DirectoryPerson(person_id=person_id, active=True)

    report = IMCommandVerificationWorker(
        store,
        Directory(),
        tenant_id=TENANT,
        worker_id="verify_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.rejected == 1
    assert store.verification_rejected[0][2]["outcome"] == (
        "rejected:invalid_owner_mention"
    )


def test_verification_rejects_inactive_role_owner():
    store = MemoryStore()
    event = signal(
        "/larkflow start collaborative_review reviewer=@_user_1",
        mentions=(IMMention("@_user_1", "person_reviewer"),),
    )
    store.verification_claims = [IMCommandClaim(event, "token", 1)]

    class Directory:
        def get_person(self, _tenant_id, person_id):
            return DirectoryPerson(
                person_id=person_id,
                active=person_id != "person_reviewer",
            )

    report = IMCommandVerificationWorker(
        store,
        Directory(),
        tenant_id=TENANT,
        worker_id="verify_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.rejected == 1
    assert store.verification_rejected[0][2]["outcome"] == (
        "rejected:inactive_owner"
    )


def test_verification_leaves_non_binding_grammar_errors_to_command_worker():
    store = MemoryStore()
    store.verification_claims = [
        IMCommandClaim(signal("/larkflow unsupported"), "token", 1)
    ]

    class Directory:
        def get_person(self, _tenant_id, person_id):
            return DirectoryPerson(person_id=person_id, active=True)

    report = IMCommandVerificationWorker(
        store,
        Directory(),
        tenant_id=TENANT,
        worker_id="verify_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.verified == 1
    assert not store.verification_rejected


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


def test_recovery_command_applies_the_version_bound_human_takeover():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, clock=lambda: NOW)
    service.create_draft(
        instance_id="instance_recovery_command",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=InstanceSnapshot(
            nodes=(
                NodeSpec(
                    "draft",
                    "Draft summary",
                    "person_owner",
                    "agent",
                    work={
                        "objective": "Draft",
                        "outputs": [{"id": "draft", "type": "data"}],
                        "acceptance": ["A draft exists"],
                    },
                ),
            )
        ),
    )
    service.confirm_draft(
        TENANT,
        "instance_recovery_command",
        actor_person_id="person_owner",
    )
    activation = service.dispatch_ready(
        TENANT,
        "instance_recovery_command",
        worker_id="edge_1",
    )[0]
    failed = service.fail_automated(
        TENANT,
        "instance_recovery_command",
        "draft",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        claim_token=activation.claim_token or "",
        error_code="provider_timeout",
        error_message="provider unavailable",
        worker_id="edge_1",
    )
    node = failed.nodes["draft"]
    payload = {
        "kind": "workflow_recovery",
        "action": "human_takeover",
        "instance_id": failed.id,
        "node_key": "draft",
        "attempt_no": node.current_attempt_no,
        "node_version": node.version,
        "instance_version": failed.version,
    }
    store = MemoryStore()
    store.command_claims = [
        IMCommandClaim(
            signal(
                "/larkflow recover "
                + json.dumps(payload, separators=(",", ":"))
            ),
            "command-token",
            1,
        )
    ]

    report = IMCommandWorker(
        store,
        service,
        TemplateService(InMemoryTemplateStore(), clock=lambda: NOW),
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.processed == 1
    recovered = service.get(TENANT, failed.id)
    assert recovered.nodes["draft"].status == NodeStatus.WAITING_HUMAN
    assert recovered.nodes["draft"].current_attempt_no == 2
    assert store.processed[0][2]["outcome"] == "human_takeover_started"


def test_start_binds_mentioned_reviewer_and_keeps_sender_as_requester():
    repository = InMemoryWorkflowRepository()
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=collaborative_template_document(),
    )
    templates.enable(
        TENANT,
        "collaborative_review",
        actor_person_id="person_admin",
    )
    service = WorkflowService(repository, clock=lambda: NOW)
    store = MemoryStore()
    event = signal(
        '/larkflow start collaborative_review {"brief":"ready"} '
        "reviewer=@_user_1",
        mentions=(IMMention("@_user_1", "person_reviewer"),),
    )
    store.command_claims = [IMCommandClaim(event, "start-token", 1)]

    report = IMCommandWorker(
        store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.processed == 1
    instance_id = store.processed[0][2]["instance_id"]
    draft = service.get(TENANT, instance_id)
    assert draft.status == InstanceStatus.DRAFT
    assert draft.owner_person_id == "person_owner"
    assert draft.snapshot.node("confirm").owner_person_id == "person_owner"
    assert draft.snapshot.node("review").owner_person_id == "person_reviewer"
    reply = store.processed[0][2]["reply_text"]
    assert "绑定给其他成员的角色：1" in reply
    assert "/larkflow confirm" in reply


def test_multi_role_start_without_mentions_requests_person_selection_card():
    repository = InMemoryWorkflowRepository()
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=collaborative_template_document(),
    )
    templates.enable(
        TENANT,
        "collaborative_review",
        actor_person_id="person_admin",
    )
    store = MemoryStore()
    event = signal(
        '/larkflow start collaborative_review {"brief":"ready"}',
    )
    store.command_claims = [IMCommandClaim(event, "start-token", 1)]

    report = IMCommandWorker(
        store,
        WorkflowService(repository, clock=lambda: NOW),
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.processed == 1
    assert not repository.list_for_owner(
        TENANT,
        owner_person_id="person_owner",
        limit=10,
    )
    request = store.role_binding_requested[0][2]["request"]
    assert request.template_id == "collaborative_review"
    assert request.template_version == 1
    assert request.inputs == {"brief": "ready"}
    assert request.roles == ("requester", "reviewer")


@pytest.mark.parametrize(
    "text, mentions",
    (
        (
            "/larkflow start collaborative_review reviewer=@_user_1",
            (),
        ),
        (
            "/larkflow start collaborative_review unknown=@_user_1",
            (IMMention("@_user_1", "person_reviewer"),),
        ),
    ),
)
def test_start_rejects_untrusted_or_unknown_role_binding(text, mentions):
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=collaborative_template_document(),
    )
    templates.enable(
        TENANT,
        "collaborative_review",
        actor_person_id="person_admin",
    )
    store = MemoryStore()
    store.command_claims = [
        IMCommandClaim(signal(text, mentions=mentions), "start-token", 1)
    ]

    report = IMCommandWorker(
        store,
        WorkflowService(InMemoryWorkflowRepository(), clock=lambda: NOW),
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.rejected == 1
    assert report.failed == 0


def test_status_is_owner_only_read_and_reports_current_dag_state():
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
    worker = IMCommandWorker(
        store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    )
    store.command_claims = [
        IMCommandClaim(
            signal('/larkflow start quick_review {"brief":"ready"}'),
            "start-token",
            1,
        )
    ]
    worker.run_once()
    instance_id = store.processed[-1][2]["instance_id"]
    store.command_claims = [
        IMCommandClaim(
            signal(f"/larkflow confirm {instance_id}", event_id="event_confirm"),
            "confirm-token",
            1,
        )
    ]
    worker.run_once()
    service.dispatch_ready(TENANT, instance_id)
    version_before = service.get(TENANT, instance_id).version
    store.command_claims = [
        IMCommandClaim(
            signal(f"/larkflow status {instance_id}", event_id="event_status"),
            "status-token",
            1,
        )
    ]

    report = worker.run_once()

    assert report.processed == 1
    reply = store.processed[-1][2]["reply_text"]
    assert store.processed[-1][2]["outcome"] == "status_shown"
    assert "状态：进行中" in reply
    assert "进度：0/1" in reply
    assert "Review brief (review)｜human｜等待人工｜责任人：你" in reply
    assert "person_owner" not in reply
    assert service.get(TENANT, instance_id).version == version_before


def test_status_hides_instance_existence_from_non_owner():
    repository = InMemoryWorkflowRepository()
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=template_document(),
    )
    templates.enable(TENANT, "quick_review", actor_person_id="person_admin")
    service = WorkflowService(repository, clock=lambda: NOW)
    owner_store = MemoryStore()
    owner_store.command_claims = [
        IMCommandClaim(
            signal('/larkflow start quick_review {"brief":"ready"}'),
            "start-token",
            1,
        )
    ]
    IMCommandWorker(
        owner_store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="command_owner",
        clock=lambda: NOW,
    ).run_once()
    instance_id = owner_store.processed[-1][2]["instance_id"]
    replies = []
    for target in (instance_id, "missing_instance"):
        event = IMCommandSignal(
            **{
                **signal(
                    f"/larkflow status {target}",
                    event_id=f"event_{target}",
                ).__dict__,
                "sender_person_id": "person_intruder",
            }
        )
        store = MemoryStore()
        store.command_claims = [IMCommandClaim(event, "status-token", 1)]
        report = IMCommandWorker(
            store,
            service,
            templates,
            tenant_id=TENANT,
            worker_id="command_intruder",
            clock=lambda: NOW,
        ).run_once()
        assert report.rejected == 1
        assert report.failed == 0
        replies.append(store.processed[-1][2]["reply_text"])

    assert replies[0] == replies[1]
    assert replies[0] == "命令未执行：实例不存在或你无权查看"


def test_list_returns_only_recent_instance_owner_summaries_without_writes():
    repository = InMemoryWorkflowRepository()
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    document = template_document()
    document["goal"] = "Review\n" + "x" * 200
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=document,
    )
    templates.enable(TENANT, "quick_review", actor_person_id="person_admin")
    snapshot = templates.instantiate(
        TENANT,
        "quick_review",
        inputs={"brief": "ready"},
        owner_bindings={"reviewer": "person_owner"},
    )
    times = [NOW + timedelta(minutes=index) for index in range(14)]
    service = WorkflowService(repository, clock=lambda: times.pop(0))
    for index in range(12):
        service.create_draft(
            instance_id=f"owner_{index:02d}",
            tenant_id=TENANT,
            owner_person_id="person_owner",
            actor_person_id="person_owner",
            snapshot=snapshot,
        )
    service.create_draft(
        instance_id="node_owner_but_not_instance_owner",
        tenant_id=TENANT,
        owner_person_id="person_other",
        actor_person_id="person_other",
        snapshot=snapshot,
    )
    service.create_draft(
        instance_id="other_tenant",
        tenant_id="tenant_other",
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot,
    )
    versions_before = {
        f"owner_{index:02d}": service.get(TENANT, f"owner_{index:02d}").version
        for index in range(12)
    }
    audit_count_before = sum(
        len(repository.audit_log(TENANT, instance_id))
        for instance_id in versions_before
    )
    store = MemoryStore()
    store.command_claims = [
        IMCommandClaim(signal("/larkflow list", event_id="event_list"), "list-token", 1)
    ]

    report = IMCommandWorker(
        store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.processed == 1
    processed = store.processed[-1][2]
    assert processed["outcome"] == "instances_listed"
    assert processed["instance_id"] is None
    reply = processed["reply_text"]
    assert "node_owner_but_not_instance_owner" not in reply
    assert "other_tenant" not in reply
    assert reply.index("owner_11") < reply.index("owner_02")
    assert "owner_01" not in reply
    assert "owner_00" not in reply
    assert "仅显示最近 10 个流程" in reply
    assert "person_owner" not in reply
    assert "person_other" not in reply
    assert "x" * 121 not in reply
    assert "…" in reply
    assert {
        instance_id: service.get(TENANT, instance_id).version
        for instance_id in versions_before
    } == versions_before
    assert sum(
        len(repository.audit_log(TENANT, instance_id))
        for instance_id in versions_before
    ) == audit_count_before
    with pytest.raises(ValueError, match="between 1 and 100"):
        service.list_for_owner(
            TENANT,
            actor_person_id="person_owner",
            limit=101,
        )


def test_list_reports_empty_owner_history_without_disclosing_other_instances():
    repository = InMemoryWorkflowRepository()
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    store = MemoryStore()
    store.command_claims = [
        IMCommandClaim(signal("/larkflow list", event_id="event_empty_list"), "list-token", 1)
    ]

    report = IMCommandWorker(
        store,
        WorkflowService(repository, clock=lambda: NOW),
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.processed == 1
    assert "暂无由你发起的流程" in store.processed[-1][2]["reply_text"]


def test_restart_requires_preview_owner_confirmation_and_is_idempotent():
    repository = InMemoryWorkflowRepository()
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=template_document(),
    )
    templates.enable(TENANT, "quick_review", actor_person_id="person_admin")
    snapshot = templates.instantiate(
        TENANT,
        "quick_review",
        inputs={"brief": "ready"},
        owner_bindings={"reviewer": "person_owner"},
    )
    service = WorkflowService(repository, clock=lambda: NOW)
    service.create_draft(
        instance_id="restart_from_im",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot,
    )
    service.confirm_draft(
        TENANT,
        "restart_from_im",
        actor_person_id="person_owner",
    )
    activation = service.dispatch_ready(TENANT, "restart_from_im")[0]
    service.submit_human(
        TENANT,
        "restart_from_im",
        "review",
        actor_person_id="person_owner",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        result={"decision": "approved"},
    )
    store = MemoryStore()
    worker = IMCommandWorker(
        store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    )
    version_before = service.get(TENANT, "restart_from_im").version
    store.command_claims = [
        IMCommandClaim(
            signal(
                "/larkflow restart restart_from_im review",
                event_id="event_restart_preview",
            ),
            "restart-preview-token",
            1,
        )
    ]

    preview_report = worker.run_once()

    assert preview_report.processed == 1
    preview_reply = store.processed[-1][2]["reply_text"]
    assert store.processed[-1][2]["outcome"] == "restart_previewed"
    assert "节点重启预览" in preview_reply
    assert "Review brief (review)" in preview_reply
    assert "旧 Attempt、结果和审计保留" in preview_reply
    assert "person_owner" not in preview_reply
    assert service.get(TENANT, "restart_from_im").version == version_before
    confirm_command = preview_reply.splitlines()[-1]
    preview_id = confirm_command.split()[-1]

    intruder_event = IMCommandSignal(
        **{
            **signal(
                confirm_command,
                event_id="event_restart_intruder",
            ).__dict__,
            "sender_person_id": "person_intruder",
        }
    )
    store.command_claims = [
        IMCommandClaim(intruder_event, "intruder-token", 1)
    ]
    intruder_report = worker.run_once()
    assert intruder_report.rejected == 1
    assert service.get(TENANT, "restart_from_im").version == version_before

    store.command_claims = [
        IMCommandClaim(
            signal(confirm_command, event_id="event_restart_confirm"),
            "restart-confirm-token",
            1,
        )
    ]
    confirm_report = worker.run_once()

    assert confirm_report.processed == 1
    assert store.processed[-1][2]["outcome"] == "restart_confirmed"
    assert "节点已重启" in store.processed[-1][2]["reply_text"]
    restarted = service.get(TENANT, "restart_from_im")
    assert restarted.status == InstanceStatus.RUNNING
    assert restarted.nodes["review"].current_attempt_no == 2
    assert restarted.nodes["review"].status.value == "ready"
    version_after = restarted.version

    store.command_claims = [
        IMCommandClaim(
            signal(confirm_command, event_id="event_restart_replay"),
            "restart-replay-token",
            1,
        )
    ]
    replay_report = worker.run_once()
    assert replay_report.processed == 1
    assert "无需重复操作" in store.processed[-1][2]["reply_text"]
    assert service.get(TENANT, "restart_from_im").version == version_after
    assert repository.get_restart_preview(TENANT, preview_id).consumed_at == NOW


def test_full_instance_restart_command_previews_all_nodes_before_confirmation():
    repository = InMemoryWorkflowRepository()
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=template_document(),
    )
    templates.enable(TENANT, "quick_review", actor_person_id="person_admin")
    snapshot = templates.instantiate(
        TENANT,
        "quick_review",
        inputs={"brief": "ready"},
        owner_bindings={"reviewer": "person_owner"},
    )
    service = WorkflowService(repository, clock=lambda: NOW)
    service.create_draft(
        instance_id="restart_all_from_im",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot,
    )
    service.confirm_draft(
        TENANT,
        "restart_all_from_im",
        actor_person_id="person_owner",
    )
    activation = service.dispatch_ready(TENANT, "restart_all_from_im")[0]
    service.submit_human(
        TENANT,
        "restart_all_from_im",
        "review",
        actor_person_id="person_owner",
        attempt_no=activation.attempt_no,
        expected_node_version=activation.expected_node_version,
        result={"decision": "approved"},
    )
    store = MemoryStore()
    worker = IMCommandWorker(
        store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    )
    version_before = service.get(TENANT, "restart_all_from_im").version
    store.command_claims = [
        IMCommandClaim(
            signal(
                "/larkflow restart-all restart_all_from_im",
                event_id="event_restart_all_preview",
            ),
            "restart-all-preview-token",
            1,
        )
    ]

    preview_report = worker.run_once()

    assert preview_report.processed == 1
    preview_reply = store.processed[-1][2]["reply_text"]
    assert store.processed[-1][2]["outcome"] == "instance_restart_previewed"
    assert "完整实例重启预览" in preview_reply
    assert "范围：全部节点" in preview_reply
    assert "Review brief (review)" in preview_reply
    assert service.get(TENANT, "restart_all_from_im").version == version_before
    confirm_command = preview_reply.splitlines()[-1]

    store.command_claims = [
        IMCommandClaim(
            signal(confirm_command, event_id="event_restart_all_confirm"),
            "restart-all-confirm-token",
            1,
        )
    ]
    confirm_report = worker.run_once()

    assert confirm_report.processed == 1
    assert "完整实例已重启" in store.processed[-1][2]["reply_text"]
    restarted = service.get(TENANT, "restart_all_from_im")
    assert restarted.status == InstanceStatus.RUNNING
    assert restarted.nodes["review"].current_attempt_no == 2
    assert restarted.nodes["review"].status.value == "ready"


def test_graph_edit_command_previews_confirms_and_replays_safely():
    repository = InMemoryWorkflowRepository()
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=editable_template_document(),
    )
    templates.enable(TENANT, "quick_review", actor_person_id="person_admin")
    snapshot = templates.instantiate(
        TENANT,
        "quick_review",
        inputs={"brief": "ready"},
        owner_bindings={"reviewer": "person_owner"},
    )
    service = WorkflowService(repository, clock=lambda: NOW)
    service.create_draft(
        instance_id="edit_from_im",
        tenant_id=TENANT,
        owner_person_id="person_owner",
        actor_person_id="person_owner",
        snapshot=snapshot,
    )
    service.confirm_draft(
        TENANT,
        "edit_from_im",
        actor_person_id="person_owner",
    )
    operations = json.dumps(
        [
            {
                "op": "update_node",
                "node_key": "publish",
                "set": {"title": "Archive decision"},
            }
        ],
        separators=(",", ":"),
    )
    store = MemoryStore()
    worker = IMCommandWorker(
        store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    )
    version_before = service.get(TENANT, "edit_from_im").version
    store.command_claims = [
        IMCommandClaim(
            signal(
                f"/larkflow edit edit_from_im {operations}",
                event_id="event_edit_preview",
            ),
            "edit-preview-token",
            1,
        )
    ]

    preview_report = worker.run_once()

    assert preview_report.processed == 1
    preview_reply = store.processed[-1][2]["reply_text"]
    assert store.processed[-1][2]["outcome"] == "graph_edit_previewed"
    assert "运行中编辑预览" in preview_reply
    assert "图版本：1 -> 2" in preview_reply
    assert "修改：publish" in preview_reply
    assert service.get(TENANT, "edit_from_im").version == version_before
    assert service.get(TENANT, "edit_from_im").snapshot.node("publish").title == (
        "Publish decision"
    )
    confirm_command = preview_reply.splitlines()[-1]

    store.command_claims = [
        IMCommandClaim(
            signal(confirm_command, event_id="event_edit_confirm"),
            "edit-confirm-token",
            1,
        )
    ]
    confirm_report = worker.run_once()

    assert confirm_report.processed == 1
    assert store.processed[-1][2]["outcome"] == "graph_edit_confirmed"
    assert "运行中编辑已应用" in store.processed[-1][2]["reply_text"]
    edited = service.get(TENANT, "edit_from_im")
    assert edited.graph_revision == 2
    assert edited.snapshot.node("publish").title == "Archive decision"
    version_after = edited.version

    store.command_claims = [
        IMCommandClaim(
            signal(confirm_command, event_id="event_edit_replay"),
            "edit-replay-token",
            1,
        )
    ]
    replay_report = worker.run_once()

    assert replay_report.processed == 1
    assert "无需重复操作" in store.processed[-1][2]["reply_text"]
    assert service.get(TENANT, "edit_from_im").version == version_after


def test_help_lists_status_command():
    store = MemoryStore()
    store.command_claims = [
        IMCommandClaim(signal("/larkflow help"), "help-token", 1)
    ]
    report = IMCommandWorker(
        store,
        WorkflowService(InMemoryWorkflowRepository(), clock=lambda: NOW),
        TemplateService(InMemoryTemplateStore(), clock=lambda: NOW),
        tenant_id=TENANT,
        worker_id="command_1",
        clock=lambda: NOW,
    ).run_once()

    assert report.processed == 1
    assert (
        "/larkflow start <template_id> [JSON输入] [role=@成员 ...]"
        in store.processed[-1][2]["reply_text"]
    )
    assert "/larkflow status <instance_id>" in store.processed[-1][2]["reply_text"]
    assert "/larkflow list" in store.processed[-1][2]["reply_text"]
    assert (
        "/larkflow restart <instance_id> <node_key>"
        in store.processed[-1][2]["reply_text"]
    )
    assert (
        "/larkflow restart-confirm <preview_id>"
        in store.processed[-1][2]["reply_text"]
    )
    assert (
        "/larkflow restart-all <instance_id>"
        in store.processed[-1][2]["reply_text"]
    )
    assert (
        "/larkflow edit <instance_id> <JSON操作数组>"
        in store.processed[-1][2]["reply_text"]
    )
    assert (
        "/larkflow edit-confirm <preview_id>"
        in store.processed[-1][2]["reply_text"]
    )


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
        "/larkflow status",
        "/larkflow status instance extra",
        "/larkflow list extra",
        "/larkflow restart instance_only",
        "/larkflow restart-all",
        "/larkflow restart-all instance extra",
        "/larkflow restart-confirm",
        "/larkflow restart-confirm preview extra",
        "/larkflow edit instance_only",
        "/larkflow edit instance_1 {}",
        "/larkflow edit-confirm",
        "/larkflow edit-confirm preview extra",
        "/larkflow start quick_review []",
        "/larkflow start quick_review reviewer=@_user_1 reviewer=@_user_2",
        "/larkflow start quick_review Reviewer=@_user_1",
        "/larkflow start quick_review reviewer=person_open_id",
        "/larkflow unsupported",
    ),
)
def test_command_parser_rejects_ambiguous_or_broad_grammar(text):
    with pytest.raises(IMCommandRejected):
        parse_im_command(text)


def test_command_parser_accepts_status():
    assert parse_im_command("/larkflow status instance_1") == (
        "status",
        "instance_1",
        {},
    )


def test_command_parser_accepts_list():
    assert parse_im_command("/larkflow list") == ("list", None, {})


def test_command_parser_accepts_restart_preview_and_confirmation():
    assert parse_im_command("/larkflow restart instance_1 review") == (
        "restart",
        "instance_1",
        {"node_key": "review"},
    )
    assert parse_im_command("/larkflow restart-confirm preview_1") == (
        "restart-confirm",
        "preview_1",
        {},
    )
    assert parse_im_command("/larkflow restart-all instance_1") == (
        "restart-all",
        "instance_1",
        {},
    )


def test_command_parser_accepts_graph_edit_preview_and_confirmation():
    operations = '[{"op":"remove_node","node_key":"review"}]'
    assert parse_im_command(f"/larkflow edit instance_1 {operations}") == (
        "edit",
        "instance_1",
        {
            "operations": [
                {"op": "remove_node", "node_key": "review"}
            ]
        },
    )
    assert parse_im_command("/larkflow edit-confirm preview_1") == (
        "edit-confirm",
        "preview_1",
        {},
    )


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


def test_recovery_reply_settles_the_clicked_card_before_sending_text():
    store = MemoryStore()
    store.reply_claims = [
        IMReplyClaim(
            event_id="event_recovery_1",
            tenant_id=TENANT,
            chat_id="chat_1",
            text="已转为人工接管。",
            idempotency_key="lf-im-recovery",
            claim_token="reply-token",
            attempt_count=1,
            card_update_token="card-update-token",
        )
    ]

    class Sender:
        def __init__(self):
            self.calls = []

        def update_chat_card(self, **kwargs):
            self.calls.append(("update", kwargs))

        def send_chat_message(self, **kwargs):
            self.calls.append(("send", kwargs))
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
    assert [kind for kind, _ in sender.calls] == ["update", "send"]
    assert sender.calls[0][1]["token"] == "card-update-token"
    assert sender.calls[0][1]["card"]["schema"] == "2.0"
