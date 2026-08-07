"""Command-line entrypoint for the loopback central workflow console."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
import signal
import sys
from threading import Event, Thread
from typing import Any

from larkflow.config import load_dotenv

from .console import ConsolePrincipal, ConsoleReadService, StaticConsoleAuthenticator
from .console_auth import (
    FeishuConsoleOAuthFlow,
    FeishuOAuthClient,
    PostgresConsoleSessionAuthenticator,
)
from .console_http import ConsoleHttpApplication, build_console_http_server
from .migrate import postgres_connection_factory, verify_migrations
from .postgres import PostgresWorkflowRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="larkflow-console",
        description="Owner-scoped read-only workflow console",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="dotenv file parsed by larkflow, never sourced by a shell",
    )
    parser.add_argument("--dsn", default=os.environ.get("LARKFLOW_TARGET_DSN"))
    parser.add_argument("--tenant", default=os.environ.get("LARKFLOW_TARGET_TENANT"))
    parser.add_argument(
        "--person",
        default=os.environ.get("LARKFLOW_CONSOLE_PERSON_ID"),
    )
    parser.add_argument(
        "--auth-mode",
        choices=("static", "feishu"),
        default=os.environ.get("LARKFLOW_CONSOLE_AUTH_MODE", "static"),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
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
                "event": "console_failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 1


def _run(namespace: argparse.Namespace) -> int:
    dsn = _required(namespace.dsn, "--dsn or LARKFLOW_TARGET_DSN")
    tenant_id = _required(namespace.tenant, "--tenant or LARKFLOW_TARGET_TENANT")
    connection_factory = postgres_connection_factory(dsn)
    verify_migrations(connection_factory)
    repository = PostgresWorkflowRepository(connection_factory)
    service = ConsoleReadService(repository)
    if namespace.auth_mode == "static":
        person_id = _required(
            namespace.person,
            "--person or LARKFLOW_CONSOLE_PERSON_ID",
        )
        access_token = _required(
            os.environ.get("LARKFLOW_CONSOLE_ACCESS_TOKEN"),
            "LARKFLOW_CONSOLE_ACCESS_TOKEN",
        )
        application = ConsoleHttpApplication(
            service,
            StaticConsoleAuthenticator(
                access_token,
                ConsolePrincipal(tenant_id=tenant_id, person_id=person_id),
            ),
        )
        access = "enter LARKFLOW_CONSOLE_ACCESS_TOKEN in the browser"
    else:
        app_id = _required(
            os.environ.get("LARKFLOW_CONSOLE_FEISHU_APP_ID"),
            "LARKFLOW_CONSOLE_FEISHU_APP_ID",
        )
        app_secret = _required(
            os.environ.get("LARKFLOW_CONSOLE_FEISHU_APP_SECRET"),
            "LARKFLOW_CONSOLE_FEISHU_APP_SECRET",
        )
        allowed_tenant_key = _required(
            os.environ.get("LARKFLOW_CONSOLE_FEISHU_TENANT_KEY"),
            "LARKFLOW_CONSOLE_FEISHU_TENANT_KEY",
        )
        public_base_url = _required(
            os.environ.get("LARKFLOW_CONSOLE_PUBLIC_BASE_URL"),
            "LARKFLOW_CONSOLE_PUBLIC_BASE_URL",
        )
        session_ttl = _bounded_integer(
            os.environ.get("LARKFLOW_CONSOLE_SESSION_TTL_SECONDS", "28800"),
            label="LARKFLOW_CONSOLE_SESSION_TTL_SECONDS",
            minimum=300,
            maximum=86_400,
        )
        sessions = PostgresConsoleSessionAuthenticator(
            connection_factory,
            ttl_seconds=session_ttl,
        )
        oauth = FeishuConsoleOAuthFlow(
            app_id=app_id,
            public_base_url=public_base_url,
            workflow_tenant_id=tenant_id,
            allowed_feishu_tenant_key=allowed_tenant_key,
            identity_provider=FeishuOAuthClient(
                app_id=app_id,
                app_secret=app_secret,
            ),
            sessions=sessions,
        )
        application = ConsoleHttpApplication(
            service,
            sessions,
            oauth=oauth,
        )
        access = "Feishu OAuth with an opaque HttpOnly session"
    server = build_console_http_server(
        application,
        host=namespace.host,
        port=namespace.port,
    )

    stop_event = Event()

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    _print(
        {
            "event": "console_started",
            "url": f"http://{namespace.host}:{namespace.port}/console/",
            "principal": "configured_server_side",
            "access": access,
            "auth_mode": namespace.auth_mode,
            "mode": "owner_scoped_read_only",
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

    serving = Thread(target=serve, name="larkflow-console-http", daemon=True)
    serving.start()
    stop_event.wait()
    server.shutdown()
    serving.join(timeout=5)
    server.server_close()
    if serving_errors:
        raise RuntimeError("console HTTP server stopped unexpectedly") from (
            serving_errors[0]
        )
    return 0


def _preparse_env_file(args: Sequence[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=".env")
    namespace, _ = parser.parse_known_args(args)
    return namespace.env_file


def _required(value: Any, label: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{label} is required")
    return str(value).strip()


def _bounded_integer(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return number


def _print(payload: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), file=stream)
