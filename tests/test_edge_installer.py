from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "deploy" / "larkflow-edge-manager.py"


def load_installer():
    loader = importlib.machinery.SourceFileLoader("larkflow_edge_manager", str(INSTALLER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def wheel(path: Path, content: bytes, *, version: str = "0.0.1") -> tuple[Path, str]:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("payload.txt", content)
        archive.writestr(
            f"larkflow-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: larkflow\nVersion: {version}\n",
        )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def fake_prepare(module, version: str):
    def prepare(target, *, wheel, wheel_sha256, python):
        del python
        command = target / "venv" / "bin" / "larkflow-edge"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o700)
        module._write_json_once(
            target / "manifest.json",
            {
                "schema_version": 1,
                "package": "larkflow",
                "package_version": version,
                "wheel_filename": wheel.name,
                "wheel_sha256": wheel_sha256,
                "installed_at": "2026-08-05T00:00:00+00:00",
            },
        )
        return version

    return prepare


def test_install_upgrade_and_rollback_preserve_external_credentials(
    tmp_path: Path,
    monkeypatch,
):
    module = load_installer()
    prefix = tmp_path / "Library" / "Application Support" / "larkflow-edge"
    link_dir = tmp_path / ".local" / "bin"
    credential = tmp_path / ".config" / "larkflow" / "edge-device.json"
    credential.parent.mkdir(parents=True)
    credential.write_text("do-not-touch-device-secret", encoding="utf-8")

    first_wheel, first_sha = wheel(
        tmp_path / "first.whl",
        b"first-wheel",
        version="0.0.1",
    )
    monkeypatch.setattr(module, "_prepare_release", fake_prepare(module, "0.0.1"))
    first = module.install(
        prefix=prefix,
        link_dir=link_dir,
        wheel=first_wheel,
        expected_sha256=first_sha,
        python=sys.executable,
    )

    second_wheel, second_sha = wheel(
        tmp_path / "second.whl",
        b"second-wheel",
        version="0.0.2",
    )
    monkeypatch.setattr(module, "_prepare_release", fake_prepare(module, "0.0.2"))
    second = module.install(
        prefix=prefix,
        link_dir=link_dir,
        wheel=second_wheel,
        expected_sha256=second_sha,
        python=sys.executable,
    )

    assert first["operation"] == "install"
    assert second["operation"] == "upgrade"
    assert second["previous_release"] == first["release"]
    assert module.status(prefix=prefix, link_dir=link_dir)["current_release"] == second[
        "release"
    ]
    assert (link_dir / "larkflow-edge").resolve() == (
        prefix / "current" / "venv" / "bin" / "larkflow-edge"
    ).resolve()
    assert credential.read_text(encoding="utf-8") == "do-not-touch-device-secret"

    rolled_back = module.rollback(prefix=prefix, link_dir=link_dir)

    assert rolled_back["release"] == first["release"]
    after = module.status(prefix=prefix, link_dir=link_dir)
    assert after["current_release"] == first["release"]
    assert after["previous_release"] == second["release"]
    assert credential.read_text(encoding="utf-8") == "do-not-touch-device-secret"
    assert first_sha not in credential.read_text(encoding="utf-8")
    assert second_sha not in credential.read_text(encoding="utf-8")


def test_reinstalling_the_current_release_is_idempotent(
    tmp_path: Path,
    monkeypatch,
):
    module = load_installer()
    prefix = tmp_path / "prefix"
    link_dir = tmp_path / "bin"
    artifact, digest = wheel(tmp_path / "edge.whl", b"same-wheel")
    monkeypatch.setattr(module, "_prepare_release", fake_prepare(module, "0.0.1"))

    first = module.install(
        prefix=prefix,
        link_dir=link_dir,
        wheel=artifact,
        expected_sha256=digest,
        python=sys.executable,
    )
    second = module.install(
        prefix=prefix,
        link_dir=link_dir,
        wheel=artifact,
        expected_sha256=digest,
        python=sys.executable,
    )

    assert second["release"] == first["release"]
    assert second["operation"] == "verify"
    assert second["previous_release"] is None
    assert len(list((prefix / "releases").iterdir())) == 1


def test_release_is_built_at_its_final_path_to_keep_venv_scripts_valid(
    tmp_path: Path,
    monkeypatch,
):
    module = load_installer()
    prefix = tmp_path / "prefix"
    artifact, digest = wheel(tmp_path / "edge.whl", b"wheel", version="0.0.2")
    targets: list[Path] = []
    prepare = fake_prepare(module, "0.0.2")

    def record_target(target, **kwargs):
        targets.append(target)
        return prepare(target, **kwargs)

    monkeypatch.setattr(module, "_prepare_release", record_target)

    report = module.install(
        prefix=prefix,
        link_dir=tmp_path / "bin",
        wheel=artifact,
        expected_sha256=digest,
        python=sys.executable,
    )

    assert targets == [prefix / "releases" / report["release"]]
    assert ".staging" not in str(targets[0])


def test_hash_mismatch_fails_before_creating_the_installation(tmp_path: Path):
    module = load_installer()
    artifact, _digest = wheel(tmp_path / "edge.whl", b"wheel")

    with pytest.raises(module.EdgeInstallError, match="does not match"):
        module.install(
            prefix=tmp_path / "prefix",
            link_dir=tmp_path / "bin",
            wheel=artifact,
            expected_sha256="0" * 64,
            python=sys.executable,
        )

    assert not (tmp_path / "prefix").exists()


def test_unrelated_stable_command_is_never_overwritten(
    tmp_path: Path,
    monkeypatch,
):
    module = load_installer()
    prefix = tmp_path / "prefix"
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    unrelated = link_dir / "larkflow-edge"
    unrelated.write_text("unrelated", encoding="utf-8")
    artifact, digest = wheel(tmp_path / "edge.whl", b"wheel")
    monkeypatch.setattr(module, "_prepare_release", fake_prepare(module, "0.0.1"))

    with pytest.raises(module.EdgeInstallError, match="unrelated command"):
        module.install(
            prefix=prefix,
            link_dir=link_dir,
            wheel=artifact,
            expected_sha256=digest,
            python=sys.executable,
        )

    assert unrelated.read_text(encoding="utf-8") == "unrelated"
    assert not (prefix / "current").exists()


def test_symlinked_command_directory_is_rejected(tmp_path: Path, monkeypatch):
    module = load_installer()
    prefix = tmp_path / "prefix"
    actual_link_dir = tmp_path / "actual-bin"
    actual_link_dir.mkdir()
    link_dir = tmp_path / "bin"
    link_dir.symlink_to(actual_link_dir, target_is_directory=True)
    artifact, digest = wheel(tmp_path / "edge.whl", b"wheel")
    monkeypatch.setattr(module, "_prepare_release", fake_prepare(module, "0.0.1"))

    with pytest.raises(module.EdgeInstallError, match="must be a real directory"):
        module.install(
            prefix=prefix,
            link_dir=link_dir,
            wheel=artifact,
            expected_sha256=digest,
            python=sys.executable,
        )

    assert list(actual_link_dir.iterdir()) == []
    assert not (prefix / "current").exists()


def test_status_output_contains_no_credential_fields(tmp_path: Path):
    module = load_installer()
    report = module.status(prefix=tmp_path / "missing", link_dir=tmp_path / "bin")

    assert report == {
        "event": "edge_install_status",
        "installed": False,
        "credential_store_checked": False,
    }
    assert "secret" not in json.dumps(report)


def test_python_auto_discovery_skips_the_manager_interpreter(monkeypatch):
    module = load_installer()
    old_python = Path("/usr/bin/python3")
    supported = Path(sys.executable).resolve()
    monkeypatch.setattr(module.sys, "executable", str(old_python))
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: str(supported) if name == "python3.12" else None,
    )
    monkeypatch.setattr(
        module,
        "_python_version_supported",
        lambda path: path == supported,
    )

    assert module._python_executable(None) == supported


def test_explicit_unsupported_python_fails_without_fallback(monkeypatch):
    module = load_installer()
    selected = Path(sys.executable).resolve()
    monkeypatch.setattr(module, "_python_version_supported", lambda _path: False)

    with pytest.raises(module.EdgeInstallError, match="selected Python"):
        module._python_executable(str(selected))
