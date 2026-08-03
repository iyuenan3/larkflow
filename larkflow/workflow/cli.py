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

from larkflow.config import load_dotenv, load_llm_roles
from larkflow.llm.client import OpenAICompatLLM

from .completion_poll import TaskCompletionPoller
from .config import (
    TargetInboundSettings,
    TargetProjectionSettings,
    TargetRuntimeSettings,
)
from .daemon import WorkflowWorkerLoop
from .directory import CliFeishuDirectory
from .executors import (
    ContentCheckToolExecutor,
    DevelopmentToolExecutor,
    LLMAgentExecutor,
    ToolExecutorRouter,
)
from .feishu import (
    CliFeishuDocumentProjection,
    CliFeishuMessageProjection,
    CliFeishuTaskProjection,
    CliFeishuTaskReader,
)
from .im_commands import (
    IMCommandVerificationWorker,
    IMCommandWorker,
    IMReplyWorker,
)
from .inbound import TaskVerificationWorker, WorkflowInboundWorker
from .inbound_daemon import InboundWorkerLoop, VerificationWorkerLoop
from .migrate import apply_migrations, postgres_connection_factory, verify_migrations
from .model import ExecutorKind, QualityResult, QualityVerdict
from .postgres import (
    PostgresIMCommandStore,
    PostgresWorkflowInbox,
    PostgresWorkflowRepository,
)
from .projection import WorkflowProjectionWorker
from .projection_daemon import ProjectionWorkerLoop
from .role_bindings import (
    RoleBindingActionWorker,
    RoleBindingCardWorker,
    RoleBindingReplyWorker,
    RoleBindingVerificationWorker,
)
from .runner import NodeRunner
from .runtime import AutomatedExecutor, WorkflowWorker
from .serde import quality_to_dict, snapshot_from_dict, to_json_value
from .service import WorkflowService
from .template_service import TemplateService, template_document


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
        "--projection-worker-id",
        default=os.environ.get("LARKFLOW_TARGET_PROJECTION_WORKER_ID"),
    )
    parser.add_argument(
        "--inbound-worker-id",
        default=os.environ.get("LARKFLOW_TARGET_INBOUND_WORKER_ID"),
    )
    parser.add_argument(
        "--lark-profile",
        default=os.environ.get("LARKFLOW_TARGET_LARK_PROFILE"),
    )
    parser.add_argument(
        "--lark-identity",
        default=os.environ.get("LARKFLOW_TARGET_LARK_IDENTITY", "bot"),
        choices=("bot", "user"),
    )
    parser.add_argument(
        "--enable-agent-executor",
        action="store_true",
        help="enable the OpenAI-compatible llm.generate Agent adapter",
    )
    parser.add_argument(
        "--enable-development-executor",
        action="store_true",
        help="enable the deterministic development.echo Tool adapter",
    )
    parser.add_argument(
        "--enable-content-check-executor",
        action="store_true",
        help="enable the deterministic content.check Tool adapter",
    )
    parser.add_argument(
        "--validate-directory",
        action="store_true",
        default=_env_boolean("LARKFLOW_TARGET_VALIDATE_DIRECTORY"),
        help="require every draft owner to be an active Feishu directory user",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="apply packaged PostgreSQL migrations")

    create = commands.add_parser("create", help="create a draft from YAML or JSON")
    create.add_argument("source", help="document path, or - for stdin")

    template_create = commands.add_parser(
        "template-create",
        help="create a draft template and immutable version 1",
    )
    template_create.add_argument("source", help="template YAML/JSON path, or -")
    template_create.add_argument("--actor", required=True)

    template_add = commands.add_parser(
        "template-add-version",
        help="append an immutable version to a draft or disabled template",
    )
    template_add.add_argument("template_id")
    template_add.add_argument("source", help="template YAML/JSON path, or -")
    template_add.add_argument("--actor", required=True)

    for command, description in (
        ("template-enable", "enable the latest immutable template version"),
        ("template-disable", "disable a template before changing it"),
        ("template-delete", "soft-delete a draft or disabled template"),
    ):
        lifecycle = commands.add_parser(command, help=description)
        lifecycle.add_argument("template_id")
        lifecycle.add_argument("--actor", required=True)

    commands.add_parser("template-list", help="list tenant templates")
    template_show = commands.add_parser(
        "template-show",
        help="show a template and one immutable version",
    )
    template_show.add_argument("template_id")
    template_show.add_argument("--version", type=int)

    create_from_template = commands.add_parser(
        "create-from-template",
        help="materialize an enabled template as a frozen draft",
    )
    create_from_template.add_argument("template_id")
    create_from_template.add_argument("--instance-id", required=True)
    create_from_template.add_argument("--owner", required=True)
    create_from_template.add_argument(
        "--bindings",
        required=True,
        help="owner-role binding YAML/JSON path, or -",
    )
    create_from_template.add_argument(
        "--inputs",
        help="template input YAML/JSON path",
    )

    preview = commands.add_parser("preview", help="validate and preview a draft")
    preview.add_argument("instance_id")
    preview.add_argument("--actor", required=True)

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
    commands.add_parser("project-once", help="run one Feishu projection tick")
    commands.add_parser(
        "reconcile-projections",
        help="rebuild missing current Human Task projections",
    )
    reconcile_instance_completion = commands.add_parser(
        "reconcile-instance-completion",
        help="repair the final document and message for one completed instance",
    )
    reconcile_instance_completion.add_argument("instance_id")
    commands.add_parser(
        "reconcile-completions",
        help="poll current Human Tasks and enqueue observed completions",
    )
    commands.add_parser("project", help="project to Feishu until SIGINT or SIGTERM")
    commands.add_parser("inbound-once", help="run one Feishu inbound event tick")
    commands.add_parser("inbound", help="consume durable Feishu events until stopped")
    commands.add_parser(
        "verify-inbound-once",
        help="run one credential-side Task verification tick",
    )
    commands.add_parser(
        "verify-inbound",
        help="verify durable Task events until stopped",
    )
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
    projection_command = namespace.command in {
        "project-once",
        "reconcile-projections",
        "reconcile-instance-completion",
        "reconcile-completions",
        "project",
    }
    inbound_command = namespace.command in {"inbound-once", "inbound"}
    verification_command = namespace.command in {
        "verify-inbound-once",
        "verify-inbound",
    }
    if projection_command or inbound_command or verification_command:
        verify_migrations(connection_factory)
        applied = ()
    else:
        applied = apply_migrations(connection_factory)
    if applied:
        log("migrations_applied", {"versions": list(applied)})
    repository = PostgresWorkflowRepository(connection_factory)
    directory = None
    if namespace.validate_directory:
        directory = CliFeishuDirectory(
            profile=_required(
                namespace.lark_profile,
                "--lark-profile or LARKFLOW_TARGET_LARK_PROFILE",
            ),
            identity=namespace.lark_identity,
        )
    service = WorkflowService(repository, directory=directory)
    templates = TemplateService(repository)

    if namespace.command == "template-create":
        template, version = templates.create_template(
            tenant_id=tenant_id,
            actor_person_id=namespace.actor,
            document=_load_mapping(namespace.source),
        )
        log("template_created", _template_payload(template, version))
        return 0

    if namespace.command == "template-add-version":
        template, version = templates.add_version(
            tenant_id=tenant_id,
            template_id=namespace.template_id,
            actor_person_id=namespace.actor,
            document=_load_mapping(namespace.source),
        )
        log("template_version_added", _template_payload(template, version))
        return 0

    if namespace.command in {
        "template-enable",
        "template-disable",
        "template-delete",
    }:
        transition = {
            "template-enable": templates.enable,
            "template-disable": templates.disable,
            "template-delete": templates.delete,
        }[namespace.command]
        template = transition(
            tenant_id,
            namespace.template_id,
            actor_person_id=namespace.actor,
        )
        log("template_status_changed", _template_payload(template))
        return 0

    if namespace.command == "template-list":
        log(
            "templates_listed",
            {
                "tenant_id": tenant_id,
                "templates": [
                    _template_payload(template)
                    for template in templates.list_templates(tenant_id)
                ],
            },
        )
        return 0

    if namespace.command == "template-show":
        template = templates.get_template(tenant_id, namespace.template_id)
        version = templates.get_version(
            tenant_id,
            namespace.template_id,
            namespace.version,
        )
        log(
            "template_state",
            {
                **_template_payload(template, version),
                "document": template_document(template, version),
                "audit": [
                    {
                        "id": event.id,
                        "event_type": event.event_type,
                        "actor_person_id": event.actor_person_id,
                        "aggregate_version": event.aggregate_version,
                        "payload": to_json_value(event.payload),
                        "occurred_at": to_json_value(event.occurred_at),
                    }
                    for event in repository.template_audit_log(
                        tenant_id,
                        namespace.template_id,
                    )
                ],
            },
        )
        return 0

    if namespace.command == "create-from-template":
        snapshot = templates.instantiate(
            tenant_id,
            namespace.template_id,
            inputs=_load_optional_mapping(namespace.inputs),
            owner_bindings=_load_mapping(namespace.bindings),
        )
        instance = service.create_draft(
            instance_id=namespace.instance_id,
            tenant_id=tenant_id,
            owner_person_id=namespace.owner,
            actor_person_id=namespace.owner,
            snapshot=snapshot,
        )
        log("template_instance_created", _draft_preview_payload(instance))
        return 0

    if namespace.command == "preview":
        instance = service.preview_draft(
            tenant_id,
            namespace.instance_id,
            actor_person_id=namespace.actor,
        )
        log("draft_previewed", _draft_preview_payload(instance))
        return 0

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

    if inbound_command:
        settings = TargetInboundSettings.from_environ(
            dsn=dsn,
            tenant_id=tenant_id,
            worker_id=namespace.inbound_worker_id,
        )
        inbox = PostgresWorkflowInbox(connection_factory)
        worker = WorkflowInboundWorker(
            service,
            repository,
            repository,
            inbox,
            tenant_id=settings.tenant_id,
            worker_id=settings.worker_id,
            claim_limit=settings.claim_limit,
            claim_ttl=settings.claim_ttl,
            retry_base=settings.retry_base,
            retry_max=settings.retry_max,
        )
        if namespace.command == "inbound-once":
            report = worker.run_once()
            log("inbound_tick", InboundWorkerLoop._report_fields(report))
            return int(bool(report.errors))

        stop_event = Event()
        _install_signal_handlers(stop_event, log, prefix="inbound")
        log(
            "inbound_started",
            {
                "tenant_id": settings.tenant_id,
                "worker_id": settings.worker_id,
                "claim_ttl_seconds": settings.claim_ttl.total_seconds(),
                "claim_limit": settings.claim_limit,
                "retry_base_seconds": settings.retry_base.total_seconds(),
                "retry_max_seconds": settings.retry_max.total_seconds(),
                "idle_min_seconds": settings.loop.idle_min_seconds,
                "idle_max_seconds": settings.loop.idle_max_seconds,
            },
        )
        InboundWorkerLoop(worker, settings=settings.loop, log=log).run(stop_event)
        return 0

    if verification_command:
        settings = TargetInboundSettings.from_environ(
            dsn=dsn,
            tenant_id=tenant_id,
            worker_id=namespace.inbound_worker_id,
        )
        inbox = PostgresWorkflowInbox(connection_factory)
        reader = CliFeishuTaskReader(
            profile=_required(
                namespace.lark_profile,
                "--lark-profile or LARKFLOW_TARGET_LARK_PROFILE",
            ),
            identity=namespace.lark_identity,
        )
        worker = TaskVerificationWorker(
            inbox,
            reader,
            tenant_id=settings.tenant_id,
            worker_id=settings.worker_id,
            claim_limit=settings.claim_limit,
            claim_ttl=settings.claim_ttl,
            retry_base=settings.retry_base,
            retry_max=settings.retry_max,
            max_attempts=settings.verification_max_attempts,
        )
        if namespace.command == "verify-inbound-once":
            report = worker.run_once()
            log(
                "inbound_verification_tick",
                VerificationWorkerLoop._report_fields(report),
            )
            return int(bool(report.errors))

        stop_event = Event()
        _install_signal_handlers(stop_event, log, prefix="inbound_verification")
        log(
            "inbound_verification_started",
            {
                "tenant_id": settings.tenant_id,
                "worker_id": settings.worker_id,
                "claim_ttl_seconds": settings.claim_ttl.total_seconds(),
                "claim_limit": settings.claim_limit,
                "retry_base_seconds": settings.retry_base.total_seconds(),
                "retry_max_seconds": settings.retry_max.total_seconds(),
                "verification_max_attempts": settings.verification_max_attempts,
                "idle_min_seconds": settings.loop.idle_min_seconds,
                "idle_max_seconds": settings.loop.idle_max_seconds,
                "lark_profile": namespace.lark_profile,
                "lark_identity": namespace.lark_identity,
            },
        )
        VerificationWorkerLoop(
            worker,
            settings=settings.loop,
            log=log,
        ).run(stop_event)
        return 0

    if projection_command:
        settings = TargetProjectionSettings.from_environ(
            dsn=dsn,
            tenant_id=tenant_id,
            worker_id=namespace.projection_worker_id,
        )
        lark_profile = _required(
            namespace.lark_profile,
            "--lark-profile or LARKFLOW_TARGET_LARK_PROFILE",
        )
        task_adapter = CliFeishuTaskProjection(
            profile=lark_profile,
            identity=namespace.lark_identity,
        )
        task_reader = CliFeishuTaskReader(
            profile=lark_profile,
            identity=namespace.lark_identity,
        )
        enable_im_commands = _env_boolean("LARKFLOW_TARGET_ENABLE_IM_COMMANDS")
        enable_im_projection = _env_boolean(
            "LARKFLOW_TARGET_ENABLE_IM_PROJECTION"
        )
        enable_doc_projection = _env_boolean(
            "LARKFLOW_TARGET_ENABLE_DOC_PROJECTION"
        )
        message_adapter = (
            CliFeishuMessageProjection(
                profile=lark_profile,
                identity=namespace.lark_identity,
            )
            if enable_im_commands or enable_im_projection
            else None
        )
        document_adapter = (
            CliFeishuDocumentProjection(
                profile=lark_profile,
                identity=namespace.lark_identity,
            )
            if enable_doc_projection
            else None
        )
        im_store = PostgresIMCommandStore(connection_factory)
        directory_adapter = (
            CliFeishuDirectory(
                profile=lark_profile,
                identity=namespace.lark_identity,
            )
            if enable_im_commands
            else None
        )
        im_verification_worker = (
            IMCommandVerificationWorker(
                im_store,
                directory_adapter,
                tenant_id=settings.tenant_id,
                worker_id=f"{settings.worker_id}:im-verify",
                claim_limit=settings.claim_limit,
                claim_ttl=settings.claim_ttl,
                retry_base=settings.retry_base,
                retry_max=settings.retry_max,
            )
            if enable_im_commands
            else None
        )
        role_binding_card_worker = (
            RoleBindingCardWorker(
                im_store,
                directory_adapter,
                message_adapter,
                tenant_id=settings.tenant_id,
                worker_id=f"{settings.worker_id}:role-card",
                claim_limit=settings.claim_limit,
                claim_ttl=settings.claim_ttl,
                retry_base=settings.retry_base,
                retry_max=settings.retry_max,
            )
            if enable_im_commands
            and directory_adapter is not None
            and message_adapter is not None
            else None
        )
        role_binding_verification_worker = (
            RoleBindingVerificationWorker(
                im_store,
                directory_adapter,
                tenant_id=settings.tenant_id,
                worker_id=f"{settings.worker_id}:role-verify",
                claim_limit=settings.claim_limit,
                claim_ttl=settings.claim_ttl,
                retry_base=settings.retry_base,
                retry_max=settings.retry_max,
            )
            if enable_im_commands and directory_adapter is not None
            else None
        )
        role_binding_reply_worker = (
            RoleBindingReplyWorker(
                im_store,
                message_adapter,
                tenant_id=settings.tenant_id,
                worker_id=f"{settings.worker_id}:role-reply",
                claim_limit=settings.claim_limit,
                claim_ttl=settings.claim_ttl,
                retry_base=settings.retry_base,
                retry_max=settings.retry_max,
            )
            if enable_im_commands and message_adapter is not None
            else None
        )
        im_reply_worker = (
            IMReplyWorker(
                im_store,
                message_adapter,
                tenant_id=settings.tenant_id,
                worker_id=f"{settings.worker_id}:im-reply",
                claim_limit=settings.claim_limit,
                claim_ttl=settings.claim_ttl,
                retry_base=settings.retry_base,
                retry_max=settings.retry_max,
            )
            if enable_im_commands and message_adapter is not None
            else None
        )
        completion_poller = TaskCompletionPoller(
            repository,
            repository,
            PostgresWorkflowInbox(connection_factory),
            task_reader,
            tenant_id=settings.tenant_id,
            batch_size=settings.completion_poll_batch_size,
        )
        worker = WorkflowProjectionWorker(
            repository,
            repository,
            repository,
            task_adapter,
            message_adapter=message_adapter if enable_im_projection else None,
            document_adapter=document_adapter,
            tenant_id=settings.tenant_id,
            worker_id=settings.worker_id,
            claim_limit=settings.claim_limit,
            claim_ttl=settings.claim_ttl,
            retry_base=settings.retry_base,
            retry_max=settings.retry_max,
        )
        if namespace.command == "project-once":
            if im_verification_worker is not None:
                verification = im_verification_worker.run_once()
                log(
                    "im_verification_tick",
                    {
                        "claimed": verification.claimed,
                        "verified": verification.verified,
                        "rejected": verification.rejected,
                        "failed": verification.failed,
                        "errors": list(verification.errors),
                    },
                )
            if role_binding_card_worker is not None:
                cards = role_binding_card_worker.run_once()
                log(
                    "role_binding_card_tick",
                    {
                        "claimed": cards.claimed,
                        "sent": cards.sent,
                        "failed": cards.failed,
                        "errors": list(cards.errors),
                    },
                )
            if role_binding_verification_worker is not None:
                bindings = role_binding_verification_worker.run_once()
                log(
                    "role_binding_verification_tick",
                    {
                        "claimed": bindings.claimed,
                        "verified": bindings.verified,
                        "rejected": bindings.rejected,
                        "failed": bindings.failed,
                        "errors": list(bindings.errors),
                    },
                )
            report = worker.run_once()
            log("projection_tick", ProjectionWorkerLoop._report_fields(report))
            if im_reply_worker is not None:
                replies = im_reply_worker.run_once()
                log(
                    "im_reply_tick",
                    {
                        "claimed": replies.claimed,
                        "sent": replies.sent,
                        "failed": replies.failed,
                        "errors": list(replies.errors),
                    },
                )
            if role_binding_reply_worker is not None:
                binding_replies = role_binding_reply_worker.run_once()
                log(
                    "role_binding_reply_tick",
                    {
                        "claimed": binding_replies.claimed,
                        "sent": binding_replies.sent,
                        "card_updates_failed": (
                            binding_replies.card_updates_failed
                        ),
                        "failed": binding_replies.failed,
                        "errors": list(binding_replies.errors),
                    },
                )
            return int(bool(report.errors))
        if namespace.command == "reconcile-projections":
            report = worker.reconcile_all(
                batch_size=settings.reconcile_batch_size,
            )
            log(
                "projection_reconciled",
                ProjectionWorkerLoop._reconciliation_fields(report),
            )
            return int(bool(report.errors) or report.interrupted)
        if namespace.command == "reconcile-instance-completion":
            report = worker.reconcile_instance_completion(namespace.instance_id)
            log(
                "instance_completion_reconciled",
                ProjectionWorkerLoop._report_fields(report),
            )
            return 0
        if namespace.command == "reconcile-completions":
            report = completion_poller.run_once()
            log(
                "completion_poll",
                ProjectionWorkerLoop._completion_fields(report),
            )
            return int(bool(report.errors) or report.interrupted)

        stop_event = Event()
        _install_signal_handlers(stop_event, log, prefix="projection")
        log(
            "projection_started",
            {
                "tenant_id": settings.tenant_id,
                "worker_id": settings.worker_id,
                "claim_ttl_seconds": settings.claim_ttl.total_seconds(),
                "claim_limit": settings.claim_limit,
                "retry_base_seconds": settings.retry_base.total_seconds(),
                "retry_max_seconds": settings.retry_max.total_seconds(),
                "reconcile_batch_size": settings.reconcile_batch_size,
                "completion_poll_seconds": settings.completion_poll_seconds,
                "completion_poll_batch_size": (
                    settings.completion_poll_batch_size
                ),
                "idle_min_seconds": settings.loop.idle_min_seconds,
                "idle_max_seconds": settings.loop.idle_max_seconds,
                "lark_profile": namespace.lark_profile,
                "lark_identity": namespace.lark_identity,
                "im_commands_enabled": enable_im_commands,
                "im_projection_enabled": enable_im_projection,
                "doc_projection_enabled": enable_doc_projection,
            },
        )
        ProjectionWorkerLoop(
            worker,
            settings=settings.loop,
            reconcile_batch_size=settings.reconcile_batch_size,
            completion_poller=completion_poller,
            im_verification_worker=im_verification_worker,
            im_reply_worker=im_reply_worker,
            role_binding_card_worker=role_binding_card_worker,
            role_binding_verification_worker=(
                role_binding_verification_worker
            ),
            role_binding_reply_worker=role_binding_reply_worker,
            completion_poll_seconds=settings.completion_poll_seconds,
            log=log,
        ).run(stop_event)
        return 0

    settings = TargetRuntimeSettings.from_environ(
        dsn=dsn,
        tenant_id=tenant_id,
        worker_id=namespace.worker_id,
    )
    if namespace.enable_development_executor:
        settings = replace(settings, enable_development_executor=True)
    if namespace.enable_agent_executor:
        settings = replace(settings, enable_agent_executor=True)
    if namespace.enable_content_check_executor:
        settings = replace(settings, enable_content_check_executor=True)
    service = WorkflowService(
        repository,
        runner=NodeRunner(claim_ttl=settings.claim_ttl),
    )
    executor_registry = _executors(settings, environ=os.environ, log=log)
    worker = WorkflowWorker(
        service,
        repository,
        tenant_id=settings.tenant_id,
        worker_id=settings.worker_id,
        executors=executor_registry,
        candidate_limit=settings.candidate_limit,
    )
    enable_im_commands = _env_boolean("LARKFLOW_TARGET_ENABLE_IM_COMMANDS")
    im_store = PostgresIMCommandStore(connection_factory)
    im_command_worker = (
        IMCommandWorker(
            im_store,
            service,
            templates,
            tenant_id=settings.tenant_id,
            worker_id=f"{settings.worker_id}:im-command",
        )
        if enable_im_commands
        else None
    )
    role_binding_worker = (
        RoleBindingActionWorker(
            im_store,
            service,
            templates,
            tenant_id=settings.tenant_id,
            worker_id=f"{settings.worker_id}:role-binding",
            claim_limit=settings.candidate_limit,
            claim_ttl=settings.claim_ttl,
        )
        if enable_im_commands
        else None
    )
    if namespace.command == "run-once":
        if im_command_worker is not None:
            command_report = im_command_worker.run_once()
            log(
                "im_command_tick",
                {
                    "claimed": command_report.claimed,
                    "processed": command_report.processed,
                    "rejected": command_report.rejected,
                    "failed": command_report.failed,
                    "errors": list(command_report.errors),
                },
            )
        if role_binding_worker is not None:
            binding_report = role_binding_worker.run_once()
            log(
                "role_binding_tick",
                {
                    "claimed": binding_report.claimed,
                    "processed": binding_report.processed,
                    "rejected": binding_report.rejected,
                    "failed": binding_report.failed,
                    "errors": list(binding_report.errors),
                },
            )
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
            "im_commands_enabled": enable_im_commands,
        },
    )
    WorkflowWorkerLoop(
        worker,
        im_command_worker=im_command_worker,
        role_binding_worker=role_binding_worker,
        settings=settings.loop,
        log=log,
    ).run(stop_event)
    return 0


