CREATE TABLE workflow_edge_pairing_tickets (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    person_id text NOT NULL,
    code_hash text NOT NULL,
    allowed_capabilities jsonb NOT NULL CHECK (
        jsonb_typeof(allowed_capabilities) = 'array'
    ),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    created_by_person_id text NOT NULL,
    used_at timestamptz,
    CHECK (expires_at > created_at)
);

CREATE INDEX workflow_edge_pairing_expiry_idx
    ON workflow_edge_pairing_tickets (expires_at)
    WHERE used_at IS NULL;

CREATE TABLE workflow_edge_devices (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    person_id text NOT NULL,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    capabilities jsonb NOT NULL CHECK (jsonb_typeof(capabilities) = 'array'),
    credential_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'revoked')),
    created_at timestamptz NOT NULL,
    last_seen_at timestamptz,
    revoked_at timestamptz,
    UNIQUE (tenant_id, id),
    CHECK (
        (status = 'active' AND revoked_at IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

CREATE INDEX workflow_edge_device_owner_idx
    ON workflow_edge_devices (tenant_id, person_id, status);

CREATE TABLE workflow_edge_events (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    person_id text NOT NULL,
    device_id text,
    event_type text NOT NULL,
    actor_person_id text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL,
    FOREIGN KEY (tenant_id, device_id)
        REFERENCES workflow_edge_devices (tenant_id, id)
);

CREATE INDEX workflow_edge_event_time_idx
    ON workflow_edge_events (tenant_id, occurred_at, id);

CREATE TRIGGER workflow_edge_events_append_only
    BEFORE UPDATE OR DELETE ON workflow_edge_events
    FOR EACH ROW EXECUTE FUNCTION workflow_reject_mutation();
