CREATE TABLE workflow_templates (
    tenant_id text NOT NULL,
    id text NOT NULL,
    name text NOT NULL,
    status text NOT NULL CHECK (status IN ('draft', 'enabled', 'disabled', 'deleted')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE workflow_template_versions (
    tenant_id text NOT NULL,
    id text NOT NULL,
    template_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    schema_version text NOT NULL,
    locked boolean NOT NULL DEFAULT false,
    definition jsonb NOT NULL,
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, template_id, version),
    FOREIGN KEY (tenant_id, template_id)
        REFERENCES workflow_templates (tenant_id, id)
);

CREATE TABLE workflow_instances (
    tenant_id text NOT NULL,
    id text NOT NULL,
    owner_person_id text NOT NULL,
    template_version_id text,
    status text NOT NULL CHECK (
        status IN ('draft', 'running', 'paused', 'done', 'failed', 'canceled', 'discarded')
    ),
    graph_revision integer NOT NULL CHECK (graph_revision > 0),
    version bigint NOT NULL CHECK (version >= 0),
    schema_version text NOT NULL,
    goal text NOT NULL DEFAULT '',
    inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
    snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    confirmed_at timestamptz,
    completed_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, template_version_id)
        REFERENCES workflow_template_versions (tenant_id, id)
);

CREATE TABLE workflow_node_instances (
    tenant_id text NOT NULL,
    instance_id text NOT NULL,
    id text NOT NULL,
    node_key text NOT NULL,
    owner_person_id text NOT NULL,
    executor text NOT NULL CHECK (executor IN ('human', 'agent', 'tool')),
    status text NOT NULL CHECK (
        status IN ('pending', 'ready', 'running', 'waiting_human', 'done', 'failed', 'canceled')
    ),
    current_attempt_no integer NOT NULL CHECK (current_attempt_no > 0),
    version bigint NOT NULL CHECK (version >= 0),
    ready_at timestamptz,
    started_at timestamptz,
    completed_at timestamptz,
    PRIMARY KEY (tenant_id, instance_id, node_key),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, instance_id)
        REFERENCES workflow_instances (tenant_id, id)
);

CREATE TABLE workflow_node_dependencies (
    tenant_id text NOT NULL,
    instance_id text NOT NULL,
    node_key text NOT NULL,
    dependency_key text NOT NULL,
    PRIMARY KEY (tenant_id, instance_id, node_key, dependency_key),
    FOREIGN KEY (tenant_id, instance_id, node_key)
        REFERENCES workflow_node_instances (tenant_id, instance_id, node_key),
    FOREIGN KEY (tenant_id, instance_id, dependency_key)
        REFERENCES workflow_node_instances (tenant_id, instance_id, node_key)
);

CREATE TABLE workflow_node_attempts (
    tenant_id text NOT NULL,
    instance_id text NOT NULL,
    node_key text NOT NULL,
    attempt_no integer NOT NULL CHECK (attempt_no > 0),
    id text NOT NULL,
    node_instance_id text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('pending', 'running', 'waiting_human', 'done', 'failed', 'canceled')
    ),
    input_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb,
    quality_result jsonb,
    claim_token text,
    claim_expires_at timestamptz,
    started_at timestamptz,
    completed_at timestamptz,
    submitted_by_person_id text,
    error_code text,
    error_message text,
    PRIMARY KEY (tenant_id, instance_id, node_key, attempt_no),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, instance_id, node_key)
        REFERENCES workflow_node_instances (tenant_id, instance_id, node_key),
    FOREIGN KEY (tenant_id, node_instance_id)
        REFERENCES workflow_node_instances (tenant_id, id)
);

CREATE TABLE workflow_projections (
    tenant_id text NOT NULL,
    id text NOT NULL,
    instance_id text NOT NULL,
    node_instance_id text NOT NULL,
    attempt_no integer NOT NULL CHECK (attempt_no > 0),
    kind text NOT NULL,
    external_id text,
    external_url text,
    idempotency_key text NOT NULL,
    sync_version bigint NOT NULL DEFAULT 0 CHECK (sync_version >= 0),
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, node_instance_id, attempt_no, kind),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, instance_id)
        REFERENCES workflow_instances (tenant_id, id),
    FOREIGN KEY (tenant_id, node_instance_id)
        REFERENCES workflow_node_instances (tenant_id, id)
);

CREATE TABLE workflow_audit_events (
    tenant_id text NOT NULL,
    id text NOT NULL,
    instance_id text NOT NULL,
    node_key text,
    attempt_no integer,
    event_type text NOT NULL,
    actor_person_id text,
    source text NOT NULL,
    correlation_id text NOT NULL,
    aggregate_version bigint NOT NULL CHECK (aggregate_version >= 0),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, instance_id)
        REFERENCES workflow_instances (tenant_id, id)
);

CREATE INDEX workflow_audit_instance_time_idx
    ON workflow_audit_events (tenant_id, instance_id, occurred_at, id);

CREATE TABLE workflow_outbox_events (
    tenant_id text NOT NULL,
    id text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    aggregate_version bigint NOT NULL CHECK (aggregate_version >= 0),
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'published', 'failed')
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL,
    claimed_by text,
    claim_token text,
    claim_expires_at timestamptz,
    published_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, event_type, aggregate_type, aggregate_id, aggregate_version)
);

CREATE INDEX workflow_outbox_pending_idx
    ON workflow_outbox_events (tenant_id, available_at, created_at)
    WHERE status IN ('pending', 'failed', 'processing');

CREATE FUNCTION workflow_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only or immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER workflow_template_versions_immutable
    BEFORE UPDATE OR DELETE ON workflow_template_versions
    FOR EACH ROW EXECUTE FUNCTION workflow_reject_mutation();

CREATE TRIGGER workflow_audit_events_append_only
    BEFORE UPDATE OR DELETE ON workflow_audit_events
    FOR EACH ROW EXECUTE FUNCTION workflow_reject_mutation();
