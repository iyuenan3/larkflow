"""Feishu task projection adapter contract tests."""
from __future__ import annotations

import pytest

from larkflow.workflow import CliFeishuTaskProjection, TaskProjectionRequest


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
        return {"guid": "task-guid", "url": "https://example.invalid/task"}

    adapter = CliFeishuTaskProjection(profile="dev", runner=runner)
    task = adapter.create_task(request())

    assert task.guid == "task-guid"
    assert task.url == "https://example.invalid/task"
    assert calls == [
        [
            "lark-cli",
            "--profile",
            "dev",
            "task",
            "+create",
            "--summary",
            "Approve brief",
            "--description",
            "Review and approve",
            "--assignee",
            "person_1",
            "--idempotency-key",
            "lf-idempotency",
            "--as",
            "bot",
            "--json",
        ]
    ]


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
