CREATE TABLE workflow_graph_edit_previews (
    tenant_id text NOT NULL,
    id text NOT NULL,
    instance_id text NOT NULL,
    actor_person_id text NOT NULL,
    operations jsonb NOT NULL CHECK (
        jsonb_typeof(operations) = 'array'
        AND jsonb_array_length(operations) > 0
    ),
    added_node_keys jsonb NOT NULL CHECK (
        jsonb_typeof(added_node_keys) = 'array'
    ),
    updated_node_keys jsonb NOT NULL CHECK (
        jsonb_typeof(updated_node_keys) = 'array'
    ),
    removed_node_keys jsonb NOT NULL CHECK (
        jsonb_typeof(removed_node_keys) = 'array'
    ),
    candidate_snapshot_hash text NOT NULL CHECK (
        length(candidate_snapshot_hash) = 64
    ),
    expected_instance_version bigint NOT NULL CHECK (
        expected_instance_version >= 0
    ),
    graph_revision integer NOT NULL CHECK (graph_revision > 0),
    proposed_graph_revision integer NOT NULL CHECK (
        proposed_graph_revision = graph_revision + 1
    ),
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

CREATE INDEX workflow_graph_edit_previews_open_idx
    ON workflow_graph_edit_previews (tenant_id, expires_at, id)
    WHERE consumed_at IS NULL;
