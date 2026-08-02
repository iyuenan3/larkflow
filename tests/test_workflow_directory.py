"""Enterprise directory validation at the workflow trust boundary."""
from __future__ import annotations

import pytest

from larkflow.workflow.directory import (
    CliFeishuDirectory,
    DirectoryPerson,
    DirectoryValidationError,
)
from larkflow.workflow.model import InstanceSnapshot, NodeSpec
from larkflow.workflow.repository import InMemoryWorkflowRepository
from larkflow.workflow.service import WorkflowService


def snapshot() -> InstanceSnapshot:
    return InstanceSnapshot(
        schema_version="0.2",
        goal="directory validation",
        nodes=(
            NodeSpec(
                key="review",
                title="Review",
                owner_person_id="person_reviewer",
                executor="human",
                work={
                    "objective": "Review the draft",
                    "inputs": [],
                    "outputs": [{"id": "decision", "type": "data", "required": True}],
                    "acceptance": ["A decision exists"],
                },
            ),
        ),
    )


def test_cli_directory_requires_active_status_and_matching_open_id() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> dict[str, object]:
        calls.append(argv)
        return {
            "user": {
                "open_id": "person_owner",
                "status": {"is_activated": True, "is_resigned": False},
            }
        }

    directory = CliFeishuDirectory(profile="dev", runner=runner)
    assert directory.get_person("tenant", "person_owner") == DirectoryPerson(
        person_id="person_owner",
        active=True,
    )
    assert calls[0][-2:] == ["bot", "--json"]


@pytest.mark.parametrize(
    "user",
    [
        {"open_id": "person_other", "status": {"is_activated": True}},
        {"open_id": "person_owner"},
        {"open_id": "person_owner", "status": {"is_activated": False}},
        {
            "open_id": "person_owner",
            "status": {"is_activated": True, "is_frozen": True},
        },
    ],
)
def test_cli_directory_fails_closed_for_unusable_user(user: dict[str, object]) -> None:
    directory = CliFeishuDirectory(
        profile="dev",
        runner=lambda _argv: {"user": user},
    )
    if user.get("open_id") == "person_owner" and isinstance(user.get("status"), dict):
        person = directory.get_person("tenant", "person_owner")
        assert person.active is False
    else:
        with pytest.raises(DirectoryValidationError):
            directory.get_person("tenant", "person_owner")


def test_workflow_rejects_inactive_node_owner_before_persistence() -> None:
    repository = InMemoryWorkflowRepository()

    class Directory:
        def get_person(self, tenant_id: str, person_id: str) -> DirectoryPerson:
            assert tenant_id == "tenant"
            return DirectoryPerson(
                person_id=person_id,
                active=person_id != "person_reviewer",
            )

    service = WorkflowService(repository, directory=Directory())
    with pytest.raises(DirectoryValidationError, match="person_reviewer"):
        service.create_draft(
            instance_id="instance",
            tenant_id="tenant",
            owner_person_id="person_owner",
            actor_person_id="person_owner",
            snapshot=snapshot(),
        )
    with pytest.raises(KeyError):
        repository.get("tenant", "instance")


def test_workflow_validates_each_distinct_owner_once() -> None:
    seen: list[str] = []

    class Directory:
        def get_person(self, tenant_id: str, person_id: str) -> DirectoryPerson:
            seen.append(person_id)
            return DirectoryPerson(person_id=person_id, active=True)

    service = WorkflowService(InMemoryWorkflowRepository(), directory=Directory())
    service.create_draft(
        instance_id="instance",
        tenant_id="tenant",
        owner_person_id="person_reviewer",
        actor_person_id="person_reviewer",
        snapshot=snapshot(),
    )
    assert seen == ["person_reviewer"]
