"""User-owned Personal Agent Edge Proof v0 command line."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import getpass
import json
from pathlib import Path
import signal
import socket
import sys
from threading import Event
from typing import Any

from .edge import PERSONAL_READONLY_CAPABILITY
from .edge_agent import EdgeAgentLoop
from .edge_client import (
    CodexReadonlyExecutor,
    EdgeDeviceLock,
    EdgeWorker,
    HttpEdgeTransport,
    load_edge_credential,
    save_edge_credential,
)


DEFAULT_CREDENTIAL_FILE = Path("~/.config/larkflow/edge-device.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="larkflow-edge",
        description="User-owned Personal Agent Edge Proof v0",
    )
    parser.add_argument(
        "--credential-file",
        default=str(DEFAULT_CREDENTIAL_FILE),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    pair = commands.add_parser("pair", help="pair this device with a one-time code")
    pair.add_argument("--server", required=True)
    pair.add_argument("--name", default=socket.gethostname())
    pair.add_argument(
        "--code",
        help="one-time code; omit to read it without terminal echo",
    )

    run_once = commands.add_parser(
        "run-once",
        help="manually claim and execute at most one read-only task",
    )
    run_once.add_argument("--workspace", required=True)
    run_once.add_argument("--wait-seconds", type=float, default=0)
    run_once.add_argument("--codex-binary", default="codex")
    run_once.add_argument("--timeout-seconds", type=float, default=240)
    run_once.add_argument("--renew-seconds", type=float, default=30)
    run_once.add_argument(
        "--inherit-loopback-proxy",
        action="store_true",
        help="pass only credential-free loopback proxy URLs to Codex",
    )
    serve = commands.add_parser(
        "serve",
        help="continuously claim read-only work in the foreground",
    )
    serve.add_argument("--workspace", required=True)
    serve.add_argument("--wait-seconds", type=float, default=20)
    serve.add_argument("--codex-binary", default="codex")
    serve.add_argument("--timeout-seconds", type=float, default=240)
    serve.add_argument("--renew-seconds", type=float, default=30)
    serve.add_argument("--retry-base-seconds", type=float, default=1)
    serve.add_argument("--retry-max-seconds", type=float, default=30)
    serve.add_argument("--heartbeat-seconds", type=float, default=60)
    serve.add_argument(
        "--max-tasks",
        type=int,
        default=0,
        help="stop after this many claimed tasks; 0 keeps serving",
    )
    serve.add_argument(
        "--inherit-loopback-proxy",
        action="store_true",
        help="pass only credential-free loopback proxy URLs to Codex",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    namespace = build_parser().parse_args(
        list(sys.argv[1:] if argv is None else argv)
    )
    try:
        return _run(namespace)
    except Exception as exc:
        _print(
            {
                "event": "edge_command_failed",
                "command": namespace.command,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 1


def _run(namespace: argparse.Namespace) -> int:
    credential_path = Path(namespace.credential_file).expanduser()
    if namespace.command == "pair":
        if credential_path.exists() or credential_path.is_symlink():
            raise FileExistsError(
                f"credential file already exists: {credential_path}"
            )
        credential_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not credential_path.parent.is_dir():
            raise ValueError("credential parent must be a directory")
        code = namespace.code or getpass.getpass("One-time pairing code: ")
        transport = HttpEdgeTransport(namespace.server)
        paired = transport.pair(
            code=_required(code, "pairing code"),
            name=_required(namespace.name, "device name"),
            capabilities=(PERSONAL_READONLY_CAPABILITY,),
        )
        save_edge_credential(credential_path, paired)
        _print(
            {
                "event": "edge_device_paired",
                "device_id": paired.device_id,
                "server_url": paired.server_url,
                "credential_file": str(credential_path),
                "capabilities": [PERSONAL_READONLY_CAPABILITY],
            }
        )
        return 0

    if namespace.wait_seconds < 0 or namespace.wait_seconds > 25:
        raise ValueError("wait-seconds must be between 0 and 25")
    if namespace.timeout_seconds <= 0 or namespace.renew_seconds <= 0:
        raise ValueError("timeout-seconds and renew-seconds must be positive")
    stored = load_edge_credential(credential_path)
    transport = HttpEdgeTransport(
        stored.server_url,
        credential=stored.credential,
    )
    workspace_path = Path(namespace.workspace).expanduser().resolve(strict=True)
    if namespace.command == "serve":
        _validate_serve_workspace(workspace_path, credential_path)
    executor = CodexReadonlyExecutor(
        workspace_path,
        codex_binary=namespace.codex_binary,
        timeout_seconds=namespace.timeout_seconds,
        inherit_loopback_proxy=namespace.inherit_loopback_proxy,
    )
    worker = EdgeWorker(
        transport,
        executor,
        renew_interval_seconds=namespace.renew_seconds,
        renewal_observer=(
            lambda expires_at: _print(
                {
                    "event": "edge_agent_lease_renewed",
                    "claim_expires_at": expires_at.isoformat(),
                }
            )
            if namespace.command == "serve"
            else None
        ),
    )
    with EdgeDeviceLock(credential_path):
        if namespace.command == "run-once":
            report = worker.run_once(wait_seconds=namespace.wait_seconds)
            _print(
                {
                    "event": "edge_run_once",
                    "status": report.status,
                    "instance_id": report.lease.instance_id if report.lease else None,
                    "node_key": report.lease.node_key if report.lease else None,
                    "attempt_no": report.lease.attempt_no if report.lease else None,
                    "error_type": report.error,
                }
            )
            return 0 if report.status in {"completed", "no_work"} else 2

        if namespace.retry_base_seconds <= 0:
            raise ValueError("retry-base-seconds must be positive")
        if namespace.retry_max_seconds < namespace.retry_base_seconds:
            raise ValueError("retry-max-seconds must be at least retry-base-seconds")
        if namespace.heartbeat_seconds <= 0:
            raise ValueError("heartbeat-seconds must be positive")
        if namespace.max_tasks < 0:
            raise ValueError("max-tasks cannot be negative")

        stop_event = Event()
        previous_handlers = _install_signal_handlers(stop_event)
        try:
            _print(
                {
                    "event": "edge_agent_started",
                    "device_id": stored.device_id,
                    "server_url": stored.server_url,
                    "workspace": str(workspace_path),
                    "capability": PERSONAL_READONLY_CAPABILITY,
                    "wait_seconds": namespace.wait_seconds,
                    "heartbeat_seconds": namespace.heartbeat_seconds,
                    "max_tasks": namespace.max_tasks,
                }
            )
            summary = EdgeAgentLoop(
                worker,
                wait_seconds=namespace.wait_seconds,
                retry_base_seconds=namespace.retry_base_seconds,
                retry_max_seconds=namespace.retry_max_seconds,
                heartbeat_seconds=namespace.heartbeat_seconds,
                max_tasks=namespace.max_tasks,
                log=lambda event, fields: _print({"event": event, **fields}),
            ).run(stop_event)
        finally:
            _restore_signal_handlers(previous_handlers)
        return 0 if summary.fatal_error is None else 3


def _required(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _validate_serve_workspace(workspace: Path, credential_path: Path) -> None:
    home = Path.home().resolve()
    if workspace == Path(workspace.anchor) or workspace == home:
        raise ValueError("serve workspace cannot be the filesystem root or user home")
    credential = credential_path.expanduser().resolve(strict=True)
    if credential.is_relative_to(workspace):
        raise ValueError("Edge credential file cannot be inside the serve workspace")


def _install_signal_handlers(stop_event: Event) -> dict[int, Any]:
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }

    def request_stop(signum: int, _frame: Any) -> None:
        _print(
            {
                "event": "edge_agent_stop_requested",
                "signal": signal.Signals(signum).name,
            }
        )
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        file=stream,
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
