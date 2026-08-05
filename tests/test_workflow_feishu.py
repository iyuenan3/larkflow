"""Feishu task projection adapter contract tests."""
from __future__ import annotations

import json

import pytest

from larkflow.io.cli import LarkCliError
from larkflow.workflow import (
    CliFeishuDocumentProjection,
    CliFeishuMessageProjection,
    CliFeishuTaskProjection,
    CliFeishuTaskReader,
    DocumentProjectionRequest,
    MessageProjectionRequest,
    TaskProjectionRequest,
)


def request() -> TaskProjectionRequest:
    return TaskProjectionRequest(
        tenant_id="tenant_1",
        instance_id="instance_1",
        node_key="approve",
        node_instance_id="node_1",
        attempt_no=1,
        owner_person_id="person_1",
        summary="Approve brief",
        description="Review and approve",
        idempotency_key="lf-idempotency",
    )


def test_cli_adapter_uses_guid_and_stable_idempotency_key():
    calls = []

    def runner(argv):
        calls.append(argv)
        return {"task": {"guid": "task-guid", "url": "https://example.invalid/task"}}

    adapter = CliFeishuTaskProjection(profile="dev", runner=runner)
    task = adapter.create_task(request())

    assert task.guid == "task-guid"
    assert task.url == "https://example.invalid/task"
    argv = calls[0]
    assert argv[:8] == [
        "lark-cli",
        "--profile",
        "dev",
        "task",
        "tasks",
        "create",
        "--user-id-type",
        "open_id",
    ]
    assert argv[-3:] == ["--as", "bot", "--json"]
    payload = __import__("json").loads(argv[argv.index("--data") + 1])
    assert payload == {
        "summary": "Approve brief",
        "description": "Review and approve",
        "client_token": "lf-idempotency",
        "extra": "lf-idempotency",
        "mode": 1,
        "members": [
            {"id": "person_1", "type": "user", "role": "assignee"}
        ],
    }


def test_cli_adapter_completes_by_task_guid():
    calls = []
    adapter = CliFeishuTaskProjection(
        profile="dev",
        identity="user",
        runner=lambda argv: calls.append(argv) or {"already_completed": True},
    )

    adapter.complete_task("task-guid")

    assert calls[0][-7:] == [
        "task",
        "+complete",
        "--task-id",
        "task-guid",
        "--as",
        "user",
        "--json",
    ]


def test_cli_adapter_rejects_create_without_guid():
    adapter = CliFeishuTaskProjection(profile="dev", runner=lambda argv: {})

    with pytest.raises(ValueError, match="no guid"):
        adapter.create_task(request())


def test_cli_adapter_distinguishes_missing_tasks_from_other_read_failures():
    missing = CliFeishuTaskProjection(
        profile="dev",
        runner=lambda _argv: (_ for _ in ()).throw(
            LarkCliError("missing", error={"code": 1470404})
        ),
    )
    forbidden = CliFeishuTaskProjection(
        profile="dev",
        runner=lambda _argv: (_ for _ in ()).throw(
            LarkCliError("forbidden", error={"code": 1470403})
        ),
    )

    assert missing.task_exists("task-guid") is False
    with pytest.raises(LarkCliError, match="forbidden"):
        forbidden.task_exists("task-guid")


def test_cli_adapter_confirms_the_expected_task_guid_exists():
    adapter = CliFeishuTaskProjection(
        profile="dev",
        runner=lambda _argv: {
            "task": {
                "guid": "task-guid",
                "status": "todo",
                "mode": 1,
                "members": [],
                "assignee_related": [],
            }
        },
    )

    assert adapter.task_exists("task-guid") is True


def test_cli_task_reader_extracts_only_authorization_fields():
    calls = []
    reader = CliFeishuTaskReader(
        profile="dev",
        runner=lambda argv: calls.append(argv) or {
            "task": {
                "guid": "task-guid",
                "status": "done",
                "mode": 1,
                "completed_at": "123",
                "source": 6,
                "extra": "lf-idempotency",
                "members": [
                    {"id": "person_1", "type": "user", "role": "assignee"},
                    {"id": "person_2", "type": "user", "role": "follower"},
                ],
                "assignee_related": [
                    {"id": "person_1", "completed_at": "123"}
                ],
            }
        },
    )

    task = reader.get_task("task-guid")

    assert task.assignee_ids == ("person_1",)
    assert task.completed_assignee_ids == ("person_1",)
    assert task.extra == "lf-idempotency"
    assert calls[0][-7:] == [
        "--task-guid",
        "task-guid",
        "--user-id-type",
        "open_id",
        "--as",
        "bot",
        "--json",
    ]


