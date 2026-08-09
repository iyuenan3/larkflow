#!/usr/bin/env python3
"""Install and atomically switch the macOS Personal Agent Edge CLI."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.parser import Parser
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from typing import Any, Sequence
from uuid import uuid4
import zipfile


DEFAULT_PREFIX = Path("~/Library/Application Support/larkflow-edge")
DEFAULT_LINK_DIR = Path("~/.local/bin")
MANAGER_NAME = "larkflow-edge-manager"
EDGE_NAME = "larkflow-edge"
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._+-]+-[0-9a-f]{12}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MINIMUM_BOOTSTRAP_PIP = (26, 1, 2)
BUNDLE_MANIFEST = "manifest.json"
BUNDLE_MANAGER = "larkflow-edge-manager"
BUNDLE_REQUIREMENTS_LOCK = "requirements.lock"
BUNDLE_SBOM = "sbom.spdx.json"
BUNDLE_BUILD_PROOF = "build-proof.json"
EDGE_DISTRIBUTION = "larkflow-personal-edge"
EDGE_MODULES = (
    "larkflow/workflow/edge_agent.py",
    "larkflow/workflow/edge_cli.py",
    "larkflow/workflow/edge_client.py",
    "larkflow/workflow/edge_contract.py",
)


class EdgeInstallError(RuntimeError):
    """Expected installation failure with a user-actionable message."""


class InstallSource:
    __slots__ = (
        "wheel",
        "wheel_sha256",
        "manager",
        "wheelhouse",
        "bundle_manifest_sha256",
        "source_commit",
        "bootstrap_pip",
        "bootstrap_pip_version",
        "requirements_lock",
    )

    def __init__(
        self,
        *,
        wheel: Path,
        wheel_sha256: str,
        manager: Path,
        wheelhouse: Path | None = None,
        bundle_manifest_sha256: str | None = None,
        source_commit: str | None = None,
        bootstrap_pip: Path | None = None,
        bootstrap_pip_version: str | None = None,
        requirements_lock: Path | None = None,
    ) -> None:
        self.wheel = wheel
        self.wheel_sha256 = wheel_sha256
        self.manager = manager
        self.wheelhouse = wheelhouse
        self.bundle_manifest_sha256 = bundle_manifest_sha256
        self.source_commit = source_commit
        self.bootstrap_pip = bootstrap_pip
        self.bootstrap_pip_version = bootstrap_pip_version
        self.requirements_lock = requirements_lock


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=MANAGER_NAME,
        description=(
            "Install, upgrade, inspect, or roll back the user-owned "
            "larkflow Personal Agent Edge CLI"
        ),
    )
    parser.add_argument(
        "--prefix",
        default=str(DEFAULT_PREFIX),
        help="versioned installation root",
    )
    parser.add_argument(
        "--link-dir",
        default=str(DEFAULT_LINK_DIR),
        help="directory for stable user command links",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    install = commands.add_parser(
        "install",
        help="install or atomically upgrade from a verified wheel",
    )
    source = install.add_mutually_exclusive_group(required=True)
    source.add_argument("--wheel")
    source.add_argument(
        "--bundle",
        help="verified offline bundle containing the wheel and dependencies",
    )
    install.add_argument(
        "--sha256",
        help="expected SHA-256 of the wheel",
    )
    install.add_argument(
        "--manifest-sha256",
        help="expected SHA-256 of the offline bundle manifest",
    )
    install.add_argument(
        "--python",
        default=None,
        help="optional Python 3.10+ used only to create the managed venv",
    )

    commands.add_parser("status", help="show non-secret installation state")
    commands.add_parser(
        "rollback",
        help="atomically switch current and previous releases",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    namespace = build_parser().parse_args(
        list(sys.argv[1:] if argv is None else argv)
    )
    try:
        _require_macos_user()
        prefix = _private_directory(Path(namespace.prefix).expanduser())
        link_dir = _private_directory(Path(namespace.link_dir).expanduser())
        if namespace.command == "install":
            report = install(
                prefix=prefix,
                link_dir=link_dir,
                wheel=(
                    Path(namespace.wheel).expanduser()
                    if namespace.wheel is not None
                    else None
                ),
                expected_sha256=namespace.sha256,
                python=namespace.python,
                bundle=(
                    Path(namespace.bundle).expanduser()
                    if namespace.bundle is not None
                    else None
                ),
                expected_manifest_sha256=namespace.manifest_sha256,
            )
        elif namespace.command == "rollback":
            report = rollback(prefix=prefix, link_dir=link_dir)
        else:
            report = status(prefix=prefix, link_dir=link_dir)
        _print(report)
        return 0
    except Exception as exc:
        _print(
            {
                "event": "edge_install_failed",
                "command": namespace.command,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 1


def install(
    *,
    prefix: Path,
    link_dir: Path,
    wheel: Path | None,
    expected_sha256: str | None,
    python: str | None,
    bundle: Path | None = None,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Build one release completely before switching the stable command."""
    python_path = _python_executable(python)
    source = _install_source(
        wheel=wheel,
        expected_sha256=expected_sha256,
        bundle=bundle,
        expected_manifest_sha256=expected_manifest_sha256,
        python=python_path,
    )
    _preflight_layout(prefix, link_dir)
    current_before = _linked_release_id(prefix, "current", required=False)
    wheel_sha256 = source.wheel_sha256
    package_version = _wheel_package_version(source.wheel)
    release_identity_sha256 = source.bundle_manifest_sha256 or wheel_sha256
    release_id = _release_id(package_version, release_identity_sha256)
    release = prefix / "releases" / release_id
    created_release = False
    activated = False
    try:
        if release.exists() or release.is_symlink():
            _validate_release(release, expected_sha256=wheel_sha256)
        else:
            release.mkdir(mode=0o700)
            created_release = True
            installed_version = _prepare_release(
                release,
                wheel=source.wheel,
                wheel_sha256=wheel_sha256,
                python=python_path,
                wheelhouse=source.wheelhouse,
                bundle_manifest_sha256=source.bundle_manifest_sha256,
                source_commit=source.source_commit,
                bootstrap_pip=source.bootstrap_pip,
                bootstrap_pip_version=source.bootstrap_pip_version,
                requirements_lock=source.requirements_lock,
            )
            if installed_version != package_version:
                raise EdgeInstallError(
                    "installed package version does not match wheel metadata"
                )
        _install_stable_commands(prefix, link_dir, manager_source=source.manager)
        previous_release = _activate(prefix, release_id)
        activated = True
    except Exception:
        if created_release and not activated and release.is_dir() and not release.is_symlink():
            shutil.rmtree(release)
        raise

    return {
        "event": "edge_install_complete",
        "operation": (
            "install"
            if current_before is None
            else "verify"
            if current_before == release_id
            else "upgrade"
        ),
        "release": release_id,
        "previous_release": previous_release,
        "package_version": package_version,
        "wheel_sha256": wheel_sha256,
        "offline_bundle": source.wheelhouse is not None,
        "bundle_manifest_sha256": source.bundle_manifest_sha256,
        "activated": activated,
        "credential_store_touched": False,
        "background_service_installed": False,
    }


