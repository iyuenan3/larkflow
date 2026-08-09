from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import larkflow.workflow.edge_cli as edge_cli
from larkflow.workflow.edge_client import (
    EdgeKeychainReference,
    StoredEdgeCredential,
    load_edge_keychain_reference,
    save_edge_credential,
)
from larkflow.workflow.edge_cli import (
    DEFAULT_CREDENTIAL_FILE,
    _load_selected_credential,
    _migrate_credential_file,
    _run,
    _validate_serve_workspace,
    build_parser,
)
from larkflow.workflow.edge_gateway_cli import (
    _loopback_host,
    _positive_integer,
    build_parser as build_gateway_parser,
)


def test_edge_cli_exposes_pair_doctor_run_once_and_foreground_serve_commands():
    parser = build_parser()
    paired = parser.parse_args(
        ["pair", "--server", "https://edge.example.com", "--name", "Mac"]
    )
    run = parser.parse_args(
        ["run-once", "--workspace", "/workspace", "--allow-model-egress"]
    )
    serve = parser.parse_args(
        ["serve", "--workspace", "/workspace", "--allow-model-egress"]
    )
    migrate = parser.parse_args(["credential-migrate", "--delete-source"])
    doctor = parser.parse_args(["doctor", "--workspace", "/workspace"])

    assert paired.command == "pair"
    assert run.command == "run-once"
    assert serve.command == "serve"
    assert migrate.command == "credential-migrate"
    assert doctor.command == "doctor"
    assert doctor.codex_binary == "codex"
    assert run.allow_model_egress is True
    assert serve.allow_model_egress is True
    assert migrate.delete_source is True
    assert serve.wait_seconds == 20
    assert serve.heartbeat_seconds == 60
    assert serve.max_tasks == 0
    assert paired.credential_store == "auto"
    assert Path(paired.credential_file) == DEFAULT_CREDENTIAL_FILE


