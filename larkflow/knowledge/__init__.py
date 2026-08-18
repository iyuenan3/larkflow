"""Provider-neutral contracts for authorized enterprise knowledge."""

from .blob import (
    EnterpriseKnowledgeBlobStore,
    EnterpriseKnowledgeBlobUnavailableError,
    FilesystemEnterpriseKnowledgeBlobStore,
    InMemoryEnterpriseKnowledgeBlobStore,
    enterprise_knowledge_object_key,
)

from .contracts import (
    ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1,
    ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1,
    EnterpriseKnowledgeAuthorizationProof,
    EnterpriseKnowledgePublication,
    EnterpriseKnowledgeRef,
    EnterpriseKnowledgeSelection,
    enterprise_knowledge_authorization_fingerprint,
    enterprise_knowledge_fingerprint,
)

__all__ = [
    "ENTERPRISE_KNOWLEDGE_AUTHORIZATION_POLICY_V1",
    "ENTERPRISE_KNOWLEDGE_AUTHORIZATION_STATEMENT_V1",
    "EnterpriseKnowledgeBlobStore",
    "EnterpriseKnowledgeBlobUnavailableError",
    "EnterpriseKnowledgeAuthorizationProof",
    "EnterpriseKnowledgePublication",
    "EnterpriseKnowledgeRef",
    "EnterpriseKnowledgeSelection",
    "FilesystemEnterpriseKnowledgeBlobStore",
    "InMemoryEnterpriseKnowledgeBlobStore",
    "enterprise_knowledge_authorization_fingerprint",
    "enterprise_knowledge_fingerprint",
    "enterprise_knowledge_object_key",
]