def status(*, prefix: Path, link_dir: Path) -> dict[str, Any]:
    current = _linked_release_id(prefix, "current", required=False)
    previous = _linked_release_id(prefix, "previous", required=False)
    if current is None:
        return {
            "event": "edge_install_status",
            "installed": False,
            "credential_store_checked": False,
        }
    manifest = _validate_release(prefix / "releases" / current)
    commands = {
        name: _external_link_matches(link_dir / name, prefix / "bin" / name)
        for name in (EDGE_NAME, MANAGER_NAME)
    }
    return {
        "event": "edge_install_status",
        "installed": True,
        "current_release": current,
        "previous_release": previous,
        "package_version": manifest["package_version"],
        "wheel_sha256": manifest["wheel_sha256"],
        "offline_bundle": bool(manifest.get("offline_bundle", False)),
        "bundle_manifest_sha256": manifest.get("bundle_manifest_sha256"),
        "source_commit": manifest.get("source_commit"),
        "stable_commands": commands,
        "credential_store_checked": False,
        "background_service_installed": False,
    }


def rollback(*, prefix: Path, link_dir: Path) -> dict[str, Any]:
    _preflight_layout(prefix, link_dir)
    current = _linked_release_id(prefix, "current", required=True)
    previous = _linked_release_id(prefix, "previous", required=True)
    if current == previous:
        raise EdgeInstallError("current and previous releases cannot be the same")
    _validate_release(prefix / "releases" / current)
    previous_manifest = _validate_release(prefix / "releases" / previous)
    _verify_edge_command(prefix / "releases" / previous / "venv" / "bin" / EDGE_NAME)
    _atomic_symlink(prefix / "current", Path("releases") / previous)
    _atomic_symlink(prefix / "previous", Path("releases") / current)
    _install_stable_commands(prefix, link_dir)
    return {
        "event": "edge_install_complete",
        "operation": "rollback",
        "release": previous,
        "previous_release": current,
        "package_version": previous_manifest["package_version"],
        "wheel_sha256": previous_manifest["wheel_sha256"],
        "credential_store_touched": False,
        "background_service_installed": False,
    }


