#!/usr/bin/env python3
"""Build one hash-locked, offline macOS Personal Agent Edge bundle."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
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
EDGE_DISTRIBUTION = "larkflow-personal-edge"
EDGE_WHEEL_STEM = "larkflow_personal_edge"
EDGE_MODULES = (
    "larkflow/workflow/edge_agent.py",
    "larkflow/workflow/edge_cli.py",
    "larkflow/workflow/edge_client.py",
    "larkflow/workflow/edge_contract.py",
)
REQUIREMENTS_LOCK = "requirements.lock"
SBOM_NAME = "sbom.spdx.json"
BUILD_PROOF_NAME = "build-proof.json"


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
        package_version = _source_larkflow_version(wheel)
        edge_wheel = staging / (
            f"{EDGE_WHEEL_STEM}-{package_version}-py3-none-any.whl"
        )
        _build_minimal_edge_wheel(wheel, edge_wheel, version=package_version)
        source_wheel_sha256 = _sha256(wheel)
        edge_wheel_sha256 = _sha256(edge_wheel)
        _download_wheelhouse(python, edge_wheel, wheelhouse)
        edge_wheel.unlink()
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

        artifact_matches = [
            item for item in wheels if _sha256(item) == edge_wheel_sha256
        ]
        if len(artifact_matches) != 1:
            raise BundleBuildError("wheelhouse does not contain the minimal Edge wheel")
        artifact = artifact_matches[0]
        artifact_name, artifact_version = _wheel_identity(artifact)
        if artifact_name != EDGE_DISTRIBUTION or artifact_version != package_version:
            raise BundleBuildError("minimal Edge wheel identity is invalid")
        _verify_minimal_edge_wheel(artifact)
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

        target = _python_target(python)
        runtime_inventory = [
            item for item in inventory if item["name"] != "pip"
        ]
        requirements_lock = staging / REQUIREMENTS_LOCK
        _write_text(requirements_lock, _requirements_lock(runtime_inventory))
        sbom_path = staging / SBOM_NAME
        _write_json(
            sbom_path,
            _spdx_sbom(
                inventory,
                source_commit=source_commit,
                artifact_sha256=edge_wheel_sha256,
            ),
        )
        build_proof_path = staging / BUILD_PROOF_NAME
        _write_json(
            build_proof_path,
            {
                "schema_version": 1,
                "source_commit": source_commit,
                "source_artifact": {
                    "filename": wheel.name,
                    "sha256": source_wheel_sha256,
                },
                "target": target,
                "edge_modules": list(EDGE_MODULES),
                "artifact": {
                    "path": artifact.relative_to(staging).as_posix(),
                    "sha256": edge_wheel_sha256,
                },
                "requirements_lock": {
                    "path": REQUIREMENTS_LOCK,
                    "sha256": _sha256(requirements_lock),
                },
                "sbom": {
                    "path": SBOM_NAME,
                    "sha256": _sha256(sbom_path),
                },
            },
        )
        for item in (requirements_lock, sbom_path, build_proof_path):
            os.chmod(item, 0o644)

        files = [
            manager_target,
            requirements_lock,
            sbom_path,
            build_proof_path,
            *wheels,
        ]
        file_entries = [
            {
                "path": item.relative_to(staging).as_posix(),
                "sha256": _sha256(item),
                "size": item.stat().st_size,
            }
            for item in files
        ]
        manifest = {
            "schema_version": 2,
            "package": EDGE_DISTRIBUTION,
            "package_version": package_version,
            "source_commit": source_commit,
            "target": target,
            "artifact": {
                "path": artifact.relative_to(staging).as_posix(),
                "sha256": edge_wheel_sha256,
            },
            "wheels": inventory,
            "bootstrap": {"pip": pip_items[0]},
            "evidence": {
                "requirements_lock": {
                    "path": REQUIREMENTS_LOCK,
                    "sha256": _sha256(requirements_lock),
                },
                "sbom": {
                    "path": SBOM_NAME,
                    "sha256": _sha256(sbom_path),
                },
                "build_proof": {
                    "path": BUILD_PROOF_NAME,
                    "sha256": _sha256(build_proof_path),
                },
            },
            "files": file_entries,
        }
        manifest_path = staging / "manifest.json"
        _write_json(manifest_path, manifest)
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


def _build_minimal_edge_wheel(
    source: Path,
    destination: Path,
    *,
    version: str,
) -> None:
    """Repackage only the employee-side modules from the verified source wheel."""
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise BundleBuildError("source wheel contains duplicate paths")
            module_payloads = {name: archive.read(name) for name in EDGE_MODULES}
    except KeyError as exc:
        raise BundleBuildError(
            f"source wheel is missing required Edge module: {exc.args[0]}"
        ) from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleBuildError("source wheel cannot be read") from exc

    dist_info = f"{EDGE_WHEEL_STEM}-{version}.dist-info"
    payloads: dict[str, bytes] = {
        "larkflow/__init__.py": (
            '"""Minimal Personal Agent Edge package."""\n'
            f'__version__ = "{version}"\n'
        ).encode("utf-8"),
        "larkflow/workflow/__init__.py": (
            '"""Employee-side Personal Agent Edge runtime only."""\n'
        ).encode("utf-8"),
        **module_payloads,
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {EDGE_DISTRIBUTION}\n"
            f"Version: {version}\n"
            "Summary: Minimal employee-side larkflow Personal Agent Edge\n"
            "Requires-Python: >=3.10\n"
            "Requires-Dist: httpx>=0.27\n"
            "\n"
        ).encode("utf-8"),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: larkflow-edge-bundle\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
            "\n"
        ).encode("utf-8"),
        f"{dist_info}/entry_points.txt": (
            "[console_scripts]\n"
            "larkflow-edge = larkflow.workflow.edge_cli:main\n"
        ).encode("utf-8"),
        f"{dist_info}/top_level.txt": b"larkflow\n",
    }
    record_path = f"{dist_info}/RECORD"
    record_lines = [
        f"{name},sha256={_record_digest(payload)},{len(payload)}"
        for name, payload in sorted(payloads.items())
    ]
    record_lines.append(f"{record_path},,")
    payloads[record_path] = ("\n".join(record_lines) + "\n").encode("utf-8")

    try:
        with zipfile.ZipFile(
            destination,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, payload in sorted(payloads.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, payload)
    except OSError as exc:
        raise BundleBuildError("minimal Edge wheel cannot be written") from exc


def _verify_minimal_edge_wheel(path: Path) -> None:
    expected_package_files = {
        "larkflow/__init__.py",
        "larkflow/workflow/__init__.py",
        *EDGE_MODULES,
    }
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleBuildError("minimal Edge wheel cannot be inspected") from exc
    if len(names) != len(set(names)):
        raise BundleBuildError("minimal Edge wheel contains duplicate paths")
    package_files = {
        name for name in names if not name.endswith("/") and ".dist-info/" not in name
    }
    if package_files != expected_package_files:
        raise BundleBuildError("minimal Edge wheel package boundary is invalid")
    forbidden = {
        "larkflow/workflow/edge.py",
        "larkflow/workflow/edge_gateway_cli.py",
        "larkflow/workflow/edge_http.py",
        "larkflow/workflow/edge_postgres.py",
    }
    if forbidden.intersection(names):
        raise BundleBuildError("minimal Edge wheel contains central runtime modules")


def _record_digest(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return encoded.rstrip(b"=").decode("ascii")


def _requirements_lock(inventory: Sequence[dict[str, str]]) -> str:
    lines = [
        f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}"
        for item in sorted(inventory, key=lambda value: value["name"])
    ]
    return "\n".join(lines) + "\n"


def _spdx_sbom(
    inventory: Sequence[dict[str, str]],
    *,
    source_commit: str,
    artifact_sha256: str,
) -> dict[str, Any]:
    packages = []
    relationships = []
    for item in sorted(inventory, key=lambda value: value["name"]):
        identifier = "SPDXRef-Package-" + re.sub(
            r"[^A-Za-z0-9.-]",
            "-",
            item["name"],
        )
        packages.append(
            {
                "SPDXID": identifier,
                "name": item["name"],
                "versionInfo": item["version"],
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "supplier": "NOASSERTION",
                "packageFileName": item["path"],
                "checksums": [
                    {
                        "algorithm": "SHA256",
                        "checksumValue": item["sha256"],
                    }
                ],
            }
        )
        relationships.append(
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": identifier,
            }
        )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"larkflow-personal-edge-{source_commit[:12]}",
        "documentNamespace": (
            "https://larkflow.local/spdx/personal-edge/"
            f"{source_commit}/{artifact_sha256}"
        ),
        "creationInfo": {
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: larkflow-edge-bundle"],
        },
        "packages": packages,
        "relationships": relationships,
    }


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


def _source_larkflow_version(path: Path) -> str:
    package_name, version = _wheel_identity(path)
    if package_name != "larkflow":
        raise BundleBuildError("source wheel package must be larkflow")
    return version


def _pip_version_supported(value: str) -> bool:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", value)
    if match is None:
        return False
    version = tuple(int(part or "0") for part in match.groups())
    return MINIMUM_BOOTSTRAP_PIP <= version < (27, 0, 0)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def _write_text(path: Path, payload: str) -> None:
    _write_bytes(path, payload.encode("utf-8"))


def _write_bytes(path: Path, encoded: bytes) -> None:
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
