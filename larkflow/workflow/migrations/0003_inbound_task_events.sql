CREATE TABLE workflow_inbox_events (
    tenant_id text NOT NULL,
    id text NOT NULL,
    source text NOT NULL,
    event_type text NOT NULL,
    external_id text NOT NULL,
    event_types jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'processed', 'failed')
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL,
    claimed_by text,
    claim_token text,
    claim_expires_at timestamptz,
    processed_at timestamptz,
    outcome text,
    last_error text,
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE INDEX workflow_inbox_claimable_idx
    ON workflow_inbox_events (tenant_id, available_at, received_at)
    WHERE status IN ('pending', 'failed', 'processing');

CREATE UNIQUE INDEX workflow_projection_external_identity_idx
    ON workflow_projections (tenant_id, kind, external_id)
    WHERE external_id IS NOT NULL;
