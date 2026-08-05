from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
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
    def prepare(
        target,
        *,
        wheel,
        wheel_sha256,
        python,
        wheelhouse=None,
        bundle_manifest_sha256=None,
        source_commit=None,
        bootstrap_pip=None,
        bootstrap_pip_version=None,
    ):
        del python, wheelhouse, bootstrap_pip
        command = target / "venv" / "bin" / "larkflow-edge"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o700)
        module._write_json_once(
            target / "manifest.json",
            {
                "schema_version": 2,
                "package": "larkflow",
                "package_version": version,
                "wheel_filename": wheel.name,
                "wheel_sha256": wheel_sha256,
                "offline_bundle": bundle_manifest_sha256 is not None,
                "bundle_manifest_sha256": bundle_manifest_sha256,
                "source_commit": source_commit,
                "bootstrap_pip_version": bootstrap_pip_version,
                "installed_at": "2026-08-05T00:00:00+00:00",
            },
        )
        return version

    return prepare


def offline_bundle(module, root: Path, artifact: Path, digest: str) -> tuple[Path, str]:
    bundle = root / "bundle"
    wheelhouse = bundle / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    bundled_artifact = wheelhouse / artifact.name
    shutil.copyfile(artifact, bundled_artifact)
    dependency = wheelhouse / "dependency-1.0-py3-none-any.whl"
    with zipfile.ZipFile(dependency, "w") as archive:
        archive.writestr(
            "dependency-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: dependency\nVersion: 1.0\n",
        )
    pip_artifact = wheelhouse / "pip-26.2.1-py3-none-any.whl"
    with zipfile.ZipFile(pip_artifact, "w") as archive:
        archive.writestr(
            "pip-26.2.1.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: pip\nVersion: 26.2.1\n",
        )
    manager = bundle / "larkflow-edge-manager"
    shutil.copyfile(INSTALLER, manager)
    manager.chmod(0o700)
    files = [manager, bundled_artifact, dependency, pip_artifact]
    manifest = {
        "schema_version": 1,
        "package": "larkflow",
        "package_version": "0.0.2",
        "source_commit": "a" * 40,
        "target": module._python_target(Path(sys.executable).resolve()),
        "artifact": {
            "path": f"wheelhouse/{artifact.name}",
            "sha256": digest,
        },
        "wheels": [
            {
                "name": name,
                "version": version,
                "path": item.relative_to(bundle).as_posix(),
                "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
            }
            for item, name, version in (
                (bundled_artifact, "larkflow", "0.0.2"),
                (dependency, "dependency", "1.0"),
                (pip_artifact, "pip", "26.2.1"),
            )
        ],
        "bootstrap": {
            "pip": {
                "name": "pip",
                "version": "26.2.1",
                "path": pip_artifact.relative_to(bundle).as_posix(),
                "sha256": hashlib.sha256(pip_artifact.read_bytes()).hexdigest(),
            }
        },
        "files": [
            {
                "path": item.relative_to(bundle).as_posix(),
                "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                "size": item.stat().st_size,
            }
            for item in files
        ],
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    return bundle, hashlib.sha256(manifest_path.read_bytes()).hexdigest()


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


def test_offline_bundle_does_not_reuse_a_direct_install_with_the_same_wheel(
    tmp_path: Path,
    monkeypatch,
):
    module = load_installer()
    prefix = tmp_path / "prefix"
    link_dir = tmp_path / "bin"
    artifact, digest = wheel(
        tmp_path / "larkflow-0.0.2-py3-none-any.whl",
        b"same-wheel",
        version="0.0.2",
    )
    monkeypatch.setattr(module, "_prepare_release", fake_prepare(module, "0.0.2"))

    direct = module.install(
        prefix=prefix,
        link_dir=link_dir,
        wheel=artifact,
        expected_sha256=digest,
        python=sys.executable,
    )
    bundle, manifest_sha256 = offline_bundle(module, tmp_path, artifact, digest)
    offline = module.install(
        prefix=prefix,
        link_dir=link_dir,
        wheel=None,
        expected_sha256=None,
        python=sys.executable,
        bundle=bundle,
        expected_manifest_sha256=manifest_sha256,
    )

    assert direct["release"].endswith(digest[:12])
    assert offline["release"].endswith(manifest_sha256[:12])
    assert offline["release"] != direct["release"]
    assert offline["previous_release"] == direct["release"]


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


def test_offline_bundle_install_verifies_all_files_before_switching(
    tmp_path: Path,
    monkeypatch,
):
    module = load_installer()
    artifact, digest = wheel(
        tmp_path / "larkflow-0.0.2-py3-none-any.whl",
        b"offline-wheel",
        version="0.0.2",
    )
    bundle, manifest_sha256 = offline_bundle(module, tmp_path, artifact, digest)
    prepared: list[dict[str, object]] = []
    base_prepare = fake_prepare(module, "0.0.2")

    def record_prepare(target, **kwargs):
        prepared.append(kwargs)
        return base_prepare(target, **kwargs)

    monkeypatch.setattr(module, "_prepare_release", record_prepare)

    report = module.install(
        prefix=tmp_path / "prefix",
        link_dir=tmp_path / "bin",
        wheel=None,
        expected_sha256=None,
        python=sys.executable,
        bundle=bundle,
        expected_manifest_sha256=manifest_sha256,
    )

    assert report["offline_bundle"] is True
    assert report["bundle_manifest_sha256"] == manifest_sha256
    assert prepared[0]["wheelhouse"] == bundle / "wheelhouse"
    assert prepared[0]["source_commit"] == "a" * 40
    assert (tmp_path / "prefix" / "bin" / "larkflow-edge-manager").read_bytes() == (
        bundle / "larkflow-edge-manager"
    ).read_bytes()


def test_offline_bundle_tamper_fails_before_installation(tmp_path: Path):
    module = load_installer()
    artifact, digest = wheel(
        tmp_path / "larkflow-0.0.2-py3-none-any.whl",
        b"offline-wheel",
        version="0.0.2",
    )
    bundle, manifest_sha256 = offline_bundle(module, tmp_path, artifact, digest)
    (bundle / "wheelhouse" / "dependency-1.0-py3-none-any.whl").write_bytes(
        b"tampered"
    )

    with pytest.raises(module.EdgeInstallError, match="verification failed"):
        module.install(
            prefix=tmp_path / "prefix",
            link_dir=tmp_path / "bin",
            wheel=None,
            expected_sha256=None,
            python=sys.executable,
            bundle=bundle,
            expected_manifest_sha256=manifest_sha256,
        )

    assert not (tmp_path / "prefix").exists()


def test_offline_bundle_rejects_unlisted_files(tmp_path: Path):
    module = load_installer()
    artifact, digest = wheel(
        tmp_path / "larkflow-0.0.2-py3-none-any.whl",
        b"offline-wheel",
        version="0.0.2",
    )
    bundle, manifest_sha256 = offline_bundle(module, tmp_path, artifact, digest)
    (bundle / "wheelhouse" / "unlisted.whl").write_bytes(b"unlisted")

    with pytest.raises(module.EdgeInstallError, match="exactly match"):
        module._verified_bundle(
            bundle,
            expected_manifest_sha256=manifest_sha256,
            python=Path(sys.executable).resolve(),
        )


def test_offline_bundle_rejects_wrong_python_target(tmp_path: Path):
    module = load_installer()
    artifact, digest = wheel(
        tmp_path / "larkflow-0.0.2-py3-none-any.whl",
        b"offline-wheel",
        version="0.0.2",
    )
    bundle, _manifest_sha256 = offline_bundle(module, tmp_path, artifact, digest)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["target"]["python_version"] = "9.9"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    new_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    with pytest.raises(module.EdgeInstallError, match="python_version"):
        module._verified_bundle(
            bundle,
            expected_manifest_sha256=new_digest,
            python=Path(sys.executable).resolve(),
        )


def test_offline_prepare_forces_no_index_and_binary_wheels(
    tmp_path: Path,
    monkeypatch,
):
    module = load_installer()
    calls: list[tuple[list[str], bool]] = []
    venv_python = tmp_path / "release" / "venv" / "bin" / "python"

    def fake_run(argv, *, stdout=None, label, offline=False):
        del stdout, label
        calls.append((argv, offline))
        if argv[:3] == [sys.executable, "-m", "venv"]:
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(module, "_run", fake_run)
    def fake_capture(argv, *, label=None, offline=False):
        del label, offline
        return "26.2.1" if "version('pip')" in argv[-1] else "0.0.2"

    monkeypatch.setattr(module, "_capture", fake_capture)
    monkeypatch.setattr(module, "_verify_edge_command", lambda _command: None)
    wheel_path = tmp_path / "larkflow.whl"
    wheel_path.write_bytes(b"wheel")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    bootstrap_pip = wheelhouse / "pip-26.2.1-py3-none-any.whl"
    bootstrap_pip.write_bytes(b"pip")

    module._prepare_release(
        tmp_path / "release",
        wheel=wheel_path,
        wheel_sha256="1" * 64,
        python=Path(sys.executable),
        wheelhouse=wheelhouse,
        bundle_manifest_sha256="2" * 64,
        source_commit="a" * 40,
        bootstrap_pip=bootstrap_pip,
        bootstrap_pip_version="26.2.1",
    )

    pip_install = next(
        argv for argv, _offline in calls if "install" in argv and "--find-links" in argv
    )
    install_calls = [argv for argv, _offline in calls if "install" in argv]
    assert install_calls[0][-1] == str(bootstrap_pip)
    assert "--no-index" in install_calls[0]
    assert install_calls[1] == pip_install
    assert "--no-index" in pip_install
    assert "--only-binary=:all:" in pip_install
    assert pip_install[pip_install.index("--find-links") + 1] == str(wheelhouse)
    assert all(offline for _argv, offline in calls)


def test_offline_subprocess_environment_removes_network_and_python_injection(
    monkeypatch,
):
    module = load_installer()
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("PYTHONPATH", "/tmp/injected")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")

    env = module._subprocess_env(offline=True)

    assert env["PIP_NO_INDEX"] == "1"
    assert env["PIP_NO_CACHE_DIR"] == "1"
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert "PIP_INDEX_URL" not in env
    assert "PYTHONPATH" not in env
    assert "HTTPS_PROXY" not in env
