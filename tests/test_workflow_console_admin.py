"""Read-only administrator overview authorization and sanitization tests."""
from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from larkflow.workflow.console import (
    ConsolePrincipal,
    ConsoleReadService,
    ConsoleResourceNotFoundError,
    StaticConsoleAuthenticator,
)
from larkflow.workflow.console_admin import (
    ConsoleAdminReadService,
    ConsoleAdminSnapshot,
    PostgresConsoleAdminRepository,
    QueueLaneSnapshot,
)
from larkflow.workflow.console_cli import _person_id_list
from larkflow.workflow.console_http import ConsoleHttpApplication
from larkflow.workflow.repository import InMemoryWorkflowRepository


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
TENANT = "tenant_admin"
ADMIN = "ou_admin_private"
MEMBER = "ou_member_private"
TOKEN = "console-admin-token-with-at-least-thirty-two-characters"


class FakeAdminRepository:
    def __init__(self) -> None:
        self.calls = []

    def read_admin_snapshot(self, tenant_id, *, now):
        self.calls.append((tenant_id, now))
        return ConsoleAdminSnapshot(
            instance_counts={"running": 2, "done": 5},
            distinct_owners=3,
            active_sessions=2,
            active_session_people=2,
            sessions_expiring_within_hour=1,
            expired_sessions=0,
            queue_lanes=(
                QueueLaneSnapshot(
                    key="outbox",
                    total=9,
                    ready=1,
                    in_flight=1,
                    failed=1,
                    exhausted=0,
                    expired_claims=0,
                    oldest_ready_at=NOW,
                ),
            ),
            applied_migrations=tuple(f"{number:04d}_test" for number in range(1, 22)),
        )


def _service(person_id=ADMIN):
    repository = FakeAdminRepository()
    service = ConsoleAdminReadService(
        repository,
        tenant_id=TENANT,
        allowed_person_ids=(ADMIN,),
        clock=lambda: NOW,
    )
    principal = ConsolePrincipal(TENANT, person_id)
    return service, repository, principal


def test_admin_overview_is_server_authorized_tenant_scoped_and_sanitized():
    service, repository, principal = _service()

    payload = service.overview(principal)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert repository.calls == [(TENANT, NOW)]
    assert payload["scope"] == "current_tenant"
    assert payload["read_only"] is True
    assert payload["workflows"]["total"] == 7
    assert payload["workflows"]["by_status"]["draft"] == 0
    assert payload["sessions"] == {
        "active": 2,
        "active_people": 2,
        "expiring_within_hour": 1,
        "expired_stored": 0,
    }
    assert payload["queues"]["attention_total"] == 1
    assert payload["queues"]["lanes"][0]["oldest_ready_at"] == NOW.isoformat()
    assert ADMIN not in encoded
    assert MEMBER not in encoded
    assert "person_id" not in encoded
    assert "last_error" not in encoded
    assert "payload" not in encoded


def test_non_admin_and_other_tenant_receive_the_same_not_found_boundary():
    service, repository, _ = _service()

    for principal in (
        ConsolePrincipal(TENANT, MEMBER),
        ConsolePrincipal("tenant_other", ADMIN),
    ):
        with pytest.raises(ConsoleResourceNotFoundError):
            service.overview(principal)

    assert repository.calls == []


def test_admin_http_route_is_hidden_from_non_admins_and_reports_role_on_auth():
    service, _, principal = _service()
    application = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        StaticConsoleAuthenticator(TOKEN, principal),
        admin_service=service,
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}

    auth = application.handle("GET", "/console/api/v1/auth", headers=headers)
    overview = application.handle(
        "GET",
        "/console/api/v1/admin/overview",
        headers=headers,
    )
    bad_query = application.handle(
        "GET",
        "/console/api/v1/admin/overview?tenant=other",
        headers=headers,
    )

    assert json.loads(auth.body)["admin"] is True
    assert overview.status == 200
    assert json.loads(overview.body)["read_only"] is True
    assert bad_query.status == 404

    member_service = ConsoleAdminReadService(
        FakeAdminRepository(),
        tenant_id=TENANT,
        allowed_person_ids=(MEMBER,),
        clock=lambda: NOW,
    )
    member_application = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        StaticConsoleAuthenticator(TOKEN, principal),
        admin_service=member_service,
    )
    forbidden = member_application.handle(
        "GET",
        "/console/api/v1/admin/overview",
        headers=headers,
    )
    missing = member_application.handle(
        "GET",
        "/console/api/v1/admin/missing",
        headers=headers,
    )

    assert json.loads(
        member_application.handle(
            "GET",
            "/console/api/v1/auth",
            headers=headers,
        ).body
    )["admin"] is False
    assert forbidden.status == missing.status == 404
    assert forbidden.body == missing.body


def test_postgres_admin_queries_keep_every_aggregate_inside_the_tenant():
    statements = []

    class Cursor:
        def __init__(self, *, rows=None, row=None):
            self.rows = rows or []
            self.row = row

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, parameters=()):
            normalized = " ".join(statement.split())
            statements.append((normalized, parameters))
            if "GROUP BY status" in normalized:
                return Cursor(rows=[{"status": "running", "count": 2}])
            if "count(DISTINCT owner_person_id)" in normalized:
                return Cursor(row={"count": 1})
            if "FROM workflow_console_sessions" in normalized:
                return Cursor(
                    row={
                        "active": 1,
                        "active_people": 1,
                        "expiring_soon": 0,
                        "expired": 0,
                    }
                )
            if "UNION ALL" in normalized:
                return Cursor(
                    rows=[
                        {
                            "lane": key,
                            "total": 0,
                            "ready": 0,
                            "in_flight": 0,
                            "failed": 0,
                            "exhausted": 0,
                            "expired_claims": 0,
                            "oldest_ready_at": None,
                        }
                        for key in (
                            "draft_requests",
                            "inbox",
                            "im_commands",
                            "im_replies",
                            "outbox",
                            "role_actions",
                            "role_progress",
                            "role_replies",
                        )
                    ]
                )
            if "FROM workflow_schema_migrations" in normalized:
                return Cursor(
                    rows=[{"version": "0021_console_session_governance"}]
                )
            raise AssertionError(normalized)

    snapshot = PostgresConsoleAdminRepository(Connection).read_admin_snapshot(
        TENANT,
        now=NOW,
    )

    assert snapshot.instance_counts["running"] == 2
    assert len(snapshot.queue_lanes) == 8
    assert "WHERE tenant_id = %s" in statements[0][0]
    assert statements[0][1] == (TENANT,)
    assert "WHERE tenant_id = %s" in statements[1][0]
    assert statements[1][1] == (TENANT,)
    assert "WHERE tenant_id = %s" in statements[2][0]
    assert statements[2][1][-1] == TENANT
    lane_parameters = statements[3][1]
    assert lane_parameters.count(TENANT) == 8
    assert len(lane_parameters) == 56


def test_admin_allowlist_parser_rejects_ambiguous_values_and_deduplicates():
    assert _person_id_list("", label="admins") == ()
    assert _person_id_list("ou_one, ou_two,ou_one", label="admins") == (
        "ou_one",
        "ou_two",
    )
    with pytest.raises(ValueError, match="empty"):
        _person_id_list("ou_one,,ou_two", label="admins")
    with pytest.raises(ValueError, match="invalid"):
        _person_id_list("ou_one,ou two", label="admins")
