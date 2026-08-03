CREATE TABLE workflow_restart_previews (
    tenant_id text NOT NULL,
    id text NOT NULL,
    instance_id text NOT NULL,
    actor_person_id text NOT NULL,
    node_key text NOT NULL,
    affected_node_keys jsonb NOT NULL CHECK (
        jsonb_typeof(affected_node_keys) = 'array'
    ),
    expected_instance_version bigint NOT NULL CHECK (
        expected_instance_version >= 0
    ),
    graph_revision integer NOT NULL CHECK (graph_revision > 0),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at),
    consumed_at timestamptz,
    applied_instance_version bigint CHECK (
        applied_instance_version IS NULL OR applied_instance_version > 0
    ),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, instance_id)
        REFERENCES workflow_instances (tenant_id, id),
    CHECK (
        (consumed_at IS NULL AND applied_instance_version IS NULL)
        OR (consumed_at IS NOT NULL AND applied_instance_version IS NOT NULL)
    )
);

CREATE INDEX workflow_restart_previews_open_idx
    ON workflow_restart_previews (tenant_id, expires_at, id)
    WHERE consumed_at IS NULL;