def _executors(
    settings: TargetRuntimeSettings,
    *,
    environ: Mapping[str, str] | None = None,
    log: JsonLogger | None = None,
) -> dict[ExecutorKind, AutomatedExecutor]:
    registry: dict[ExecutorKind, AutomatedExecutor] = {}
    tool_adapters: list[object] = []
    if settings.enable_agent_executor:
        values = os.environ if environ is None else environ
        roles = load_llm_roles(dict(values))
        if not roles:
            raise ValueError(
                "Agent executor requires a complete LLM_BASE_URL, LLM_API_KEY, "
                "and LLM_MODEL route"
            )
        maximum_seconds = _maximum_llm_route_seconds(roles)
        required_seconds = (
            maximum_seconds + settings.agent_claim_safety.total_seconds()
        )
        if settings.claim_ttl.total_seconds() <= required_seconds:
            raise ValueError(
                "Target claim TTL must exceed the longest LLM route budget plus "
                f"the Agent safety margin ({required_seconds:g}s required)"
            )

        def note_call(fields: dict[str, Any]) -> None:
            if log is not None:
                log("agent_llm_call", fields)

        def note_failover(fields: dict[str, Any]) -> None:
            if log is not None:
                log("agent_llm_failover", fields, stream=sys.stderr)

        client = OpenAICompatLLM(
            roles,
            on_call=note_call,
            on_failover=note_failover,
        )
        registry[ExecutorKind.AGENT] = LLMAgentExecutor(
            client,
            max_prompt_chars=settings.agent_max_prompt_chars,
            max_result_chars=settings.agent_max_result_chars,
        )
    if settings.enable_development_executor:
        tool_adapters.append(DevelopmentToolExecutor())
    if settings.enable_content_check_executor:
        tool_adapters.append(
            ContentCheckToolExecutor(
                max_source_chars=settings.content_check_max_chars,
            )
        )
    if tool_adapters:
        registry[ExecutorKind.TOOL] = ToolExecutorRouter(tool_adapters)
    return registry


