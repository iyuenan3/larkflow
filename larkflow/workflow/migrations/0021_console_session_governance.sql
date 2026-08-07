ALTER TABLE workflow_console_sessions
    ADD COLUMN id text;

UPDATE workflow_console_sessions
SET id = md5(
    credential_digest || ':' || tenant_id || ':' || created_at::text
)
WHERE id IS NULL;

ALTER TABLE workflow_console_sessions
    ALTER COLUMN id SET NOT NULL;

ALTER TABLE workflow_console_sessions
    ADD CONSTRAINT workflow_console_sessions_id_format CHECK (
        id ~ '^[0-9a-f]{32}$'
    );

ALTER TABLE workflow_console_sessions
    ADD CONSTRAINT workflow_console_sessions_tenant_id_unique
    UNIQUE (tenant_id, id);

CREATE TABLE workflow_console_session_revocation_previews (
    tenant_id text NOT NULL,
    id text NOT NULL CHECK (id ~ '^[0-9a-f]{32}$'),
    actor_person_id text NOT NULL CHECK (actor_person_id <> ''),
    target_session_id text NOT NULL CHECK (
        target_session_id ~ '^[0-9a-f]{32}$'
    ),
    target_person_id text NOT NULL CHECK (target_person_id <> ''),
    target_created_at timestamptz NOT NULL,
    target_expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at),
    consumed_at timestamptz,
    revoked_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    CHECK (target_expires_at > target_created_at),
    CHECK (
        (consumed_at IS NULL AND revoked_at IS NULL)
        OR (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)
    )
);

CREATE INDEX workflow_console_session_revocation_previews_open_idx
    ON workflow_console_session_revocation_previews (
        tenant_id, expires_at, id
    )
    WHERE consumed_at IS NULL;

CREATE TABLE workflow_console_session_events (
    tenant_id text NOT NULL,
    id text NOT NULL CHECK (id ~ '^[0-9a-f]{32}$'),
    event_type text NOT NULL CHECK (event_type = 'session.revoked'),
    actor_person_id text NOT NULL CHECK (actor_person_id <> ''),
    target_person_id text NOT NULL CHECK (target_person_id <> ''),
    target_session_id text NOT NULL CHECK (
        target_session_id ~ '^[0-9a-f]{32}$'
    ),
    preview_id text NOT NULL,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, preview_id),
    FOREIGN KEY (tenant_id, preview_id)
        REFERENCES workflow_console_session_revocation_previews (tenant_id, id)
);

CREATE INDEX workflow_console_session_events_recent_idx
    ON workflow_console_session_events (tenant_id, occurred_at DESC, id DESC);

CREATE TRIGGER workflow_console_session_events_append_only
    BEFORE UPDATE OR DELETE ON workflow_console_session_events
    FOR EACH ROW EXECUTE FUNCTION workflow_reject_mutation();
