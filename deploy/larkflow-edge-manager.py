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
from pathlib import Path
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


class EdgeInstallError(RuntimeError):
    """Expected installation failure with a user-actionable message."""


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
    install.add_argument("--wheel", required=True)
    install.add_argument(
        "--sha256",
        required=True,
        help="expected SHA-256 of the wheel",
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
                wheel=Path(namespace.wheel).expanduser(),
                expected_sha256=namespace.sha256,
                python=namespace.python,
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
    wheel: Path,
    expected_sha256: str,
    python: str | None,
) -> dict[str, Any]:
    """Build one release completely before switching the stable command."""
    wheel = _verified_wheel(wheel, expected_sha256)
    python_path = _python_executable(python)
    _preflight_layout(prefix, link_dir)
    current_before = _linked_release_id(prefix, "current", required=False)
    wheel_sha256 = _sha256(wheel)
    package_version = _wheel_package_version(wheel)
    release_id = _release_id(package_version, wheel_sha256)
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
                wheel=wheel,
                wheel_sha256=wheel_sha256,
                python=python_path,
            )
            if installed_version != package_version:
                raise EdgeInstallError(
                    "installed package version does not match wheel metadata"
                )
        previous_release = _activate(prefix, release_id)
        _install_stable_commands(prefix, link_dir)
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
) -> str:
    venv = target / "venv"
    _run(
        [str(python), "-m", "venv", str(venv)],
        label="create managed virtual environment",
    )
    venv_python = venv / "bin" / "python"
    _run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            str(wheel),
        ],
        label="install wheel and dependencies",
    )
    _run(
        [str(venv_python), "-m", "pip", "check"],
        label="validate installed dependencies",
    )
    package_version = _capture(
        [
            str(venv_python),
            "-c",
            "from importlib.metadata import version; print(version('larkflow'))",
        ]
    ).strip()
    if not package_version or not re.fullmatch(r"[A-Za-z0-9._+-]+", package_version):
        raise EdgeInstallError("installed larkflow package version is invalid")
    _verify_edge_command(venv / "bin" / EDGE_NAME)
    _write_json_once(
        target / "manifest.json",
        {
            "schema_version": 1,
            "package": "larkflow",
            "package_version": package_version,
            "wheel_filename": wheel.name,
            "wheel_sha256": wheel_sha256,
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


def _install_stable_commands(prefix: Path, link_dir: Path) -> None:
    bin_dir = prefix / "bin"
    bin_dir.mkdir(mode=0o700, exist_ok=True)
    manager_source = Path(__file__).resolve(strict=True)
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


def _wheel_package_version(path: Path) -> str:
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
    package_name = str(metadata.get("Name", "")).lower().replace("_", "-")
    version = str(metadata.get("Version", ""))
    if package_name != "larkflow":
        raise EdgeInstallError("wheel package must be larkflow")
    if not version or not re.fullmatch(r"[A-Za-z0-9._+-]+", version):
        raise EdgeInstallError("wheel package version is invalid")
    return version


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
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
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


def _capture(argv: list[str]) -> str:
    completed = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise EdgeInstallError("installed package verification failed")
    return completed.stdout


def _run(argv: list[str], *, stdout: Any = None, label: str) -> None:
    env = os.environ.copy()
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    completed = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=None,
        env=env,
        check=False,
        timeout=300,
    )
    if completed.returncode != 0:
        raise EdgeInstallError(
            f"{label} failed with status {completed.returncode}"
        )


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
