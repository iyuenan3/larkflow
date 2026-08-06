"""Loopback HTTP boundary for the central workflow console."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import ipaddress
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .console import (
    ConsoleReadService,
    ConsoleResourceNotFoundError,
    InvalidConsoleCredentialError,
    StaticConsoleAuthenticator,
)


_INSTANCE_ROUTE = re.compile(
    r"^/console/api/v1/instances/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$"
)
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@dataclass(frozen=True)
class ConsoleHttpResponse:
    status: int
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: Mapping[str, str] = field(default_factory=dict)


class ConsoleHttpApplication:
    """Serve immutable UI assets and authenticated, read-only JSON routes."""

    def __init__(
        self,
        service: ConsoleReadService,
        authenticator: StaticConsoleAuthenticator,
    ) -> None:
        self.service = service
        self.authenticator = authenticator

    def handle(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> ConsoleHttpResponse:
        method = method.upper()
        if method not in {"GET", "HEAD"}:
            return self._error(
                405,
                "method_not_allowed",
                "console routes are read-only",
                headers={"Allow": "GET, HEAD"},
            )
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            return self._error(400, "invalid_request", "absolute URLs are not accepted")
        if parsed.fragment:
            return self._error(400, "invalid_request", "URL fragments are not accepted")

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
                return self._error(400, "invalid_request", "asset query is not accepted")
            name, content_type = asset
            return ConsoleHttpResponse(
                status=200,
                body=_asset(name),
                content_type=content_type,
            )

        if not parsed.path.startswith("/console/api/"):
            return self._error(404, "not_found", "resource does not exist")
        try:
            principal = self.authenticator.authenticate(headers or {})
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
            return self._error(
                401,
                "invalid_credential",
                "console credential is invalid",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except ConsoleResourceNotFoundError:
            return self._error(404, "not_found", "resource does not exist")
        except (TypeError, ValueError) as exc:
            return self._error(400, "invalid_request", str(exc))
        except Exception:
            return self._error(500, "internal_error", "internal server error")

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


def build_console_http_server(
    application: ConsoleHttpApplication,
    *,
    host: str = "127.0.0.1",
    port: int = 8780,
) -> ThreadingHTTPServer:
    """Build a console server that cannot bind to a non-loopback address."""

    _require_loopback(host)
    if port < 0 or port > 65_535:
        raise ValueError("port must be between 0 and 65535")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._send(application.handle("GET", self.path, headers=self.headers))

        def do_HEAD(self) -> None:  # noqa: N802
            self._send(
                application.handle("HEAD", self.path, headers=self.headers),
                include_body=False,
            )

        def _reject_write(self) -> None:
            self.close_connection = True
            self._send(application.handle(self.command, self.path, headers=self.headers))

        do_DELETE = _reject_write  # type: ignore[assignment]  # noqa: N815
        do_OPTIONS = _reject_write  # type: ignore[assignment]  # noqa: N815
        do_PATCH = _reject_write  # type: ignore[assignment]  # noqa: N815
        do_POST = _reject_write  # type: ignore[assignment]  # noqa: N815
        do_PUT = _reject_write  # type: ignore[assignment]  # noqa: N815

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


def _require_loopback(host: str) -> None:
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("console host must be a loopback IP or localhost") from exc
    if not address.is_loopback:
        raise ValueError("console can only bind to loopback")


@lru_cache(maxsize=3)
def _asset(name: str) -> bytes:
    return files("larkflow.workflow.console_assets").joinpath(name).read_bytes()