def test_cli_message_projection_sends_as_bot_with_stable_key():
    calls = []
    adapter = CliFeishuMessageProjection(
        profile="dev",
        runner=lambda argv: calls.append(argv) or {"message_id": "message_1"},
    )

    message = adapter.send_message(
        MessageProjectionRequest(
            recipient_person_id="person_1",
            text="Node completed",
            idempotency_key="lf-message-key",
        )
    )

    assert message.message_id == "message_1"
    assert calls[0] == [
        "lark-cli",
        "--profile",
        "dev",
        "im",
        "+messages-send",
        "--user-id",
        "person_1",
        "--text",
        "Node completed",
        "--idempotency-key",
        "lf-message-key",
        "--as",
        "bot",
        "--json",
    ]


def test_cli_message_projection_sends_direct_card_as_bot():
    calls = []
    adapter = CliFeishuMessageProjection(
        profile="dev",
        runner=lambda argv: calls.append(argv) or {"message_id": "failure_card_1"},
    )
    card = {
        "schema": "2.0",
        "body": {"elements": [{"tag": "markdown", "content": "failed"}]},
    }

    message = adapter.send_message(
        MessageProjectionRequest(
            recipient_person_id="person_1",
            text="Agent failed",
            idempotency_key="lf-failure-card",
            card=card,
        )
    )

    assert message.message_id == "failure_card_1"
    assert calls[0][5:9] == [
        "--user-id",
        "person_1",
        "--msg-type",
        "interactive",
    ]
    assert calls[0][-5:] == [
        "--idempotency-key",
        "lf-failure-card",
        "--as",
        "bot",
        "--json",
    ]


def test_cli_message_projection_sends_and_updates_card_2_as_bot():
    calls = []
    adapter = CliFeishuMessageProjection(
        profile="dev",
        runner=lambda argv: calls.append(argv) or {"message_id": "card_1"},
    )
    card = {
        "schema": "2.0",
        "body": {"elements": [{"tag": "markdown", "content": "ready"}]},
    }

    message_id = adapter.send_chat_card(
        chat_id="chat_1",
        card=card,
        idempotency_key="lf-card-key",
    )
    adapter.update_chat_card(token="update-token", card=card)

    assert message_id == "card_1"
    assert calls[0][3:8] == [
        "im",
        "+messages-send",
        "--chat-id",
        "chat_1",
        "--msg-type",
    ]
    assert calls[0][calls[0].index("--content") + 1].startswith('{"schema":"2.0"')
    assert calls[1][3:7] == [
        "api",
        "POST",
        "/open-apis/interactive/v1/card/update",
        "--data",
    ]
    assert '"token":"update-token"' in calls[1][7]


def test_cli_message_projection_updates_card_by_message_id_as_bot():
    calls = []
    adapter = CliFeishuMessageProjection(
        profile="dev",
        runner=lambda argv: calls.append(argv) or {},
    )
    card = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {"elements": [{"tag": "markdown", "content": "ready"}]},
    }

    adapter.update_chat_card_message(message_id="om_card_1", card=card)

    assert calls[0][3:7] == [
        "api",
        "PATCH",
        "/open-apis/im/v1/messages/om_card_1",
        "--data",
    ]
    body = json.loads(calls[0][7])
    assert json.loads(body["content"]) == card
    assert calls[0][-3:] == ["--as", "bot", "--json"]


def test_cli_message_projection_requires_multi_update_card_for_message_patch():
    adapter = CliFeishuMessageProjection(
        profile="dev",
        runner=lambda argv: {},
    )

    with pytest.raises(ValueError, match="multi-update Card 2.0"):
        adapter.update_chat_card_message(
            message_id="om_card_1",
            card={
                "schema": "2.0",
                "config": {"update_multi": False},
                "body": {"elements": []},
            },
        )


def test_cli_document_projection_extracts_nested_document_response():
    calls = []
    adapter = CliFeishuDocumentProjection(
        profile="dev",
        runner=lambda argv: calls.append(argv) or {
            "data": {
                "document": {
                    "document_id": "document_1",
                    "url": "https://example.invalid/docx/document_1",
                }
            }
        },
    )

    document = adapter.create_document(
        DocumentProjectionRequest(
            title="Workflow result",
            content_xml="<title>Workflow result</title><p>Done</p>",
        )
    )

    assert document.document_id == "document_1"
    assert document.url == "https://example.invalid/docx/document_1"
    assert calls[0][-3:] == ["--as", "bot", "--json"]
    assert calls[0][calls[0].index("--content") + 1].startswith("<title>")
