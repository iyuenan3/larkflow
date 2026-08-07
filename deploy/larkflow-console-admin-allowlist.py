#!/usr/bin/env python3
"""Safely preview, apply, and roll back Console administrator changes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4


ADMIN_KEY = "LARKFLOW_CONSOLE_ADMIN_PERSON_IDS"
DSN_KEY = "LARKFLOW_TARGET_DSN"
TENANT_KEY = "LARKFLOW_TARGET_TENANT"
DEFAULT_ENV_FILE = Path("/etc/larkflow-target-console.env")
DEFAULT_STATE_DIR = Path("/var/lib/larkflow-console-admin-allowlist")
DEFAULT_SERVICE = "larkflow-target-console.service"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8780"
DEFAULT_RUN_AS_USER = "lf_target_dev"
PREVIEW_TTL = timedelta(minutes=10)
REFERENCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TENANT_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class AllowlistOperationError(RuntimeError):
    """Expected operator-facing failure."""


SessionResolver = Callable[[str, str, str], str]
HealthCheck = Callable[[str, str], Mapping[str, Any]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="larkflow-console-admin-allowlist",
        description=(
            "Preview and confirm server-side Console administrator allowlist changes"
        ),
    )
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--run-as-user", default=DEFAULT_RUN_AS_USER)
    commands = parser.add_subparsers(dest="command", required=True)

    preview = commands.add_parser("preview", help="create a short-lived preview")
    preview.add_argument("action", choices=("add", "remove"))
    preview.add_argument("session_id", help="active Console session reference")

    confirm = commands.add_parser("confirm", help="confirm one preview")
    confirm.add_argument("preview_id")

    rollback = commands.add_parser("rollback", help="roll back one applied operation")
    rollback.add_argument("operation_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    namespace = build_parser().parse_args(
        list(sys.argv[1:] if argv is None else argv)
    )
    try:
        _require_root()
        env_file = Path(namespace.env_file)
        state_dir = Path(namespace.state_dir)
        if namespace.command == "preview":
            report = preview_change(
                action=namespace.action,
                session_id=namespace.session_id,
                env_file=env_file,
                state_dir=state_dir,
                session_resolver=lambda tenant, dsn, session_id: _resolve_session(
                    tenant,
                    dsn,
                    session_id,
                    run_as_user=namespace.run_as_user,
                ),
            )
        elif namespace.command == "confirm":
            report = confirm_change(
                preview_id=namespace.preview_id,
                env_file=env_file,
                state_dir=state_dir,
                session_resolver=lambda tenant, dsn, session_id: _resolve_session(
                    tenant,
                    dsn,
                    session_id,
                    run_as_user=namespace.run_as_user,
                ),
                health_check=lambda service, url: _restart_and_check(service, url),
                service=namespace.service,
                health_url=namespace.health_url,
            )
        else:
            report = rollback_change(
                operation_id=namespace.operation_id,
                env_file=env_file,
                state_dir=state_dir,
                health_check=lambda service, url: _restart_and_check(service, url),
                service=namespace.service,
                health_url=namespace.health_url,
            )
        _print(report)
        return 0
    except Exception as exc:
        _print(
            {
                "event": "console_admin_allowlist_failed",
                "command": namespace.command,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 1


def preview_change(
    *,
    action: str,
    session_id: str,
    env_file: Path,
    state_dir: Path,
    session_resolver: SessionResolver,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve one active tenant session and persist an immutable preview."""
    if action not in {"add", "remove"}:
        raise AllowlistOperationError("action must be add or remove")
    session_id = _reference(session_id, "session ID")
    now = _utc_now(now)
    paths = _state_paths(state_dir)
    with _operation_lock(paths["lock"]):
        env_bytes, values, env_sha256 = _read_environment(env_file)
        del env_bytes
        tenant = _tenant(values)
        person_id = _person_id(
            session_resolver(tenant, _local_dsn(values), session_id)
        )
        current = list(_person_ids(values.get(ADMIN_KEY, "")))
        already_admin = person_id in current
        if action == "add":
            desired = current if already_admin else [*current, person_id]
        else:
            if already_admin and len(current) == 1:
                raise AllowlistOperationError("cannot remove the last Console administrator")
            desired = [item for item in current if item != person_id]
        if len(desired) > 100:
            raise AllowlistOperationError("administrator allowlist accepts at most 100 IDs")

        preview_id = uuid4().hex
        record = {
            "schema_version": 1,
            "kind": "console_admin_allowlist_preview",
            "id": preview_id,
            "action": action,
            "session_id": session_id,
            "person_id": person_id,
            "tenant": tenant,
            "env_sha256": env_sha256,
            "current_admin_ids": current,
            "desired_admin_ids": desired,
            "created_at": _timestamp(now),
            "expires_at": _timestamp(now + PREVIEW_TTL),
        }
        _write_json_exclusive(paths["previews"] / f"{preview_id}.json", record)
        _append_audit(
            paths["audit"],
            {
                "event": "preview_created",
                "operation_id": preview_id,
                "action": action,
                "target_digest": _digest(person_id),
                "change_required": current != desired,
                "before_count": len(current),
                "after_count": len(desired),
                "occurred_at": _timestamp(now),
            },
        )
    return _public_preview(record)


