"""User-owned Personal Agent Edge Proof v0 command line."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import getpass
import json
from pathlib import Path
import socket
import sys
from typing import Any

from .edge import PERSONAL_READONLY_CAPABILITY
from .edge_client import (
    CodexReadonlyExecutor,
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
    stored = load_edge_credential(credential_path)
    transport = HttpEdgeTransport(
        stored.server_url,
        credential=stored.credential,
    )
    executor = CodexReadonlyExecutor(
        namespace.workspace,
        codex_binary=namespace.codex_binary,
        timeout_seconds=namespace.timeout_seconds,
        inherit_loopback_proxy=namespace.inherit_loopback_proxy,
    )
    report = EdgeWorker(
        transport,
        executor,
        renew_interval_seconds=namespace.renew_seconds,
    ).run_once(wait_seconds=namespace.wait_seconds)
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


def _required(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        file=stream,
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
