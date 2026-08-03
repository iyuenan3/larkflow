"""Deployment assets must cover every long-lived development process."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent


def _env_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


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


def test_development_role_binding_grants_are_narrow_and_explicit():
    sql = (
        ROOT / "deploy" / "larkflow-target-development-grants.sql"
    ).read_text(encoding="utf-8")

    match = re.search(
        r"GRANT\s+(.+?)\s+ON TABLE\s+"
        r"public\.workflow_role_binding_actions\s+TO\s+\"lf-dev\";",
        sql,
        re.I | re.S,
    )
    assert match
    privileges = {
        item.strip().upper() for item in match.group(1).split(",")
    }
    assert privileges == {"SELECT", "INSERT", "UPDATE"}
    assert "REVOKE ALL PRIVILEGES ON TABLE public.workflow_role_binding_actions" in sql
    assert "workflow_instances" not in sql
    assert "workflow_node_instances" not in sql
    assert "workflow_node_attempts" not in sql


def test_development_interactive_worker_idle_caps_are_one_second():
    runtime = _env_values(ROOT / "deploy" / "larkflow-target.env.example")
    projection = _env_values(
        ROOT / "deploy" / "larkflow-target-projection.env.example"
    )

    assert float(runtime["LARKFLOW_TARGET_IDLE_MIN_SECONDS"]) > 0
    assert float(runtime["LARKFLOW_TARGET_IDLE_MAX_SECONDS"]) == 1
    assert float(projection["LARKFLOW_TARGET_PROJECTION_IDLE_MIN_SECONDS"]) > 0
    assert float(projection["LARKFLOW_TARGET_PROJECTION_IDLE_MAX_SECONDS"]) == 1
