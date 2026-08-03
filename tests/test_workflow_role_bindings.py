"""Person-selection card tests for multi-owner workflow drafts."""
from __future__ import annotations

from datetime import datetime, timezone
import json

from larkflow.workflow import (
    DirectoryPerson,
    InMemoryTemplateStore,
    InMemoryWorkflowRepository,
    InstanceStatus,
    RoleBindingActionClaim,
    RoleBindingActionInboxBridge,
    RoleBindingActionSignal,
    RoleBindingActionWorker,
    RoleBindingCardClaim,
    RoleBindingCardWorker,
    RoleBindingReplyClaim,
    RoleBindingReplyWorker,
    RoleBindingRequest,
    RoleBindingVerificationWorker,
    TemplateService,
    WorkflowService,
    role_binding_card,
    role_binding_instance_id,
)


NOW = datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)
TENANT = "tenant_roles"


def request(*, candidates=(), card_message_id=None):
    return RoleBindingRequest(
        command_id="command_1",
        tenant_id=TENANT,
        message_id="message_start",
        chat_id="chat_p2p",
        initiator_person_id="person_owner",
        template_id="collaborative_review",
        template_version=1,
        goal="Confirm, draft, and review",
        inputs={"brief": "ready"},
        roles=("requester", "reviewer"),
        candidate_person_ids=tuple(candidates),
        card_message_id=card_message_id,
    )


def action(*, operator="person_owner", form_value=None):
    return RoleBindingActionSignal(
        id="action_1",
        tenant_id=TENANT,
        message_id="card_message_1",
        chat_id="chat_p2p",
        operator_person_id=operator,
        action_tag="button",
        action_name="role_binding_submit",
        form_value=form_value
        or json.dumps(
            {
                "role__requester": "person_owner",
                "role__reviewer": "person_reviewer",
            }
        ),
        update_token="update_token",
        occurred_at=NOW,
        received_at=NOW,
    )


class MemoryRoleStore:
    def __init__(self):
        self.card_claims = []
        self.verification_claims = []
        self.action_claims = []
        self.reply_claims = []
        self.appended = []
        self.card_sent = []
        self.card_failed = []
        self.verified = []
        self.rejected = []
        self.verification_failed = []
        self.processed = []
        self.failed = []
        self.reply_sent = []
        self.reply_failed = []

    def claim_role_binding_cards(self, *_args, **_kwargs):
        claims, self.card_claims = self.card_claims, []
        return tuple(claims)

    def mark_role_binding_card_sent(self, tenant_id, command_id, **kwargs):
        self.card_sent.append((tenant_id, command_id, kwargs))

    def mark_role_binding_card_failed(self, tenant_id, command_id, **kwargs):
        self.card_failed.append((tenant_id, command_id, kwargs))

    def append_role_binding_action(self, event):
        if any(existing.id == event.id for existing in self.appended):
            return False
        self.appended.append(event)
        return True

    def claim_role_binding_verification(self, *_args, **_kwargs):
        claims, self.verification_claims = self.verification_claims, []
        return tuple(claims)

    def mark_role_binding_verified(self, tenant_id, event_id, **kwargs):
        self.verified.append((tenant_id, event_id, kwargs))

    def mark_role_binding_rejected(self, tenant_id, event_id, **kwargs):
        self.rejected.append((tenant_id, event_id, kwargs))

    def mark_role_binding_verification_failed(self, tenant_id, event_id, **kwargs):
        self.verification_failed.append((tenant_id, event_id, kwargs))

    def claim_role_binding_actions(self, *_args, **_kwargs):
        claims, self.action_claims = self.action_claims, []
        return tuple(claims)

    def mark_role_binding_processed(self, tenant_id, event_id, **kwargs):
        self.processed.append((tenant_id, event_id, kwargs))

    def mark_role_binding_failed(self, tenant_id, event_id, **kwargs):
        self.failed.append((tenant_id, event_id, kwargs))

    def claim_role_binding_replies(self, *_args, **_kwargs):
        claims, self.reply_claims = self.reply_claims, []
        return tuple(claims)

    def mark_role_binding_reply_sent(self, tenant_id, event_id, **kwargs):
        self.reply_sent.append((tenant_id, event_id, kwargs))

    def mark_role_binding_reply_failed(self, tenant_id, event_id, **kwargs):
        self.reply_failed.append((tenant_id, event_id, kwargs))