def _prepare_release(
    target: Path,
    *,
    wheel: Path,
    wheel_sha256: str,
    python: Path,
    wheelhouse: Path | None = None,
    bundle_manifest_sha256: str | None = None,
    source_commit: str | None = None,
    bootstrap_pip: Path | None = None,
    bootstrap_pip_version: str | None = None,
    requirements_lock: Path | None = None,
) -> str:
    venv = target / "venv"
    _run(
        [str(python), "-m", "venv", str(venv)],
        label="create managed virtual environment",
        offline=True,
    )
    venv_python = venv / "bin" / "python"
    if wheelhouse is not None:
        if bootstrap_pip is None or bootstrap_pip_version is None:
            raise EdgeInstallError("offline bundle bootstrap pip is missing")
        _run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-index",
                "--only-binary=:all:",
                "--no-deps",
                "--force-reinstall",
                str(bootstrap_pip),
            ],
            label="install verified bootstrap pip",
            offline=True,
        )
        installed_pip_version = _capture(
            [
                str(venv_python),
                "-c",
                "from importlib.metadata import version; print(version('pip'))",
            ],
            label="read installed pip version",
            offline=True,
        ).strip()
        if installed_pip_version != bootstrap_pip_version:
            raise EdgeInstallError("installed pip version does not match the bundle")
    install_argv = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
    ]
    if wheelhouse is not None:
        install_argv.extend(
            [
                "--no-index",
                "--only-binary=:all:",
                "--find-links",
                str(wheelhouse),
            ]
        )
    if requirements_lock is not None:
        install_argv.extend(["--require-hashes", "-r", str(requirements_lock)])
    else:
        install_argv.append(str(wheel))
    _run(
        install_argv,
        label="install wheel and dependencies",
        offline=wheelhouse is not None,
    )
    _run(
        [str(venv_python), "-m", "pip", "check"],
        label="validate installed dependencies",
        offline=True,
    )
    package_name, _wheel_version = _wheel_identity(wheel)
    package_version = _capture(
        [
            str(venv_python),
            "-c",
            (
                "from importlib.metadata import version; import sys; "
                "print(version(sys.argv[1]))"
            ),
            package_name,
        ]
    ).strip()
    if not package_version or not re.fullmatch(r"[A-Za-z0-9._+-]+", package_version):
        raise EdgeInstallError("installed larkflow package version is invalid")
    _verify_edge_command(venv / "bin" / EDGE_NAME)
    _write_json_once(
        target / "manifest.json",
        {
            "schema_version": 2,
            "package": package_name,
            "package_version": package_version,
            "wheel_filename": wheel.name,
            "wheel_sha256": wheel_sha256,
            "offline_bundle": wheelhouse is not None,
            "bundle_manifest_sha256": bundle_manifest_sha256,
            "source_commit": source_commit,
            "bootstrap_pip_version": bootstrap_pip_version,
            "installed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return package_version


def _activate(prefix: Path, release_id: str) -> str | None:
    current = _linked_release_id(prefix, "current", required=False)
    if current == release_id:
        return _linked_release_id(prefix, "previous", required=False)
    if current is not None:
        _atomic_symlink(prefix / "previous", Path("releases") / current)
    _atomic_symlink(prefix / "current", Path("releases") / release_id)
    return current


def _install_stable_commands(
    prefix: Path,
    link_dir: Path,
    *,
    manager_source: Path | None = None,
) -> None:
    bin_dir = prefix / "bin"
    bin_dir.mkdir(mode=0o700, exist_ok=True)
    manager_source = (
        Path(__file__).resolve(strict=True)
        if manager_source is None
        else manager_source.resolve(strict=True)
    )
    if manager_source.is_symlink() or not manager_source.is_file():
        raise EdgeInstallError("manager source must be a regular file")
    manager_target = bin_dir / MANAGER_NAME
    temporary = bin_dir / f".{MANAGER_NAME}.{uuid4().hex}.tmp"
    shutil.copyfile(manager_source, temporary)
    os.chmod(temporary, 0o700)
    os.replace(temporary, manager_target)
    _atomic_symlink(
        bin_dir / EDGE_NAME,
        Path("..") / "current" / "venv" / "bin" / EDGE_NAME,
    )
    link_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in (EDGE_NAME, MANAGER_NAME):
        _install_external_link(link_dir / name, bin_dir / name)


def _preflight_layout(prefix: Path, link_dir: Path) -> None:
    prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
    if prefix.is_symlink() or not prefix.is_dir():
        raise EdgeInstallError("installation prefix must be a real directory")
    os.chmod(prefix, 0o700)
    link_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if link_dir.is_symlink() or not link_dir.is_dir():
        raise EdgeInstallError("stable command directory must be a real directory")
    releases = prefix / "releases"
    releases.mkdir(mode=0o700, exist_ok=True)
    if releases.is_symlink() or not releases.is_dir():
        raise EdgeInstallError("release root must be a real directory")
    bin_dir = prefix / "bin"
    if bin_dir.exists() or bin_dir.is_symlink():
        if bin_dir.is_symlink() or not bin_dir.is_dir():
            raise EdgeInstallError("managed command directory must be a real directory")
    for name in ("current", "previous"):
        _linked_release_id(prefix, name, required=False)
    for name in (EDGE_NAME, MANAGER_NAME):
        destination = link_dir / name
        stable = prefix / "bin" / name
        if destination.exists() or destination.is_symlink():
            if not _external_link_matches(destination, stable):
                raise EdgeInstallError(
                    f"refusing to replace unrelated command: {destination}"
                )


def _private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise EdgeInstallError("installation paths must be absolute")
    return path


def _verified_wheel(path: Path, expected_sha256: str) -> Path:
    if not SHA256_PATTERN.fullmatch(expected_sha256):
        raise EdgeInstallError("--sha256 must contain exactly 64 hexadecimal characters")
    if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
        raise EdgeInstallError("wheel must be a regular .whl file, not a symlink")
    actual = _sha256(path)
    if not hmac.compare_digest(actual, expected_sha256.lower()):
        raise EdgeInstallError("wheel SHA-256 does not match the expected value")
    return path.resolve(strict=True)


def _install_source(
    *,
    wheel: Path | None,
    expected_sha256: str | None,
    bundle: Path | None,
    expected_manifest_sha256: str | None,
    python: Path,
) -> InstallSource:
    if bundle is not None:
        if wheel is not None or expected_sha256 is not None:
            raise EdgeInstallError("--bundle cannot be combined with --wheel or --sha256")
        if expected_manifest_sha256 is None:
            raise EdgeInstallError("--manifest-sha256 is required with --bundle")
        return _verified_bundle(
            bundle,
            expected_manifest_sha256=expected_manifest_sha256,
            python=python,
        )
    if wheel is None:
        raise EdgeInstallError("--wheel is required without --bundle")
    if expected_sha256 is None:
        raise EdgeInstallError("--sha256 is required with --wheel")
    if expected_manifest_sha256 is not None:
        raise EdgeInstallError("--manifest-sha256 is only valid with --bundle")
    verified = _verified_wheel(wheel, expected_sha256)
    return InstallSource(
        wheel=verified,
        wheel_sha256=_sha256(verified),
        manager=Path(__file__).resolve(strict=True),
    )


def _verified_bundle(
    path: Path,
    *,
    expected_manifest_sha256: str,
    python: Path,
) -> InstallSource:
    if not SHA256_PATTERN.fullmatch(expected_manifest_sha256):
        raise EdgeInstallError(
            "--manifest-sha256 must contain exactly 64 hexadecimal characters"
        )
    if path.is_symlink() or not path.is_dir():
        raise EdgeInstallError("bundle must be a real directory")
    bundle = path.resolve(strict=True)
    manifest_path = bundle / BUNDLE_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise EdgeInstallError("bundle manifest is missing")
    actual_manifest_sha256 = _sha256(manifest_path)
    if not hmac.compare_digest(
        actual_manifest_sha256,
        expected_manifest_sha256.lower(),
    ):
        raise EdgeInstallError("bundle manifest SHA-256 does not match")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EdgeInstallError("bundle manifest cannot be read") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") not in {1, 2}
    ):
        raise EdgeInstallError("bundle manifest is invalid")
    schema_version = payload["schema_version"]
    bundle_package = payload.get("package")
    expected_package = "larkflow" if schema_version == 1 else EDGE_DISTRIBUTION
    if bundle_package != expected_package:
        raise EdgeInstallError(f"bundle package must be {expected_package}")
    source_commit = payload.get("source_commit")
    if not isinstance(source_commit, str) or not COMMIT_PATTERN.fullmatch(source_commit):
        raise EdgeInstallError("bundle source commit is invalid")
    target = payload.get("target")
    if not isinstance(target, dict):
        raise EdgeInstallError("bundle target is invalid")
    current_target = _python_target(python)
    for key in ("platform", "machine", "python_implementation", "python_version"):
        if target.get(key) != current_target[key]:
            raise EdgeInstallError(f"bundle target {key} does not match this Mac")

    declared_files = payload.get("files")
    if not isinstance(declared_files, list) or not declared_files:
        raise EdgeInstallError("bundle file list is invalid")
    expected_files: dict[str, tuple[str, int]] = {}
    for item in declared_files:
        if not isinstance(item, dict):
            raise EdgeInstallError("bundle file entry is invalid")
        relative = _safe_bundle_relative_path(item.get("path"))
        digest = item.get("sha256")
        size = item.get("size")
        if (
            relative in expected_files
            or not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise EdgeInstallError("bundle file entry is invalid")
        expected_files[relative] = (digest.lower(), size)
    actual_files = _bundle_regular_files(bundle)
    if set(actual_files) != set(expected_files):
        raise EdgeInstallError("bundle files do not exactly match the manifest")
    for relative, file_path in actual_files.items():
        expected_digest, expected_size = expected_files[relative]
        if file_path.stat().st_size != expected_size or not hmac.compare_digest(
            _sha256(file_path),
            expected_digest,
        ):
            raise EdgeInstallError(f"bundle file verification failed: {relative}")

    wheel_paths = {
        relative
        for relative in expected_files
        if relative.startswith("wheelhouse/")
        and relative.count("/") == 1
        and relative.endswith(".whl")
    }
    inventory = _verify_bundle_inventory(
        bundle,
        payload.get("wheels"),
        wheel_paths=wheel_paths,
        expected_files=expected_files,
        required_package=expected_package,
    )
    bootstrap = payload.get("bootstrap")
    if not isinstance(bootstrap, dict) or not isinstance(bootstrap.get("pip"), dict):
        raise EdgeInstallError("bundle bootstrap metadata is invalid")
    pip_bootstrap = bootstrap["pip"]
    pip_path = _safe_bundle_relative_path(pip_bootstrap.get("path"))
    pip_version = pip_bootstrap.get("version")
    pip_digest = pip_bootstrap.get("sha256")
    if (
        inventory.get("pip") != (pip_path, pip_version, pip_digest)
        or not isinstance(pip_version, str)
        or not _pip_version_supported(pip_version)
    ):
        raise EdgeInstallError("bundle bootstrap pip is invalid")

    artifact = payload.get("artifact")
    if not isinstance(artifact, dict):
        raise EdgeInstallError("bundle artifact is invalid")
    wheel_relative = _safe_bundle_relative_path(artifact.get("path"))
    wheel_sha256 = artifact.get("sha256")
    if (
        not wheel_relative.startswith("wheelhouse/")
        or wheel_relative.count("/") != 1
        or not wheel_relative.endswith(".whl")
        or not isinstance(wheel_sha256, str)
        or not SHA256_PATTERN.fullmatch(wheel_sha256)
    ):
        raise EdgeInstallError("bundle artifact is invalid")
    wheelhouse = bundle / "wheelhouse"
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise EdgeInstallError("bundle wheelhouse is invalid")
    wheel = _verified_wheel(bundle / wheel_relative, wheel_sha256)
    if expected_files[wheel_relative][0] != wheel_sha256.lower():
        raise EdgeInstallError("bundle artifact hash disagrees with the file list")
    artifact_name, package_version = _wheel_identity(wheel)
    if artifact_name != expected_package:
        raise EdgeInstallError("bundle artifact package disagrees with the manifest")
    if payload.get("package_version") != package_version:
        raise EdgeInstallError("bundle package version disagrees with the artifact")
    requirements_lock = _verify_bundle_evidence(
        bundle,
        schema_version=schema_version,
        evidence=payload.get("evidence"),
        expected_files=expected_files,
        inventory=inventory,
        source_commit=source_commit,
        target=target,
        artifact_path=wheel_relative,
        artifact_sha256=wheel_sha256.lower(),
    )
    evidence_paths = (
        {BUNDLE_REQUIREMENTS_LOCK, BUNDLE_SBOM, BUNDLE_BUILD_PROOF}
        if schema_version == 2
        else set()
    )
    for relative in expected_files:
        if relative == BUNDLE_MANAGER or relative in evidence_paths:
            continue
        if (
            not relative.startswith("wheelhouse/")
            or relative.count("/") != 1
            or not relative.endswith(".whl")
        ):
            raise EdgeInstallError("bundle contains an unsupported file")
    manager = bundle / BUNDLE_MANAGER
    if BUNDLE_MANAGER not in expected_files or not os.access(manager, os.X_OK):
        raise EdgeInstallError("bundle manager is missing or not executable")
    return InstallSource(
        wheel=wheel,
        wheel_sha256=wheel_sha256.lower(),
        manager=manager,
        wheelhouse=wheelhouse,
        bundle_manifest_sha256=actual_manifest_sha256,
        source_commit=source_commit,
        bootstrap_pip=bundle / pip_path,
        bootstrap_pip_version=pip_version,
        requirements_lock=requirements_lock,
    )


def _safe_bundle_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise EdgeInstallError("bundle file path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise EdgeInstallError("bundle file path is invalid")
    return pure.as_posix()


def _bundle_regular_files(bundle: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for root, directories, filenames in os.walk(bundle, followlinks=False):
        root_path = Path(root)
        for name in directories:
            candidate = root_path / name
            if candidate.is_symlink():
                raise EdgeInstallError("bundle directories cannot be symlinks")
        for name in filenames:
            candidate = root_path / name
            relative = candidate.relative_to(bundle).as_posix()
            if relative == BUNDLE_MANIFEST:
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise EdgeInstallError("bundle files must be regular files")
            files[relative] = candidate
    return files


def _verify_bundle_inventory(
    bundle: Path,
    value: Any,
    *,
    wheel_paths: set[str],
    expected_files: dict[str, tuple[str, int]],
    required_package: str,
) -> dict[str, tuple[str, str, str]]:
    if not isinstance(value, list) or not value:
        raise EdgeInstallError("bundle wheel inventory is invalid")
    declared_paths: set[str] = set()
    package_names: set[str] = set()
    inventory: dict[str, tuple[str, str, str]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise EdgeInstallError("bundle wheel inventory is invalid")
        relative = _safe_bundle_relative_path(item.get("path"))
        name = item.get("name")
        version = item.get("version")
        digest = item.get("sha256")
        if (
            relative in declared_paths
            or relative not in wheel_paths
            or not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
            or name in package_names
            or not isinstance(version, str)
            or not re.fullmatch(r"[A-Za-z0-9._+-]+", version)
            or not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
            or expected_files[relative][0] != digest.lower()
        ):
            raise EdgeInstallError("bundle wheel inventory is invalid")
        actual_name, actual_version = _wheel_identity(bundle / relative)
        if actual_name != name or actual_version != version:
            raise EdgeInstallError("bundle wheel inventory disagrees with metadata")
        declared_paths.add(relative)
        package_names.add(name)
        inventory[name] = (relative, version, digest.lower())
    if declared_paths != wheel_paths or required_package not in package_names:
        raise EdgeInstallError("bundle wheel inventory is incomplete")
    return inventory


def _verify_bundle_evidence(
    bundle: Path,
    *,
    schema_version: int,
    evidence: Any,
    expected_files: dict[str, tuple[str, int]],
    inventory: dict[str, tuple[str, str, str]],
    source_commit: str,
    target: dict[str, Any],
    artifact_path: str,
    artifact_sha256: str,
) -> Path | None:
    if schema_version == 1:
        if evidence is not None:
            raise EdgeInstallError("legacy bundle cannot declare build evidence")
        return None
    if not isinstance(evidence, dict):
        raise EdgeInstallError("bundle build evidence is missing")
    expected = {
        "requirements_lock": BUNDLE_REQUIREMENTS_LOCK,
        "sbom": BUNDLE_SBOM,
        "build_proof": BUNDLE_BUILD_PROOF,
    }
    evidence_digests: dict[str, str] = {}
    for key, expected_path in expected.items():
        item = evidence.get(key)
        if not isinstance(item, dict):
            raise EdgeInstallError("bundle build evidence is invalid")
        relative = _safe_bundle_relative_path(item.get("path"))
        digest = item.get("sha256")
        if (
            relative != expected_path
            or not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
            or expected_files.get(relative, (None, None))[0] != digest.lower()
        ):
            raise EdgeInstallError("bundle build evidence is invalid")
        evidence_digests[key] = digest.lower()

    lock_path = bundle / BUNDLE_REQUIREMENTS_LOCK
    try:
        actual_lock = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EdgeInstallError("bundle requirements lock cannot be read") from exc
    runtime_inventory = {
        name: value for name, value in inventory.items() if name != "pip"
    }
    if actual_lock != _requirements_lock(runtime_inventory):
        raise EdgeInstallError("bundle requirements lock disagrees with wheel inventory")

    try:
        sbom = json.loads((bundle / BUNDLE_SBOM).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EdgeInstallError("bundle SBOM cannot be read") from exc
    if not isinstance(sbom, dict) or sbom.get("spdxVersion") != "SPDX-2.3":
        raise EdgeInstallError("bundle SBOM is invalid")
    sbom_packages = sbom.get("packages")
    if not isinstance(sbom_packages, list):
        raise EdgeInstallError("bundle SBOM package inventory is invalid")
    described: dict[str, tuple[str, str]] = {}
    for item in sbom_packages:
        if not isinstance(item, dict):
            raise EdgeInstallError("bundle SBOM package inventory is invalid")
        name = item.get("name")
        version = item.get("versionInfo")
        checksums = item.get("checksums")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(checksums, list)
        ):
            raise EdgeInstallError("bundle SBOM package inventory is invalid")
        sha256_values = {
            value.get("checksumValue")
            for value in checksums
            if isinstance(value, dict) and value.get("algorithm") == "SHA256"
        }
        if len(sha256_values) != 1:
            raise EdgeInstallError("bundle SBOM package inventory is invalid")
        digest = next(iter(sha256_values))
        if not isinstance(digest, str):
            raise EdgeInstallError("bundle SBOM package inventory is invalid")
        described[name] = (version, digest.lower())
    expected_described = {
        name: (version, digest)
        for name, (_path, version, digest) in inventory.items()
    }
    if described != expected_described:
        raise EdgeInstallError("bundle SBOM disagrees with wheel inventory")

    try:
        proof = json.loads((bundle / BUNDLE_BUILD_PROOF).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EdgeInstallError("bundle build proof cannot be read") from exc
    if (
        not isinstance(proof, dict)
        or proof.get("schema_version") != 1
        or proof.get("source_commit") != source_commit
        or proof.get("target") != target
        or proof.get("edge_modules") != list(EDGE_MODULES)
        or proof.get("artifact")
        != {"path": artifact_path, "sha256": artifact_sha256}
        or proof.get("requirements_lock")
        != {
            "path": BUNDLE_REQUIREMENTS_LOCK,
            "sha256": evidence_digests["requirements_lock"],
        }
        or proof.get("sbom")
        != {"path": BUNDLE_SBOM, "sha256": evidence_digests["sbom"]}
    ):
        raise EdgeInstallError("bundle build proof is invalid")
    source_artifact = proof.get("source_artifact")
    if (
        not isinstance(source_artifact, dict)
        or not isinstance(source_artifact.get("filename"), str)
        or not isinstance(source_artifact.get("sha256"), str)
        or not SHA256_PATTERN.fullmatch(source_artifact["sha256"])
    ):
        raise EdgeInstallError("bundle source artifact proof is invalid")
    return lock_path


def _requirements_lock(
    inventory: dict[str, tuple[str, str, str]],
) -> str:
    lines = [
        f"{name}=={version} --hash=sha256:{digest}"
        for name, (_path, version, digest) in sorted(inventory.items())
    ]
    return "\n".join(lines) + "\n"


def _python_target(python: Path) -> dict[str, str]:
    raw = _capture(
        [
            str(python),
            "-c",
            (
                "import json,platform,sys; "
                "print(json.dumps({'platform': sys.platform, "
                "'machine': platform.machine(), "
                "'python_implementation': sys.implementation.name, "
                "'python_version': f'{sys.version_info.major}.{sys.version_info.minor}'}))"
            ),
        ]
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EdgeInstallError("selected Python target cannot be inspected") from exc
    if not isinstance(payload, dict):
        raise EdgeInstallError("selected Python target cannot be inspected")
    return {str(key): str(value) for key, value in payload.items()}


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_files = [
                name
                for name in archive.namelist()
                if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_files) != 1:
                raise EdgeInstallError("wheel must contain one dist-info METADATA file")
            raw = archive.read(metadata_files[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise EdgeInstallError("wheel metadata cannot be read") from exc
    metadata = Parser().parsestr(raw)
    package_name = re.sub(
        r"[-_.]+",
        "-",
        str(metadata.get("Name", "")).strip().lower(),
    )
    version = str(metadata.get("Version", ""))
    if not package_name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", package_name):
        raise EdgeInstallError("wheel package name is invalid")
    if not version or not re.fullmatch(r"[A-Za-z0-9._+-]+", version):
        raise EdgeInstallError("wheel package version is invalid")
    return package_name, version


def _wheel_package_version(path: Path) -> str:
    package_name, version = _wheel_identity(path)
    if package_name not in {"larkflow", EDGE_DISTRIBUTION}:
        raise EdgeInstallError("wheel package must be larkflow or larkflow-personal-edge")
    return version


def _pip_version_supported(value: str) -> bool:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", value)
    if match is None:
        return False
    version = tuple(int(part or "0") for part in match.groups())
    return MINIMUM_BOOTSTRAP_PIP <= version < (27, 0, 0)


def _python_executable(value: str | None) -> Path:
    names = (
        [value]
        if value is not None
        else [
            sys.executable,
            "python3.13",
            "python3.12",
            "python3.11",
            "python3.10",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "python3",
        ]
    )
    seen: set[Path] = set()
    for name in names:
        if name is None:
            continue
        candidate = Path(name).expanduser()
        if os.path.sep in name:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
        else:
            found = shutil.which(name)
            if found is None:
                continue
            resolved = Path(found).resolve(strict=True)
        if resolved in seen:
            continue
        seen.add(resolved)
        if (
            resolved.is_file()
            and os.access(resolved, os.X_OK)
            and _python_version_supported(resolved)
        ):
            return resolved
    if value is not None:
        raise EdgeInstallError("the selected Python must be executable and version 3.10+")
    raise EdgeInstallError("Python 3.10+ was not found; install it before Edge")


def _python_version_supported(path: Path) -> bool:
    try:
        completed = subprocess.run(
            [
                str(path),
                "-c",
                "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_subprocess_env(offline=True),
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _validate_release(
    release: Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    if release.is_symlink() or not release.is_dir():
        raise EdgeInstallError("release must be a real directory")
    manifest_path = release / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise EdgeInstallError("release manifest is missing")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") not in {1, 2}
    ):
        raise EdgeInstallError("release manifest is invalid")
    wheel_sha256 = payload.get("wheel_sha256")
    if not isinstance(wheel_sha256, str) or not SHA256_PATTERN.fullmatch(wheel_sha256):
        raise EdgeInstallError("release manifest SHA-256 is invalid")
    if expected_sha256 is not None and not hmac.compare_digest(
        wheel_sha256,
        expected_sha256,
    ):
        raise EdgeInstallError("existing release does not match the wheel")
    package_version = payload.get("package_version")
    if not isinstance(package_version, str) or not package_version:
        raise EdgeInstallError("release package version is invalid")
    if payload.get("schema_version") == 2:
        offline_bundle = payload.get("offline_bundle")
        manifest_sha256 = payload.get("bundle_manifest_sha256")
        source_commit = payload.get("source_commit")
        bootstrap_pip_version = payload.get("bootstrap_pip_version")
        if not isinstance(offline_bundle, bool):
            raise EdgeInstallError("release source metadata is invalid")
        if offline_bundle:
            if (
                not isinstance(manifest_sha256, str)
                or not SHA256_PATTERN.fullmatch(manifest_sha256)
                or not isinstance(source_commit, str)
                or not COMMIT_PATTERN.fullmatch(source_commit)
                or not isinstance(bootstrap_pip_version, str)
                or not _pip_version_supported(bootstrap_pip_version)
            ):
                raise EdgeInstallError("release source metadata is invalid")
        elif (
            manifest_sha256 is not None
            or source_commit is not None
            or bootstrap_pip_version is not None
        ):
            raise EdgeInstallError("release source metadata is invalid")
        release_identity_sha256 = manifest_sha256 if offline_bundle else wheel_sha256
    else:
        release_identity_sha256 = wheel_sha256
    if release.name != _release_id(package_version, release_identity_sha256):
        raise EdgeInstallError("release identifier does not match its manifest")
    _verify_edge_command(release / "venv" / "bin" / EDGE_NAME)
    return payload


def _linked_release_id(
    prefix: Path,
    name: str,
    *,
    required: bool,
) -> str | None:
    link = prefix / name
    if not link.exists() and not link.is_symlink():
        if required:
            raise EdgeInstallError(f"{name} release is not configured")
        return None
    if not link.is_symlink():
        raise EdgeInstallError(f"{name} must be a symbolic link")
    target = Path(os.readlink(link))
    if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != "releases":
        raise EdgeInstallError(f"{name} release link is outside the managed root")
    release_id = target.parts[1]
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise EdgeInstallError(f"{name} release identifier is invalid")
    release = prefix / target
    if not release.is_dir() or release.is_symlink():
        raise EdgeInstallError(f"{name} release does not exist")
    return release_id


def _install_external_link(destination: Path, target: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if _external_link_matches(destination, target):
            return
        raise EdgeInstallError(f"refusing to replace unrelated command: {destination}")
    _atomic_symlink(destination, target)


def _external_link_matches(destination: Path, target: Path) -> bool:
    if not destination.is_symlink():
        return False
    raw = Path(os.readlink(destination))
    actual = raw if raw.is_absolute() else destination.parent / raw
    try:
        return actual.resolve(strict=False) == target.resolve(strict=False)
    except OSError:
        return False


def _atomic_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = link.parent / f".{link.name}.{uuid4().hex}.tmp"
    os.symlink(str(target), temporary)
    try:
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_once(path: Path, payload: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_edge_command(command: Path) -> None:
    if command.is_symlink() or not command.is_file() or not os.access(command, os.X_OK):
        raise EdgeInstallError("installed larkflow-edge command is missing")
    _run(
        [str(command), "--help"],
        stdout=subprocess.DEVNULL,
        label="start installed larkflow-edge",
        offline=True,
    )


def _release_id(package_version: str, wheel_sha256: str) -> str:
    release_id = f"{package_version}-{wheel_sha256[:12]}"
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise EdgeInstallError("release identifier is invalid")
    return release_id


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _capture(
    argv: list[str],
    *,
    label: str = "installed package verification",
    offline: bool = True,
) -> str:
    completed = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(offline=offline),
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise EdgeInstallError(f"{label} failed with status {completed.returncode}")
    return completed.stdout


def _run(
    argv: list[str],
    *,
    stdout: Any = None,
    label: str,
    offline: bool = False,
) -> None:
    completed = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=None,
        env=_subprocess_env(offline=offline),
        check=False,
        timeout=300,
    )
    if completed.returncode != 0:
        raise EdgeInstallError(
            f"{label} failed with status {completed.returncode}"
        )


def _subprocess_env(*, offline: bool) -> dict[str, str]:
    blocked = {"PYTHONHOME", "PYTHONPATH"}
    if offline:
        blocked.update(
            {
                "ALL_PROXY",
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "all_proxy",
                "https_proxy",
                "http_proxy",
            }
        )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in blocked and not key.upper().startswith("PIP_")
    }
    env.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    if offline:
        env["PIP_NO_INDEX"] = "1"
    return env


def _require_macos_user() -> None:
    if sys.platform != "darwin":
        raise EdgeInstallError("this installer currently supports macOS only")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise EdgeInstallError("run the installer as the employee user, not root")


def _print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        file=stream,
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
