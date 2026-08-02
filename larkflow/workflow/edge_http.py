"""Small HTTPS-ready HTTP boundary for the Personal Agent Edge proof."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
from typing import Any

from .edge import (
    DeviceRevokedError,
    EdgeControlService,
    EdgeDeviceNotFoundError,
    EdgeError,
    InvalidDeviceCredentialError,
    InvalidPairingCodeError,
    PairingCodeExpiredError,
    PairingCodeUsedError,
)
from .repository import ConcurrentUpdateError, InstanceNotFoundError
from .runner import ClaimExpiredError, InvalidClaimError, StaleAttemptError
from .serde import to_json_value
from .transitions import TransitionError


@dataclass(frozen=True)
class EdgeHttpResponse:
    status: int
    body: Mapping[str, Any] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


class EdgeHttpApplication:
    """Route versioned JSON commands without exposing the repository directly."""

    def __init__(
        self,
        service: EdgeControlService,
        *,
        max_body_bytes: int = 256_000,
        max_wait_seconds: float = 25.0,
        poll_seconds: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if max_wait_seconds < 0:
            raise ValueError("max_wait_seconds cannot be negative")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.service = service
        self.max_body_bytes = max_body_bytes
        self.max_wait_seconds = max_wait_seconds
        self.poll_seconds = poll_seconds
        self.monotonic = monotonic
        self.sleep = sleep

    def handle(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> EdgeHttpResponse:
        if method.upper() != "POST":
            return self._error(405, "method_not_allowed", "POST is required")
        if len(body) > self.max_body_bytes:
            return self._error(413, "payload_too_large", "request body is too large")
        try:
            payload = self._payload(body)
            if path == "/edge/v1/devices/pair":
                return self._pair(payload)
            credential = self._bearer(headers or {})
            if path == "/edge/v1/leases/claim":
                return self._claim(credential, payload)
            if path == "/edge/v1/leases/renew":
                return self._renew(credential, payload)
            if path == "/edge/v1/leases/complete":
                return self._complete(credential, payload)
            if path == "/edge/v1/leases/fail":
                return self._fail(credential, payload)
            return self._error(404, "not_found", "endpoint does not exist")
        except InvalidPairingCodeError:
            return self._error(401, "invalid_pairing_code", "pairing code is invalid")
        except InvalidDeviceCredentialError:
            return self._error(
                401,
                "invalid_device_credential",
                "device credential is invalid",
            )
        except DeviceRevokedError:
            return self._error(403, "device_revoked", "device has been revoked")
        except PairingCodeExpiredError:
            return self._error(409, "pairing_code_expired", "pairing code has expired")
        except PairingCodeUsedError:
            return self._error(409, "pairing_code_used", "pairing code was already used")
        except EdgeDeviceNotFoundError:
            return self._error(404, "device_not_found", "device does not exist")
        except (
            ClaimExpiredError,
            ConcurrentUpdateError,
            InvalidClaimError,
            StaleAttemptError,
            TransitionError,
        ):
            return self._error(409, "stale_lease", "execution lease is no longer current")
        except InstanceNotFoundError:
            return self._error(404, "work_not_found", "work contract does not exist")
        except (KeyError, TypeError, ValueError) as exc:
            return self._error(400, "invalid_request", str(exc))
        except EdgeError as exc:
            return self._error(400, "edge_error", str(exc))
        except Exception:
            return self._error(500, "internal_error", "internal server error")

    def _pair(self, payload: Mapping[str, Any]) -> EdgeHttpResponse:
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, list):
            raise ValueError("capabilities must be an array")
        paired = self.service.pair_device(
            _text(payload, "code"),
            name=_text(payload, "name"),
            capabilities=tuple(_text_value(value, "capability") for value in capabilities),
        )
        return EdgeHttpResponse(
            status=201,
            body={
                "device": {
                    "id": paired.device.id,
                    "tenant_id": paired.device.tenant_id,
                    "person_id": paired.device.person_id,
                    "name": paired.device.name,
                    "capabilities": list(paired.device.capabilities),
                    "status": paired.device.status.value,
                },
                "credential": paired.credential,
            },
        )

    def _claim(
        self,
        credential: str,
        payload: Mapping[str, Any],
    ) -> EdgeHttpResponse:
        raw_wait = payload.get("wait_seconds", 0)
        if isinstance(raw_wait, bool) or not isinstance(raw_wait, (int, float)):
            raise ValueError("wait_seconds must be a number")
        wait_seconds = min(max(float(raw_wait), 0.0), self.max_wait_seconds)
        deadline = self.monotonic() + wait_seconds
        while True:
            lease = self.service.claim(credential)
            if lease is not None:
                return EdgeHttpResponse(
                    status=200,
                    body={"lease": _lease_payload(lease)},
                )
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return EdgeHttpResponse(status=204)
            self.sleep(min(self.poll_seconds, remaining))

    def _renew(
        self,
        credential: str,
        payload: Mapping[str, Any],
    ) -> EdgeHttpResponse:
        fields = _lease_fields(payload)
        expires_at = self.service.renew(credential, **fields)
        return EdgeHttpResponse(
            status=200,
            body={"claim_expires_at": expires_at.isoformat()},
        )

    def _complete(
        self,
        credential: str,
        payload: Mapping[str, Any],
    ) -> EdgeHttpResponse:
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("result must be an object")
        self.service.complete(
            credential,
            **_lease_fields(payload),
            result={str(key): value for key, value in result.items()},
        )
        return EdgeHttpResponse(status=200, body={"accepted": True})

    def _fail(
        self,
        credential: str,
        payload: Mapping[str, Any],
    ) -> EdgeHttpResponse:
        self.service.fail(
            credential,
            **_lease_fields(payload),
            error_code=_text(payload, "error_code"),
            error_message=_text(payload, "error_message"),
        )
        return EdgeHttpResponse(status=200, body={"accepted": True})

    @staticmethod
    def _payload(body: bytes) -> Mapping[str, Any]:
        if not body:
            return {}
        try:
            payload = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("request body must be a JSON object")
        return payload

    @staticmethod
    def _bearer(headers: Mapping[str, str]) -> str:
        authorization = next(
            (
                value
                for key, value in headers.items()
                if key.lower() == "authorization"
            ),
            "",
        )
        scheme, separator, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not credential.strip():
            raise InvalidDeviceCredentialError("device credential is invalid")
        return credential.strip()

    @staticmethod
    def _error(status: int, code: str, message: str) -> EdgeHttpResponse:
        return EdgeHttpResponse(
            status=status,
            body={"error": {"code": code, "message": message}},
        )


def build_edge_http_server(
    application: EdgeHttpApplication,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Build a loopback-default server intended for TLS termination upstream."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            content_length = self.headers.get("Content-Length", "0")
            try:
                size = int(content_length)
            except ValueError:
                self._send(application._error(400, "invalid_request", "invalid body size"))
                return
            if size < 0 or size > application.max_body_bytes:
                self._send(
                    application._error(
                        413,
                        "payload_too_large",
                        "request body is too large",
                    )
                )
                return
            response = application.handle(
                "POST",
                self.path,
                headers=dict(self.headers.items()),
                body=self.rfile.read(size),
            )
            self._send(response)

        def do_GET(self) -> None:  # noqa: N802
            self._send(application.handle("GET", self.path))

        do_DELETE = do_GET  # type: ignore[assignment]  # noqa: N815
        do_OPTIONS = do_GET  # type: ignore[assignment]  # noqa: N815
        do_PATCH = do_GET  # type: ignore[assignment]  # noqa: N815
        do_PUT = do_GET  # type: ignore[assignment]  # noqa: N815

        def _send(self, response: EdgeHttpResponse) -> None:
            encoded = (
                json.dumps(
                    to_json_value(response.body),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                if response.body is not None
                else b""
            )
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if encoded:
                self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def _lease_payload(lease: Any) -> dict[str, Any]:
    request = lease.request
    return {
        "device_id": lease.device_id,
        "tenant_id": request.tenant_id,
        "instance_id": request.instance_id,
        "node_key": request.node_key,
        "attempt_id": request.attempt_id,
        "attempt_no": request.attempt_no,
        "owner_person_id": request.owner_person_id,
        "executor": request.executor.value,
        "work": to_json_value(request.work),
        "input_snapshot": to_json_value(request.input_snapshot),
        "expected_node_version": request.expected_node_version,
        "claim_token": request.claim_token,
        "claim_expires_at": request.claim_expires_at.isoformat(),
        "idempotency_key": request.idempotency_key,
    }


def _lease_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    attempt_no = payload.get("attempt_no")
    expected_node_version = payload.get("expected_node_version")
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int):
        raise ValueError("attempt_no must be an integer")
    if isinstance(expected_node_version, bool) or not isinstance(
        expected_node_version,
        int,
    ):
        raise ValueError("expected_node_version must be an integer")
    return {
        "instance_id": _text(payload, "instance_id"),
        "node_key": _text(payload, "node_key"),
        "attempt_no": attempt_no,
        "expected_node_version": expected_node_version,
        "claim_token": _text(payload, "claim_token"),
    }


def _text(payload: Mapping[str, Any], key: str) -> str:
    return _text_value(payload.get(key), key)


def _text_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")
