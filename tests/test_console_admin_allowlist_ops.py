"""Server-side Console administrator allowlist operations stay recoverable."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "larkflow-console-admin-allowlist.py"
SPEC = importlib.util.spec_from_file_location("console_admin_allowlist_ops", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)

SESSION_ONE = "1" * 32
SESSION_TWO = "2" * 32
PERSON_ONE = "ou_admin"
PERSON_TWO = "ou_member"
NOW = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)


def _environment(tmp_path: Path, admins: str = PERSON_ONE) -> Path:
    path = tmp_path / "console.env"
    path.write_text(
        "# retained comment\n"
        "LARKFLOW_TARGET_DSN=postgresql:///larkflow_target_dev\n"
        "LARKFLOW_TARGET_TENANT=dev\n"
        f"LARKFLOW_CONSOLE_ADMIN_PERSON_IDS={admins}\n"
        "LARKFLOW_CONSOLE_FEISHU_APP_SECRET=not-read-or-logged\n",
        encoding="utf-8",
    )
    path.chmod(0o640)
    return path


def _resolver(tenant: str, dsn: str, session_id: str) -> str:
    assert tenant == "dev"
    assert dsn == "postgresql:///larkflow_target_dev"
    return {SESSION_ONE: PERSON_ONE, SESSION_TWO: PERSON_TWO}[session_id]


def _healthy(service: str, url: str) -> dict[str, object]:
    assert service == "larkflow-target-console.service"
    assert url == "http://127.0.0.1:8780"
    return {
        "service_active": True,
        "http_status": {
            "console": 200,
            "auth": 200,
            "admin_unauthenticated": 401,
        },
    }


def _preview(tmp_path: Path, *, action: str, session_id: str) -> tuple[Path, Path, dict]:
    env_file = _environment(tmp_path)
    state_dir = tmp_path / "state"
    report = ops.preview_change(
        action=action,
        session_id=session_id,
        env_file=env_file,
        state_dir=state_dir,
        session_resolver=_resolver,
        now=NOW,
    )
    return env_file, state_dir, report


def test_preview_uses_active_session_without_disclosing_person_id(tmp_path: Path):
    _, state_dir, report = _preview(
        tmp_path, action="add", session_id=SESSION_TWO
    )

    assert report == {
        "event": "console_admin_allowlist_preview_created",
        "preview_id": report["preview_id"],
        "action": "add",
        "target_session_suffix": "22222222",
        "change_required": True,
        "before_count": 1,
        "after_count": 2,
        "expires_at": "2026-08-08T01:10:00+00:00",
    }
    assert PERSON_ONE not in json.dumps(report)
    assert PERSON_TWO not in json.dumps(report)
    assert SESSION_TWO not in json.dumps(report)
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((state_dir / "audit.jsonl").stat().st_mode) == 0o600


def test_confirm_atomically_applies_and_replays_without_second_restart(tmp_path: Path):
    env_file, state_dir, preview = _preview(
        tmp_path, action="add", session_id=SESSION_TWO
    )
    calls: list[tuple[str, str]] = []

    def health(service: str, url: str) -> dict[str, object]:
        calls.append((service, url))
        return _healthy(service, url)

    report = ops.confirm_change(
        preview_id=preview["preview_id"],
        env_file=env_file,
        state_dir=state_dir,
        session_resolver=_resolver,
        health_check=health,
        service="larkflow-target-console.service",
        health_url="http://127.0.0.1:8780",
        now=NOW + timedelta(minutes=1),
    )
    replay = ops.confirm_change(
        preview_id=preview["preview_id"],
        env_file=env_file,
        state_dir=state_dir,
        session_resolver=_resolver,
        health_check=health,
        service="larkflow-target-console.service",
        health_url="http://127.0.0.1:8780",
        now=NOW + timedelta(minutes=2),
    )

    assert report["status"] == "applied"
    assert report["health_checked"] is True
    assert report["service_active"] is True
    assert replay["replayed"] is True
    assert len(calls) == 1
    assert "LARKFLOW_CONSOLE_ADMIN_PERSON_IDS=ou_admin,ou_member\n" in (
        env_file.read_text(encoding="utf-8")
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o640
    assert len(list(tmp_path.glob("console.env.bak.*.admin-*"))) == 1
    assert not list((state_dir / "previews").iterdir())
    assert (state_dir / "history" / f"{preview['preview_id']}.json").is_file()


def test_existing_admin_confirmation_is_noop_without_restart_or_backup(tmp_path: Path):
    env_file, state_dir, preview = _preview(
        tmp_path, action="add", session_id=SESSION_ONE
    )

    report = ops.confirm_change(
        preview_id=preview["preview_id"],
        env_file=env_file,
        state_dir=state_dir,
        session_resolver=_resolver,
        health_check=lambda service, url: pytest.fail("must not restart"),
        service="larkflow-target-console.service",
        health_url="http://127.0.0.1:8780",
        now=NOW + timedelta(minutes=1),
    )

    assert report["status"] == "confirmed_no_change"
    assert report["change_required"] is False
    assert report["health_checked"] is False
    assert report["service_active"] is None
    assert env_file.read_text(encoding="utf-8").count(PERSON_ONE) == 1
    assert not list(tmp_path.glob("console.env.bak.*.admin-*"))


def test_remove_last_administrator_is_rejected_at_preview(tmp_path: Path):
    env_file = _environment(tmp_path)

    with pytest.raises(ops.AllowlistOperationError, match="last Console administrator"):
        ops.preview_change(
            action="remove",
            session_id=SESSION_ONE,
            env_file=env_file,
            state_dir=tmp_path / "state",
            session_resolver=_resolver,
            now=NOW,
        )


def test_stale_preview_cannot_overwrite_external_env_change(tmp_path: Path):
    env_file, state_dir, preview = _preview(
        tmp_path, action="add", session_id=SESSION_TWO
    )
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "LARKFLOW_EXTRA=value\n",
        encoding="utf-8",
    )

    with pytest.raises(ops.AllowlistOperationError, match="env file changed"):
        ops.confirm_change(
            preview_id=preview["preview_id"],
            env_file=env_file,
            state_dir=state_dir,
            session_resolver=_resolver,
            health_check=_healthy,
            service="larkflow-target-console.service",
            health_url="http://127.0.0.1:8780",
            now=NOW + timedelta(minutes=1),
        )


def test_failed_runtime_check_restores_original_env_and_records_history(tmp_path: Path):
    env_file, state_dir, preview = _preview(
        tmp_path, action="add", session_id=SESSION_TWO
    )
    original = env_file.read_bytes()
    calls = 0

    def health(service: str, url: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unhealthy")
        return _healthy(service, url)

    with pytest.raises(ops.AllowlistOperationError, match="original env was restored"):
        ops.confirm_change(
            preview_id=preview["preview_id"],
            env_file=env_file,
            state_dir=state_dir,
            session_resolver=_resolver,
            health_check=health,
            service="larkflow-target-console.service",
            health_url="http://127.0.0.1:8780",
            now=NOW + timedelta(minutes=1),
        )

    assert calls == 2
    assert env_file.read_bytes() == original
    history = json.loads(
        (state_dir / "history" / f"{preview['preview_id']}.json").read_text()
    )
    assert history["status"] == "failed_rolled_back"


def test_applied_operation_can_be_rolled_back_once(tmp_path: Path):
    env_file, state_dir, preview = _preview(
        tmp_path, action="add", session_id=SESSION_TWO
    )
    ops.confirm_change(
        preview_id=preview["preview_id"],
        env_file=env_file,
        state_dir=state_dir,
        session_resolver=_resolver,
        health_check=_healthy,
        service="larkflow-target-console.service",
        health_url="http://127.0.0.1:8780",
        now=NOW + timedelta(minutes=1),
    )

    report = ops.rollback_change(
        operation_id=preview["preview_id"],
        env_file=env_file,
        state_dir=state_dir,
        health_check=_healthy,
        service="larkflow-target-console.service",
        health_url="http://127.0.0.1:8780",
        now=NOW + timedelta(minutes=2),
    )
    replay = ops.rollback_change(
        operation_id=preview["preview_id"],
        env_file=env_file,
        state_dir=state_dir,
        health_check=lambda service, url: pytest.fail("must not restart"),
        service="larkflow-target-console.service",
        health_url="http://127.0.0.1:8780",
        now=NOW + timedelta(minutes=3),
    )

    assert report["status"] == "rolled_back"
    assert replay["replayed"] is True
    assert "LARKFLOW_CONSOLE_ADMIN_PERSON_IDS=ou_admin\n" in env_file.read_text()


def test_expired_preview_is_rejected(tmp_path: Path):
    env_file, state_dir, preview = _preview(
        tmp_path, action="add", session_id=SESSION_TWO
    )

    with pytest.raises(ops.AllowlistOperationError, match="expired"):
        ops.confirm_change(
            preview_id=preview["preview_id"],
            env_file=env_file,
            state_dir=state_dir,
            session_resolver=_resolver,
            health_check=_healthy,
            service="larkflow-target-console.service",
            health_url="http://127.0.0.1:8780",
            now=NOW + timedelta(minutes=11),
        )


def test_session_lookup_uses_psql_stdin_for_variable_substitution(monkeypatch):
    seen: dict[str, object] = {}

    def run(command, **kwargs):
        seen.update({"command": command, **kwargs})
        return SimpleNamespace(returncode=0, stdout=f"{PERSON_TWO}\n", stderr="")

    monkeypatch.setattr(ops.subprocess, "run", run)

    assert ops._resolve_session(
        "dev",
        "postgresql:///larkflow_target_dev",
        SESSION_TWO,
        run_as_user="lf_target_dev",
    ) == PERSON_TWO
    assert "--command" not in seen["command"]
    assert "WHERE tenant_id = :'tenant_id'" in seen["input"]
    assert seen["cwd"] == "/"
