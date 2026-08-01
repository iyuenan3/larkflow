"""Operational CLI for the Target PostgreSQL workflow runtime."""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
from threading import Event
from typing import Any

import yaml

from larkflow.config import load_dotenv

from .config import TargetRuntimeSettings
from .daemon import WorkflowWorkerLoop
from .executors import DevelopmentToolExecutor
from .migrate import apply_migrations, postgres_connection_factory
from .model import ExecutorKind, QualityResult, QualityVerdict
from .postgres import PostgresWorkflowRepository
from .runner import NodeRunner
from .runtime import AutomatedExecutor, WorkflowWorker
from .serde import quality_to_dict, snapshot_from_dict, to_json_value
from .service import WorkflowService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="larkflow-target",
        description="larkflow Target PostgreSQL runtime",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="dotenv file parsed by larkflow, never sourced by a shell",
    )
    parser.add_argument("--dsn", default=os.environ.get("LARKFLOW_TARGET_DSN"))
    parser.add_argument(
        "--tenant",
        default=os.environ.get("LARKFLOW_TARGET_TENANT"),
    )
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("LARKFLOW_TARGET_WORKER_ID"),
    )
    parser.add_argument(
        "--enable-development-executor",
        action="store_true",
        help="enable the deterministic development.echo Tool adapter",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="apply packaged PostgreSQL migrations")

    create = commands.add_parser("create", help="create a draft from YAML or JSON")
    create.add_argument("source", help="document path, or - for stdin")

    confirm = commands.add_parser("confirm", help="confirm and start a draft")
    confirm.add_argument("instance_id")
    confirm.add_argument("--actor", required=True)

    show = commands.add_parser("show", help="show persisted instance state")
    show.add_argument("instance_id")

    submit = commands.add_parser("submit-human", help="submit a Human node result")
    submit.add_argument("instance_id")
    submit.add_argument("node_key")
    submit.add_argument("--actor", required=True)
    submit.add_argument("--attempt", required=True, type=int)
    submit.add_argument("--node-version", required=True, type=int)
    submit.add_argument("--result", required=True, help="JSON/YAML path, or -")
    submit.add_argument("--quality", choices=("pass", "fail"))
    submit.add_argument("--evidence", default="")
    submit.add_argument("--suggestion", default="")

    commands.add_parser("run-once", help="run one durable worker tick")
    commands.add_parser("serve", help="run worker ticks until SIGINT or SIGTERM")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env_file = _preparse_env_file(args)
    loaded = load_dotenv(env_file)
    parser = build_parser()
    namespace = parser.parse_args(args)
    log = JsonLogger()
    if loaded.set or loaded.skipped:
        log(
            "environment_loaded",
            {"set": loaded.set, "skipped": loaded.skipped, "path": env_file},
        )
    try:
        return _run(namespace, log)
    except Exception as exc:
        log(
            "command_failed",
            {
                "command": getattr(namespace, "command", None),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 1


def _run(namespace: argparse.Namespace, log: JsonLogger) -> int:
    dsn = _required(namespace.dsn, "--dsn or LARKFLOW_TARGET_DSN")
    connection_factory = postgres_connection_factory(dsn)
    if namespace.command == "migrate":
        applied = apply_migrations(connection_factory)
        log("migrations_applied", {"versions": list(applied)})
        return 0

    tenant_id = _required(namespace.tenant, "--tenant or LARKFLOW_TARGET_TENANT")
    applied = apply_migrations(connection_factory)
    if applied:
        log("migrations_applied", {"versions": list(applied)})
    repository = PostgresWorkflowRepository(connection_factory)
    service = WorkflowService(repository)

    if namespace.command == "create":
        document = _load_mapping(namespace.source)
        instance_id = _required(document.get("instance_id"), "instance_id")
        owner_person_id = _required(
            document.get("owner_person_id"),
            "owner_person_id",
        )
        snapshot_data = dict(document)
        snapshot_data.pop("instance_id", None)
        snapshot_data.pop("owner_person_id", None)
        snapshot_data.setdefault("schema_version", "0.2")
        instance = service.create_draft(
            instance_id=instance_id,
            tenant_id=tenant_id,
            owner_person_id=owner_person_id,
            actor_person_id=owner_person_id,
            snapshot=snapshot_from_dict(snapshot_data),
        )
        log("instance_created", _instance_payload(instance))
        return 0

    if namespace.command == "confirm":
        instance = service.confirm_draft(
            tenant_id,
            namespace.instance_id,
            actor_person_id=namespace.actor,
        )
        log("instance_confirmed", _instance_payload(instance))
        return 0

    if namespace.command == "show":
        log(
            "instance_state",
            _instance_payload(service.get(tenant_id, namespace.instance_id)),
        )
        return 0

    if namespace.command == "submit-human":
        result = _load_mapping(namespace.result)
        quality = None
        if namespace.quality:
            quality = QualityResult(
                verdict=QualityVerdict(namespace.quality),
                evidence=namespace.evidence,
                suggestion=namespace.suggestion,
            )
        instance = service.submit_human(
            tenant_id,
            namespace.instance_id,
            namespace.node_key,
            actor_person_id=namespace.actor,
            attempt_no=namespace.attempt,
            expected_node_version=namespace.node_version,
            result=result,
            quality_result=quality,
        )
        log("human_result_submitted", _instance_payload(instance))
        return 0

    settings = TargetRuntimeSettings.from_environ(
        dsn=dsn,
        tenant_id=tenant_id,
        worker_id=namespace.worker_id,
    )
    if namespace.enable_development_executor:
        settings = replace(settings, enable_development_executor=True)
    service = WorkflowService(
        repository,
        runner=NodeRunner(claim_ttl=settings.claim_ttl),
    )
    executor_registry = _executors(settings)
    worker = WorkflowWorker(
        service,
        repository,
        tenant_id=settings.tenant_id,
        worker_id=settings.worker_id,
        executors=executor_registry,
        candidate_limit=settings.candidate_limit,
    )
    if namespace.command == "run-once":
        report = worker.run_once()
        log("worker_tick", WorkflowWorkerLoop._report_fields(report))
        return int(bool(report.errors))

    stop_event = Event()
    _install_signal_handlers(stop_event, log)
    log(
        "worker_started",
        {
            "tenant_id": settings.tenant_id,
            "worker_id": settings.worker_id,
            "claim_ttl_seconds": settings.claim_ttl.total_seconds(),
            "candidate_limit": settings.candidate_limit,
            "idle_min_seconds": settings.loop.idle_min_seconds,
            "idle_max_seconds": settings.loop.idle_max_seconds,
            "executors": [kind.value for kind in executor_registry],
        },
    )
    WorkflowWorkerLoop(worker, settings=settings.loop, log=log).run(stop_event)
    return 0


def _executors(
    settings: TargetRuntimeSettings,
) -> dict[ExecutorKind, AutomatedExecutor]:
    if not settings.enable_development_executor:
        return {}
    return {ExecutorKind.TOOL: DevelopmentToolExecutor()}


def _install_signal_handlers(stop_event: Event, log: JsonLogger) -> None:
    def stop(signum: int, _frame: Any) -> None:
        log("worker_stop_requested", {"signal": signal.Signals(signum).name})
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


def _preparse_env_file(args: Sequence[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=".env")
    namespace, _ = parser.parse_known_args(args)
    return namespace.env_file


def _load_mapping(source: str) -> dict[str, Any]:
    if source == "-":
        text = sys.stdin.read()
    else:
        text = Path(source).read_text(encoding="utf-8-sig")
    value = yaml.safe_load(text)
    if not isinstance(value, Mapping):
        raise ValueError("document must be a JSON/YAML object")
    return {str(key): item for key, item in value.items()}


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _instance_payload(instance: Any) -> dict[str, Any]:
    nodes = []
    for spec in instance.snapshot.nodes:
        node = instance.nodes.get(spec.key)
        attempt = instance.current_attempt(spec.key) if node is not None else None
        nodes.append(
            {
                "key": spec.key,
                "title": spec.title,
                "owner_person_id": spec.owner_person_id,
                "executor": spec.executor.value,
                "deps": list(spec.deps),
                "status": node.status.value if node is not None else None,
                "node_version": node.version if node is not None else None,
                "attempt": None
                if attempt is None
                else {
                    "id": attempt.id,
                    "number": attempt.attempt_no,
                    "status": attempt.status.value,
                    "claimed_by": attempt.claimed_by,
                    "claim_expires_at": to_json_value(attempt.claim_expires_at),
                    "result": to_json_value(attempt.result),
                    "quality_result": quality_to_dict(attempt.quality_result),
                    "error_code": attempt.error_code,
                    "error_message": attempt.error_message,
                },
            }
        )
    return {
        "tenant_id": instance.tenant_id,
        "instance_id": instance.id,
        "owner_person_id": instance.owner_person_id,
        "status": instance.status.value,
        "version": instance.version,
        "graph_revision": instance.graph_revision,
        "goal": instance.snapshot.goal,
        "created_at": to_json_value(instance.created_at),
        "confirmed_at": to_json_value(instance.confirmed_at),
        "completed_at": to_json_value(instance.completed_at),
        "nodes": nodes,
    }


class JsonLogger:
    def __call__(
        self,
        event: str,
        fields: dict[str, Any],
        *,
        stream: Any = sys.stdout,
    ) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **to_json_value(fields),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
