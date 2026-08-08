"""Enterprise directory boundary for workflow owners."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
import json
from threading import Lock
import time
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from larkflow.io.cli import run_cli


class DirectoryValidationError(ValueError):
    """Raised when an owner cannot be proven active in the tenant directory."""


@dataclass(frozen=True)
class DirectoryPerson:
    person_id: str
    active: bool
    name: str = ""


class PersonDirectory(Protocol):
    def get_person(self, tenant_id: str, person_id: str) -> DirectoryPerson:
        """Return the current directory state for one tenant-scoped person."""


class CandidateDirectory(PersonDirectory, Protocol):
    def list_candidate_people(
        self,
        tenant_id: str,
        *,
        limit: int,
    ) -> tuple[DirectoryPerson, ...]:
        """Return a bounded snapshot of active people visible to the app."""


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
        name = user.get("name")
        return DirectoryPerson(
            person_id=person_id,
            active=active,
            name=name.strip() if isinstance(name, str) else "",
        )

    def list_candidate_people(
        self,
        tenant_id: str,
        *,
        limit: int,
    ) -> tuple[DirectoryPerson, ...]:
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if limit < 1 or limit > 100:
            raise ValueError("directory candidate limit must be between 1 and 100")

        user_ids: set[str] = set()
        department_ids: set[str] = set()
        page_token: str | None = None
        while True:
            data = self._api_get(
                "/open-apis/contact/v3/scopes",
                {
                    "user_id_type": "open_id",
                    "page_size": 100,
                    **({"page_token": page_token} if page_token else {}),
                },
            )
            user_ids.update(_text_items(data.get("user_ids")))
            department_ids.update(_text_items(data.get("department_ids")))
            if not data.get("has_more"):
                break
            page_token = _required_page_token(data)

        queue = deque(sorted(department_ids))
        visited_departments: set[str] = set()
        while queue:
            department_id = queue.popleft()
            if department_id in visited_departments:
                continue
            visited_departments.add(department_id)
            self._collect_department_users(department_id, user_ids)
            for child_id in self._child_departments(department_id):
                if child_id not in visited_departments:
                    queue.append(child_id)
            if len(user_ids) > limit:
                raise DirectoryValidationError(
                    f"directory exposes more than {limit} card candidates"
                )

        people = []
        for person_id in sorted(user_ids):
            person = self.get_person(tenant_id, person_id)
            if person.active:
                people.append(person)
            if len(people) > limit:
                raise DirectoryValidationError(
                    f"directory exposes more than {limit} active card candidates"
                )
        return tuple(people)

    def _collect_department_users(
        self,
        department_id: str,
        target: set[str],
    ) -> None:
        page_token: str | None = None
        while True:
            data = self._api_get(
                "/open-apis/contact/v3/users/find_by_department",
                {
                    "department_id": department_id,
                    "department_id_type": "open_department_id",
                    "user_id_type": "open_id",
                    "page_size": 100,
                    **({"page_token": page_token} if page_token else {}),
                },
            )
            for item in data.get("items") or ():
                if isinstance(item, dict):
                    person_id = item.get("open_id")
                    if isinstance(person_id, str) and person_id.strip():
                        target.add(person_id)
            if not data.get("has_more"):
                return
            page_token = _required_page_token(data)

    def _child_departments(self, department_id: str) -> tuple[str, ...]:
        children: list[str] = []
        page_token: str | None = None
        while True:
            data = self._api_get(
                "/open-apis/contact/v3/departments/"
                f"{quote(department_id, safe='')}/children",
                {
                    "department_id_type": "open_department_id",
                    "user_id_type": "open_id",
                    "page_size": 50,
                    **({"page_token": page_token} if page_token else {}),
                },
            )
            for item in data.get("items") or ():
                if isinstance(item, dict):
                    child_id = item.get("open_department_id")
                    if isinstance(child_id, str) and child_id.strip():
                        children.append(child_id)
            if not data.get("has_more"):
                return tuple(children)
            page_token = _required_page_token(data)

    def _api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        data = self.runner(
            [
                self.executable,
                "--profile",
                self.profile,
                "api",
                "GET",
                path,
                "--params",
                json.dumps(params, ensure_ascii=False, separators=(",", ":")),
                "--as",
                self.identity,
                "--json",
            ]
        )
        nested = data.get("data")
        return nested if isinstance(nested, dict) else data


class FeishuAppDirectory:
    """Read a bounded tenant directory with the Console app credential."""

    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    API_ORIGIN = "https://open.feishu.cn"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not app_id.strip() or not app_secret.strip():
            raise ValueError("Feishu app directory requires app credentials")
        self.app_id = app_id.strip()
        self._app_secret = app_secret.strip()
        self._client = client
        self._clock = clock
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = Lock()

    def get_person(self, tenant_id: str, person_id: str) -> DirectoryPerson:
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not person_id.strip():
            raise ValueError("person_id is required")
        data = self._api_get(
            f"/open-apis/contact/v3/users/{quote(person_id, safe='')}",
            {
                "user_id_type": "open_id",
                "department_id_type": "open_department_id",
            },
        )
        user = data.get("user")
        if not isinstance(user, dict) or user.get("open_id") != person_id:
            raise DirectoryValidationError("directory returned an invalid person")
        status = user.get("status")
        if not isinstance(status, dict) or not isinstance(
            status.get("is_activated"), bool
        ):
            raise DirectoryValidationError("directory activation status is unavailable")
        inactive_flags = ("is_frozen", "is_resigned", "is_exited", "is_unjoin")
        active = bool(status["is_activated"]) and not any(
            status.get(key) is True for key in inactive_flags
        )
        name = user.get("name")
        return DirectoryPerson(
            person_id=person_id,
            active=active,
            name=name.strip() if isinstance(name, str) else "",
        )

    def list_candidate_people(
        self,
        tenant_id: str,
        *,
        limit: int,
    ) -> tuple[DirectoryPerson, ...]:
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if limit < 1 or limit > 100:
            raise ValueError("directory candidate limit must be between 1 and 100")

        user_ids: set[str] = set()
        department_ids: set[str] = set()
        page_token: str | None = None
        while True:
            data = self._api_get(
                "/open-apis/contact/v3/scopes",
                {
                    "user_id_type": "open_id",
                    "page_size": 100,
                    **({"page_token": page_token} if page_token else {}),
                },
            )
            user_ids.update(_text_items(data.get("user_ids")))
            department_ids.update(_text_items(data.get("department_ids")))
            if not data.get("has_more"):
                break
            page_token = _required_page_token(data)

        queue = deque(sorted(department_ids))
        visited_departments: set[str] = set()
        while queue:
            department_id = queue.popleft()
            if department_id in visited_departments:
                continue
            visited_departments.add(department_id)
            self._collect_department_users(department_id, user_ids)
            for child_id in self._child_departments(department_id):
                if child_id not in visited_departments:
                    queue.append(child_id)
            if len(user_ids) > limit:
                raise DirectoryValidationError(
                    f"directory exposes more than {limit} candidates"
                )

        people = []
        for person_id in sorted(user_ids):
            person = self.get_person(tenant_id, person_id)
            if person.active:
                people.append(person)
            if len(people) > limit:
                raise DirectoryValidationError(
                    f"directory exposes more than {limit} active candidates"
                )
        return tuple(people)

    def _collect_department_users(
        self,
        department_id: str,
        target: set[str],
    ) -> None:
        page_token: str | None = None
        while True:
            data = self._api_get(
                "/open-apis/contact/v3/users/find_by_department",
                {
                    "department_id": department_id,
                    "department_id_type": "open_department_id",
                    "user_id_type": "open_id",
                    "page_size": 100,
                    **({"page_token": page_token} if page_token else {}),
                },
            )
            for item in data.get("items") or ():
                if isinstance(item, dict):
                    person_id = item.get("open_id")
                    if isinstance(person_id, str) and person_id.strip():
                        target.add(person_id)
            if not data.get("has_more"):
                return
            page_token = _required_page_token(data)

    def _child_departments(self, department_id: str) -> tuple[str, ...]:
        children: list[str] = []
        page_token: str | None = None
        while True:
            data = self._api_get(
                "/open-apis/contact/v3/departments/"
                f"{quote(department_id, safe='')}/children",
                {
                    "department_id_type": "open_department_id",
                    "user_id_type": "open_id",
                    "page_size": 50,
                    **({"page_token": page_token} if page_token else {}),
                },
            )
            for item in data.get("items") or ():
                if isinstance(item, dict):
                    child_id = item.get("open_department_id")
                    if isinstance(child_id, str) and child_id.strip():
                        children.append(child_id)
            if not data.get("has_more"):
                return tuple(children)
            page_token = _required_page_token(data)

    def _api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{self.API_ORIGIN}{path}",
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            params=params,
        )
        nested = response.get("data")
        if not isinstance(nested, dict):
            raise DirectoryValidationError("directory response contains no data")
        return nested

    def _tenant_token(self) -> str:
        now = self._clock()
        with self._token_lock:
            if self._token and now < self._token_expires_at:
                return self._token
            document = self._request(
                "POST",
                self.TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self._app_secret},
            )
            token = document.get("tenant_access_token")
            expire = document.get("expire", 7200)
            if not isinstance(token, str) or not token.strip():
                raise DirectoryValidationError("Feishu issued no tenant token")
            if not isinstance(expire, int) or expire < 120:
                expire = 7200
            self._token = token.strip()
            self._token_expires_at = now + expire - 60
            return self._token

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        owned_client = self._client is None
        client = self._client or httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )
        context = client if owned_client else nullcontext(client)
        try:
            with context as active_client:
                response = active_client.request(method, url, **kwargs)
                document = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise DirectoryValidationError("Feishu directory request failed") from exc
        if (
            response.status_code != 200
            or not isinstance(document, dict)
            or document.get("code") not in {0, "0"}
        ):
            raise DirectoryValidationError("Feishu directory rejected the request")
        return document


def _text_items(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _required_page_token(data: dict[str, Any]) -> str:
    token = data.get("page_token")
    if not isinstance(token, str) or not token.strip():
        raise DirectoryValidationError("directory pagination returned no page_token")
    return token


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