def confirm_change(
    *,
    preview_id: str,
    env_file: Path,
    state_dir: Path,
    session_resolver: SessionResolver,
    health_check: HealthCheck,
    service: str,
    health_url: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Confirm a fresh preview and automatically restore on runtime failure."""
    preview_id = _reference(preview_id, "preview ID")
    now = _utc_now(now)
    paths = _state_paths(state_dir)
    with _operation_lock(paths["lock"]):
        preview_path = paths["previews"] / f"{preview_id}.json"
        history_path = paths["history"] / f"{preview_id}.json"
        if history_path.exists():
            record = _read_json(history_path)
            return _public_confirmation(record, replayed=True)
        record = _read_json(preview_path)
        _validate_preview(record, preview_id=preview_id, now=now)
        env_bytes, values, env_sha256 = _read_environment(env_file)
        if env_sha256 != record["env_sha256"]:
            raise AllowlistOperationError("preview is stale because the env file changed")
        tenant = _tenant(values)
        if tenant != record["tenant"]:
            raise AllowlistOperationError("preview tenant no longer matches the env file")
        person_id = _person_id(
            session_resolver(tenant, _local_dsn(values), record["session_id"])
        )
        if person_id != record["person_id"]:
            raise AllowlistOperationError("preview target no longer matches the active session")
        current = list(_person_ids(values.get(ADMIN_KEY, "")))
        if current != record["current_admin_ids"]:
            raise AllowlistOperationError("preview is stale because the allowlist changed")
        desired = list(record["desired_admin_ids"])
        if record["action"] == "remove" and not desired:
            raise AllowlistOperationError("cannot remove the last Console administrator")

        operation = dict(record)
        operation.update(
            {
                "kind": "console_admin_allowlist_operation",
                "confirmed_at": _timestamp(now),
                "change_required": current != desired,
                "status": "confirmed_no_change",
                "before_sha256": env_sha256,
                "after_sha256": env_sha256,
                "backup_path": None,
                "health": None,
            }
        )
        if current != desired:
            backup_path = _backup_environment(env_file, preview_id, env_bytes, now)
            operation["backup_path"] = str(backup_path)
            updated = _replace_environment_value(env_bytes, ADMIN_KEY, ",".join(desired))
            try:
                _atomic_replace_bytes(env_file, updated)
                operation["after_sha256"] = _sha256(updated)
                operation["health"] = dict(health_check(service, health_url))
                operation["status"] = "applied"
            except Exception as exc:
                _atomic_replace_bytes(env_file, env_bytes)
                rollback_health = dict(health_check(service, health_url))
                operation.update(
                    {
                        "status": "failed_rolled_back",
                        "failure_type": type(exc).__name__,
                        "failure": str(exc),
                        "rollback_health": rollback_health,
                        "rolled_back_at": _timestamp(_utc_now()),
                    }
                )
                _consume_preview(preview_path, history_path, operation)
                _append_operation_audit(paths["audit"], operation)
                raise AllowlistOperationError(
                    "runtime validation failed; the original env was restored"
                ) from exc

        _consume_preview(preview_path, history_path, operation)
        _append_operation_audit(paths["audit"], operation)
    return _public_confirmation(operation, replayed=False)


def rollback_change(
    *,
    operation_id: str,
    env_file: Path,
    state_dir: Path,
    health_check: HealthCheck,
    service: str,
    health_url: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Restore the env backup for one applied operation after a fresh readback."""
    operation_id = _reference(operation_id, "operation ID")
    now = _utc_now(now)
    paths = _state_paths(state_dir)
    with _operation_lock(paths["lock"]):
        operation = _read_json(paths["history"] / f"{operation_id}.json")
        if operation.get("status") != "applied":
            raise AllowlistOperationError("only an applied operation can be rolled back")
        existing_rollbacks = sorted(paths["history"].glob(f"rollback-*-{operation_id}.json"))
        if existing_rollbacks:
            return _public_rollback(_read_json(existing_rollbacks[-1]), replayed=True)
        current_bytes, _, current_sha256 = _read_environment(env_file)
        if current_sha256 != operation.get("after_sha256"):
            raise AllowlistOperationError("rollback is stale because the env file changed")
        backup_path = Path(str(operation.get("backup_path") or ""))
        if not backup_path.is_file() or backup_path.is_symlink():
            raise AllowlistOperationError("operation backup is unavailable")
        backup_bytes = backup_path.read_bytes()
        if _sha256(backup_bytes) != operation.get("before_sha256"):
            raise AllowlistOperationError("operation backup digest does not match")

        rollback_id = uuid4().hex
        rollback = {
            "schema_version": 1,
            "kind": "console_admin_allowlist_rollback",
            "id": rollback_id,
            "operation_id": operation_id,
            "before_sha256": current_sha256,
            "after_sha256": operation["before_sha256"],
            "created_at": _timestamp(now),
            "status": "rolled_back",
        }
        try:
            _atomic_replace_bytes(env_file, backup_bytes)
            rollback["health"] = dict(health_check(service, health_url))
        except Exception as exc:
            _atomic_replace_bytes(env_file, current_bytes)
            rollback_health = dict(health_check(service, health_url))
            rollback.update(
                {
                    "status": "rollback_failed_restored_applied_state",
                    "failure_type": type(exc).__name__,
                    "failure": str(exc),
                    "recovery_health": rollback_health,
                }
            )
            rollback_path = paths["history"] / f"rollback-{rollback_id}-{operation_id}.json"
            _write_json_exclusive(rollback_path, rollback)
            _append_rollback_audit(paths["audit"], rollback)
            raise AllowlistOperationError(
                "rollback validation failed; the applied env was restored"
            ) from exc
        rollback_path = paths["history"] / f"rollback-{rollback_id}-{operation_id}.json"
        _write_json_exclusive(rollback_path, rollback)
        _append_rollback_audit(paths["audit"], rollback)
    return _public_rollback(rollback, replayed=False)


def _resolve_session(
    tenant: str,
    dsn: str,
    session_id: str,
    *,
    run_as_user: str,
) -> str:
    query = (
        "SELECT person_id FROM workflow_console_sessions "
        "WHERE tenant_id = :'tenant_id' AND id = :'session_id' "
        "AND expires_at > CURRENT_TIMESTAMP"
    )
    command = [
        "/usr/sbin/runuser",
        "-u",
        run_as_user,
        "--",
        "/usr/bin/psql",
        "-X",
        "--no-align",
        "--tuples-only",
        "--set",
        "ON_ERROR_STOP=1",
        "--set",
        f"tenant_id={tenant}",
        "--set",
        f"session_id={session_id}",
        "--dbname",
        dsn,
        "--command",
        query,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PGCONNECT_TIMEOUT": "5"},
    )
    if completed.returncode != 0:
        raise AllowlistOperationError("active Console session lookup failed")
    rows = [line for line in completed.stdout.splitlines() if line]
    if len(rows) != 1:
        raise AllowlistOperationError("session is missing, expired, or ambiguous")
    return rows[0]


def _restart_and_check(service: str, health_url: str) -> Mapping[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9@_.-]+\.service", service):
        raise AllowlistOperationError("invalid systemd service name")
    subprocess.run(
        ["/usr/bin/systemctl", "restart", service],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", service],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    base = health_url.rstrip("/")
    codes = {
        "console": _http_status(f"{base}/console/"),
        "auth": _http_status(f"{base}/console/api/v1/auth"),
        "admin_unauthenticated": _http_status(
            f"{base}/console/api/v1/admin/snapshot"
        ),
    }
    if codes != {"console": 200, "auth": 200, "admin_unauthenticated": 401}:
        raise AllowlistOperationError("Console health readback returned unexpected status")
    return {"service_active": True, "http_status": codes}


def _http_status(url: str) -> int:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as response:
            return int(response.status)
    except HTTPError as exc:
        return int(exc.code)
    except URLError as exc:
        raise AllowlistOperationError("Console health endpoint is unavailable") from exc


def _read_environment(path: Path) -> tuple[bytes, dict[str, str], str]:
    if path.is_symlink() or not path.is_file():
        raise AllowlistOperationError("env file must be a regular file")
    env_bytes = path.read_bytes()
    try:
        text = env_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AllowlistOperationError("env file must be UTF-8") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            raise AllowlistOperationError("env file contains an invalid line")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise AllowlistOperationError("env file contains an invalid key")
        if key in values:
            raise AllowlistOperationError(f"env file contains duplicate key {key}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    for key in (ADMIN_KEY, DSN_KEY, TENANT_KEY):
        if key not in values:
            raise AllowlistOperationError(f"env file is missing {key}")
    _person_ids(values[ADMIN_KEY])
    _tenant(values)
    _local_dsn(values)
    return env_bytes, values, _sha256(env_bytes)


def _replace_environment_value(env_bytes: bytes, key: str, value: str) -> bytes:
    text = env_bytes.decode("utf-8")
    lines = text.splitlines(keepends=True)
    found = 0
    output: list[str] = []
    for line in lines:
        candidate = line.rstrip("\r\n")
        newline = line[len(candidate) :]
        if "=" in candidate and candidate.split("=", 1)[0].strip() == key:
            found += 1
            output.append(f"{key}={value}{newline}")
        else:
            output.append(line)
    if found != 1:
        raise AllowlistOperationError(f"env file must contain exactly one {key}")
    return "".join(output).encode("utf-8")


def _backup_environment(
    env_file: Path,
    operation_id: str,
    env_bytes: bytes,
    now: datetime,
) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    backup = env_file.with_name(f"{env_file.name}.bak.{stamp}.admin-{operation_id[:8]}")
    _write_bytes_exclusive(backup, env_bytes, source=env_file)
    return backup


def _atomic_replace_bytes(path: Path, payload: bytes) -> None:
    source_stat = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(source_stat.st_mode))
            os.fchown(stream.fileno(), source_stat.st_uid, source_stat.st_gid)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _write_bytes_exclusive(path: Path, payload: bytes, *, source: Path) -> None:
    source_stat = source.stat()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, stat.S_IMODE(source_stat.st_mode))
        os.fchown(descriptor, source_stat.st_uid, source_stat.st_gid)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _state_paths(state_dir: Path) -> dict[str, Path]:
    if state_dir.is_symlink():
        raise AllowlistOperationError("state directory cannot be a symlink")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    previews = state_dir / "previews"
    history = state_dir / "history"
    for directory in (previews, history):
        if directory.is_symlink():
            raise AllowlistOperationError("state subdirectory cannot be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    return {
        "root": state_dir,
        "previews": previews,
        "history": history,
        "audit": state_dir / "audit.jsonl",
        "lock": state_dir / "operation.lock",
    }


@contextmanager
def _operation_lock(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AllowlistOperationError("operation record does not exist")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AllowlistOperationError("operation record is unreadable") from exc
    if not isinstance(value, dict):
        raise AllowlistOperationError("operation record is invalid")
    return value


def _consume_preview(preview: Path, history: Path, operation: Mapping[str, Any]) -> None:
    _write_json_exclusive(history, operation)
    preview.unlink()
    _fsync_directory(preview.parent)


def _append_audit(path: Path, event: Mapping[str, Any]) -> None:
    payload = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_operation_audit(path: Path, operation: Mapping[str, Any]) -> None:
    _append_audit(
        path,
        {
            "event": "operation_finished",
            "operation_id": operation["id"],
            "action": operation["action"],
            "target_digest": _digest(str(operation["person_id"])),
            "status": operation["status"],
            "before_count": len(operation["current_admin_ids"]),
            "after_count": len(operation["desired_admin_ids"]),
            "before_sha256": operation["before_sha256"],
            "after_sha256": operation["after_sha256"],
            "occurred_at": operation["confirmed_at"],
        },
    )


def _append_rollback_audit(path: Path, rollback: Mapping[str, Any]) -> None:
    _append_audit(
        path,
        {
            "event": "rollback_finished",
            "rollback_id": rollback["id"],
            "operation_id": rollback["operation_id"],
            "status": rollback["status"],
            "before_sha256": rollback["before_sha256"],
            "after_sha256": rollback["after_sha256"],
            "occurred_at": rollback["created_at"],
        },
    )


def _validate_preview(record: Mapping[str, Any], *, preview_id: str, now: datetime) -> None:
    if record.get("kind") != "console_admin_allowlist_preview":
        raise AllowlistOperationError("operation record is not a preview")
    if record.get("id") != preview_id:
        raise AllowlistOperationError("preview ID does not match its record")
    try:
        expires_at = datetime.fromisoformat(str(record["expires_at"]))
    except (KeyError, ValueError) as exc:
        raise AllowlistOperationError("preview expiry is invalid") from exc
    if expires_at.tzinfo is None or expires_at <= now:
        raise AllowlistOperationError("preview has expired")


def _public_preview(record: Mapping[str, Any]) -> dict[str, Any]:
    current = record["current_admin_ids"]
    desired = record["desired_admin_ids"]
    return {
        "event": "console_admin_allowlist_preview_created",
        "preview_id": record["id"],
        "action": record["action"],
        "target_session_suffix": str(record["session_id"])[-8:],
        "change_required": current != desired,
        "before_count": len(current),
        "after_count": len(desired),
        "expires_at": record["expires_at"],
    }


def _public_confirmation(record: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
    return {
        "event": "console_admin_allowlist_confirmed",
        "operation_id": record["id"],
        "action": record["action"],
        "status": record.get("status"),
        "change_required": bool(record.get("change_required")),
        "before_count": len(record["current_admin_ids"]),
        "after_count": len(record["desired_admin_ids"]),
        "service_active": bool((record.get("health") or {}).get("service_active")),
        "replayed": replayed,
    }


def _public_rollback(record: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
    return {
        "event": "console_admin_allowlist_rolled_back",
        "rollback_id": record["id"],
        "operation_id": record["operation_id"],
        "status": record["status"],
        "service_active": bool((record.get("health") or {}).get("service_active")),
        "replayed": replayed,
    }


def _person_ids(value: str) -> tuple[str, ...]:
    items = [item.strip() for item in value.split(",")]
    if items == [""]:
        return ()
    if any(not item for item in items):
        raise AllowlistOperationError("administrator allowlist contains an empty ID")
    validated = tuple(_person_id(item) for item in items)
    unique = tuple(dict.fromkeys(validated))
    if len(unique) != len(validated):
        raise AllowlistOperationError("administrator allowlist contains duplicate IDs")
    if len(unique) > 100:
        raise AllowlistOperationError("administrator allowlist accepts at most 100 IDs")
    return unique


def _person_id(value: str) -> str:
    value = str(value)
    if not value or len(value) > 256 or any(character.isspace() for character in value):
        raise AllowlistOperationError("resolved person ID is invalid")
    return value


def _tenant(values: Mapping[str, str]) -> str:
    tenant = values.get(TENANT_KEY, "")
    if not TENANT_PATTERN.fullmatch(tenant):
        raise AllowlistOperationError("Console tenant is invalid")
    return tenant


def _local_dsn(values: Mapping[str, str]) -> str:
    dsn = values.get(DSN_KEY, "")
    if not dsn or any(character.isspace() for character in dsn):
        raise AllowlistOperationError("Console DSN is invalid")
    if "@" in dsn or "password=" in dsn.lower():
        raise AllowlistOperationError("administrator tool requires a credential-free local DSN")
    return dsn


def _reference(value: str, label: str) -> str:
    value = str(value).strip()
    if not REFERENCE_PATTERN.fullmatch(value):
        raise AllowlistOperationError(f"{label} must be 32 lowercase hexadecimal characters")
    return value


def _require_root() -> None:
    if os.geteuid() != 0:
        raise AllowlistOperationError("this command must run as root")


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise AllowlistOperationError("timestamp must include a timezone")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _print(value: Mapping[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
