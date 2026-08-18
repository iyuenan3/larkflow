"""Fail-closed project attachment context for one Agent Attempt."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from larkflow.agent_runtime.contracts import (
    AgentContextRequest,
    PROJECT_ATTACHMENTS_INPUT,
)
from larkflow.planning.context import (
    AttachmentRef,
    ContextBundle,
    ContextChunk,
    SourceRef,
    sha256_hex,
)

from .console_attachments import (
    AttachmentBlobStore,
    AttachmentContextRejected,
    ConsoleAttachmentRepository,
)


AGENT_CONTEXT_TTL = timedelta(minutes=5)
DEFAULT_AGENT_CONTEXT_MAX_CHARS = 12_000


class AgentContextRejected(ValueError):
    """A requested source failed deterministic authorization or integrity."""

    error_code = "agent_context_rejected"


class AgentContextService:
    """Resolve frozen Instance refs without exposing repository or blob handles."""

    def __init__(
        self,
        repository: ConsoleAttachmentRepository,
        blob_store: AttachmentBlobStore,
        *,
        model_egress_policy: str = "deny",
        max_context_chars: int = DEFAULT_AGENT_CONTEXT_MAX_CHARS,
        clock: Any = None,
    ) -> None:
        if model_egress_policy not in {"allow", "deny"}:
            raise ValueError("Agent context model egress policy is invalid")
        if max_context_chars < 1:
            raise ValueError("Agent context character budget must be positive")
        self.repository = repository
        self.blob_store = blob_store
        self.model_egress_policy = model_egress_policy
        self.max_context_chars = max_context_chars
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def resolve(self, request: AgentContextRequest) -> ContextBundle | None:
        if not _declares_project_attachments(request.work_contract):
            return None
        if self.model_egress_policy != "allow":
            raise AgentContextRejected("当前 Worker 未允许项目附件模型外发")
        instance_inputs = request.input_snapshot.get("instance_inputs")
        if not isinstance(instance_inputs, Mapping):
            raise AgentContextRejected("当前 Attempt 缺少 Instance 输入快照")
        raw_attachments = instance_inputs.get("project_attachments")
        raw_manifest = instance_inputs.get("context_manifest")
        references = _attachment_manifest(raw_attachments)
        manifest = _context_manifest(raw_manifest)
        if not references:
            raise AgentContextRejected("当前 Instance 没有冻结的项目附件")
        try:
            attachments = self.repository.resolve_for_agent(
                request.tenant_id,
                request.instance_id,
                references,
            )
        except AttachmentContextRejected as exc:
            raise AgentContextRejected("项目附件冻结清单不可用") from exc
        if len(attachments) != len(references):
            raise AgentContextRejected("项目附件冻结清单不完整")
        origin_ids = {item.origin_request_id for item in attachments}
        uploader_ids = {item.uploader_person_id for item in attachments}
        if origin_ids != {manifest["scope_id"]} or len(uploader_ids) != 1:
            raise AgentContextRejected("项目附件来源绑定不一致")

        sources: list[SourceRef] = []
        chunks: list[ContextChunk] = []
        total_chars = 0
        for order, (expected, attachment) in enumerate(zip(references, attachments)):
            if attachment.status != "ready" or attachment.revoked_at is not None:
                raise AgentContextRejected("项目附件已撤销或不可用")
            if attachment.data_classification != "internal":
                raise AgentContextRejected("项目附件分级不受支持")
            if attachment.model_egress_policy != "allow":
                raise AgentContextRejected("项目附件模型外发未获授权")
            try:
                content = self.blob_store.get(attachment.object_key)
            except (FileNotFoundError, ValueError) as exc:
                raise AgentContextRejected("项目附件正文不可用") from exc
            if len(content) != attachment.size_bytes:
                raise AgentContextRejected("项目附件长度校验失败")
            if sha256_hex(content) != attachment.content_sha256:
                raise AgentContextRejected("项目附件完整性校验失败")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AgentContextRejected("项目附件不是有效 UTF-8 文本") from exc
            total_chars += len(text)
            if total_chars > self.max_context_chars:
                raise AgentContextRejected("项目附件超过 Agent 上下文字符预算")
            sources.append(
                SourceRef(
                    source_id=expected.source_id,
                    kind="attachment",
                    label=expected.display_filename,
                    content_sha256=expected.content_sha256,
                )
            )
            chunks.append(
                ContextChunk(
                    source_id=expected.source_id,
                    order=order,
                    text=text,
                )
            )

        now = _utc(self.clock())
        planning_bundle = ContextBundle(
            tenant_id=request.tenant_id,
            scope_kind="console_draft_request",
            scope_id=manifest["scope_id"],
            purpose="planning",
            actor_person_id=next(iter(uploader_ids)),
            sources=tuple(sources),
            attachments=references,
            chunks=tuple(chunks),
            data_classification=manifest["data_classification"],
            egress_decision=manifest["egress_decision"],
            created_at=now,
            expires_at=now + AGENT_CONTEXT_TTL,
        )
        if planning_bundle.fingerprint != manifest["fingerprint"]:
            raise AgentContextRejected("项目附件原始规划清单指纹不一致")

        return ContextBundle(
            tenant_id=request.tenant_id,
            scope_kind="workflow_instance",
            scope_id=request.instance_id,
            purpose="agent_execution",
            actor_person_id=request.owner_person_id,
            node_key=request.node_key,
            attempt_id=request.attempt_id,
            sources=tuple(sources),
            attachments=references,
            chunks=tuple(chunks),
            data_classification="internal",
            egress_decision="allow",
            created_at=now,
            expires_at=now + AGENT_CONTEXT_TTL,
        )


def _declares_project_attachments(work: Mapping[str, Any]) -> bool:
    values = work.get("inputs") or ()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return False
    for value in values:
        reference = value.get("ref") if isinstance(value, Mapping) else value
        if reference == PROJECT_ATTACHMENTS_INPUT:
            return True
    return False


def _attachment_manifest(value: object) -> tuple[AttachmentRef, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AgentContextRejected("项目附件冻结清单无效")
    references = []
    for item in value:
        if not isinstance(item, Mapping):
            raise AgentContextRejected("项目附件冻结引用无效")
        try:
            references.append(AttachmentRef(**dict(item)))
        except (TypeError, ValueError) as exc:
            raise AgentContextRejected("项目附件冻结引用无效") from exc
    if len({item.attachment_id for item in references}) != len(references):
        raise AgentContextRejected("项目附件冻结引用重复")
    return tuple(references)


def _context_manifest(value: object) -> dict[str, str]:
    required = {
        "scope_kind",
        "scope_id",
        "purpose",
        "data_classification",
        "egress_decision",
        "fingerprint",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise AgentContextRejected("项目附件上下文清单无效")
    normalized = {key: item for key, item in value.items() if isinstance(item, str)}
    if set(normalized) != required:
        raise AgentContextRejected("项目附件上下文清单无效")
    if (
        normalized["scope_kind"] != "console_draft_request"
        or normalized["purpose"] != "planning"
        or normalized["data_classification"] != "internal"
        or normalized["egress_decision"] != "allow"
    ):
        raise AgentContextRejected("项目附件上下文策略无效")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Agent context clock must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "AGENT_CONTEXT_TTL",
    "AgentContextRejected",
    "AgentContextService",
    "DEFAULT_AGENT_CONTEXT_MAX_CHARS",
]