class Directory:
    def __init__(self, people=("person_owner", "person_reviewer")):
        self.people = tuple(people)
        self.get_calls = []

    def list_candidate_people(self, tenant_id, *, limit):
        assert tenant_id == TENANT
        assert limit == 100
        return tuple(DirectoryPerson(person_id=item, active=True) for item in self.people)

    def get_person(self, tenant_id, person_id):
        assert tenant_id == TENANT
        self.get_calls.append(person_id)
        return DirectoryPerson(person_id=person_id, active=person_id in self.people)


class Sender:
    def __init__(self):
        self.cards = []
        self.updates = []
        self.messages = []

    def send_chat_card(self, **kwargs):
        self.cards.append(kwargs)
        return "card_message_1"

    def update_chat_card(self, **kwargs):
        self.updates.append(kwargs)

    def send_chat_message(self, **kwargs):
        self.messages.append(kwargs)
        return "reply_message_1"


def template_document():
    work = {
        "objective": "Complete the step",
        "inputs": ["instance_inputs.brief"],
        "outputs": [{"id": "result", "type": "data"}],
        "acceptance": ["A result exists"],
    }
    return {
        "schema_version": "0.2",
        "template": {
            "id": "collaborative_review",
            "version": 1,
            "name": "Collaborative review",
            "status": "draft",
            "locked": True,
        },
        "goal": "Confirm, draft, and review",
        "parameters": {"brief": {"type": "text", "required": True}},
        "nodes": [
            {
                "id": "confirm",
                "title": "Confirm brief",
                "owner_role": "requester",
                "executor": "human",
                "deps": [],
                "work": work,
            },
            {
                "id": "review",
                "title": "Review result",
                "owner_role": "reviewer",
                "executor": "human",
                "deps": ["confirm"],
                "work": {**work, "inputs": ["dependencies.confirm"]},
            },
        ],
    }


def test_card_bridge_accepts_only_the_named_role_binding_form():
    store = MemoryRoleStore()
    bridge = RoleBindingActionInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)
    payload = {
        "event_id": "action_1",
        "message_id": "card_message_1",
        "chat_id": "chat_p2p",
        "operator_id": "person_owner",
        "action_tag": "button",
        "action_name": "role_binding_submit",
        "form_value": '{"role__requester":"person_owner"}',
        "token": "update_token",
        "timestamp": "1785739200000",
    }

    assert bridge("card.action.trigger", payload) is True
    assert bridge("card.action.trigger", payload) is False
    assert bridge("card.action.trigger", {**payload, "action_name": "other"}) is False
    assert store.appended[0].operator_person_id == "person_owner"
    assert store.appended[0].occurred_at == datetime(
        2026, 8, 3, 6, 40, tzinfo=timezone.utc
    )


def test_card_bridge_accepts_real_microsecond_callback_timestamp():
    store = MemoryRoleStore()
    bridge = RoleBindingActionInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)

    assert bridge(
        "card.action.trigger",
        {
            "event_id": "action_microseconds",
            "message_id": "card_message_1",
            "chat_id": "chat_p2p",
            "operator_id": "person_owner",
            "action_tag": "button",
            "action_name": "role_binding_submit",
            "form_value": '{"role__requester":"person_owner"}',
            "token": "update_token",
            # Captured from the real lark-cli callback shape.
            "timestamp": "1785001477632461",
        },
    ) is True
    assert store.appended[0].occurred_at == datetime(
        2026, 7, 25, 17, 44, 37, 632461, tzinfo=timezone.utc
    )


