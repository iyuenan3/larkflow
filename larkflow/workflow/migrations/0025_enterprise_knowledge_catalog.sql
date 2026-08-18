CREATE TABLE workflow_enterprise_knowledge_versions (
    tenant_id text NOT NULL,
    source_id text NOT NULL CHECK (
        source_id ~ '^enterprise:[a-z][a-z0-9_.:-]{0,116}$'
    ),
    version_id text NOT NULL CHECK (
        version_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
    ),
    display_label text NOT NULL CHECK (
        char_length(display_label) BETWEEN 1 AND 200
    ),
    media_type text NOT NULL CHECK (
        media_type IN ('text/plain', 'text/markdown')
    ),
    size_bytes integer NOT NULL CHECK (size_bytes > 0),
    content_sha256 text NOT NULL CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    data_classification text NOT NULL CHECK (
        data_classification = 'internal'
    ),
    model_egress_policy text NOT NULL CHECK (
        model_egress_policy IN ('allow', 'deny')
    ),
    published_by_person_id text NOT NULL,
    published_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('published', 'revoked')),
    revoked_at timestamptz,
    PRIMARY KEY (tenant_id, source_id, version_id),
    CONSTRAINT workflow_enterprise_knowledge_revocation_shape CHECK (
        (status = 'published' AND revoked_at IS NULL)
        OR (
            status = 'revoked'
            AND revoked_at IS NOT NULL
            AND revoked_at >= published_at
        )
    )
);

CREATE UNIQUE INDEX workflow_enterprise_knowledge_one_published_version
    ON workflow_enterprise_knowledge_versions (tenant_id, source_id)
    WHERE status = 'published';

CREATE INDEX workflow_enterprise_knowledge_catalog_idx
    ON workflow_enterprise_knowledge_versions (
        tenant_id, status, published_at, source_id, version_id
    );

CREATE TABLE workflow_enterprise_knowledge_audit (
    tenant_id text NOT NULL,
    id text NOT NULL CHECK (id ~ '^[0-9a-f]{32}$'),
    source_id text NOT NULL,
    version_id text NOT NULL,
    event_type text NOT NULL CHECK (
        event_type IN ('enterprise_knowledge.published', 'enterprise_knowledge.revoked')
    ),
    actor_person_id text NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, source_id, version_id)
        REFERENCES workflow_enterprise_knowledge_versions (
            tenant_id, source_id, version_id
        )
);

CREATE INDEX workflow_enterprise_knowledge_audit_source_idx
    ON workflow_enterprise_knowledge_audit (
        tenant_id, source_id, version_id, occurred_at, id
    );

CREATE FUNCTION workflow_enterprise_knowledge_versions_guard_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'published'
       AND NEW.status = 'revoked'
       AND NEW.revoked_at IS NOT NULL
       AND OLD.tenant_id = NEW.tenant_id
       AND OLD.source_id = NEW.source_id
       AND OLD.version_id = NEW.version_id
       AND OLD.display_label = NEW.display_label
       AND OLD.media_type = NEW.media_type
       AND OLD.size_bytes = NEW.size_bytes
       AND OLD.content_sha256 = NEW.content_sha256
       AND OLD.data_classification = NEW.data_classification
       AND OLD.model_egress_policy = NEW.model_egress_policy
       AND OLD.published_by_person_id = NEW.published_by_person_id
       AND OLD.published_at = NEW.published_at
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'workflow enterprise knowledge versions are immutable';
END;
$$;

CREATE TRIGGER workflow_enterprise_knowledge_versions_immutable
    BEFORE UPDATE ON workflow_enterprise_knowledge_versions
    FOR EACH ROW
    EXECUTE FUNCTION workflow_enterprise_knowledge_versions_guard_update();

CREATE FUNCTION workflow_enterprise_knowledge_deny_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'workflow enterprise knowledge history is append-preserved';
END;
$$;

CREATE TRIGGER workflow_enterprise_knowledge_versions_preserve_history
    BEFORE DELETE ON workflow_enterprise_knowledge_versions
    FOR EACH ROW
    EXECUTE FUNCTION workflow_enterprise_knowledge_deny_delete();

CREATE TRIGGER workflow_enterprise_knowledge_audit_append_only
    BEFORE UPDATE OR DELETE ON workflow_enterprise_knowledge_audit
    FOR EACH ROW
    EXECUTE FUNCTION workflow_enterprise_knowledge_deny_delete();
