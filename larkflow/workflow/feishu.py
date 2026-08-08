"""Feishu projection adapters backed by lark-cli."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from typing import Any
from urllib.parse import quote

from larkflow.io.cli import LarkCliError, run_cli

from .inbound import ExternalTaskState
from .projection import (
    DocumentProjectionRequest,
    ExternalDocument,
    ExternalMessage,
    ExternalTask,
    MessageProjectionRequest,
    TaskProjectionRequest,
)


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

    def reassign_task(
        self,
        task_guid: str,
        *,
        previous_owner_person_id: str,
        new_owner_person_id: str,
        idempotency_key: str,
    ) -> None:
        if not all(
            value.strip()
            for value in (
                task_guid,
                previous_owner_person_id,
                new_owner_person_id,
                idempotency_key,
            )
        ):
            raise ValueError("Feishu task reassignment requires complete bindings")
        self._run(
            [
                "task",
                "+assign",
                "--task-id",
                task_guid,
                "--remove",
                previous_owner_person_id,
                "--add",
                new_owner_person_id,
                "--idempotency-key",
                idempotency_key,
                "--as",
                self.identity,
            ]
        )

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


class CliFeishuMessageProjection:
    """Send direct and chat messages as the central Feishu application."""

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
        if identity != "bot":
            raise ValueError("Target IM projection requires bot identity")
        self.profile = profile
        self.identity = identity
        self.executable = executable
        self.runner = runner

    def send_message(self, request: MessageProjectionRequest) -> ExternalMessage:
        if request.card is None:
            data = self._send(
                recipient_flag="--user-id",
                recipient=request.recipient_person_id,
                text=request.text,
                idempotency_key=request.idempotency_key,
            )
        else:
            data = self._send_card(
                recipient_flag="--user-id",
                recipient=request.recipient_person_id,
                card=request.card,
                idempotency_key=request.idempotency_key,
            )
        return ExternalMessage(message_id=_message_id(data))

    def send_chat_message(
        self,
        *,
        chat_id: str,
        text: str,
        idempotency_key: str,
    ) -> str:
        data = self._send(
            recipient_flag="--chat-id",
            recipient=chat_id,
            text=text,
            idempotency_key=idempotency_key,
        )
        return _message_id(data)

    def send_chat_card(
        self,
        *,
        chat_id: str,
        card: Mapping[str, Any],
        idempotency_key: str,
    ) -> str:
        data = self._send_card(
            recipient_flag="--chat-id",
            recipient=chat_id,
            card=card,
            idempotency_key=idempotency_key,
        )
        return _message_id(data)

    def update_chat_card(
        self,
        *,
        token: str,
        card: Mapping[str, Any],
    ) -> None:
        if not token.strip() or card.get("schema") != "2.0":
            raise ValueError("Feishu card update token and Card 2.0 body are required")
        self.runner(
            [
                self.executable,
                "--profile",
                self.profile,
                "api",
                "POST",
                "/open-apis/interactive/v1/card/update",
                "--data",
                json.dumps(
                    {"token": token, "card": dict(card)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "--as",
                self.identity,
                "--json",
            ]
        )

    def update_chat_card_message(
        self,
        *,
        message_id: str,
        card: Mapping[str, Any],
    ) -> None:
        message_id = message_id.strip()
        config = card.get("config")
        if (
            not message_id
            or card.get("schema") != "2.0"
            or not isinstance(config, Mapping)
            or config.get("update_multi") is not True
        ):
            raise ValueError(
                "Feishu message_id and multi-update Card 2.0 body are required"
            )
        content = json.dumps(
            dict(card),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.runner(
            [
                self.executable,
                "--profile",
                self.profile,
                "api",
                "PATCH",
                f"/open-apis/im/v1/messages/{quote(message_id, safe='')}",
                "--data",
                json.dumps(
                    {"content": content},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "--as",
                self.identity,
                "--json",
            ]
        )

    def _send(
        self,
        *,
        recipient_flag: str,
        recipient: str,
        text: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not recipient.strip() or not text.strip() or not idempotency_key.strip():
            raise ValueError("Feishu message recipient, text, and key are required")
        return self.runner(
            [
                self.executable,
                "--profile",
                self.profile,
                "im",
                "+messages-send",
                recipient_flag,
                recipient,
                "--text",
                text,
                "--idempotency-key",
                idempotency_key,
                "--as",
                self.identity,
                "--json",
            ]
        )

    def _send_card(
        self,
        *,
        recipient_flag: str,
        recipient: str,
        card: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not recipient.strip() or not idempotency_key.strip():
            raise ValueError("Feishu card recipient and key are required")
        if card.get("schema") != "2.0":
            raise ValueError("Target interactive message must use Card 2.0")
        return self.runner(
            [
                self.executable,
                "--profile",
                self.profile,
                "im",
                "+messages-send",
                recipient_flag,
                recipient,
                "--msg-type",
                "interactive",
                "--content",
                json.dumps(card, ensure_ascii=False, separators=(",", ":")),
                "--idempotency-key",
                idempotency_key,
                "--as",
                self.identity,
                "--json",
            ]
        )


class CliFeishuDocumentProjection:
    """Create a compact completion document as the central application."""

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
        if identity != "bot":
            raise ValueError("Target document projection requires bot identity")
        self.profile = profile
        self.identity = identity
        self.executable = executable
        self.runner = runner

    def create_document(
        self,
        request: DocumentProjectionRequest,
    ) -> ExternalDocument:
        if not request.content_xml.strip():
            raise ValueError("Feishu document content is required")
        data = self.runner(
            [
                self.executable,
                "--profile",
                self.profile,
                "docs",
                "+create",
                "--content",
                request.content_xml,
                "--as",
                self.identity,
                "--json",
            ]
        )
        document = data.get("document")
        if not isinstance(document, dict):
            nested = data.get("data")
            document = nested.get("document") if isinstance(nested, dict) else None
        if not isinstance(document, dict):
            raise ValueError("lark-cli docs create returned no document")
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("lark-cli docs create returned no document_id")
        url = document.get("url")
        return ExternalDocument(
            document_id=document_id,
            url=url if isinstance(url, str) and url else None,
        )


def _message_id(data: dict[str, Any]) -> str:
    message_id = data.get("message_id")
    if not isinstance(message_id, str):
        nested = data.get("data")
        message_id = nested.get("message_id") if isinstance(nested, dict) else None
    if not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("lark-cli message send returned no message_id")
    return message_id


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
