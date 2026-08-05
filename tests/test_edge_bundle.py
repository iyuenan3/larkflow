from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parent.parent
BUILDER = ROOT / "deploy" / "build-larkflow-edge-bundle.py"


def load_builder():
    loader = importlib.machinery.SourceFileLoader("larkflow_edge_bundle", str(BUILDER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "larkflow-0.0.2.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: larkflow\nVersion: 0.0.2\n",
        )
    return path


def dependency_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "dependency-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: dependency\nVersion: 1.0\n",
        )
    return path


def pip_wheel(path: Path, *, version: str = "26.2.1") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"pip-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: pip\nVersion: {version}\n",
        )
    return path


def test_builder_creates_hash_locked_offline_bundle(tmp_path: Path, monkeypatch):
    module = load_builder()
    artifact = wheel(tmp_path / "larkflow-0.0.2-py3-none-any.whl")
    output = tmp_path / "release" / "larkflow-edge"

    def fake_download(_python, source, wheelhouse):
        shutil.copyfile(source, wheelhouse / source.name)
        dependency_wheel(wheelhouse / "dependency-1.0-py3-none-any.whl")

    monkeypatch.setattr(module, "_download_wheelhouse", fake_download)
    monkeypatch.setattr(
        module,
        "_download_bootstrap_pip",
        lambda _python, wheelhouse: pip_wheel(
            wheelhouse / "pip-26.2.1-py3-none-any.whl"
        ),
    )

    report = module.build_bundle(
        wheel=artifact,
        output=output,
        source_commit="a" * 40,
        python=Path(sys.executable),
    )

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert report["manifest_sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    assert report["network_required_to_install"] is False
    assert report["signed"] is False
    assert report["notarized"] is False
    assert manifest["source_commit"] == "a" * 40
    assert manifest["target"]["platform"] == "darwin"
    assert manifest["artifact"]["path"].startswith("wheelhouse/")
    assert {(item["name"], item["version"]) for item in manifest["wheels"]} == {
        ("dependency", "1.0"),
        ("larkflow", "0.0.2"),
        ("pip", "26.2.1"),
    }
    assert manifest["bootstrap"]["pip"]["name"] == "pip"
    assert manifest["bootstrap"]["pip"]["version"] == "26.2.1"
    assert {item["path"] for item in manifest["files"]} == {
        "larkflow-edge-manager",
        "wheelhouse/larkflow-0.0.2-py3-none-any.whl",
        "wheelhouse/dependency-1.0-py3-none-any.whl",
        "wheelhouse/pip-26.2.1-py3-none-any.whl",
    }
    for item in manifest["files"]:
        candidate = output / item["path"]
        assert candidate.stat().st_size == item["size"]
        assert hashlib.sha256(candidate.read_bytes()).hexdigest() == item["sha256"]


def test_builder_refuses_to_replace_an_existing_output(tmp_path: Path):
    module = load_builder()
    artifact = wheel(tmp_path / "larkflow-0.0.2-py3-none-any.whl")
    output = tmp_path / "release"
    output.mkdir()

    with pytest.raises(module.BundleBuildError, match="already exists"):
        module.build_bundle(
            wheel=artifact,
            output=output,
            source_commit="a" * 40,
            python=Path(sys.executable),
        )


def test_builder_requires_full_lowercase_source_commit(tmp_path: Path):
    module = load_builder()
    artifact = wheel(tmp_path / "larkflow-0.0.2-py3-none-any.whl")

    with pytest.raises(module.BundleBuildError, match="full lowercase"):
        module.build_bundle(
            wheel=artifact,
            output=tmp_path / "release",
            source_commit="ABC123",
            python=Path(sys.executable),
        )


def test_builder_rejects_a_vulnerable_bootstrap_pip(tmp_path: Path, monkeypatch):
    module = load_builder()
    artifact = wheel(tmp_path / "larkflow-0.0.2-py3-none-any.whl")

    monkeypatch.setattr(
        module,
        "_download_wheelhouse",
        lambda _python, source, wheelhouse: shutil.copyfile(
            source, wheelhouse / source.name
        ),
    )
    monkeypatch.setattr(
        module,
        "_download_bootstrap_pip",
        lambda _python, wheelhouse: pip_wheel(
            wheelhouse / "pip-26.1-py3-none-any.whl",
            version="26.1",
        ),
    )

    with pytest.raises(module.BundleBuildError, match="supported bootstrap pip"):
        module.build_bundle(
            wheel=artifact,
            output=tmp_path / "release",
            source_commit="a" * 40,
            python=Path(sys.executable),
        )
