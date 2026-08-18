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
        "larkflow-target-draft-generator.service",
        "larkflow-target-projection.service",
        "larkflow-target-interactive@1.service",
        "larkflow-target-interactive@2.service",
        "larkflow-target-inbound-adapter.service",
        "larkflow-target-inbound.service",
        "larkflow-target-edge.service",
        "larkflow-target-console.service",
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
    interactive = _env_values(
        ROOT / "deploy" / "larkflow-target-interactive.env.example"
    )

    assert float(runtime["LARKFLOW_TARGET_IDLE_MIN_SECONDS"]) > 0
    assert float(runtime["LARKFLOW_TARGET_IDLE_MAX_SECONDS"]) == 1
    assert "LARKFLOW_TARGET_ENABLE_IM_COMMANDS" not in projection
    assert float(projection["LARKFLOW_TARGET_PROJECTION_IDLE_MIN_SECONDS"]) > 0
    assert float(projection["LARKFLOW_TARGET_PROJECTION_IDLE_MAX_SECONDS"]) == 1
    assert "LARKFLOW_DELIVERABLE_FOLDER_TOKEN" in projection
    assert "LARKFLOW_DRIVE_FOLDER" not in projection
    assert int(interactive["LARKFLOW_TARGET_INTERACTIVE_CLAIM_LIMIT"]) == 1
    assert float(interactive["LARKFLOW_TARGET_INTERACTIVE_IDLE_MIN_SECONDS"]) > 0
    assert float(interactive["LARKFLOW_TARGET_INTERACTIVE_IDLE_MAX_SECONDS"]) == 1


def test_interactive_systemd_template_runs_the_isolated_credential_lane():
    unit = (
        ROOT / "deploy" / "larkflow-target-interactive@.service"
    ).read_text(encoding="utf-8")

    assert "User=lf-dev" in unit
    assert "--env-file /etc/larkflow-target-interactive.env" in unit
    assert "--interactive-worker-id interactive-%H-%i" in unit
    assert "interactive-%H-%i interact" in unit
    assert "KillMode=control-group" in unit
    assert "ReadWritePaths=/srv/larkflow/dev/.lark-cli" in unit


def test_draft_generator_is_credential_free_and_uses_a_two_call_lease():
    unit = (
        ROOT / "deploy" / "larkflow-target-draft-generator.service"
    ).read_text(encoding="utf-8")
    environment = _env_values(
        ROOT / "deploy" / "larkflow-target-draft-generator.env.example"
    )

    assert "User=lf_target_dev" in unit
    assert "generate-drafts" in unit
    assert "lark-cli" not in unit
    assert int(environment["LARKFLOW_TARGET_DRAFT_CLAIM_LIMIT"]) == 1
    assert int(environment["LARKFLOW_TARGET_DRAFT_CLAIM_TTL_SECONDS"]) > (
        2 * 240 + int(environment["LARKFLOW_TARGET_DRAFT_CLAIM_SAFETY_SECONDS"])
    )


def test_console_service_is_loopback_only_and_owner_scoped():
    unit = (
        ROOT / "deploy" / "larkflow-target-console.service"
    ).read_text(encoding="utf-8")
    environment = _env_values(
        ROOT / "deploy" / "larkflow-target-console.env.example"
    )

    assert "User=lf_target_dev" in unit
    assert "--env-file /etc/larkflow-target-console.env" in unit
    assert "--host 127.0.0.1 --port 8780" in unit
    assert "ProtectSystem=strict" in unit
    assert "IPAddressAllow=" not in unit
    assert "IPAddressDeny=" not in unit
    assert "EnvironmentFile=" not in unit
    assert environment["LARKFLOW_TARGET_DSN"] == (
        "postgresql:///larkflow_target_dev"
    )
    assert environment["LARKFLOW_TARGET_TENANT"] == "dev"
    assert environment["LARKFLOW_CONSOLE_AUTH_MODE"] == "static"
    assert environment["LARKFLOW_CONSOLE_PERSON_ID"] == ""
    assert environment["LARKFLOW_CONSOLE_ACCESS_TOKEN"] == ""
    assert environment["LARKFLOW_CONSOLE_FEISHU_APP_ID"] == ""
    assert environment["LARKFLOW_CONSOLE_FEISHU_APP_SECRET"] == ""
    assert environment["LARKFLOW_CONSOLE_FEISHU_TENANT_KEY"] == ""
    assert environment["LARKFLOW_CONSOLE_PUBLIC_BASE_URL"] == ""
    assert environment["LARKFLOW_CONSOLE_SESSION_TTL_SECONDS"] == "28800"
    assert environment["LARKFLOW_CONSOLE_RATE_LIMIT_WINDOW_SECONDS"] == "60"
    assert environment["LARKFLOW_CONSOLE_RATE_LIMIT_REQUESTS_PER_CLIENT"] == "300"
    assert environment["LARKFLOW_CONSOLE_RATE_LIMIT_AUTH_REQUESTS_PER_CLIENT"] == "30"
    assert environment["LARKFLOW_CONSOLE_RATE_LIMIT_ADMIN_WRITES_PER_CLIENT"] == "30"
    assert environment["LARKFLOW_CONSOLE_RATE_LIMIT_WORKFLOW_WRITES_PER_CLIENT"] == "60"
    assert environment["LARKFLOW_CONSOLE_RATE_LIMIT_GLOBAL_REQUESTS"] == "3000"


