"""Administrative CLI and loopback gateway for Personal Agent Edge Proof v0."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import timedelta
import ipaddress
import json
import os
import signal
import sys
from threading import Event, Thread
from typing import Any

from larkflow.config import load_dotenv

from .edge import EdgeControlService, PERSONAL_READONLY_CAPABILITY
from .edge_http import EdgeHttpApplication, build_edge_http_server
from .edge_postgres import PostgresEdgeStore
from .migrate import postgres_connection_factory, verify_migrations
from .postgres import PostgresWorkflowRepository
from .runner import NodeRunner
from .serde import to_json_value
from .service import WorkflowService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="larkflow-edge-gateway",
        description="Personal Agent Edge Proof v0 control plane",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="dotenv file parsed by larkflow, never sourced by a shell",
    )
    parser.add_argument("--dsn", default=os.environ.get("LARKFLOW_TARGET_DSN"))
    commands = parser.add_subparsers(dest="command", required=True)

    pairing = commands.add_parser(
        "pairing-create",
        help="issue a one-time personal.readonly pairing code",
    )
    pairing.add_argument("--tenant", required=True)
    pairing.add_argument("--person", required=True)
    pairing.add_argument("--actor", required=True)
    pairing.add_argument("--ttl-seconds", type=int, default=600)

    devices = commands.add_parser("devices", help="list tenant Edge devices")
    devices.add_argument("--tenant", required=True)

    revoke = commands.add_parser("revoke", help="revoke one Edge device")
    revoke.add_argument("--tenant", required=True)
    revoke.add_argument("--device", required=True)
    revoke.add_argument("--actor", required=True)
    revoke.add_argument("--reason", required=True)

    serve = commands.add_parser(
        "serve",
        help="serve the private Edge API on a loopback address",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env_file = _preparse_env_file(args)
    load_dotenv(env_file)
    namespace = build_parser().parse_args(args)
    try:
        return _run(namespace)
    except Exception as exc:
        _print(
            {
                "event": "edge_gateway_command_failed",
                "command": namespace.command,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 1


def _run(namespace: argparse.Namespace) -> int:
    dsn = _required(namespace.dsn, "--dsn or LARKFLOW_TARGET_DSN")
    connection_factory = postgres_connection_factory(dsn)
    verify_migrations(connection_factory)
    repository = PostgresWorkflowRepository(connection_factory)
    claim_ttl = _positive_seconds(
        os.environ.get("LARKFLOW_EDGE_CLAIM_TTL_SECONDS", "300"),
        "LARKFLOW_EDGE_CLAIM_TTL_SECONDS",
    )
    max_result_bytes = _positive_integer(
        os.environ.get("LARKFLOW_EDGE_MAX_RESULT_BYTES", "100000"),
        "LARKFLOW_EDGE_MAX_RESULT_BYTES",
    )
    service = WorkflowService(
        repository,
        runner=NodeRunner(claim_ttl=timedelta(seconds=claim_ttl)),
    )
    store = PostgresEdgeStore(connection_factory)
    edge = EdgeControlService(
        store,
        service,
        repository,
        max_result_bytes=max_result_bytes,
    )

    if namespace.command == "pairing-create":
        grant = edge.issue_pairing(
            tenant_id=namespace.tenant,
            person_id=namespace.person,
            actor_person_id=namespace.actor,
            ttl=timedelta(seconds=namespace.ttl_seconds),
            allowed_capabilities=(PERSONAL_READONLY_CAPABILITY,),
        )
        _print(
            {
                "event": "edge_pairing_issued",
                "code": grant.code,
                "expires_at": grant.expires_at,
                "capabilities": grant.allowed_capabilities,
                "warning": "The code is one-time and is not recoverable.",
            }
        )
        return 0

    if namespace.command == "devices":
        _print(
            {
                "event": "edge_devices_listed",
                "tenant_id": namespace.tenant,
                "devices": [
                    {
                        "id": item.id,
                        "person_id": item.person_id,
                        "name": item.name,
                        "capabilities": item.capabilities,
                        "status": item.status.value,
                        "created_at": item.created_at,
                        "last_seen_at": item.last_seen_at,
                        "revoked_at": item.revoked_at,
                    }
                    for item in store.list_devices(namespace.tenant)
                ],
            }
        )
        return 0

    if namespace.command == "revoke":
        device = edge.revoke_device(
            tenant_id=namespace.tenant,
            device_id=namespace.device,
            actor_person_id=namespace.actor,
            reason=namespace.reason,
        )
        _print(
            {
                "event": "edge_device_revoked",
                "tenant_id": device.tenant_id,
                "device_id": device.id,
                "status": device.status.value,
                "revoked_at": device.revoked_at,
            }
        )
        return 0

    host = _loopback_host(namespace.host)
    if namespace.port < 1 or namespace.port > 65_535:
        raise ValueError("port must be between 1 and 65535")
    application = EdgeHttpApplication(edge)
    server = build_edge_http_server(
        application,
        host=host,
        port=namespace.port,
    )

    stop_event = Event()

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    _print(
        {
            "event": "edge_gateway_started",
            "host": host,
            "port": namespace.port,
            "tls": "required at the reverse proxy before remote access",
            "capabilities": [PERSONAL_READONLY_CAPABILITY],
        }
    )
    serving_errors: list[BaseException] = []

    def serve() -> None:
        try:
            server.serve_forever(poll_interval=0.25)
        except BaseException as exc:
            serving_errors.append(exc)
        finally:
            stop_event.set()

    serving = Thread(
        target=serve,
        name="larkflow-edge-http",
        daemon=True,
    )
    serving.start()
    stop_event.wait()
    server.shutdown()
    serving.join(timeout=5)
    server.server_close()
    if serving_errors:
        raise RuntimeError("Edge HTTP server stopped unexpectedly") from (
            serving_errors[0]
        )
    return 0


def _preparse_env_file(args: Sequence[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=".env")
    namespace, _ = parser.parse_known_args(args)
    return namespace.env_file


def _loopback_host(value: str) -> str:
    host = _required(value, "host")
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("Edge gateway host must be a loopback IP or localhost") from exc
    if not address.is_loopback:
        raise ValueError("Edge gateway can only bind to loopback in Proof v0")
    return host


def _positive_seconds(value: str, label: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number") from exc
    if number <= 0:
        raise ValueError(f"{label} must be positive")
    return number


def _positive_integer(value: str, label: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if number <= 0:
        raise ValueError(f"{label} must be positive")
    return number


def _required(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(
            to_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=stream,
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
