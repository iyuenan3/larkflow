CREATE TABLE workflow_console_sessions (
    credential_digest text PRIMARY KEY CHECK (
        credential_digest ~ '^[0-9a-f]{64}$'
    ),
    tenant_id text NOT NULL CHECK (tenant_id <> ''),
    person_id text NOT NULL CHECK (person_id <> ''),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > created_at)
);

CREATE INDEX workflow_console_sessions_expiry_idx
    ON workflow_console_sessions (expires_at);

CREATE INDEX workflow_console_sessions_principal_idx
    ON workflow_console_sessions (tenant_id, person_id, expires_at);
