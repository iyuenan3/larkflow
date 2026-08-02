from __future__ import annotations

from pathlib import Path

import pytest

from larkflow.workflow.edge_cli import DEFAULT_CREDENTIAL_FILE, build_parser
from larkflow.workflow.edge_gateway_cli import (
    _loopback_host,
    _positive_integer,
    build_parser as build_gateway_parser,
)


def test_edge_cli_exposes_only_manual_pair_and_run_once_commands():
    parser = build_parser()
    paired = parser.parse_args(
        ["pair", "--server", "https://edge.example.com", "--name", "Mac"]
    )
    run = parser.parse_args(["run-once", "--workspace", "/workspace"])

    assert paired.command == "pair"
    assert run.command == "run-once"
    assert Path(paired.credential_file) == DEFAULT_CREDENTIAL_FILE


def test_gateway_rejects_non_loopback_bind_addresses():
    assert _loopback_host("127.0.0.1") == "127.0.0.1"
    assert _loopback_host("::1") == "::1"
    assert _loopback_host("localhost") == "localhost"
    with pytest.raises(ValueError, match="loopback"):
        _loopback_host("0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        _loopback_host("edge.example.com")


def test_gateway_parser_does_not_expose_capability_escalation():
    parser = build_gateway_parser()
    args = parser.parse_args(
        [
            "pairing-create",
            "--tenant",
            "tenant_1",
            "--person",
            "person_1",
            "--actor",
            "admin_1",
        ]
    )

    assert args.command == "pairing-create"
    assert not hasattr(args, "capability")
    assert _positive_integer("100000", "limit") == 100000
    with pytest.raises(ValueError):
        _positive_integer("0", "limit")
