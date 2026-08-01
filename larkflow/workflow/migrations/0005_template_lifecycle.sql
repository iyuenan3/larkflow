ALTER TABLE workflow_templates
    ADD COLUMN version bigint NOT NULL DEFAULT 0 CHECK (version >= 0);

CREATE TABLE workflow_template_events (
    tenant_id text NOT NULL,
    id text NOT NULL,
    template_id text NOT NULL,
    event_type text NOT NULL,
    actor_person_id text NOT NULL,
    aggregate_version bigint NOT NULL CHECK (aggregate_version >= 0),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, template_id, aggregate_version),
    FOREIGN KEY (tenant_id, template_id)
        REFERENCES workflow_templates (tenant_id, id)
);

CREATE INDEX workflow_template_events_aggregate_idx
    ON workflow_template_events (tenant_id, template_id, occurred_at, id);

CREATE TRIGGER workflow_template_events_append_only
    BEFORE UPDATE OR DELETE ON workflow_template_events
    FOR EACH ROW EXECUTE FUNCTION workflow_reject_mutation();
