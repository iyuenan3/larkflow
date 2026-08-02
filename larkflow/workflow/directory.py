"""Enterprise directory boundary for workflow owners."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from larkflow.io.cli import run_cli


class DirectoryValidationError(ValueError):
    """Raised when an owner cannot be proven active in the tenant directory."""


@dataclass(frozen=True)
class DirectoryPerson:
    person_id: str
    active: bool


class PersonDirectory(Protocol):
    def get_person(self, tenant_id: str, person_id: str) -> DirectoryPerson:
        """Return the current directory state for one tenant-scoped person."""


class CliFeishuDirectory:
    """Read Feishu directory users through a bot-scoped lark-cli profile."""

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
            raise ValueError("directory validation requires bot identity")
        self.profile = profile
        self.identity = identity
        self.executable = executable
        self.runner = runner

    def get_person(self, tenant_id: str, person_id: str) -> DirectoryPerson:
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not person_id.strip():
            raise ValueError("person_id is required")
        data = self.runner(
            [
                self.executable,
                "--profile",
                self.profile,
                "contact",
                "+get-user",
                "--user-id",
                person_id,
                "--user-id-type",
                "open_id",
                "--as",
                self.identity,
                "--json",
            ]
        )
        user = data.get("user")
        if not isinstance(user, dict):
            nested = data.get("data")
            user = nested.get("user") if isinstance(nested, dict) else None
        if not isinstance(user, dict):
            raise DirectoryValidationError("directory response contains no user")
        returned_id = user.get("open_id")
        if returned_id != person_id:
            raise DirectoryValidationError("directory returned a different person")
        status = user.get("status")
        if not isinstance(status, dict):
            raise DirectoryValidationError("directory response contains no status")
        activated = status.get("is_activated")
        if not isinstance(activated, bool):
            raise DirectoryValidationError("directory activation status is unavailable")
        inactive_flags = ("is_frozen", "is_resigned", "is_exited", "is_unjoin")
        active = activated and not any(status.get(key) is True for key in inactive_flags)
        return DirectoryPerson(person_id=person_id, active=active)


def validate_snapshot_owners(
    directory: PersonDirectory,
    *,
    tenant_id: str,
    instance_owner_person_id: str,
    node_owner_person_ids: tuple[str, ...],
) -> None:
    """Fail closed unless every distinct workflow owner is currently active."""
    people = {instance_owner_person_id, *node_owner_person_ids}
    for person_id in sorted(people):
        person = directory.get_person(tenant_id, person_id)
        if person.person_id != person_id or not person.active:
            raise DirectoryValidationError(
                f"workflow owner is not active in the enterprise directory: {person_id}"
            )