def _maximum_llm_route_seconds(roles: Mapping[str, Mapping[str, Any]]) -> float:
    """Bound one routed call, including every configured failover link."""

    maximum = 0.0
    for primary in roles.values():
        chain = (primary, *(primary.get("fallbacks") or ()))
        route_seconds = sum(
            float(link.get("timeout") or OpenAICompatLLM.DEFAULT_TIMEOUT)
            for link in chain
        )
        maximum = max(maximum, route_seconds)
    return maximum


def _install_signal_handlers(
    stop_event: Event,
    log: JsonLogger,
    *,
    prefix: str = "worker",
) -> None:
    def stop(signum: int, _frame: Any) -> None:
        log(f"{prefix}_stop_requested", {"signal": signal.Signals(signum).name})
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


def _load_optional_mapping(source: str | None) -> dict[str, Any]:
    return {} if source is None else _load_mapping(source)


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _env_boolean(name: str) -> bool:
    value = os.environ.get(name, "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be a boolean")


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
        "template_version_id": instance.snapshot.template_version_id,
        "locked": instance.snapshot.locked,
        "created_at": to_json_value(instance.created_at),
        "confirmed_at": to_json_value(instance.confirmed_at),
        "completed_at": to_json_value(instance.completed_at),
        "nodes": nodes,
    }