def test_card_bridge_falls_back_for_out_of_range_callback_timestamp():
    store = MemoryRoleStore()
    bridge = RoleBindingActionInboxBridge(store, tenant_id=TENANT, clock=lambda: NOW)

    assert bridge(
        "card.action.trigger",
        {
            "event_id": "action_out_of_range",
            "message_id": "card_message_1",
            "chat_id": "chat_p2p",
            "operator_id": "person_owner",
            "action_tag": "button",
            "action_name": "role_binding_submit",
            "form_value": '{"role__requester":"person_owner"}',
            "token": "update_token",
            "timestamp": "999999999999999999999999999999999999",
        },
    ) is True
    assert store.appended[0].occurred_at == NOW


def test_card_worker_freezes_candidates_and_projects_card_2():
    store = MemoryRoleStore()
    store.card_claims = [RoleBindingCardClaim(request(), "card-token", 1)]
    sender = Sender()

    report = RoleBindingCardWorker(
        store,
        Directory(),
        sender,
        tenant_id=TENANT,
        worker_id="card_worker",
        clock=lambda: NOW,
    ).run_once()

    assert report.sent == 1
    assert store.card_sent[0][2]["candidate_person_ids"] == (
        "person_owner",
        "person_reviewer",
    )
    card = sender.cards[0]["card"]
    assert card["schema"] == "2.0"
    form = card["body"]["elements"][1]
    assert form["tag"] == "form"
    assert form["elements"][-1]["form_action_type"] == "submit"
    assert "behaviors" not in form["elements"][-1]


def test_verification_trusts_operator_envelope_and_revalidates_people():
    store = MemoryRoleStore()
    req = request(
        candidates=("person_owner", "person_reviewer"),
        card_message_id="card_message_1",
    )
    store.verification_claims = [
        RoleBindingActionClaim(action(), req, "verify-token", 1),
        RoleBindingActionClaim(
            action(operator="person_reviewer"),
            req,
            "verify-token",
            1,
        ),
    ]

    report = RoleBindingVerificationWorker(
        store,
        Directory(),
        tenant_id=TENANT,
        worker_id="verify_worker",
        clock=lambda: NOW,
    ).run_once()

    assert report.verified == 1
    assert report.rejected == 1
    assert store.verified[0][2]["owner_bindings"] == {
        "requester": "person_owner",
        "reviewer": "person_reviewer",
    }
    assert store.rejected[0][2]["reply_text"] is None


def test_verified_binding_creates_one_frozen_draft_and_queues_reply():
    repository = InMemoryWorkflowRepository()
    templates = TemplateService(InMemoryTemplateStore(), clock=lambda: NOW)
    templates.create_template(
        tenant_id=TENANT,
        actor_person_id="person_admin",
        document=template_document(),
    )
    templates.enable(TENANT, "collaborative_review", actor_person_id="person_admin")
    service = WorkflowService(repository, clock=lambda: NOW)
    store = MemoryRoleStore()
    bindings = {"requester": "person_owner", "reviewer": "person_reviewer"}
    store.action_claims = [
        RoleBindingActionClaim(
            action(),
            request(
                candidates=("person_owner", "person_reviewer"),
                card_message_id="card_message_1",
            ),
            "action-token",
            1,
            owner_bindings=bindings,
        )
    ]

    report = RoleBindingActionWorker(
        store,
        service,
        templates,
        tenant_id=TENANT,
        worker_id="action_worker",
        clock=lambda: NOW,
    ).run_once()

    assert report.processed == 1
    instance_id = role_binding_instance_id(TENANT, "message_start")
    draft = service.get(TENANT, instance_id)
    assert draft.status == InstanceStatus.DRAFT
    assert draft.snapshot.node("confirm").owner_person_id == "person_owner"
    assert draft.snapshot.node("review").owner_person_id == "person_reviewer"
    assert store.processed[0][2]["instance_id"] == instance_id
    assert f"/larkflow confirm {instance_id}" in store.processed[0][2]["reply_text"]


