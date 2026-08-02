"""Deployment assets must cover every long-lived development process."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_development_restart_asset_covers_all_python_services():
    script = (ROOT / "deploy" / "restart-development-services").read_text(
        encoding="utf-8"
    )

    expected = {
        "larkflow-target.service",
        "larkflow-target-projection.service",
        "larkflow-target-inbound-adapter.service",
        "larkflow-target-inbound.service",
        "larkflow-target-edge.service",
        "larkflow@dev.service",
    }
    declared = {
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("larkflow")
    }

    assert declared == expected
    assert "ExecMainStartTimestamp" in script
    assert "NRestarts" in script