def test_console_public_ip_tls_uses_certbot_without_logging_oauth_codes():
    caddyfile = (
        ROOT / "deploy" / "larkflow-console-ip.Caddyfile.example"
    ).read_text(encoding="utf-8")
    installer = (
        ROOT / "deploy" / "larkflow-install-ip-certificate"
    ).read_text(encoding="utf-8")
    service = (
        ROOT / "deploy" / "larkflow-certbot-renew.service"
    ).read_text(encoding="utf-8")
    timer = (
        ROOT / "deploy" / "larkflow-certbot-renew.timer"
    ).read_text(encoding="utf-8")

    assert "PUBLIC_IP" in caddyfile
    assert "auto_https off" in caddyfile
    assert "default_sni PUBLIC_IP" in caddyfile
    assert "read_body 15s" in caddyfile
    assert "read_header 10s" in caddyfile
    assert "write 30s" in caddyfile
    assert "idle 2m" in caddyfile
    assert "max_header_size 32KB" in caddyfile
    assert "0rtt off" in caddyfile
    assert "method POST" in caddyfile
    assert "path_regexp ^/console/api/v1/drafts/[0-9a-f]{32}/attachments$" in caddyfile
    assert "max_size 262144" in caddyfile
    assert "max_size 65536" in caddyfile
    assert caddyfile.index("max_size 262144") < caddyfile.index("max_size 65536")
    assert "/var/lib/larkflow-certbot-webroot" in caddyfile
    assert "/etc/caddy/certs/larkflow-console/current/fullchain.pem" in caddyfile
    assert "reverse_proxy 127.0.0.1:8780" in caddyfile
    assert "header_up X-Larkflow-Client-IP {http.request.remote.host}" in caddyfile
    assert 'X-Frame-Options "DENY"' in caddyfile
    assert 'Cross-Origin-Opener-Policy "same-origin"' in caddyfile
    assert 'Cross-Origin-Resource-Policy "same-origin"' in caddyfile
    assert 'Permissions-Policy "accelerometer=()' in caddyfile
    assert "\n\tlog" not in caddyfile

    assert "RENEWED_LINEAGE" in installer
    assert "/etc/letsencrypt/live/*" in installer
    assert "openssl x509" in installer
    assert 'chown root:caddy "$CERT_STAGE"' in installer
    assert 'chmod 0750 "$CERT_STAGE"' in installer
    assert 'chmod 0750 "$CERT_RELEASE"' in installer
    assert "mv -Tf" in installer
    assert "caddy validate" in installer
    assert "systemctl reload caddy" in installer

    assert "certbot renew --quiet" in service
    assert "--deploy-hook /usr/local/sbin/larkflow-install-ip-certificate" in service
    assert "NoNewPrivileges=yes" in service
    assert "OnCalendar=*-*-* 03,15:00:00" in timer
    assert "RandomizedDelaySec=30m" in timer
    assert "Persistent=true" in timer


def test_console_admin_allowlist_tool_is_server_side_and_does_not_source_env():
    tool = (
        ROOT / "deploy" / "larkflow-console-admin-allowlist.py"
    ).read_text(encoding="utf-8")

    assert tool.startswith("#!/usr/bin/env python3\n")
    assert "preview" in tool
    assert "confirm" in tool
    assert "rollback" in tool
    assert "workflow_console_sessions" in tool
    assert "expires_at > CURRENT_TIMESTAMP" in tool
    assert "cannot remove the last Console administrator" in tool
    assert "os.replace" in tool
    assert "ProxyHandler({})" in tool
    assert "shell=True" not in tool
    assert "source " not in tool
