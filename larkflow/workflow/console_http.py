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
from .console_rate_limit import ConsoleRequestRateLimiter


LOGGER = logging.getLogger(__name__)


_INSTANCE_ROUTE = re.compile(
    r"^/console/api/v1/instances/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$"
)
_ADMIN_SESSION_PREVIEW_ROUTE = re.compile(
    r"^/console/api/v1/admin/sessions/([0-9a-f]{32})/revoke-preview$"
)
_ADMIN_SESSION_CONFIRM_ROUTE = re.compile(
    r"^/console/api/v1/admin/session-revocations/([0-9a-f]{32})/confirm$"
)
_ADMIN_ACTION_HEADER = "x-larkflow-console-action"
_ADMIN_ACTION_VALUE = "session-governance-v1"
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
    ) -> None:
        self.service = service
        self.authenticator = authenticator
        self.oauth = oauth
        self.admin_service = admin_service
        self.admin_session_service = admin_session_service
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
            "/console/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }.get(parsed.path)
        if asset is not None:
            if parsed.query:
                query = parse_qs(parsed.query, keep_blank_values=True)
                auth_error = query.get("auth_error", [])
                if (
                    asset[0] != "index.html"
                    or set(query) != {"auth_error"}
                    or len(auth_error) != 1
                    or auth_error[0] not in {"access_denied", "login_failed"}
                ):
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
        except ConsoleResourceNotFoundError:
            return self._error(404, "not_found", "resource does not exist")
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
            response = limited or application.handle(
                method,
                self.path,
                headers=self.headers,
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


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next(
        (value.strip() for key, value in headers.items() if key.lower() == name),
        None,
    )


@lru_cache(maxsize=3)
def _asset(name: str) -> bytes:
    return files("larkflow.workflow.console_assets").joinpath(name).read_bytes()