def test_reply_worker_settles_card_and_sends_stable_text_reply():
    store = MemoryRoleStore()
    req = request(
        candidates=("person_owner", "person_reviewer"),
        card_message_id="card_message_1",
    )
    bindings = {"requester": "person_owner", "reviewer": "person_reviewer"}
    store.reply_claims = [
        RoleBindingReplyClaim(
            action=action(),
            request=req,
            owner_bindings=bindings,
            instance_id="im_instance",
            text="draft ready",
            claim_token="reply-token",
            attempt_count=1,
        )
    ]
    sender = Sender()

    report = RoleBindingReplyWorker(
        store,
        sender,
        tenant_id=TENANT,
        worker_id="reply_worker",
        clock=lambda: NOW,
    ).run_once()

    assert report.sent == 1
    assert sender.updates[0]["card"]["header"]["template"] == "green"
    assert sender.updates[0]["card"]["config"]["update_multi"] is True
    assert sender.messages[0]["text"] == "draft ready"
    assert sender.messages[0]["idempotency_key"].startswith("lf-role-reply-")
    assert store.reply_sent[0][2]["external_id"] == "reply_message_1"


def test_reply_worker_reports_card_update_error_without_losing_text_reply():
    class FailingCardSender(Sender):
        def update_chat_card(self, **kwargs):
            self.updates.append(kwargs)
            raise RuntimeError("Feishu card update rejected")

    store = MemoryRoleStore()
    store.reply_claims = [
        RoleBindingReplyClaim(
            action=action(),
            request=request(
                candidates=("person_owner", "person_reviewer"),
                card_message_id="card_message_1",
            ),
            owner_bindings={
                "requester": "person_owner",
                "reviewer": "person_reviewer",
            },
            instance_id="im_instance",
            text="draft ready",
            claim_token="reply-token",
            attempt_count=1,
        )
    ]
    sender = FailingCardSender()

    report = RoleBindingReplyWorker(
        store,
        sender,
        tenant_id=TENANT,
        worker_id="reply_worker",
        clock=lambda: NOW,
    ).run_once()

    assert report.sent == 1
    assert report.failed == 0
    assert report.card_updates_failed == 1
    assert report.errors == (
        "action_1: card update RuntimeError: Feishu card update rejected",
    )
    assert store.reply_sent[0][2]["external_id"] == "reply_message_1"
    assert store.reply_failed == []


def test_role_binding_card_requires_a_bounded_candidate_snapshot():
    try:
        role_binding_card(request(), ())
    except ValueError as exc:
        assert "requires candidates" in str(exc)
    else:
        raise AssertionError("empty candidates must be rejected")


def test_settled_role_binding_card_never_combines_required_and_disabled():
    req = request(candidates=("person_owner", "person_reviewer"))

    editable = role_binding_card(req, req.candidate_person_ids)
    settled = role_binding_card(
        req,
        req.candidate_person_ids,
        owner_bindings={
            "requester": "person_owner",
            "reviewer": "person_reviewer",
        },
        settled_instance_id="im_instance",
    )

    editable_form = editable["body"]["elements"][1]
    settled_form = settled["body"]["elements"][1]
    editable_selectors = [
        element
        for element in editable_form["elements"]
        if element["tag"] == "select_person"
    ]
    settled_selectors = [
        element
        for element in settled_form["elements"]
        if element["tag"] == "select_person"
    ]

    assert all(element["required"] is True for element in editable_selectors)
    assert all(element["disabled"] is False for element in editable_selectors)
    assert all("required" not in element for element in settled_selectors)
    assert all(element["disabled"] is True for element in settled_selectors)
