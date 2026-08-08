"""Enterprise directory validation at the workflow trust boundary."""
from __future__ import annotations

import httpx
import pytest

from larkflow.workflow.directory import (
    CliFeishuDirectory,
    DirectoryPerson,
    DirectoryValidationError,
    FeishuAppDirectory,
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


def test_app_directory_lists_scoped_active_people_with_names() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        if request.url.path.endswith("/contact/v3/scopes"):
            assert request.headers["authorization"] == "Bearer token"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "user_ids": ["person_active", "person_inactive"],
                        "department_ids": [],
                        "has_more": False,
                    },
                },
            )
        person_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "user": {
                        "open_id": person_id,
                        "name": "Active Person" if person_id == "person_active" else "Inactive Person",
                        "status": {"is_activated": person_id == "person_active"},
                    }
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        people = FeishuAppDirectory(
            app_id="app_id",
            app_secret="app_secret",
            client=client,
        ).list_candidate_people("tenant", limit=10)

    assert people == (
        DirectoryPerson(
            person_id="person_active",
            active=True,
            name="Active Person",
        ),
    )
    assert sum(
        request.url.path.endswith("/tenant_access_token/internal")
        for request in requests
    ) == 1


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


def test_cli_directory_expands_authorized_departments_and_revalidates_users() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> dict[str, object]:
        calls.append(argv)
        if "/open-apis/contact/v3/scopes" in argv:
            return {
                "user_ids": ["person_owner"],
                "department_ids": ["od-a"],
                "has_more": False,
            }
        if "/open-apis/contact/v3/departments/od-a/children" in argv:
            return {
                "items": [{"open_department_id": "od-b"}],
                "has_more": False,
            }
        if "/open-apis/contact/v3/departments/od-b/children" in argv:
            return {"items": [], "has_more": False}
        if "/open-apis/contact/v3/users/find_by_department" in argv:
            params = argv[argv.index("--params") + 1]
            person_id = "person_reviewer" if "od-a" in params else "person_writer"
            return {"items": [{"open_id": person_id}], "has_more": False}
        if "+get-user" in argv:
            person_id = argv[argv.index("--user-id") + 1]
            return {
                "user": {
                    "open_id": person_id,
                    "status": {"is_activated": True},
                }
            }
        raise AssertionError(argv)

    people = CliFeishuDirectory(profile="dev", runner=runner).list_candidate_people(
        "tenant",
        limit=10,
    )

    assert tuple(person.person_id for person in people) == (
        "person_owner",
        "person_reviewer",
        "person_writer",
    )
    assert sum("+get-user" in call for call in calls) == 3


def test_cli_directory_rejects_candidate_sets_larger_than_the_card_limit() -> None:
    def runner(argv: list[str]) -> dict[str, object]:
        if "/open-apis/contact/v3/scopes" in argv:
            return {
                "user_ids": ["person_1", "person_2"],
                "department_ids": [],
                "has_more": False,
            }
        person_id = argv[argv.index("--user-id") + 1]
        return {
            "user": {
                "open_id": person_id,
                "status": {"is_activated": True},
            }
        }

    with pytest.raises(DirectoryValidationError, match="more than 1"):
        CliFeishuDirectory(profile="dev", runner=runner).list_candidate_people(
            "tenant",
            limit=1,
        )
