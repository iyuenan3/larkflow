"""User-owned Personal Agent Edge Proof v0 command line."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import getpass
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
from threading import Event
from typing import Any

from .edge import PERSONAL_READONLY_CAPABILITY
from .edge_agent import EdgeAgentLoop
from .edge_client import (
    CodexReadonlyExecutor,
    EdgeCredentialNotKeychainReferenceError,
    EdgeDeviceLock,
    EdgeKeychainReference,
    EdgeWorker,
    HttpEdgeTransport,
    StoredEdgeCredential,
    delete_edge_keychain_credential,
    edge_keychain_credential_exists,
    load_edge_keychain_credential,
    load_edge_keychain_reference,
    load_edge_credential,
    replace_edge_credential_with_keychain_reference,
    save_edge_keychain_credential,
    save_edge_keychain_reference,
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
    parser.add_argument(
        "--credential-store",
        choices=("auto", "keychain", "file"),
        default="auto",
        help="auto prefers macOS Keychain, then a legacy credential file",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    pair = commands.add_parser("pair", help="pair this device with a one-time code")
    pair.add_argument("--server", required=True)
    pair.add_argument("--name", default=socket.gethostname())
    pair.add_argument(
        "--code",
        help="one-time code; omit to read it without terminal echo",
    )
    migrate = commands.add_parser(
        "credential-migrate",
        help="copy a legacy credential file into macOS Keychain",
    )
    migrate.add_argument(
        "--delete-source",
        action="store_true",
        help=(
            "remove the verified plaintext secret and replace the source with "
            "non-secret Keychain metadata"
        ),
    )
    doctor = commands.add_parser(
        "doctor",
        help="validate local credentials and Codex without contacting the server",
    )
    doctor.add_argument("--codex-binary", default="codex")

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
    if namespace.command == "credential-migrate":
        return _migrate_credential_file(
            credential_path,
            delete_source=namespace.delete_source,
        )

    if namespace.command == "pair":
        credential_store = _pair_credential_store(namespace, credential_path)
        if credential_path.exists() or credential_path.is_symlink():
            raise FileExistsError(
                f"credential file already exists: {credential_path}"
            )
        if credential_store == "file":
            credential_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not credential_path.parent.is_dir():
                raise ValueError("credential parent must be a directory")
        elif edge_keychain_credential_exists():
            raise FileExistsError("Edge credential already exists in macOS Keychain")
        code = namespace.code or getpass.getpass("One-time pairing code: ")
        transport = HttpEdgeTransport(namespace.server)
        paired = transport.pair(
            code=_required(code, "pairing code"),
            name=_required(namespace.name, "device name"),
            capabilities=(PERSONAL_READONLY_CAPABILITY,),
        )
        if credential_store == "keychain":
            keychain_created = False
            try:
                save_edge_keychain_credential(paired)
                keychain_created = True
                save_edge_keychain_reference(credential_path, paired)
            except Exception as exc:
                if keychain_created:
                    delete_edge_keychain_credential()
                raise RuntimeError(
                    "pairing succeeded but Keychain storage failed; "
                    "revoke the new device before retrying"
                ) from exc
        else:
            save_edge_credential(credential_path, paired)
        _print(
            {
                "event": "edge_device_paired",
                "device_id": paired.device_id,
                "server_url": paired.server_url,
                "credential_store": credential_store,
                "credential_file": str(credential_path),
                "capabilities": [PERSONAL_READONLY_CAPABILITY],
            }
        )
        return 0

    if namespace.command == "doctor":
        return _doctor(namespace, credential_path)

    if namespace.wait_seconds < 0 or namespace.wait_seconds > 25:
        raise ValueError("wait-seconds must be between 0 and 25")
    if namespace.timeout_seconds <= 0 or namespace.renew_seconds <= 0:
        raise ValueError("timeout-seconds and renew-seconds must be positive")
    stored, credential_store, lock_path, credential_source_path = (
        _load_selected_credential(namespace, credential_path)
    )
    transport = HttpEdgeTransport(
        stored.server_url,
        credential=stored.credential,
    )
    workspace_path = Path(namespace.workspace).expanduser().resolve(strict=True)
    if namespace.command == "serve":
        _validate_serve_workspace(workspace_path, credential_source_path)
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
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with EdgeDeviceLock(lock_path):
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
                    "credential_store": credential_store,
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


def _pair_credential_store(
    namespace: argparse.Namespace,
    credential_path: Path,
) -> str:
    if namespace.credential_store == "file":
        return "file"
    if namespace.credential_store == "keychain":
        edge_keychain_credential_exists()
        return "keychain"
    if sys.platform != "darwin":
        return "file"
    if edge_keychain_credential_exists():
        return "keychain"
    if credential_path.exists() or credential_path.is_symlink():
        raise FileExistsError(
            "legacy credential file exists; run credential-migrate before pairing"
        )
    return "keychain"


def _load_selected_credential(
    namespace: argparse.Namespace,
    credential_path: Path,
) -> tuple[StoredEdgeCredential, str, Path, Path | None]:
    if namespace.credential_store == "file":
        return (
            load_edge_credential(credential_path),
            "file",
            credential_path,
            credential_path,
        )
    if namespace.credential_store == "keychain":
        stored = _load_keychain_credential_from_metadata(credential_path)
        return stored, "keychain", credential_path, credential_path
    if sys.platform == "darwin" and edge_keychain_credential_exists():
        stored = _load_keychain_credential_from_metadata(credential_path)
        return stored, "keychain", credential_path, credential_path
    if credential_path.exists() or credential_path.is_symlink():
        if sys.platform == "darwin":
            try:
                reference = load_edge_keychain_reference(credential_path)
            except EdgeCredentialNotKeychainReferenceError:
                pass
            else:
                stored = load_edge_keychain_credential(reference)
                return stored, "keychain", credential_path, credential_path
        return (
            load_edge_credential(credential_path),
            "file",
            credential_path,
            credential_path,
        )
    raise FileNotFoundError(
        f"Edge credential is not configured: {credential_path}; run pair first"
    )


def _load_keychain_credential_from_metadata(
    credential_path: Path,
) -> StoredEdgeCredential:
    try:
        reference = load_edge_keychain_reference(credential_path)
    except EdgeCredentialNotKeychainReferenceError:
        legacy = load_edge_credential(credential_path)
        reference = EdgeKeychainReference(
            server_url=legacy.server_url,
            device_id=legacy.device_id,
        )
    return load_edge_keychain_credential(reference)


def _migrate_credential_file(
    credential_path: Path,
    *,
    delete_source: bool,
) -> int:
    if edge_keychain_credential_exists():
        raise FileExistsError("Edge credential already exists in macOS Keychain")
    stored = load_edge_credential(credential_path)
    reference = EdgeKeychainReference(
        server_url=stored.server_url,
        device_id=stored.device_id,
    )
    save_edge_keychain_credential(stored)
    try:
        if load_edge_keychain_credential(reference) != stored:
            raise ValueError("Keychain credential verification did not match")
        if delete_source:
            replace_edge_credential_with_keychain_reference(
                credential_path,
                stored,
            )
    except Exception:
        delete_edge_keychain_credential()
        raise
    _print(
        {
            "event": "edge_credential_migrated",
            "credential_store": "keychain",
            "source_file": str(credential_path),
            "source_secret_removed": delete_source,
        }
    )
    return 0


def _doctor(namespace: argparse.Namespace, credential_path: Path) -> int:
    stored, credential_store, _lock_path, _source_path = _load_selected_credential(
        namespace,
        credential_path,
    )
    codex_binary = _required(namespace.codex_binary, "codex binary")
    if os.path.sep in codex_binary:
        candidate = Path(codex_binary).expanduser()
        codex_available = (
            candidate.is_file() and os.access(candidate, os.X_OK)
        )
    else:
        codex_available = shutil.which(codex_binary) is not None
    connection_mode = (
        "loopback_tunnel_required"
        if stored.server_url.startswith("http://")
        else "private_https"
    )
    local_ready = codex_available
    _print(
        {
            "event": "edge_doctor",
            "status": "ready" if local_ready else "blocked",
            "credential": {
                "status": "ok",
                "store": credential_store,
                "secret_in_metadata": False if credential_store == "keychain" else None,
            },
            "codex": {
                "status": "ok" if codex_available else "missing",
            },
            "network": {
                "status": "not_checked",
                "mode": connection_mode,
            },
            "background_service": "not_installed",
        }
    )
    return 0 if local_ready else 2


def _required(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _validate_serve_workspace(
    workspace: Path,
    credential_path: Path | None,
) -> None:
    home = Path.home().resolve()
    if workspace == Path(workspace.anchor) or workspace == home:
        raise ValueError("serve workspace cannot be the filesystem root or user home")
    if credential_path is None:
        return
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
