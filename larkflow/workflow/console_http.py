"""Loopback HTTP boundary for the central workflow console."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import ipaddress
import json
import logging
import re
import secrets
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from .console import (
    ConsoleAuthentication,
    ConsoleReadService,
    ConsolePrincipal,
    ConsoleResourceNotFoundError,
    InvalidConsoleCredentialError,
)
from .console_auth import (
    ConsoleOAuthAccessDeniedError,
    ConsoleOAuthError,
    FeishuConsoleOAuthFlow,
)
from .console_admin import ConsoleAdminReadService
from .console_admin_sessions import (
    ConsoleAdminSessionConflictError,
    ConsoleAdminSessionPreviewExpiredError,
    ConsoleAdminSessionPreviewStaleError,
    ConsoleAdminSessionService,
)
from .console_actions import ConsoleActionConflictError, ConsoleActionService
from .console_drafts import (
    ConsoleDraftConflictError,
    ConsoleDraftNotFoundError,
    ConsoleDraftService,
)
from .console_attachments import (
    ConsoleAttachmentConflictError,
    ConsoleAttachmentNotFoundError,
    ConsoleAttachmentService,
    MAX_ATTACHMENT_UPLOAD_BODY_BYTES,
)
from .console_rate_limit import ConsoleRequestRateLimiter
from .console_tasks import (
    ConsoleTaskConflictError,
    ConsoleTaskNotFoundError,
    ConsoleTaskService,
)


LOGGER = logging.getLogger(__name__)


_INSTANCE_ROUTE = re.compile(
    r"^/console/api/v1/instances/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$"
)
_INSTANCE_ACTION_ROUTE = re.compile(
    r"^/console/api/v1/instances/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/"
    r"(confirm|pause|resume|cancel-preview|restart-preview)$"
)
_CANCEL_CONFIRM_ROUTE = re.compile(
    r"^/console/api/v1/instances/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/"
    r"cancel-confirm/([0-9]{1,19})$"
)
_NODE_RESTART_PREVIEW_ROUTE = re.compile(
    r"^/console/api/v1/instances/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/"
    r"nodes/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/restart-preview$"
)
_RESTART_CONFIRM_ROUTE = re.compile(
    r"^/console/api/v1/restart-previews/"
    r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/confirm$"
)
_GRAPH_EDIT_PREVIEW_ROUTE = re.compile(
    r"^/console/api/v1/instances/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/"
    r"graph-edit-preview$"
)
_GRAPH_EDIT_CONFIRM_ROUTE = re.compile(
    r"^/console/api/v1/graph-edit-previews/"
    r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/confirm$"
)
_TASK_ROUTE = re.compile(
    r"^/console/api/v1/tasks/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/"
    r"nodes/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$"
)
_TASK_ACTION_ROUTE = re.compile(
    r"^/console/api/v1/tasks/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/"
    r"nodes/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/(submit|transfer|decision)$"
)
_DRAFT_ROUTE = re.compile(
    r"^/console/api/v1/drafts/([0-9a-f]{32})$"
)
_DRAFT_ATTACHMENTS_ROUTE = re.compile(
    r"^/console/api/v1/drafts/([0-9a-f]{32})/attachments$"
)
_DRAFT_ATTACHMENT_REVOKE_ROUTE = re.compile(
    r"^/console/api/v1/drafts/([0-9a-f]{32})/attachments/"
    r"([0-9a-f]{32})/revoke$"
)
_DRAFT_GENERATE_ROUTE = re.compile(
    r"^/console/api/v1/drafts/([0-9a-f]{32})/generate$"
)
_ADMIN_SESSION_PREVIEW_ROUTE = re.compile(
    r"^/console/api/v1/admin/sessions/([0-9a-f]{32})/revoke-preview$"
)
_ADMIN_SESSION_CONFIRM_ROUTE = re.compile(
    r"^/console/api/v1/admin/session-revocations/([0-9a-f]{32})/confirm$"
)
_ADMIN_ACTION_HEADER = "x-larkflow-console-action"
_ADMIN_ACTION_VALUE = "session-governance-v1"
_WORKFLOW_ACTION_VALUE = "workflow-action-v1"
_MAX_TASK_BODY_BYTES = 65_536
_CONSOLE_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "microphone=(), payment=(), usb=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Permitted-Cross-Domain-Policies": "none",
}
_TRUSTED_CLIENT_SOURCE_HEADER = "x-larkflow-client-ip"


class _AdminWriteRequestError(PermissionError):
    pass


class _WorkflowWriteRequestError(PermissionError):
    pass


@dataclass(frozen=True)
class ConsoleHttpResponse:
    status: int
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: Mapping[str, str] = field(default_factory=dict)


class ConsoleAuthenticator(Protocol):
    mode: str

    def authenticate(self, headers: Mapping[str, str]) -> ConsolePrincipal:
        """Resolve request credentials to a server-side principal."""

    def authenticate_context(
        self,
        headers: Mapping[str, str],
    ) -> ConsoleAuthentication:
        """Resolve the principal and opaque current-session reference."""


class ConsoleHttpApplication:
    """Serve immutable UI assets and narrowly scoped authenticated routes."""

    def __init__(
        self,
        service: ConsoleReadService,
        authenticator: ConsoleAuthenticator,
        *,
        oauth: FeishuConsoleOAuthFlow | None = None,
        admin_service: ConsoleAdminReadService | None = None,
        admin_session_service: ConsoleAdminSessionService | None = None,
        action_service: ConsoleActionService | None = None,
        task_service: ConsoleTaskService | None = None,
        draft_service: ConsoleDraftService | None = None,
        attachment_service: ConsoleAttachmentService | None = None,
    ) -> None:
        self.service = service
        self.authenticator = authenticator
        self.oauth = oauth
        self.admin_service = admin_service
        self.admin_session_service = admin_session_service
        self.action_service = action_service
        self.task_service = task_service
        self.draft_service = draft_service
        self.attachment_service = attachment_service
        if authenticator.mode not in {"static", "feishu"}:
            raise ValueError("console authenticator mode is unsupported")
        if (authenticator.mode == "feishu") != (oauth is not None):
            raise ValueError("Feishu authentication requires one OAuth flow")
        if admin_session_service is not None and admin_service is None:
            raise ValueError("admin session governance requires admin overview")

    def handle(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> ConsoleHttpResponse:
        method = method.upper()
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            return self._error(400, "invalid_request", "absolute URLs are not accepted")
        if parsed.fragment:
            return self._error(400, "invalid_request", "URL fragments are not accepted")
        request_headers = headers or {}

        if parsed.path == "/console/api/v1/auth":
            if method not in {"GET", "HEAD"}:
                return self._method_not_allowed("GET, HEAD")
            if parsed.query:
                return self._error(400, "invalid_request", "auth query is not accepted")
            authenticated = False
            admin = False
            try:
                authentication = self.authenticator.authenticate_context(
                    request_headers
                )
                principal = authentication.principal
                authenticated = True
                admin = (
                    self.admin_service is not None
                    and self.admin_service.is_admin(principal)
                )
            except InvalidConsoleCredentialError:
                pass
            return self._json(
                200,
                {
                    "mode": self.authenticator.mode,
                    "authenticated": authenticated,
                    "admin": admin,
                    "login_url": (
                        "/console/auth/login"
                        if self.authenticator.mode == "feishu"
                        else None
                    ),
                    "logout_available": self.authenticator.mode == "feishu",
                    "capabilities": {
                        "attachment_planning": self._attachment_planning_enabled(),
                    },
                },
            )

        if parsed.path == "/console/auth/login":
            if method != "GET":
                return self._method_not_allowed("GET")
            if parsed.query or self.oauth is None:
                return self._error(404, "not_found", "resource does not exist")
            try:
                self.authenticator.authenticate(request_headers)
                return ConsoleHttpResponse(
                    status=302,
                    headers={"Location": "/console/"},
                )
            except InvalidConsoleCredentialError:
                start = self.oauth.begin()
                return ConsoleHttpResponse(
                    status=302,
                    headers={
                        "Location": start.location,
                        "Set-Cookie": start.state_cookie,
                    },
                )

        if parsed.path == "/console/auth/callback":
            if method != "GET":
                return self._method_not_allowed("GET")
            if self.oauth is None:
                return self._error(404, "not_found", "resource does not exist")
            try:
                query = _single_value_query(
                    parsed.query,
                    allowed={"code", "state", "error"},
                )
                if bool(query.get("code")) == bool(query.get("error")):
                    raise ConsoleOAuthError(
                        "OAuth callback requires exactly one code or error"
                    )
                finish = self.oauth.finish(
                    code=query.get("code"),
                    state=query.get("state"),
                    error=query.get("error"),
                    headers=request_headers,
                )
                return ConsoleHttpResponse(
                    status=302,
                    headers={
                        "Location": finish.location,
                        "Set-Cookie": finish.session_cookie,
                    },
                )
            except ConsoleOAuthAccessDeniedError:
                return ConsoleHttpResponse(
                    status=302,
                    headers={"Location": "/console/?auth_error=access_denied"},
                )
            except ConsoleOAuthError as exc:
                LOGGER.warning("Console OAuth callback rejected: %s", exc)
                return ConsoleHttpResponse(
                    status=302,
                    headers={"Location": "/console/?auth_error=login_failed"},
                )
            except ValueError:
                LOGGER.warning("Console OAuth callback rejected: invalid query")
                return ConsoleHttpResponse(
                    status=302,
                    headers={"Location": "/console/?auth_error=login_failed"},
                )

        if parsed.path == "/console/auth/logout":
            if method != "POST":
                return self._method_not_allowed("POST")
            if parsed.query or self.oauth is None:
                return self._error(404, "not_found", "resource does not exist")
            self.oauth.sessions.revoke(request_headers)
            return ConsoleHttpResponse(
                status=204,
                body=b"",
                headers={
                    "Set-Cookie": self.oauth.sessions.clear_session_cookie(),
                },
            )

        if method == "POST" and parsed.path.startswith(
            "/console/api/v1/admin/"
        ):
            return self._handle_admin_write(
                parsed.path,
                parsed.query,
                request_headers,
            )

        if method == "POST" and parsed.path.startswith(
            "/console/api/v1/tasks/"
        ):
            return self._handle_task_write(
                parsed.path,
                parsed.query,
                request_headers,
                body,
            )

        if method == "POST" and parsed.path == "/console/api/v1/drafts":
            return self._handle_draft_write(
                parsed.query,
                request_headers,
                body,
            )

        if method == "POST" and parsed.path.startswith(
            "/console/api/v1/drafts/"
        ):
            return self._handle_attachment_write(
                parsed.path,
                parsed.query,
                request_headers,
                body,
            )

        if method == "POST" and (
            parsed.path.startswith("/console/api/v1/instances/")
            or parsed.path.startswith("/console/api/v1/restart-previews/")
            or parsed.path.startswith("/console/api/v1/graph-edit-previews/")
        ):
            return self._handle_workflow_write(
                parsed.path,
                parsed.query,
                request_headers,
                body,
            )

        if method not in {"GET", "HEAD"}:
            return self._method_not_allowed("GET, HEAD")

        if parsed.path in {"/", "/console"}:
            return ConsoleHttpResponse(
                status=302,
                headers={"Location": "/console/"},
            )
        asset = {
            "/console/": ("index.html", "text/html; charset=utf-8"),
            "/console/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/console/canvas.js": ("canvas.js", "text/javascript; charset=utf-8"),
            "/console/canvas.css": ("canvas.css", "text/css; charset=utf-8"),
            "/console/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }.get(parsed.path)
        if asset is not None:
            if parsed.query:
                query = parse_qs(parsed.query, keep_blank_values=True)
                auth_error = query.get("auth_error", [])
                task_link = (
                    asset[0] == "index.html"
                    and set(query) == {"action", "instance", "node"}
                    and query.get("action") == ["task"]
                    and len(query.get("instance", [])) == 1
                    and len(query.get("node", [])) == 1
                    and _CONSOLE_RESOURCE_ID.fullmatch(query["instance"][0])
                    is not None
                    and _CONSOLE_RESOURCE_ID.fullmatch(query["node"][0])
                    is not None
                )
                auth_error_link = (
                    asset[0] == "index.html"
                    and set(query) == {"auth_error"}
                    and len(auth_error) == 1
                    and auth_error[0] in {"access_denied", "login_failed"}
                )
                if not task_link and not auth_error_link:
                    return self._error(
                        400,
                        "invalid_request",
                        "asset query is not accepted",
                    )
            name, content_type = asset
            return ConsoleHttpResponse(
                status=200,
                body=_asset(name),
                content_type=content_type,
            )

        if not parsed.path.startswith("/console/api/"):
            return self._error(404, "not_found", "resource does not exist")
        try:
            authentication = self.authenticator.authenticate_context(
                request_headers
            )
            principal = authentication.principal
            if parsed.path == "/console/api/v1/admin/overview":
                if parsed.query or self.admin_service is None:
                    raise ConsoleResourceNotFoundError("admin overview")
                return self._json(200, self.admin_service.overview(principal))
            if parsed.path == "/console/api/v1/admin/sessions":
                if self.admin_session_service is None:
                    raise ConsoleResourceNotFoundError("admin sessions")
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"limit"}:
                    raise ValueError("only the limit query parameter is accepted")
                values = query.get("limit", ["100"])
                if len(values) != 1:
                    raise ValueError("limit must be supplied once")
                try:
                    limit = int(values[0])
                except ValueError as exc:
                    raise ValueError("limit must be an integer") from exc
                return self._json(
                    200,
                    self.admin_session_service.list_sessions(
                        principal,
                        current_session_id=authentication.session_id,
                        limit=limit,
                    ),
                )
            if parsed.path == "/console/api/v1/instances":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"limit"}:
                    raise ValueError("only the limit query parameter is accepted")
                values = query.get("limit", ["30"])
                if len(values) != 1:
                    raise ValueError("limit must be supplied once")
                try:
                    limit = int(values[0])
                except ValueError as exc:
                    raise ValueError("limit must be an integer") from exc
                payload = self.service.list_instances(principal, limit=limit)
                return self._json(200, payload)

            if parsed.path == "/console/api/v1/tasks":
                if self.task_service is None:
                    raise ConsoleTaskNotFoundError("tasks")
                limit = _single_limit(parsed.query, default=30)
                return self._json(
                    200,
                    self.task_service.list_tasks(principal, limit=limit),
                )
            if parsed.path == "/console/api/v1/people":
                if self.task_service is None:
                    raise ConsoleTaskNotFoundError("people")
                limit = _single_limit(parsed.query, default=100)
                return self._json(
                    200,
                    self.task_service.list_people(principal, limit=limit),
                )

            if parsed.path == "/console/api/v1/drafts":
                if self.draft_service is None:
                    raise ConsoleDraftNotFoundError("draft requests")
                limit = _single_limit(parsed.query, default=10, maximum=20)
                return self._json(
                    200,
                    self.draft_service.list(principal, limit=limit),
                )

            attachments_match = _DRAFT_ATTACHMENTS_ROUTE.fullmatch(parsed.path)
            if attachments_match is not None and not parsed.query:
                if self.attachment_service is None:
                    raise ConsoleAttachmentNotFoundError("attachments")
                return self._json(
                    200,
                    self.attachment_service.list(
                        principal,
                        attachments_match.group(1),
                    ),
                )

            draft_match = _DRAFT_ROUTE.fullmatch(parsed.path)
            if draft_match is not None and not parsed.query:
                if self.draft_service is None:
                    raise ConsoleDraftNotFoundError("draft request")
                return self._json(
                    200,
                    self.draft_service.get(principal, draft_match.group(1)),
                )

            task_match = _TASK_ROUTE.fullmatch(parsed.path)
            if task_match is not None and not parsed.query:
                if self.task_service is None:
                    raise ConsoleTaskNotFoundError("task")
                return self._json(
                    200,
                    self.task_service.get_task(
                        principal,
                        task_match.group(1),
                        task_match.group(2),
                    ),
                )

            match = _INSTANCE_ROUTE.fullmatch(parsed.path)
            if match is not None and not parsed.query:
                payload = self.service.get_instance(principal, match.group(1))
                return self._json(200, payload)
            return self._error(404, "not_found", "resource does not exist")
        except InvalidConsoleCredentialError:
            challenge = (
                {"WWW-Authenticate": "Bearer"}
                if self.authenticator.mode == "static"
                else {}
            )
            return self._error(
                401,
                "invalid_credential",
                "console credential is invalid",
                headers=challenge,
            )
        except (
            ConsoleResourceNotFoundError,
            ConsoleTaskNotFoundError,
            ConsoleDraftNotFoundError,
            ConsoleAttachmentNotFoundError,
        ):
            return self._error(404, "not_found", "resource does not exist")
        except (
            ConsoleTaskConflictError,
            ConsoleDraftConflictError,
            ConsoleAttachmentConflictError,
        ) as exc:
            return self._error(409, exc.code, str(exc))
        except (TypeError, ValueError) as exc:
            return self._error(400, "invalid_request", str(exc))
        except Exception:
            return self._error(500, "internal_error", "internal server error")

    def _handle_task_write(
        self,
        path: str,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ConsoleHttpResponse:
        try:
            principal = self.authenticator.authenticate_context(headers).principal
            if self.task_service is None:
                raise ConsoleTaskNotFoundError("task actions")
            self._validate_task_write_request(query, headers, body)
            match = _TASK_ACTION_ROUTE.fullmatch(path)
            if match is None:
                raise ConsoleTaskNotFoundError("task action")
            instance_id, node_key, action = match.groups()
            document = _json_object_body(body)
            allowed = {
                "transfer": {
                    "attempt_no",
                    "expected_node_version",
                    "new_owner_person_id",
                },
                "decision": {
                    "attempt_no",
                    "expected_instance_version",
                    "expected_node_version",
                    "decision",
                    "feedback",
                },
            }
            if action == "submit":
                common = {"attempt_no", "expected_node_version"}
                fields = set(document)
                if fields != common | {"content"} and fields != common | {"result"}:
                    raise ValueError("task action fields are invalid")
            elif set(document) != allowed[action]:
                raise ValueError("task action fields are invalid")
            attempt_no = _positive_json_integer(document["attempt_no"], "attempt_no")
            expected_node_version = _nonnegative_json_integer(
                document["expected_node_version"],
                "expected_node_version",
            )
            if action == "submit":
                content = document.get("content")
                result = document.get("result")
                if content is not None and not isinstance(content, str):
                    raise ValueError("content must be a string")
                if result is not None and not isinstance(result, Mapping):
                    raise ValueError("result must be an object")
                return self._json(
                    200,
                    self.task_service.submit(
                        principal,
                        instance_id,
                        node_key,
                        attempt_no=attempt_no,
                        expected_node_version=expected_node_version,
                        content=content,
                        result=result,
                    ),
                )
            if action == "decision":
                expected_instance_version = _nonnegative_json_integer(
                    document["expected_instance_version"],
                    "expected_instance_version",
                )
                decision = document["decision"]
                feedback = document["feedback"]
                if not isinstance(decision, str):
                    raise ValueError("decision must be a string")
                if feedback is not None and not isinstance(feedback, str):
                    raise ValueError("feedback must be a string or null")
                return self._json(
                    200,
                    self.task_service.submit_decision(
                        principal,
                        instance_id,
                        node_key,
                        attempt_no=attempt_no,
                        expected_instance_version=expected_instance_version,
                        expected_node_version=expected_node_version,
                        decision=decision,
                        feedback=feedback,
                    ),
                )
            new_owner_person_id = document["new_owner_person_id"]
            if not isinstance(new_owner_person_id, str):
                raise ValueError("new_owner_person_id must be a string")
            return self._json(
                200,
                self.task_service.transfer(
                    principal,
                    instance_id,
                    node_key,
                    attempt_no=attempt_no,
                    expected_node_version=expected_node_version,
                    new_owner_person_id=new_owner_person_id,
                ),
            )
        except InvalidConsoleCredentialError:
            challenge = (
                {"WWW-Authenticate": "Bearer"}
                if self.authenticator.mode == "static"
                else {}
            )
            return self._error(
                401,
                "invalid_credential",
                "console credential is invalid",
                headers=challenge,
            )
        except ConsoleTaskNotFoundError:
            return self._error(404, "not_found", "resource does not exist")
        except _WorkflowWriteRequestError:
            return self._error(
                403,
                "request_rejected",
                "workflow action request was rejected",
            )
        except ConsoleTaskConflictError as exc:
            return self._error(409, exc.code, str(exc))
        except (TypeError, ValueError) as exc:
            return self._error(400, "invalid_request", str(exc))
        except Exception:
            return self._error(500, "internal_error", "internal server error")

    def _handle_draft_write(
        self,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ConsoleHttpResponse:
        try:
            principal = self.authenticator.authenticate_context(headers).principal
            if self.draft_service is None:
                raise ConsoleDraftNotFoundError("draft requests")
            self._validate_draft_write_request(query, headers, body)
            document = _json_object_body(body)
            required_fields = {
                "request_id",
                "brief",
                "context",
                "collaborator_person_id",
            }
            if not required_fields <= set(document) or not set(document) <= {
                *required_fields,
                "defer_generation",
            }:
                raise ValueError("draft request fields are invalid")
            collaborator = document["collaborator_person_id"]
            if collaborator is not None and not isinstance(collaborator, str):
                raise ValueError("collaborator_person_id must be a string or null")
            defer_generation = document.get("defer_generation", False)
            if not isinstance(defer_generation, bool):
                raise ValueError("defer_generation must be a boolean")
            if defer_generation and not self._attachment_planning_enabled():
                raise ConsoleDraftConflictError(
                    "attachment_planning_unavailable",
                    "当前部署未启用项目资料规划。",
                )
            return self._json(
                202,
                self.draft_service.create(
                    principal,
                    request_id=document["request_id"],
                    brief=document["brief"],
                    context=document["context"],
                    collaborator_person_id=collaborator,
                    defer_generation=defer_generation,
                ),
            )
        except InvalidConsoleCredentialError:
            challenge = (
                {"WWW-Authenticate": "Bearer"}
                if self.authenticator.mode == "static"
                else {}
            )
            return self._error(
                401,
                "invalid_credential",
                "console credential is invalid",
                headers=challenge,
            )
        except ConsoleDraftNotFoundError:
            return self._error(404, "not_found", "resource does not exist")
        except _WorkflowWriteRequestError:
            return self._error(
                403,
                "request_rejected",
                "workflow action request was rejected",
            )
        except ConsoleDraftConflictError as exc:
            return self._error(409, exc.code, str(exc))
        except (TypeError, ValueError) as exc:
            return self._error(400, "invalid_request", str(exc))
        except Exception:
            return self._error(500, "internal_error", "internal server error")

    def _attachment_planning_enabled(self) -> bool:
        return (
            self.attachment_service is not None
            and self.attachment_service.planning_enabled
        )

    def _handle_attachment_write(
        self,
        path: str,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ConsoleHttpResponse:
        try:
            principal = self.authenticator.authenticate_context(headers).principal
            if self.attachment_service is None:
                raise ConsoleAttachmentNotFoundError("attachments")
            upload = _DRAFT_ATTACHMENTS_ROUTE.fullmatch(path)
            revoke = _DRAFT_ATTACHMENT_REVOKE_ROUTE.fullmatch(path)
            generate = _DRAFT_GENERATE_ROUTE.fullmatch(path)
            if upload is not None:
                self._validate_attachment_upload_request(query, headers, body)
                document = _json_object_body(body)
                if set(document) != {"display_filename", "media_type", "content"}:
                    raise ValueError("attachment upload fields are invalid")
                return self._json(
                    201,
                    self.attachment_service.upload(
                        principal,
                        upload.group(1),
                        display_filename=document["display_filename"],
                        media_type=document["media_type"],
                        content=document["content"],
                    ),
                )
            self._validate_workflow_write_request(query, headers, body)
            if revoke is not None:
                return self._json(
                    200,
                    self.attachment_service.revoke(
                        principal,
                        revoke.group(1),
                        revoke.group(2),
                    ),
                )
            if generate is not None:
                return self._json(
                    202,
                    self.attachment_service.generate(principal, generate.group(1)),
                )
            raise ConsoleAttachmentNotFoundError("attachment action")
        except InvalidConsoleCredentialError:
            challenge = (
                {"WWW-Authenticate": "Bearer"}
                if self.authenticator.mode == "static"
                else {}
            )
            return self._error(
                401,
                "invalid_credential",
                "console credential is invalid",
                headers=challenge,
            )
        except (ConsoleAttachmentNotFoundError, ConsoleDraftNotFoundError):
            return self._error(404, "not_found", "resource does not exist")
        except _WorkflowWriteRequestError:
            return self._error(
                403,
                "request_rejected",
                "workflow action request was rejected",
            )
        except ConsoleAttachmentConflictError as exc:
            return self._error(409, exc.code, str(exc))
        except (TypeError, ValueError) as exc:
            return self._error(400, "invalid_request", str(exc))
        except Exception:
            return self._error(500, "internal_error", "internal server error")

    def _handle_admin_write(
        self,
        path: str,
        query: str,
        headers: Mapping[str, str],
    ) -> ConsoleHttpResponse:
        try:
            authentication = self.authenticator.authenticate_context(headers)
            principal = authentication.principal
            if (
                self.admin_session_service is None
                or not self.admin_session_service.authorizer.is_admin(principal)
            ):
                raise ConsoleResourceNotFoundError("admin session governance")
            self._validate_admin_write_request(query, headers)
            preview_match = _ADMIN_SESSION_PREVIEW_ROUTE.fullmatch(path)
            if preview_match is not None:
                return self._json(
                    201,
                    self.admin_session_service.preview_revocation(
                        principal,
                        preview_match.group(1),
                        current_session_id=authentication.session_id,
                    ),
                )
            confirm_match = _ADMIN_SESSION_CONFIRM_ROUTE.fullmatch(path)
            if confirm_match is not None:
                return self._json(
                    200,
                    self.admin_session_service.confirm_revocation(
                        principal,
                        confirm_match.group(1),
                        current_session_id=authentication.session_id,
                    ),
                )
            raise ConsoleResourceNotFoundError("admin session governance")
        except InvalidConsoleCredentialError:
            challenge = (
                {"WWW-Authenticate": "Bearer"}
                if self.authenticator.mode == "static"
                else {}
            )
            return self._error(
                401,
                "invalid_credential",
                "console credential is invalid",
                headers=challenge,
            )
        except ConsoleResourceNotFoundError:
            return self._error(404, "not_found", "resource does not exist")
        except _AdminWriteRequestError:
            return self._error(
                403,
                "request_rejected",
                "admin action request was rejected",
            )
        except ConsoleAdminSessionConflictError as exc:
            return self._error(409, "session_conflict", str(exc))
        except ConsoleAdminSessionPreviewExpiredError:
            return self._error(
                409,
                "preview_expired",
                "session revocation preview expired",
            )
        except ConsoleAdminSessionPreviewStaleError:
            return self._error(
                409,
                "preview_stale",
                "session changed after revocation preview",
            )
        except (TypeError, ValueError) as exc:
            return self._error(400, "invalid_request", str(exc))
        except Exception:
            return self._error(500, "internal_error", "internal server error")

    def _handle_workflow_write(
        self,
        path: str,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ConsoleHttpResponse:
        try:
            authentication = self.authenticator.authenticate_context(headers)
            principal = authentication.principal
            if self.action_service is None:
                raise ConsoleResourceNotFoundError("workflow actions")
            graph_edit_preview = _GRAPH_EDIT_PREVIEW_ROUTE.fullmatch(path)
            self._validate_workflow_write_request(
                query,
                headers,
                body,
                allow_body=graph_edit_preview is not None,
            )

            if graph_edit_preview is not None:
                document = _json_object_body(body)
                if set(document) != {"operations"}:
                    raise ValueError("graph edit preview fields are invalid")
                operations = document["operations"]
                if not isinstance(operations, list):
                    raise ValueError("graph edit operations must be an array")
                return self._json(
                    201,
                    self.action_service.preview_graph_edit(
                        principal,
                        graph_edit_preview.group(1),
                        operations,
                    ),
                )

            node_restart = _NODE_RESTART_PREVIEW_ROUTE.fullmatch(path)
            if node_restart is not None:
                return self._json(
                    201,
                    self.action_service.preview_restart(
                        principal,
                        node_restart.group(1),
                        node_key=node_restart.group(2),
                    ),
                )

            instance_action = _INSTANCE_ACTION_ROUTE.fullmatch(path)
            if instance_action is not None:
                instance_id, action = instance_action.groups()
                if action == "confirm":
                    payload = self.action_service.confirm_draft(
                        principal,
                        instance_id,
                    )
                    return self._json(200, payload)
                if action == "pause":
                    return self._json(
                        200,
                        self.action_service.pause(principal, instance_id),
                    )
                if action == "resume":
                    return self._json(
                        200,
                        self.action_service.resume(principal, instance_id),
                    )
                if action == "cancel-preview":
                    return self._json(
                        200,
                        self.action_service.preview_cancellation(
                            principal,
                            instance_id,
                        ),
                    )
                return self._json(
                    201,
                    self.action_service.preview_restart(
                        principal,
                        instance_id,
                    ),
                )

            cancel_confirm = _CANCEL_CONFIRM_ROUTE.fullmatch(path)
            if cancel_confirm is not None:
                version = int(cancel_confirm.group(2))
                if version > 9_223_372_036_854_775_807:
                    raise ValueError("instance version is out of range")
                return self._json(
                    200,
                    self.action_service.confirm_cancellation(
                        principal,
                        cancel_confirm.group(1),
                        version,
                    ),
                )

            restart_confirm = _RESTART_CONFIRM_ROUTE.fullmatch(path)
            if restart_confirm is not None:
                return self._json(
                    200,
                    self.action_service.confirm_restart(
                        principal,
                        restart_confirm.group(1),
                    ),
                )

            graph_edit_confirm = _GRAPH_EDIT_CONFIRM_ROUTE.fullmatch(path)
            if graph_edit_confirm is not None:
                return self._json(
                    200,
                    self.action_service.confirm_graph_edit(
                        principal,
                        graph_edit_confirm.group(1),
                    ),
                )
            raise ConsoleResourceNotFoundError("workflow action")
        except InvalidConsoleCredentialError:
            challenge = (
                {"WWW-Authenticate": "Bearer"}
                if self.authenticator.mode == "static"
                else {}
            )
            return self._error(
                401,
                "invalid_credential",
                "console credential is invalid",
                headers=challenge,
            )
        except ConsoleResourceNotFoundError:
            return self._error(404, "not_found", "resource does not exist")
        except _WorkflowWriteRequestError:
            return self._error(
                403,
                "request_rejected",
                "workflow action request was rejected",
            )
        except ConsoleActionConflictError as exc:
            return self._error(409, exc.code, str(exc))
        except (TypeError, ValueError) as exc:
            return self._error(400, "invalid_request", str(exc))
        except Exception:
            return self._error(500, "internal_error", "internal server error")

    def _validate_admin_write_request(
        self,
        query: str,
        headers: Mapping[str, str],
    ) -> None:
        if query:
            raise ValueError("admin action query is not accepted")
        content_length = _header(headers, "content-length")
        if content_length not in {None, "", "0"}:
            raise ValueError("admin action body is not accepted")
        if _header(headers, "transfer-encoding") is not None:
            raise ValueError("admin action body is not accepted")
        if not secrets.compare_digest(
            _header(headers, _ADMIN_ACTION_HEADER) or "",
            _ADMIN_ACTION_VALUE,
        ):
            raise _AdminWriteRequestError(
                "admin action request header is invalid"
            )
        if self.authenticator.mode == "feishu":
            if self.oauth is None or not secrets.compare_digest(
                _header(headers, "origin") or "",
                self.oauth.public_base_url,
            ):
                raise _AdminWriteRequestError(
                    "admin action origin is invalid"
                )

    def _validate_workflow_write_request(
        self,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        allow_body: bool = False,
    ) -> None:
        if query:
            raise ValueError("workflow action query is not accepted")
        if _header(headers, "transfer-encoding") is not None:
            raise ValueError("workflow action transfer encoding is not accepted")
        content_length = _header(headers, "content-length")
        if allow_body:
            if content_length is None:
                raise ValueError("workflow action content length is required")
            try:
                parsed_length = int(content_length)
            except ValueError as exc:
                raise ValueError("workflow action content length is invalid") from exc
            if (
                parsed_length < 1
                or parsed_length > _MAX_TASK_BODY_BYTES
                or parsed_length != len(body)
            ):
                raise ValueError("workflow action body size is invalid")
            content_type = (_header(headers, "content-type") or "").lower()
            if content_type not in {
                "application/json",
                "application/json; charset=utf-8",
            }:
                raise ValueError(
                    "workflow action content type must be application/json"
                )
        elif content_length not in {None, "", "0"} or body:
            raise ValueError("workflow action body is not accepted")
        if not secrets.compare_digest(
            _header(headers, _ADMIN_ACTION_HEADER) or "",
            _WORKFLOW_ACTION_VALUE,
        ):
            raise _WorkflowWriteRequestError(
                "workflow action request header is invalid"
            )
        if self.authenticator.mode == "feishu":
            if self.oauth is None or not secrets.compare_digest(
                _header(headers, "origin") or "",
                self.oauth.public_base_url,
            ):
                raise _WorkflowWriteRequestError(
                    "workflow action origin is invalid"
                )

    def _validate_task_write_request(
        self,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        if query:
            raise ValueError("task action query is not accepted")
        if _header(headers, "transfer-encoding") is not None:
            raise ValueError("task action transfer encoding is not accepted")
        raw_length = _header(headers, "content-length")
        if raw_length is None:
            raise ValueError("task action content length is required")
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("task action content length is invalid") from exc
        if (
            content_length < 1
            or content_length > _MAX_TASK_BODY_BYTES
            or content_length != len(body)
        ):
            raise ValueError("task action body size is invalid")
        content_type = (_header(headers, "content-type") or "").lower()
        if content_type not in {
            "application/json",
            "application/json; charset=utf-8",
        }:
            raise ValueError("task action content type must be application/json")
        if not secrets.compare_digest(
            _header(headers, _ADMIN_ACTION_HEADER) or "",
            _WORKFLOW_ACTION_VALUE,
        ):
            raise _WorkflowWriteRequestError(
                "workflow action request header is invalid"
            )
        if self.authenticator.mode == "feishu":
            if self.oauth is None or not secrets.compare_digest(
                _header(headers, "origin") or "",
                self.oauth.public_base_url,
            ):
                raise _WorkflowWriteRequestError(
                    "workflow action origin is invalid"
                )

    def _validate_draft_write_request(
        self,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        self._validate_task_write_request(query, headers, body)

    def _validate_attachment_upload_request(
        self,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        if query:
            raise ValueError("attachment upload query is not accepted")
        if _header(headers, "transfer-encoding") is not None:
            raise ValueError("attachment transfer encoding is not accepted")
        raw_length = _header(headers, "content-length")
        if raw_length is None:
            raise ValueError("attachment content length is required")
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("attachment content length is invalid") from exc
        if (
            content_length < 1
            or content_length > MAX_ATTACHMENT_UPLOAD_BODY_BYTES
            or content_length != len(body)
        ):
            raise ValueError("attachment body size is invalid")
        content_type = (_header(headers, "content-type") or "").lower()
        if content_type not in {
            "application/json",
            "application/json; charset=utf-8",
        }:
            raise ValueError("attachment content type must be application/json")
        if not secrets.compare_digest(
            _header(headers, _ADMIN_ACTION_HEADER) or "",
            _WORKFLOW_ACTION_VALUE,
        ):
            raise _WorkflowWriteRequestError(
                "workflow action request header is invalid"
            )
        if self.authenticator.mode == "feishu":
            if self.oauth is None or not secrets.compare_digest(
                _header(headers, "origin") or "",
                self.oauth.public_base_url,
            ):
                raise _WorkflowWriteRequestError(
                    "workflow action origin is invalid"
                )

    @staticmethod
    def _json(status: int, payload: Mapping[str, Any]) -> ConsoleHttpResponse:
        return ConsoleHttpResponse(
            status=status,
            body=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
        )

    @classmethod
    def _error(
        cls,
        status: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> ConsoleHttpResponse:
        response = cls._json(
            status,
            {"error": {"code": code, "message": message}},
        )
        return ConsoleHttpResponse(
            status=response.status,
            body=response.body,
            content_type=response.content_type,
            headers=headers or {},
        )

    @classmethod
    def _method_not_allowed(cls, allow: str) -> ConsoleHttpResponse:
        return cls._error(
            405,
            "method_not_allowed",
            "method is not allowed for this console route",
            headers={"Allow": allow},
        )


def build_console_http_server(
    application: ConsoleHttpApplication,
    *,
    host: str = "127.0.0.1",
    port: int = 8780,
    rate_limiter: ConsoleRequestRateLimiter | None = None,
) -> ThreadingHTTPServer:
    """Build a console server that cannot bind to a non-loopback address."""

    _require_loopback(host)
    if port < 0 or port > 65_535:
        raise ValueError("port must be between 0 and 65535")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch("HEAD", include_body=False)

        def _handle_write(self) -> None:
            self.close_connection = True
            self._dispatch(self.command)

        do_DELETE = _handle_write  # type: ignore[assignment]  # noqa: N815
        do_OPTIONS = _handle_write  # type: ignore[assignment]  # noqa: N815
        do_PATCH = _handle_write  # type: ignore[assignment]  # noqa: N815
        do_POST = _handle_write  # type: ignore[assignment]  # noqa: N815
        do_PUT = _handle_write  # type: ignore[assignment]  # noqa: N815

        def _dispatch(self, method: str, *, include_body: bool = True) -> None:
            limited = _rate_limit_response(
                rate_limiter,
                method=method,
                target=self.path,
                headers=self.headers,
                peer_address=self.client_address[0],
            )
            body = b""
            if limited is None and method not in {"GET", "HEAD"}:
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    content_length = int(raw_length)
                except ValueError:
                    response = application._error(
                        400,
                        "invalid_request",
                        "content length is invalid",
                    )
                    self._send(response, include_body=include_body)
                    return
                body_limit = _request_body_limit(self.path)
                if content_length < 0 or content_length > body_limit:
                    response = application._error(
                        413,
                        "request_too_large",
                        "request body is too large",
                    )
                    self._send(response, include_body=include_body)
                    return
                if content_length:
                    body = self.rfile.read(content_length)
            response = limited or application.handle(
                method,
                self.path,
                headers=self.headers,
                body=body,
            )
            self._send(response, include_body=include_body)

        def _send(
            self,
            response: ConsoleHttpResponse,
            *,
            include_body: bool = True,
        ) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for key, value in _SECURITY_HEADERS.items():
                self.send_header(key, value)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if include_body and response.body:
                self.wfile.write(response.body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def _rate_limit_response(
    rate_limiter: ConsoleRequestRateLimiter | None,
    *,
    method: str,
    target: str,
    headers: Mapping[str, str],
    peer_address: str,
) -> ConsoleHttpResponse | None:
    if rate_limiter is None:
        return None
    source = peer_address
    if _is_loopback(peer_address):
        source = _header(headers, _TRUSTED_CLIENT_SOURCE_HEADER) or peer_address
    decision = rate_limiter.check(method, target, source)
    if decision.allowed:
        return None
    response = ConsoleHttpApplication._error(
        429,
        "rate_limited",
        "request rate limit exceeded",
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )
    return response


def _request_body_limit(target: str) -> int:
    path = urlsplit(target).path
    if _DRAFT_ATTACHMENTS_ROUTE.fullmatch(path) is not None:
        return MAX_ATTACHMENT_UPLOAD_BODY_BYTES
    return _MAX_TASK_BODY_BYTES


def _require_loopback(host: str) -> None:
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("console host must be a loopback IP or localhost") from exc
    if not address.is_loopback:
        raise ValueError("console can only bind to loopback")


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _single_value_query(
    raw_query: str,
    *,
    allowed: set[str],
) -> dict[str, str]:
    query = parse_qs(raw_query, keep_blank_values=True)
    if set(query) - allowed:
        raise ValueError("OAuth callback contains unsupported parameters")
    values: dict[str, str] = {}
    for key, items in query.items():
        if len(items) != 1:
            raise ValueError("OAuth callback parameters must be supplied once")
        values[key] = items[0]
    return values


def _single_limit(
    raw_query: str,
    *,
    default: int,
    maximum: int = 100,
) -> int:
    query = parse_qs(raw_query, keep_blank_values=True)
    if set(query) - {"limit"}:
        raise ValueError("only the limit query parameter is accepted")
    values = query.get("limit", [str(default)])
    if len(values) != 1:
        raise ValueError("limit must be supplied once")
    try:
        limit = int(values[0])
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if limit < 1 or limit > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return limit


def _json_object_body(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("task action body must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("task action body must be a JSON object")
    return value


def _positive_json_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    if value > 2_147_483_647:
        raise ValueError(f"{field_name} is out of range")
    return value


def _nonnegative_json_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")
    if value > 9_223_372_036_854_775_807:
        raise ValueError(f"{field_name} is out of range")
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next(
        (value.strip() for key, value in headers.items() if key.lower() == name),
        None,
    )


@lru_cache(maxsize=3)
def _asset(name: str) -> bytes:
    return files("larkflow.workflow.console_assets").joinpath(name).read_bytes()
