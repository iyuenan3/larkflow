"""Feishu projection adapters backed by lark-cli."""
from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

from larkflow.io.cli import LarkCliError, run_cli

from .inbound import ExternalTaskState
from .projection import ExternalTask, TaskProjectionRequest


class CliFeishuTaskProjection:
    """Create and settle Feishu Tasks with stable client idempotency."""

    def __init__(
        self,
        *,
        profile: str,
        identity: str = "bot",
        executable: str = "lark-cli",
        runner: Callable[..., dict[str, Any]] = run_cli,
    ) -> None:
        if not profile.strip():
            raise ValueError("Feishu lark-cli profile is required")
        if identity not in {"bot", "user"}:
            raise ValueError("Feishu identity must be bot or user")
        self.profile = profile
        self.identity = identity
        self.executable = executable
        self.runner = runner

    def create_task(self, request: TaskProjectionRequest) -> ExternalTask:
        data = self._run(
            [
                "task",
                "tasks",
                "create",
                "--user-id-type",
                "open_id",
                "--data",
                json.dumps(
                    {
                        "summary": request.summary,
                        "description": request.description,
                        "client_token": request.idempotency_key,
                        "extra": request.idempotency_key,
                        "mode": 1,
                        "members": [
                            {
                                "id": request.owner_person_id,
                                "type": "user",
                                "role": "assignee",
                            }
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "--as",
                self.identity,
            ]
        )
        task = data.get("task") if isinstance(data.get("task"), dict) else data
        guid = task.get("guid")
        if not isinstance(guid, str) or not guid.strip():
            raise ValueError("lark-cli task create returned no guid")
        url = task.get("url")
        return ExternalTask(
            guid=guid,
            url=url if isinstance(url, str) and url else None,
        )

    def complete_task(self, task_guid: str) -> None:
        if not task_guid.strip():
            raise ValueError("Feishu task guid is required")
        self._run(
            [
                "task",
                "+complete",
                "--task-id",
                task_guid,
                "--as",
                self.identity,
            ]
        )

    def task_exists(self, task_guid: str) -> bool:
        if not task_guid.strip():
            raise ValueError("Feishu task guid is required")
        reader = CliFeishuTaskReader(
            profile=self.profile,
            identity=self.identity,
            executable=self.executable,
            runner=self.runner,
        )
        try:
            task = reader.get_task(task_guid)
        except LarkCliError as exc:
            if str(exc.error.get("code")) == "1470404":
                return False
            raise
        if task.guid != task_guid:
            raise ValueError("lark-cli task get returned a different guid")
        return True

    def _run(self, args: list[str]) -> dict[str, Any]:
        argv = [self.executable, "--profile", self.profile, *args, "--json"]
        return self.runner(argv)


class CliFeishuTaskReader:
    """Read the server-confirmed fields needed to authorize an inbound completion."""

    def __init__(
        self,
        *,
        profile: str,
        identity: str = "bot",
        executable: str = "lark-cli",
        runner: Callable[..., dict[str, Any]] = run_cli,
    ) -> None:
        if not profile.strip():
            raise ValueError("Feishu lark-cli profile is required")
        if identity not in {"bot", "user"}:
            raise ValueError("Feishu identity must be bot or user")
        self.profile = profile
        self.identity = identity
        self.executable = executable
        self.runner = runner

    def get_task(self, task_guid: str) -> ExternalTaskState:
        if not task_guid.strip():
            raise ValueError("Feishu task guid is required")
        data = self.runner(
            [
                self.executable,
                "--profile",
                self.profile,
                "task",
                "tasks",
                "get",
                "--task-guid",
                task_guid,
                "--user-id-type",
                "open_id",
                "--as",
                self.identity,
                "--json",
            ]
        )
        task = data.get("task")
        if not isinstance(task, dict):
            raise ValueError("lark-cli task get returned no task")
        members = task.get("members")
        related = task.get("assignee_related")
        assignees = sorted(
            {
                str(member.get("id"))
                for member in members if isinstance(member, dict)
                and member.get("type") == "user"
                and member.get("role") == "assignee"
                and isinstance(member.get("id"), str)
                and member.get("id")
            }
        ) if isinstance(members, list) else []
        completed = sorted(
            {
                str(member.get("id"))
                for member in related if isinstance(member, dict)
                and isinstance(member.get("id"), str)
                and member.get("id")
                and member.get("completed_at")
            }
        ) if isinstance(related, list) else []
        return ExternalTaskState(
            guid=str(task.get("guid") or ""),
            status=str(task.get("status") or ""),
            mode=_optional_int(task.get("mode")),
            completed_at=_optional_text(task.get("completed_at")),
            source=_optional_int(task.get("source")),
            extra=_optional_text(task.get("extra")),
            assignee_ids=tuple(assignees),
            completed_assignee_ids=tuple(completed),
        )


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