def test_doctor_validates_local_keychain_and_codex_without_exposing_identity(
    tmp_path: Path,
    monkeypatch,
):
    stored = StoredEdgeCredential(
        server_url="http://127.0.0.1:18765",
        device_id="private-device-id",
        credential="private-device-id.private-secret",
    )
    messages: list[dict[str, object]] = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = tmp_path / "metadata.json"
    metadata.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        edge_cli,
        "_load_selected_credential",
        lambda _namespace, path: (stored, "keychain", path, path),
    )
    monkeypatch.setattr(edge_cli.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(
        edge_cli,
        "probe_codex_workspace_isolation",
        lambda _workspace, _binary: None,
    )
    monkeypatch.setattr(edge_cli, "_print", lambda value: messages.append(value))
    namespace = build_parser().parse_args(
        [
            "--credential-file",
            str(metadata),
            "doctor",
            "--workspace",
            str(workspace),
        ]
    )

    assert _run(namespace) == 0

    assert messages == [
        {
            "event": "edge_doctor",
            "status": "ready",
            "credential": {
                "status": "ok",
                "store": "keychain",
                "secret_in_metadata": False,
            },
            "codex": {"status": "ok"},
            "workspace_isolation": {
                "status": "ok",
                "mode": "codex_permission_profile",
                "error_type": None,
            },
            "network": {
                "status": "not_checked",
                "mode": "loopback_tunnel_required",
            },
            "background_service": "not_installed",
        }
    ]
    rendered = repr(messages)
    assert stored.device_id not in rendered
    assert stored.credential not in rendered
    assert stored.server_url not in rendered


def test_doctor_reports_missing_codex_as_blocked(tmp_path: Path, monkeypatch):
    stored = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )
    messages: list[dict[str, object]] = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credential = tmp_path / "device.json"
    credential.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        edge_cli,
        "_load_selected_credential",
        lambda _namespace, path: (stored, "file", path, path),
    )
    monkeypatch.setattr(edge_cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr(edge_cli, "_print", lambda value: messages.append(value))
    namespace = build_parser().parse_args(
        [
            "--credential-file",
            str(credential),
            "doctor",
            "--workspace",
            str(workspace),
        ]
    )

    assert _run(namespace) == 2
    assert messages[0]["status"] == "blocked"
    assert messages[0]["codex"] == {"status": "missing"}
    assert messages[0]["workspace_isolation"]["status"] == "blocked"


def test_execution_requires_explicit_model_egress_acknowledgement(tmp_path: Path):
    namespace = build_parser().parse_args(
        ["run-once", "--workspace", str(tmp_path)]
    )

    with pytest.raises(ValueError, match="--allow-model-egress"):
        _run(namespace)


def test_serve_workspace_cannot_contain_the_device_credential(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credential = workspace / "device.json"
    credential.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot be inside"):
        _validate_serve_workspace(workspace, credential)

    _validate_serve_workspace(workspace, None)


def test_credential_migration_verifies_keychain_before_deleting_source(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "device.json"
    stored = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )
    save_edge_credential(source, stored)
    keychain: list[StoredEdgeCredential] = []
    messages: list[dict[str, object]] = []
    monkeypatch.setattr(
        edge_cli,
        "edge_keychain_credential_exists",
        lambda: bool(keychain),
    )
    monkeypatch.setattr(
        edge_cli,
        "save_edge_keychain_credential",
        lambda value: keychain.append(value),
    )
    monkeypatch.setattr(
        edge_cli,
        "load_edge_keychain_credential",
        lambda _reference: keychain[0],
    )
    monkeypatch.setattr(edge_cli, "_print", lambda value: messages.append(value))

    assert _migrate_credential_file(source, delete_source=True) == 0

    assert keychain == [stored]
    assert source.exists()
    assert stored.credential not in source.read_text(encoding="utf-8")
    assert load_edge_keychain_reference(source) == EdgeKeychainReference(
        stored.server_url,
        stored.device_id,
    )
    assert messages == [
        {
            "event": "edge_credential_migrated",
            "credential_store": "keychain",
            "source_file": str(source),
            "source_secret_removed": True,
        }
    ]
    assert stored.credential not in repr(messages)


def test_credential_migration_rolls_back_a_mismatched_keychain_item(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "device.json"
    stored = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )
    different = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_2",
        credential="device_2.secret",
    )
    save_edge_credential(source, stored)
    deleted: list[bool] = []
    monkeypatch.setattr(
        edge_cli,
        "edge_keychain_credential_exists",
        lambda: False,
    )
    monkeypatch.setattr(
        edge_cli,
        "save_edge_keychain_credential",
        lambda _value: None,
    )
    monkeypatch.setattr(
        edge_cli,
        "load_edge_keychain_credential",
        lambda _reference: different,
    )
    monkeypatch.setattr(
        edge_cli,
        "delete_edge_keychain_credential",
        lambda: deleted.append(True),
    )

    with pytest.raises(ValueError, match="did not match"):
        _migrate_credential_file(source, delete_source=True)

    assert source.exists()
    assert deleted == [True]


def test_keychain_loader_combines_non_secret_metadata_with_keychain(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "device.json"
    stored = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )
    edge_cli.save_edge_keychain_reference(source, stored)
    references: list[EdgeKeychainReference] = []
    monkeypatch.setattr(
        edge_cli,
        "load_edge_keychain_credential",
        lambda reference: references.append(reference) or stored,
    )

    selected = _load_selected_credential(
        SimpleNamespace(credential_store="keychain"),
        source,
    )

    assert selected == (stored, "keychain", source, source)
    assert references == [EdgeKeychainReference(stored.server_url, stored.device_id)]


def test_pair_rolls_back_a_new_keychain_item_when_metadata_write_fails(
    tmp_path: Path,
    monkeypatch,
):
    target = tmp_path / "device.json"
    stored = StoredEdgeCredential(
        server_url="https://edge.example.com",
        device_id="device_1",
        credential="device_1.secret",
    )
    keychain: list[StoredEdgeCredential] = []
    deleted: list[bool] = []

    class Transport:
        def __init__(self, _server: str) -> None:
            pass

        def pair(self, **_kwargs):
            return stored

    monkeypatch.setattr(edge_cli, "HttpEdgeTransport", Transport)
    monkeypatch.setattr(
        edge_cli,
        "edge_keychain_credential_exists",
        lambda: bool(keychain),
    )
    monkeypatch.setattr(
        edge_cli,
        "save_edge_keychain_credential",
        lambda value: keychain.append(value),
    )
    def fail_metadata_write(_path: Path, _value: StoredEdgeCredential) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        edge_cli,
        "save_edge_keychain_reference",
        fail_metadata_write,
    )

    def delete() -> None:
        keychain.clear()
        deleted.append(True)

    monkeypatch.setattr(edge_cli, "delete_edge_keychain_credential", delete)
    namespace = build_parser().parse_args(
        [
            "--credential-file",
            str(target),
            "--credential-store",
            "keychain",
            "pair",
            "--server",
            "https://edge.example.com",
            "--code",
            "pair-once",
        ]
    )

    with pytest.raises(RuntimeError, match="Keychain storage failed"):
        _run(namespace)

    assert keychain == []
    assert deleted == [True]
    assert not target.exists()


def test_gateway_rejects_non_loopback_bind_addresses():
    assert _loopback_host("127.0.0.1") == "127.0.0.1"
    assert _loopback_host("::1") == "::1"
    assert _loopback_host("localhost") == "localhost"
    with pytest.raises(ValueError, match="loopback"):
        _loopback_host("0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        _loopback_host("edge.example.com")


def test_gateway_parser_does_not_expose_capability_escalation():
    parser = build_gateway_parser()
    args = parser.parse_args(
        [
            "pairing-create",
            "--tenant",
            "tenant_1",
            "--person",
            "person_1",
            "--actor",
            "admin_1",
        ]
    )

    assert args.command == "pairing-create"
    assert not hasattr(args, "capability")
    assert _positive_integer("100000", "limit") == 100000
    with pytest.raises(ValueError):
        _positive_integer("0", "limit")
