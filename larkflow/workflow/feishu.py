"""Feishu projection adapters backed by lark-cli."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from larkflow.io.cli import run_cli

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
                "+create",
                "--summary",
                request.summary,
                "--description",
                request.description,
                "--assignee",
                request.owner_person_id,
                "--idempotency-key",
                request.idempotency_key,
                "--as",
                self.identity,
            ]
        )
        guid = data.get("guid")
        if not isinstance(guid, str) or not guid.strip():
            raise ValueError("lark-cli task create returned no guid")
        url = data.get("url")
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

    def _run(self, args: list[str]) -> dict[str, Any]:
        argv = [self.executable, "--profile", self.profile, *args, "--json"]
        return self.runner(argv)
