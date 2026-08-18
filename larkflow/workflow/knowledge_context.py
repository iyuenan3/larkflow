"""Fail-closed enterprise knowledge context for planning and Agent Attempts."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from larkflow.agent_runtime.contracts import (
    AgentContextRequest,
    ENTERPRISE_KNOWLEDGE_INPUT,
)
from larkflow.knowledge.blob import (
    EnterpriseKnowledgeBlobStore,
    EnterpriseKnowledgeBlobUnavailableError,
    enterprise_knowledge_object_key,
)
from larkflow.knowledge.contracts import (
    EnterpriseKnowledgePublication,
    EnterpriseKnowledgeRef,
)
from larkflow.knowledge.repository import (
    EnterpriseKnowledgeNotFoundError,
    EnterpriseKnowledgeRepository,
)
from larkflow.planning.context import (
    ContextBundle,
    ContextChunk,
    SourceRef,
    sha256_hex,
)

from .draft_generation import DraftGenerationRejected


PLANNING_KNOWLEDGE_CONTEXT_TTL = timedelta(minutes=15)
AGENT_KNOWLEDGE_CONTEXT_TTL = timedelta(minutes=5)
DEFAULT_PLANNING_CONTEXT_MAX_CHARS = 60_000


class EnterpriseKnowledgeContextRejected(DraftGenerationRejected):
    """A selected source failed deterministic authorization or integrity."""

    error_code = "agent_context_rejected"


class EnterpriseKnowledgeContextUnavailable(RuntimeError):
    """The configured content store could not be read and may recover."""


class EnterpriseKnowledgeContextService:
    """Re-authorize immutable source versions before reading any body."""

    def __init__(
        self,
        repository: EnterpriseKnowledgeRepository,
        blob_store: EnterpriseKnowledgeBlobStore,
        *,
        model_egress_policy: str = "deny",
        planning_max_chars: int = DEFAULT_PLANNING_CONTEXT_MAX_CHARS,
        clock: Any = None,
    ) -> None:
        if model_egress_policy not in {"allow", "deny"}:
            raise ValueError("enterprise knowledge model egress policy is invalid")
        if planning_max_chars < 1:
            raise ValueError("enterprise knowledge character budget is invalid")
        self.repository = repository
        self.blob_store = blob_store
        self.model_egress_policy = model_egress_policy
        self.planning_max_chars = planning_max_chars
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def build_for_planning(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_person_id: str,
    ) -> ContextBundle | None:
        references = tuple(
            item
            for item in self.repository.list_published(tenant_id)
            if item.content_authorized
        )
        if not references:
            return None
        now = _utc(self.clock())
        return self._build(
            tenant_id=tenant_id,
            references=references,
            scope_kind="console_draft_request",
            scope_id=request_id,
            purpose="planning",
            actor_person_id=actor_person_id,
            node_key=None,
            attempt_id=None,
            max_chars=self.planning_max_chars,
            created_at=now,
            expires_at=now + PLANNING_KNOWLEDGE_CONTEXT_TTL,
        )

    def build_for_agent(
        self,
        request: AgentContextRequest,
        references: tuple[EnterpriseKnowledgeRef, ...],
        *,
        max_chars: int,
    ) -> ContextBundle:
        now = _utc(self.clock())
        return self._build(
            tenant_id=request.tenant_id,
            references=references,
            scope_kind="workflow_instance",
            scope_id=request.instance_id,
            purpose="agent_execution",
            actor_person_id=request.owner_person_id,
            node_key=request.node_key,
            attempt_id=request.attempt_id,
            max_chars=max_chars,
            created_at=now,
            expires_at=now + AGENT_KNOWLEDGE_CONTEXT_TTL,
        )

    def _build(
        self,
        *,
        tenant_id: str,
        references: tuple[EnterpriseKnowledgeRef, ...],
        scope_kind: str,
        scope_id: str,
        purpose: str,
        actor_person_id: str,
        node_key: str | None,
        attempt_id: str | None,
        max_chars: int,
        created_at: datetime,
        expires_at: datetime,
    ) -> ContextBundle:
        if self.model_egress_policy != "allow":
            raise EnterpriseKnowledgeContextRejected(
                "当前 Worker 未允许企业共享资料模型外发"
            )
        if len({item.source_id for item in references}) != len(references):
            raise EnterpriseKnowledgeContextRejected("企业共享资料来源重复")
        sources: list[SourceRef] = []
        chunks: list[ContextChunk] = []
        total_chars = 0
        for order, expected in enumerate(references):
            publication = self._authorize_current(tenant_id, expected)
            content = self._read_content(publication)
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise EnterpriseKnowledgeContextRejected(
                    "企业共享资料不是有效 UTF-8 文本"
                ) from exc
            total_chars += len(text)
            if total_chars > max_chars:
                raise EnterpriseKnowledgeContextRejected(
                    "企业共享资料超过上下文字符预算"
                )
            sources.append(
                SourceRef(
                    source_id=expected.source_id,
                    kind="enterprise_knowledge",
                    label=expected.display_label,
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
        return ContextBundle(
            tenant_id=tenant_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
            purpose=purpose,
            actor_person_id=actor_person_id,
            node_key=node_key,
            attempt_id=attempt_id,
            sources=tuple(sources),
            chunks=tuple(chunks),
            enterprise_knowledge=references,
            data_classification="internal",
            egress_decision="allow",
            created_at=created_at,
            expires_at=expires_at,
        )

    def _authorize_current(
        self,
        tenant_id: str,
        expected: EnterpriseKnowledgeRef,
    ) -> EnterpriseKnowledgePublication:
        if expected.tenant_id != tenant_id:
            raise EnterpriseKnowledgeContextRejected(
                "企业共享资料引用跨越 tenant"
            )
        try:
            current = self.repository.get_version(
                tenant_id,
                expected.source_id,
                expected.version_id,
            )
        except EnterpriseKnowledgeNotFoundError as exc:
            raise EnterpriseKnowledgeContextRejected(
                "企业共享资料版本不存在"
            ) from exc
        if current.status != "published":
            raise EnterpriseKnowledgeContextRejected("企业共享资料版本已撤销")
        if current.authorization_proof is None:
            raise EnterpriseKnowledgeContextRejected("企业共享资料缺少授权证明")
        if current.ref != expected:
            raise EnterpriseKnowledgeContextRejected("企业共享资料版本发生漂移")
        if expected.data_classification != "internal":
            raise EnterpriseKnowledgeContextRejected("企业共享资料分级不受支持")
        if expected.egress_decision != "allow":
            raise EnterpriseKnowledgeContextRejected(
                "企业共享资料模型外发未获授权"
            )
        return current

    def _read_content(self, publication: EnterpriseKnowledgePublication) -> bytes:
        ref = publication.ref
        object_key = enterprise_knowledge_object_key(
            tenant_id=ref.tenant_id,
            source_id=ref.source_id,
            version_id=ref.version_id,
            content_sha256=ref.content_sha256,
        )
        try:
            content = self.blob_store.get(object_key)
        except (FileNotFoundError, ValueError) as exc:
            raise EnterpriseKnowledgeContextRejected(
                "企业共享资料正文不可用"
            ) from exc
        except EnterpriseKnowledgeBlobUnavailableError as exc:
            raise EnterpriseKnowledgeContextUnavailable(
                "企业共享资料存储暂时不可用"
            ) from exc
        if len(content) != ref.size_bytes:
            raise EnterpriseKnowledgeContextRejected("企业共享资料长度校验失败")
        if sha256_hex(content) != ref.content_sha256:
            raise EnterpriseKnowledgeContextRejected("企业共享资料完整性校验失败")
        return content


class PlanningKnowledgeContextService:
    """Merge project attachments and tenant-wide knowledge deterministically."""

    def __init__(
        self,
        *,
        project_service: Any = None,
        enterprise_service: EnterpriseKnowledgeContextService | None = None,
        max_context_chars: int = DEFAULT_PLANNING_CONTEXT_MAX_CHARS,
    ) -> None:
        if project_service is None and enterprise_service is None:
            raise ValueError("planning knowledge context has no source service")
        self.project_service = project_service
        self.enterprise_service = enterprise_service
        self.max_context_chars = max_context_chars

    def build_for_planning(self, request: Any) -> ContextBundle | None:
        if request.attachment_manifest and self.project_service is None:
            raise EnterpriseKnowledgeContextRejected(
                "项目附件上下文服务未配置"
            )
        project = (
            self.project_service.build_for_planning(request)
            if self.project_service is not None
            else None
        )
        enterprise = (
            self.enterprise_service.build_for_planning(
                tenant_id=request.tenant_id,
                request_id=request.id,
                actor_person_id=request.requester_person_id,
            )
            if self.enterprise_service is not None
            else None
        )
        return merge_context_bundles(
            project,
            enterprise,
            max_chars=self.max_context_chars,
        )

    def build_for_identity(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_person_id: str,
    ) -> ContextBundle | None:
        if self.enterprise_service is None:
            return None
        return self.enterprise_service.build_for_planning(
            tenant_id=tenant_id,
            request_id=request_id,
            actor_person_id=actor_person_id,
        )

    def promote(self, request: Any, *, instance_id: str) -> None:
        if request.attachment_manifest:
            if self.project_service is None:
                raise EnterpriseKnowledgeContextRejected(
                    "项目附件上下文服务未配置"
                )
            self.project_service.promote(request, instance_id=instance_id)


class EnterpriseAgentContextService:
    """Resolve an exact enterprise selection for one new Agent Attempt."""

    def __init__(
        self,
        context_service: EnterpriseKnowledgeContextService,
        *,
        max_context_chars: int,
    ) -> None:
        self.context_service = context_service
        self.max_context_chars = max_context_chars

    def resolve(self, request: AgentContextRequest) -> ContextBundle | None:
        if not _declares_enterprise_knowledge(request.work_contract):
            return None
        inputs = request.input_snapshot.get("instance_inputs")
        if not isinstance(inputs, Mapping):
            raise EnterpriseKnowledgeContextRejected(
                "当前 Attempt 缺少 Instance 输入快照"
            )
        references = enterprise_knowledge_manifest(
            inputs.get("enterprise_knowledge"),
            tenant_id=request.tenant_id,
        )
        if not references:
            raise EnterpriseKnowledgeContextRejected(
                "当前 Instance 没有冻结的企业共享资料"
            )
        return self.context_service.build_for_agent(
            request,
            references,
            max_chars=self.max_context_chars,
        )


class CombinedAgentContextService:
    """Apply one shared budget across project and enterprise Agent context."""

    def __init__(
        self,
        *,
        project_service: Any = None,
        enterprise_service: EnterpriseAgentContextService | None = None,
        max_context_chars: int,
    ) -> None:
        if project_service is None and enterprise_service is None:
            raise ValueError("Agent context has no source service")
        self.project_service = project_service
        self.enterprise_service = enterprise_service
        self.max_context_chars = max_context_chars

    def resolve(self, request: AgentContextRequest) -> ContextBundle | None:
        project = (
            self.project_service.resolve(request)
            if self.project_service is not None
            else None
        )
        enterprise = (
            self.enterprise_service.resolve(request)
            if self.enterprise_service is not None
            else None
        )
        merged = merge_context_bundles(
            project,
            enterprise,
            max_chars=self.max_context_chars,
        )
        if merged is not None:
            _validate_frozen_planning_manifest(request, merged)
        return merged


def merge_context_bundles(
    project: ContextBundle | None,
    enterprise: ContextBundle | None,
    *,
    max_chars: int,
) -> ContextBundle | None:
    bundles = tuple(item for item in (project, enterprise) if item is not None)
    if not bundles:
        return None
    if max_chars < 1:
        raise ValueError("merged context character budget is invalid")
    baseline = bundles[0]
    bindings = (
        "tenant_id",
        "scope_kind",
        "scope_id",
        "purpose",
        "actor_person_id",
        "node_key",
        "attempt_id",
        "data_classification",
        "egress_decision",
    )
    if any(
        getattr(item, field) != getattr(baseline, field)
        for item in bundles[1:]
        for field in bindings
    ):
        raise EnterpriseKnowledgeContextRejected(
            "上下文来源的授权范围不一致"
        )
    if sum(len(chunk.text) for item in bundles for chunk in item.chunks) > max_chars:
        raise EnterpriseKnowledgeContextRejected("合并上下文超过字符预算")
    sources = tuple(source for item in bundles for source in item.sources)
    if len({source.source_id for source in sources}) != len(sources):
        raise EnterpriseKnowledgeContextRejected("合并上下文来源重复")
    chunks = tuple(
        ContextChunk(
            source_id=chunk.source_id,
            order=order,
            text=chunk.text,
            text_sha256=chunk.text_sha256,
        )
        for order, chunk in enumerate(
            chunk for item in bundles for chunk in item.chunks
        )
    )
    return ContextBundle(
        tenant_id=baseline.tenant_id,
        scope_kind=baseline.scope_kind,
        scope_id=baseline.scope_id,
        purpose=baseline.purpose,
        actor_person_id=baseline.actor_person_id,
        node_key=baseline.node_key,
        attempt_id=baseline.attempt_id,
        sources=sources,
        chunks=chunks,
        attachments=tuple(item for bundle in bundles for item in bundle.attachments),
        enterprise_knowledge=tuple(
            item for bundle in bundles for item in bundle.enterprise_knowledge
        ),
        data_classification="internal",
        egress_decision="allow",
        created_at=max(item.created_at for item in bundles),
        expires_at=min(item.expires_at for item in bundles if item.expires_at),
    )


def enterprise_knowledge_manifest(
    value: object,
    *,
    tenant_id: str,
) -> tuple[EnterpriseKnowledgeRef, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise EnterpriseKnowledgeContextRejected(
            "企业共享资料冻结清单无效"
        )
    references = []
    for item in value:
        if not isinstance(item, Mapping):
            raise EnterpriseKnowledgeContextRejected(
                "企业共享资料冻结引用无效"
            )
        try:
            document = dict(item)
            if "tenant_id" in document:
                raise ValueError("tenant_id is server-owned")
            published_at = document.get("published_at")
            if not isinstance(published_at, str):
                raise ValueError("published_at is invalid")
            document["published_at"] = datetime.fromisoformat(published_at)
            references.append(
                EnterpriseKnowledgeRef(tenant_id=tenant_id, **document)
            )
        except (TypeError, ValueError) as exc:
            raise EnterpriseKnowledgeContextRejected(
                "企业共享资料冻结引用无效"
            ) from exc
    if len({item.source_id for item in references}) != len(references):
        raise EnterpriseKnowledgeContextRejected("企业共享资料冻结引用重复")
    return tuple(references)


def _declares_enterprise_knowledge(work: Mapping[str, Any]) -> bool:
    values = work.get("inputs") or ()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return False
    for value in values:
        reference = value.get("ref") if isinstance(value, Mapping) else value
        if reference == ENTERPRISE_KNOWLEDGE_INPUT:
            return True
    return False


def _validate_frozen_planning_manifest(
    request: AgentContextRequest,
    agent_bundle: ContextBundle,
) -> None:
    inputs = request.input_snapshot.get("instance_inputs")
    manifest = inputs.get("context_manifest") if isinstance(inputs, Mapping) else None
    base_required = {
        "scope_kind",
        "scope_id",
        "purpose",
        "data_classification",
        "egress_decision",
        "fingerprint",
    }
    allowed_shapes = (base_required, base_required | {"source_kinds"})
    if not isinstance(manifest, Mapping) or set(manifest) not in allowed_shapes:
        raise EnterpriseKnowledgeContextRejected("冻结上下文清单无效")
    if any(not isinstance(value, str) for value in manifest.values()):
        raise EnterpriseKnowledgeContextRejected("冻结上下文清单无效")
    if (
        manifest["scope_kind"] != "console_draft_request"
        or manifest["purpose"] != "planning"
        or manifest["data_classification"] != "internal"
        or manifest["egress_decision"] != "allow"
    ):
        raise EnterpriseKnowledgeContextRejected("冻结上下文策略无效")
    actual_kinds = ",".join(
        dict.fromkeys(item.kind for item in agent_bundle.sources)
    )
    declared_kinds = manifest.get("source_kinds", "attachment")
    if declared_kinds != actual_kinds:
        raise EnterpriseKnowledgeContextRejected("冻结上下文来源类型不一致")
    planning = ContextBundle(
        tenant_id=agent_bundle.tenant_id,
        scope_kind="console_draft_request",
        scope_id=manifest["scope_id"],
        purpose="planning",
        actor_person_id=agent_bundle.actor_person_id,
        sources=agent_bundle.sources,
        chunks=agent_bundle.chunks,
        attachments=agent_bundle.attachments,
        enterprise_knowledge=agent_bundle.enterprise_knowledge,
        data_classification="internal",
        egress_decision="allow",
        created_at=agent_bundle.created_at,
        expires_at=agent_bundle.expires_at,
    )
    if planning.fingerprint != manifest["fingerprint"]:
        raise EnterpriseKnowledgeContextRejected("冻结上下文清单指纹不一致")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("knowledge context clock must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "AGENT_KNOWLEDGE_CONTEXT_TTL",
    "CombinedAgentContextService",
    "DEFAULT_PLANNING_CONTEXT_MAX_CHARS",
    "EnterpriseAgentContextService",
    "EnterpriseKnowledgeContextRejected",
    "EnterpriseKnowledgeContextService",
    "EnterpriseKnowledgeContextUnavailable",
    "PLANNING_KNOWLEDGE_CONTEXT_TTL",
    "PlanningKnowledgeContextService",
    "enterprise_knowledge_manifest",
    "merge_context_bundles",
]