def _draft_preview_payload(instance: Any) -> dict[str, Any]:
    return {
        "tenant_id": instance.tenant_id,
        "instance_id": instance.id,
        "owner_person_id": instance.owner_person_id,
        "status": instance.status.value,
        "template_version_id": instance.snapshot.template_version_id,
        "locked": instance.snapshot.locked,
        "schema_version": instance.snapshot.schema_version,
        "goal": instance.snapshot.goal,
        "inputs": to_json_value(instance.snapshot.inputs),
        "nodes": [
            {
                "key": node.key,
                "title": node.title,
                "owner_person_id": node.owner_person_id,
                "executor": node.executor.value,
                "deps": list(node.deps),
                "work": to_json_value(node.work),
            }
            for node in instance.snapshot.nodes
        ],
    }


def _template_payload(template: Any, version: Any | None = None) -> dict[str, Any]:
    payload = {
        "tenant_id": template.tenant_id,
        "template_id": template.id,
        "name": template.name,
        "status": template.status.value,
        "aggregate_version": template.version,
        "created_at": to_json_value(template.created_at),
        "updated_at": to_json_value(template.updated_at),
        "deleted_at": to_json_value(template.deleted_at),
    }
    if version is not None:
        payload["template_version"] = {
            "id": version.id,
            "version": version.version,
            "schema_version": version.schema_version,
            "locked": version.locked,
            "content_hash": version.content_hash,
            "created_at": to_json_value(version.created_at),
        }
    return payload


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
