ALTER TABLE workflow_enterprise_knowledge_versions
    ADD CONSTRAINT workflow_enterprise_knowledge_version_content_identity
    UNIQUE (tenant_id, source_id, version_id, content_sha256);

CREATE TABLE workflow_enterprise_knowledge_authorizations (
    tenant_id text NOT NULL,
    proof_id text NOT NULL CHECK (proof_id ~ '^kp_[0-9a-f]{32}$'),
    source_id text NOT NULL,
    version_id text NOT NULL,
    content_sha256 text NOT NULL CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    authorization_scope text NOT NULL CHECK (
        authorization_scope = 'tenant_all_members'
    ),
    policy_version text NOT NULL CHECK (
        policy_version = 'tenant_all_members_v1'
    ),
    statement_sha256 text NOT NULL CHECK (
        statement_sha256 ~ '^[0-9a-f]{64}$'
    ),
    authorized_by_person_id text NOT NULL,
    authorized_at timestamptz NOT NULL,
    proof_fingerprint text NOT NULL CHECK (
        proof_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    PRIMARY KEY (tenant_id, proof_id),
    UNIQUE (tenant_id, source_id, version_id),
    FOREIGN KEY (
        tenant_id, source_id, version_id, content_sha256
    ) REFERENCES workflow_enterprise_knowledge_versions (
        tenant_id, source_id, version_id, content_sha256
    )
);

CREATE INDEX workflow_enterprise_knowledge_authorization_source_idx
    ON workflow_enterprise_knowledge_authorizations (
        tenant_id, source_id, version_id, authorized_at, proof_id
    );

CREATE TRIGGER workflow_enterprise_knowledge_authorizations_append_only
    BEFORE UPDATE OR DELETE ON workflow_enterprise_knowledge_authorizations
    FOR EACH ROW
    EXECUTE FUNCTION workflow_enterprise_knowledge_deny_delete();
