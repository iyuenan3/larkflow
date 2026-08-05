#!/usr/bin/env python3
"""Build one hash-locked, offline macOS Personal Agent Edge bundle."""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Sequence
import zipfile


MANAGER_NAME = "larkflow-edge-manager"
MANAGER_SOURCE = Path(__file__).with_name("larkflow-edge-manager.py")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MINIMUM_BOOTSTRAP_PIP = (26, 1, 2)


class BundleBuildError(RuntimeError):
    """Expected bundle build failure with a non-secret message."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build-larkflow-edge-bundle",
        description="Build an offline, hash-locked macOS Edge wheelhouse",
    )
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python 3.10+ matching the employee Mac target",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    namespace = build_parser().parse_args(
        list(sys.argv[1:] if argv is None else argv)
    )
    try:
        report = build_bundle(
            wheel=Path(namespace.wheel).expanduser(),
            output=Path(namespace.output).expanduser(),
            source_commit=namespace.source_commit,
            python=Path(namespace.python).expanduser(),
        )
        _print(report)
        return 0
    except Exception as exc:
        _print(
            {
                "event": "edge_bundle_build_failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 1


def build_bundle(
    *,
    wheel: Path,
    output: Path,
    source_commit: str,
    python: Path,
) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise BundleBuildError("offline Edge bundles must be built on macOS")
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise BundleBuildError("--source-commit must be a full lowercase Git SHA")
    wheel = _regular_file(wheel, suffix=".whl", label="larkflow wheel")
    manager = _regular_file(MANAGER_SOURCE, suffix=".py", label="Edge manager")
    python = _supported_python(python)
    if not output.is_absolute():
        raise BundleBuildError("--output must be an absolute path")
    if output.exists() or output.is_symlink():
        raise BundleBuildError("output already exists")
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise BundleBuildError("output parent must be a real directory")

    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.", dir=str(output.parent))
    )
    try:
        wheelhouse = staging / "wheelhouse"
        wheelhouse.mkdir(mode=0o755)
        _download_wheelhouse(python, wheel, wheelhouse)
        _download_bootstrap_pip(python, wheelhouse)
        wheels = sorted(wheelhouse.iterdir(), key=lambda item: item.name.casefold())
        if not wheels or any(
            item.is_symlink() or not item.is_file() or item.suffix != ".whl"
            for item in wheels
        ):
            raise BundleBuildError("wheelhouse must contain only wheel files")
        casefolded = [item.name.casefold() for item in wheels]
        if len(casefolded) != len(set(casefolded)):
            raise BundleBuildError("wheelhouse filenames must be case-insensitively unique")

        wheel_sha256 = _sha256(wheel)
        artifact_matches = [item for item in wheels if _sha256(item) == wheel_sha256]
        if len(artifact_matches) != 1:
            raise BundleBuildError("downloaded wheelhouse does not contain the source wheel")
        artifact = artifact_matches[0]
        package_version = _wheel_package_version(artifact)
        inventory = []
        package_names: set[str] = set()
        for item in wheels:
            package_name, version = _wheel_identity(item)
            if package_name in package_names:
                raise BundleBuildError(
                    "wheelhouse must contain exactly one wheel per package"
                )
            package_names.add(package_name)
            inventory.append(
                {
                    "name": package_name,
                    "version": version,
                    "path": item.relative_to(staging).as_posix(),
                    "sha256": _sha256(item),
                }
            )
        pip_items = [item for item in inventory if item["name"] == "pip"]
        if len(pip_items) != 1 or not _pip_version_supported(pip_items[0]["version"]):
            raise BundleBuildError("wheelhouse must contain one supported bootstrap pip")

        manager_target = staging / MANAGER_NAME
        shutil.copyfile(manager, manager_target)
        os.chmod(manager_target, 0o755)
        for item in wheels:
            os.chmod(item, 0o644)

        files = [manager_target, *wheels]
        file_entries = [
            {
                "path": item.relative_to(staging).as_posix(),
                "sha256": _sha256(item),
                "size": item.stat().st_size,
            }
            for item in files
        ]
        manifest = {
            "schema_version": 1,
            "package": "larkflow",
            "package_version": package_version,
            "source_commit": source_commit,
            "target": _python_target(python),
            "artifact": {
                "path": artifact.relative_to(staging).as_posix(),
                "sha256": wheel_sha256,
            },
            "wheels": inventory,
            "bootstrap": {"pip": pip_items[0]},
            "files": file_entries,
        }
        manifest_path = staging / "manifest.json"
        _write_manifest(manifest_path, manifest)
        os.chmod(manifest_path, 0o644)
        os.chmod(staging, 0o755)
        os.replace(staging, output)
    except Exception:
        if staging.exists() and staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise

    final_manifest = output / "manifest.json"
    return {
        "event": "edge_bundle_built",
        "bundle": str(output),
        "package_version": package_version,
        "source_commit": source_commit,
        "target": manifest["target"],
        "wheel_count": len(wheels),
        "manifest_sha256": _sha256(final_manifest),
        "network_required_to_install": False,
        "signed": False,
        "notarized": False,
    }


def _download_wheelhouse(python: Path, wheel: Path, wheelhouse: Path) -> None:
    completed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-input",
            "--only-binary=:all:",
            "--dest",
            str(wheelhouse),
            str(wheel),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=_build_env(),
        check=False,
        timeout=900,
    )
    if completed.returncode != 0:
        raise BundleBuildError(
            f"download wheelhouse failed with status {completed.returncode}"
        )


def _download_bootstrap_pip(python: Path, wheelhouse: Path) -> None:
    completed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-input",
            "--only-binary=:all:",
            "--no-deps",
            "--dest",
            str(wheelhouse),
            "pip>=26.1.2,<27",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=_build_env(),
        check=False,
        timeout=300,
    )
    if completed.returncode != 0:
        raise BundleBuildError(
            f"download bootstrap pip failed with status {completed.returncode}"
        )


def _regular_file(path: Path, *, suffix: str, label: str) -> Path:
    if path.is_symlink() or not path.is_file() or path.suffix != suffix:
        raise BundleBuildError(f"{label} must be a regular {suffix} file")
    return path.resolve(strict=True)


def _supported_python(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BundleBuildError("selected Python does not exist") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise BundleBuildError("selected Python is not executable")
    completed = subprocess.run(
        [
            str(resolved),
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_inspect_env(),
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise BundleBuildError("selected Python must be version 3.10+")
    return resolved


def _python_target(python: Path) -> dict[str, str]:
    completed = subprocess.run(
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
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_inspect_env(),
        text=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise BundleBuildError("selected Python target cannot be inspected")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BundleBuildError("selected Python target cannot be inspected") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("platform") != "darwin"
        or payload.get("machine") not in {"arm64", "x86_64"}
    ):
        raise BundleBuildError("selected Python must target macOS arm64 or x86_64")
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
                raise BundleBuildError("wheel must contain one dist-info METADATA file")
            raw = archive.read(metadata_files[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise BundleBuildError("wheel metadata cannot be read") from exc
    metadata = Parser().parsestr(raw)
    package_name = re.sub(
        r"[-_.]+",
        "-",
        str(metadata.get("Name", "")).strip().lower(),
    )
    version = str(metadata.get("Version", ""))
    if not package_name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", package_name):
        raise BundleBuildError("wheel package name is invalid")
    if not version or not re.fullmatch(r"[A-Za-z0-9._+-]+", version):
        raise BundleBuildError("wheel package version is invalid")
    return package_name, version


def _wheel_package_version(path: Path) -> str:
    package_name, version = _wheel_identity(path)
    if package_name != "larkflow":
        raise BundleBuildError("wheel package must be larkflow")
    return version


def _pip_version_supported(value: str) -> bool:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", value)
    if match is None:
        return False
    version = tuple(int(part or "0") for part in match.groups())
    return MINIMUM_BOOTSTRAP_PIP <= version < (27, 0, 0)


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inspect_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH"}
        and not key.upper().startswith("PIP_")
    }
    env.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return env


def _build_env() -> dict[str, str]:
    env = _inspect_env()
    env.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_NO_CACHE_DIR": "1",
        }
    )
    return env


def _print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        file=stream,
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
